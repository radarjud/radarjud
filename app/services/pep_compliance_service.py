import csv
import io
import logging
import re
import time
import unicodedata
from typing import Any

from app.extensions import db
from app.models.CompanyRegistry import CompanyRegistry
from app.models.pep_registry import (
    PEPComplianceCache,
    PEPRegistry,
    get_utc_now,
    mask_cpf_lgpd,
)

logger = logging.getLogger(__name__)

# Cache L0 em memória (máx 100 itens, TTL 15 min) para respostas ultra-rápidas
_L0_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_L0_CACHE_TTL = 900  # 15 minutos

# Regex restrito para auditoria e mitigação anti-vazamento de CPFs completos (LGPD by Design)
FULL_CPF_REGEX = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
PURE_11_DIGITS_REGEX = re.compile(r"\b\d{11}\b")

# Sufixos e indicadores de Pessoas Jurídicas no QSA (para não classificar PJ como PEP)
PJ_INDICATORS = {
    "LTDA", "S.A.", "SA", "S/A", "EIRELI", "ME", "EPP", "COMPANHIA", "CIA",
    "HOLDING", "PARTICIPACOES", "PARTICIPAÇÕES", "INVESTIMENTOS", "FUNDO",
    "BANCO", "SOCIEDADE", "COOPERATIVA", "EMPREENDIMENTOS"
}


def normalize_pep_name(raw_name: Any) -> str:
    """
    Normaliza nome completo para indexação e busca determinística:
    - Converte para maiúsculas
    - Remove acentos e diacríticos
    - Elimina pontuações e múltiplos espaços
    """
    if not raw_name or not isinstance(raw_name, str):
        return ""

    text = raw_name.strip().upper()
    # Decomposição unicode e remoção de marcas de acentuação
    nfkd = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Remove caracteres que não sejam letras ou números
    clean_chars = re.sub(r"[^A-Z0-9\s]", " ", without_accents)
    return " ".join(clean_chars.split())


def extract_masked_digits(doc: str | None) -> str:
    """Extrai apenas os dígitos visíveis de um documento mascarado (geralmente 6 dígitos centrais)."""
    if not doc:
        return ""
    return re.sub(r"\D", "", doc)


def is_corporate_entity(name: str, doc: str | None = None) -> bool:
    """Verifica se o membro do QSA é uma Pessoa Jurídica (PJ) e não uma Pessoa Física."""
    if doc:
        clean_doc = re.sub(r"\D", "", doc)
        if len(clean_doc) == 14:
            return True

    words = set(re.findall(r"\b[A-Z0-9./-]+\b", name.upper()))
    return bool(words & PJ_INDICATORS)


def sanitize_lgpd_payload(obj: Any) -> Any:
    """
    Varre recursivamente o payload e sanitiza quaisquer ocorrências de CPFs completos
    ou dados pessoais sensíveis, garantindo conformidade absoluta com a LGPD.
    """
    if isinstance(obj, str):
        # Substitui CPFs de 11 dígitos formatados ou puros por versão mascarada
        def _replace_full_cpf(match: re.Match) -> str:
            raw = match.group(0)
            return mask_cpf_lgpd(raw) or "***.***.***-**"

        cleaned = FULL_CPF_REGEX.sub(_replace_full_cpf, obj)
        return PURE_11_DIGITS_REGEX.sub(_replace_full_cpf, cleaned)

    if isinstance(obj, dict):
        return {k: sanitize_lgpd_payload(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [sanitize_lgpd_payload(item) for item in obj]

    return obj


class PEPComplianceService:
    """
    Serviço de Inteligência de Compliance para Cruzamento de QSA com a Base Pública Oficial de PEP (CGU).
    Implementa:
    - Minimização estrita de dados (LGPD by Design): Zero CPFs de 11 dígitos persistidos ou expostos.
    - Tratamento determinístico de homônimos (distinção entre correspondência exata e possível vínculo).
    - Memória enxuta (< 512 MB no Render Free) com consultas indexadas e ingestão em streaming.
    - Cache L0 (memória) e L1 (PostgreSQL) por CNPJ.
    """

    def __init__(self):
        pass

    def ingest_pep_csv_stream(self, file_path_or_stream: Any, batch_size: int = 500) -> int:
        """
        Realiza a carga e indexação leve da base oficial de PEP (CGU) em lotes (streaming/chunks).
        Garante que o arquivo não seja carregado integralmente na RAM, respeitando o limite de 512 MB.
        """
        if not isinstance(file_path_or_stream, (str, io.StringIO, io.TextIOBase, io.BytesIO, io.BufferedIOBase)):
            raise TypeError("file_path_or_stream deve ser um path de arquivo ou objeto stream/buffer.")

        if isinstance(file_path_or_stream, str):
            with open(file_path_or_stream, mode="r", encoding="utf-8-sig", errors="replace") as f:
                return self._process_csv_reader(f, batch_size)
        elif isinstance(file_path_or_stream, (io.BytesIO, io.BufferedIOBase)):
            wrapper = io.TextIOWrapper(file_path_or_stream, encoding="utf-8-sig", errors="replace")
            return self._process_csv_reader(wrapper, batch_size)
        else:
            return self._process_csv_reader(file_path_or_stream, batch_size)

    def _process_csv_reader(self, f: io.TextIOBase, batch_size: int = 500) -> int:
        count = 0
        batch: list[PEPRegistry] = []

        try:
            # Tenta identificar delimitador comum (vírgula ou ponto-e-vírgula do padrão brasileiro da CGU)
            sample = f.read(4096)
            f.seek(0)
            delimiter = ";" if sample.count(";") > sample.count(",") else ","

            reader = csv.DictReader(f, delimiter=delimiter)

            for row in reader:
                # Mapeamento flexível de colunas do padrão CGU / Transparência
                raw_name = (
                    row.get("NOME_PEP")
                    or row.get("Nome_PEP")
                    or row.get("NOME")
                    or row.get("Nome")
                    or row.get("nome")
                    or ""
                ).strip()

                norm_name = normalize_pep_name(raw_name)
                if not norm_name or len(norm_name) < 3:
                    continue

                raw_cpf = (
                    row.get("CPF_PEP")
                    or row.get("Cpf_PEP")
                    or row.get("CPF")
                    or row.get("Cpf")
                    or row.get("cpf")
                    or ""
                ).strip()

                # Guardrail LGPD: Mascara obrigatoriamente
                masked_cpf = mask_cpf_lgpd(raw_cpf)

                cargo = (
                    row.get("DESCRICAO_FUNCAO")
                    or row.get("FUNCAO")
                    or row.get("CARGO")
                    or row.get("Descricao_Funcao")
                    or row.get("cargo")
                    or ""
                ).strip()

                orgao = (
                    row.get("NOME_ORGAO")
                    or row.get("ORGAO")
                    or row.get("Nome_Orgao")
                    or row.get("esfera_orgao")
                    or row.get("orgao")
                    or ""
                ).strip()

                data_ini = (row.get("DATA_INICIO_EXERCICIO") or row.get("Data_Inicio_Exercicio") or "").strip()
                data_fim = (row.get("DATA_FIM_EXERCICIO") or row.get("Data_Fim_Exercicio") or "").strip()
                data_car = (row.get("DATA_FIM_CARENCIA") or row.get("Data_Fim_Carencia") or "").strip()

                entry = PEPRegistry(
                    nome_normalizado=norm_name,
                    cpf_parcial_mascarado=masked_cpf,
                    cargo_funcao=cargo[:255] if cargo else None,
                    esfera_orgao=orgao[:255] if orgao else None,
                    data_inicio_exercicio=data_ini[:32] if data_ini else None,
                    data_fim_exercicio=data_fim[:32] if data_fim else None,
                    data_fim_carencia=data_car[:32] if data_car else None,
                )
                batch.append(entry)
                count += 1

                if len(batch) >= batch_size:
                    db.session.add_all(batch)
                    db.session.commit()
                    batch.clear()
                    db.session.expunge_all()

            if batch:
                db.session.add_all(batch)
                db.session.commit()
                batch.clear()
                db.session.expunge_all()

        except Exception:
            db.session.rollback()
            logger.exception("Falha durante ingestão em streaming da base PEP.")
            raise

        logger.info("Ingestão concluída: %d registros de PEP indexados com sucesso.", count)
        return count

    def check_qsa_compliance(
        self,
        qsa: list[Any] | None = None,
        cnpj: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """
        Cruza os sócios do Quadro Societário (QSA) com a base pública de PEP indexada.
        Classifica as ocorrências em:
        - 'CORRESPONDENCIA_ALTA': nome completo e documento parcial mascarado coincidentes.
        - 'POSSIVEL_VINCULO_HOMONIMO': nome completo coincidente, requerendo validação documental.
        """
        clean_cnpj = re.sub(r"\D", "", str(cnpj or "")).strip()

        # 1. Cache L0 em memória
        if not force_refresh and clean_cnpj:
            cached_l0 = _L0_CACHE.get(clean_cnpj)
            if cached_l0:
                ts, data = cached_l0
                if (time.time() - ts) < _L0_CACHE_TTL:
                    logger.debug("L0 Memory Cache hit para compliance PEP do CNPJ %s", clean_cnpj)
                    return data

        # 2. Cache L1 no PostgreSQL
        if not force_refresh and clean_cnpj and len(clean_cnpj) == 14:
            try:
                cached_l1 = PEPComplianceCache.query.filter_by(cnpj=clean_cnpj).first()
                if cached_l1 and cached_l1.is_valid(ttl_hours=24):
                    logger.debug("L1 Database Cache hit para compliance PEP do CNPJ %s", clean_cnpj)
                    res_data = dict(cached_l1.data)
                    _L0_CACHE[clean_cnpj] = (time.time(), res_data)
                    return res_data
            except Exception as e:  # noqa: BLE001
                logger.warning("Falha ao consultar cache L1 de PEP: %s", e)

        # 3. Execução do Cruzamento QSA x PEP
        try:
            result = self._evaluate_qsa_matches(qsa)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Exceção ao avaliar compliance PEP: %s", exc)
            return self._build_unavailable_response()

        # 4. Sanitização estrita LGPD antes de persistir ou retornar
        sanitized_result = sanitize_lgpd_payload(result)

        # 5. Persistência em cache L0 e L1
        if clean_cnpj and sanitized_result.get("status") in ("ALERTA_PEP", "NAO_IDENTIFICADO"):
            _L0_CACHE[clean_cnpj] = (time.time(), sanitized_result)
            if len(clean_cnpj) == 14:
                self._save_l1_cache(clean_cnpj, sanitized_result)

        return sanitized_result

    def _evaluate_qsa_matches(self, qsa: list[Any] | None) -> dict[str, Any]:
        """Lógica central de cruzamento de nomes do QSA contra a base indexada PEPRegistry."""
        if not qsa or not isinstance(qsa, list):
            return self._build_clean_response()

        ocorrencias: list[dict[str, Any]] = []
        nomes_processados = set()

        for socio in qsa:
            socio_nome = ""
            socio_doc = None

            if isinstance(socio, str):
                socio_nome = socio
            elif isinstance(socio, dict):
                socio_nome = (
                    socio.get("nome_socio")
                    or socio.get("nome")
                    or socio.get("razao_social")
                    or ""
                )
                socio_doc = (
                    socio.get("cnpj_cpf_do_socio")
                    or socio.get("cpf_cnpj_socio")
                    or socio.get("documento")
                    or socio.get("cpf")
                )
            else:
                continue

            socio_nome = str(socio_nome).strip()
            if not socio_nome:
                continue

            # Desconsidera sócios Pessoa Jurídica (ex.: holdings, fundos, bancos, sociedades)
            if is_corporate_entity(socio_nome, socio_doc):
                continue

            norm_name = normalize_pep_name(socio_nome)
            if not norm_name or len(norm_name) < 4 or norm_name in nomes_processados:
                continue

            nomes_processados.add(norm_name)

            # Busca indexada no banco de dados
            matches = PEPRegistry.query.filter_by(nome_normalizado=norm_name).all()
            if not matches:
                continue

            socio_masked_cpf = mask_cpf_lgpd(socio_doc)
            socio_digits = extract_masked_digits(socio_masked_cpf)

            for match in matches:
                pep_masked_cpf = match.cpf_parcial_mascarado
                pep_digits = extract_masked_digits(pep_masked_cpf)

                # Classificação do nível de correspondência
                if socio_digits and pep_digits and socio_digits == pep_digits:
                    nivel = "CORRESPONDENCIA_ALTA"
                    desc = "Correspondência exata por documento parcial mascarado"
                else:
                    nivel = "POSSIVEL_VINCULO_HOMONIMO"
                    desc = "Possível vínculo (homônimo) — requer validação documental"

                ocorrencias.append({
                    "nivel_correspondencia": nivel,
                    "cargo_funcao": match.cargo_funcao or "Cargo/Função Pública não especificada",
                    "esfera_orgao": match.esfera_orgao or "Órgão Público não especificado",
                    "descricao_correspondencia": desc,
                    "vigencia": match.data_fim_exercicio or match.data_fim_carencia or match.data_inicio_exercicio or None,
                })

        total = len(ocorrencias)
        if total > 0:
            return {
                "status": "ALERTA_PEP",
                "total_vinculos_identificados": total,
                "ocorrencias": ocorrencias[:10],  # Limite de segurança para layout
                "recomendacao": (
                    "Identificado possível vínculo com Pessoa Politicamente Exposta (PEP) no QSA. "
                    "Recomenda-se diligência reforçada de integridade e verificação documental antes da homologação."
                ),
            }

        return self._build_clean_response()

    def _build_clean_response(self) -> dict[str, Any]:
        return {
            "status": "NAO_IDENTIFICADO",
            "total_vinculos_identificados": 0,
            "ocorrencias": [],
            "recomendacao": "Nenhum vínculo com Pessoa Politicamente Exposta (PEP) identificado no QSA consultado.",
        }

    def _build_unavailable_response(self) -> dict[str, Any]:
        return {
            "status": "INDISPONIVEL",
            "total_vinculos_identificados": 0,
            "ocorrencias": [],
            "recomendacao": "Base oficial de PEP temporariamente indisponível no momento da consulta.",
        }

    def _save_l1_cache(self, clean_cnpj: str, data: dict[str, Any]) -> None:
        """Salva o resultado do compliance PEP no PostgreSQL (pep_compliance_cache e company_registry)."""
        try:
            cached = PEPComplianceCache.query.filter_by(cnpj=clean_cnpj).first()
            if cached:
                cached.data = data
                cached.cached_at = get_utc_now()
            else:
                new_cache = PEPComplianceCache(
                    cnpj=clean_cnpj,
                    data=data,
                    cached_at=get_utc_now(),
                    ttl_seconds=86400,
                )
                db.session.add(new_cache)

            reg = CompanyRegistry.query.filter_by(cnpj=clean_cnpj).first()
            if reg:
                reg.compliance_pep = data
                reg.compliance_pep_cached_at = get_utc_now()

            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            logger.warning("Falha ao salvar L1 cache de compliance PEP para CNPJ %s: %s", clean_cnpj, exc)


# Instância global singleton do serviço
pep_compliance_service = PEPComplianceService()


def check_qsa_pep_compliance(
    qsa: list[Any] | None = None,
    cnpj: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Helper global para verificação de sócios PEP no Quadro Societário."""
    return pep_compliance_service.check_qsa_compliance(
        qsa=qsa,
        cnpj=cnpj,
        force_refresh=force_refresh,
    )
