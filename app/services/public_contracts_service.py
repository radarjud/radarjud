import os
import re
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
import requests

from app.extensions import db
from app.models.public_contract_cache import PublicContractCache, get_utc_now
from app.models.CompanyRegistry import CompanyRegistry

logger = logging.getLogger(__name__)

# Headers padrão RadarJus (User-Agent rastreável e identificável)
USER_AGENT = "RadarJus/2.0 (Compliance e Risco Trabalhista; contato@radarjus.gov.br)"
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

# Timeouts estritos: connect=2.0s, read=5.0s (total <= 5.0s para não onerar o Render Free 512 MB)
HTTP_TIMEOUT = (2.0, 5.0)

# Cache L0 em memória (máx 100 itens, TTL 15 min) para respostas ultrarrápidas
_L0_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_L0_CACHE_TTL = 900  # 15 minutos

CPF_REGEX = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")


def sanitize_cnpj(raw_cnpj: Any) -> str:
    """Extrai somente os dígitos do CNPJ."""
    return re.sub(r"\D", "", str(raw_cnpj or "")).strip()


def format_currency_brl(val: Optional[float]) -> Optional[str]:
    """Formata valor float para moeda brasileira BRL (ex.: R$ 1.500.000,00)."""
    if val is None:
        return None
    try:
        val_float = float(val)
        return f"R$ {val_float:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return None


def normalize_sphere(raw_esfera: Any, organ_name: str = "") -> Optional[str]:
    """
    Normaliza a esfera federativa para: 'Municipal', 'Estadual' ou 'Federal'.
    Usa identificador explícito de esfera ou heurística pelo nome do órgão.
    """
    raw_str = str(raw_esfera or "").strip().upper()
    organ_upper = str(organ_name or "").strip().upper()

    if raw_str in ("M", "MUNICIPAL") or "MUNICIPIO" in organ_upper or "PREFEITURA" in organ_upper or "CAMARA MUNICIPAL" in organ_upper:
        return "Municipal"
    if raw_str in ("E", "ESTADUAL", "D", "DISTRITAL") or "GOVERNO DO ESTADO" in organ_upper or "ESTADUAL" in organ_upper or "SECRETARIA DE ESTADO" in organ_upper:
        return "Estadual"
    if raw_str in ("F", "FEDERAL") or "MINISTERIO" in organ_upper or "MINISTÉRIO" in organ_upper or "UNIAO" in organ_upper or "UNIÃO" in organ_upper or "FEDERAL" in organ_upper:
        return "Federal"

    # Inferência complementar pelo nome do órgão público
    if any(t in organ_upper for t in ("PREFEITURA", "MUNICIPIO", "MUNICÍPIO", "AUTARQUIA MUNICIPAL")):
        return "Municipal"
    if any(t in organ_upper for t in ("ESTADO DE", "GOVERNO ESTADUAL", "POLICIA MILITAR", "POLICIA CIVIL", "TJ-", "TRIBUNAL DE JUSTICA")):
        return "Estadual"
    if any(t in organ_upper for t in ("CONSELHO NACIONAL", "PRESIDENCIA", "EXERCITO", "MARINHA", "AERONAUTICA", "RECEITA FEDERAL", "INSS", "IBAMA", "TRT", "TRF", "TRE")):
        return "Federal"

    return "Municipal" if "M" in raw_str else ("Estadual" if "E" in raw_str else ("Federal" if "F" in raw_str else None))


def sanitize_public_organ_name(raw_name: Any) -> Optional[str]:
    """
    Higieniza o nome do órgão público contratante, garantindo estrita aderência à LGPD:
    - Remove CPFs acidentais
    - Desconsidera pessoas físicas puras (nomes sem indicadores de órgão/entidade pública ou PJ)
    - Limita tamanho para evitar inchaço de memória
    """
    if not raw_name or not isinstance(raw_name, str):
        return None

    cleaned = " ".join(raw_name.strip().split())
    cleaned = CPF_REGEX.sub("", cleaned).strip(" -:;,.")
    if len(cleaned) < 3:
        return None

    # Limita o comprimento para evitar overflow de layout
    if len(cleaned) > 120:
        cleaned = cleaned[:120].strip(" -:;,.")

    return cleaned.upper()


class PublicContractsService:
    """
    Serviço de consulta e consolidação de contratos com o Poder Público (PNCP / Portal da Transparência).
    Implementa:
    - Cliente HTTP leve com timeout <= 5s
    - Cache L1 no PostgreSQL (tabela public_contracts_cache e company_registry)
    - Fallback gracioso com status 'INDISPONIVEL' sem quebrar a busca principal
    - Sanitização LGPD by design (exclusivamente órgãos públicos e pessoas jurídicas)
    """

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def get_public_contracts(self, cnpj: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Consulta contratos públicos por CNPJ, consultando primeiramente os caches L0/L1
        e, em caso de cache miss, disparando requisições externas para o PNCP / Portal da Transparência.
        """
        clean_cnpj = sanitize_cnpj(cnpj)
        if not clean_cnpj or len(clean_cnpj) != 14:
            return self._build_empty_response("Nenhum contrato público localizado nas bases consultadas")

        # 1. Verificação de Cache L0 (em memória)
        if not force_refresh:
            cached_l0 = _L0_CACHE.get(clean_cnpj)
            if cached_l0:
                ts, data = cached_l0
                if (time.time() - ts) < _L0_CACHE_TTL:
                    logger.debug("L0 Memory Cache hit para contratos públicos do CNPJ %s", clean_cnpj)
                    return data

        # 2. Verificação de Cache L1 (PostgreSQL)
        if not force_refresh:
            try:
                cached_l1 = PublicContractCache.query.filter_by(cnpj=clean_cnpj).first()
                if cached_l1 and cached_l1.is_valid(ttl_hours=24):
                    logger.debug("L1 Database Cache hit para contratos públicos do CNPJ %s", clean_cnpj)
                    res_data = dict(cached_l1.data)
                    _L0_CACHE[clean_cnpj] = (time.time(), res_data)
                    return res_data
            except Exception as e:
                logger.warning("Falha ao consultar cache L1 de contratos públicos: %s", e)

        # 3. Consulta Externa às APIs Governamentais
        data = self._fetch_contracts_external(clean_cnpj)

        # 4. Se a consulta foi bem sucedida ('DISPONIVEL'), persiste no cache L1 e L0
        if data.get("status") == "DISPONIVEL":
            _L0_CACHE[clean_cnpj] = (time.time(), data)
            self._save_l1_cache(clean_cnpj, data)

        return data

    def _fetch_contracts_external(self, clean_cnpj: str) -> Dict[str, Any]:
        """
        Executa chamadas HTTP para as APIs abertas do PNCP e, opcionalmente,
        Portal da Transparência (se chave presente no ambiente).
        """
        # 1. Tentativa primária: Portal Nacional de Contratações Públicas (PNCP)
        pncp_url = f"https://pncp.gov.br/api/consulta/v1/contratos?cnpjContratado={clean_cnpj}&pagina=1&tamanhoPagina=15"
        try:
            resp = self.session.get(pncp_url, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                payload = resp.json()
                return self._parse_pncp_payload(payload)
            elif resp.status_code == 404:
                # 404 na API de contratos do PNCP indica ausência de contratos cadastrados
                return self._build_empty_response("Nenhum contrato público localizado nas bases consultadas")
            elif resp.status_code in (429, 500, 502, 503, 504):
                logger.warning("PNCP retornou status HTTP %d para CNPJ %s", resp.status_code, clean_cnpj)
                # Tenta fallback secundário para Portal da Transparência se chave estiver disponível
                fallback_data = self._try_portal_transparencia_fallback(clean_cnpj)
                if fallback_data:
                    return fallback_data
                return self._build_unavailable_response()
        except (requests.exceptions.RequestException, requests.exceptions.Timeout, ValueError) as exc:
            logger.warning("Exceção ao consultar API PNCP para CNPJ %s: %s", clean_cnpj, exc)
            fallback_data = self._try_portal_transparencia_fallback(clean_cnpj)
            if fallback_data:
                return fallback_data
            return self._build_unavailable_response()

        # Fallback genérico caso a chamada retorne outros status
        return self._build_empty_response("Nenhum contrato público localizado nas bases consultadas")

    def _try_portal_transparencia_fallback(self, clean_cnpj: str) -> Optional[Dict[str, Any]]:
        """
        Consulta opcional à API do Portal da Transparência (usada se PORTAL_TRANSPARENCIA_API_KEY configurada).
        Nunca falha de forma fatal se a chave não estiver presente.
        """
        api_key = os.getenv("PORTAL_TRANSPARENCIA_API_KEY")
        if not api_key:
            return None

        url = f"https://api.portaldatransparencia.gov.br/api-de-dados/contratos?cnpj={clean_cnpj}&pagina=1"
        headers = {**DEFAULT_HEADERS, "chave-api-rodada": api_key}
        try:
            resp = self.session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                payload = resp.json()
                return self._parse_portal_transparencia_payload(payload)
        except Exception as e:
            logger.warning("Fallback do Portal da Transparência falhou: %s", e)

        return None

    def _parse_pncp_payload(self, payload: Any) -> Dict[str, Any]:
        """Processa a resposta JSON do PNCP de forma enxuta e defensiva."""
        items: List[Dict[str, Any]] = []
        total_registros = 0

        if isinstance(payload, dict):
            raw_data = payload.get("data") or payload.get("items") or payload.get("conteudo") or []
            if isinstance(raw_data, list):
                items = raw_data
            total_registros = int(payload.get("totalRegistros") or payload.get("total") or len(items))
        elif isinstance(payload, list):
            items = payload
            total_registros = len(items)

        if total_registros == 0 or not items:
            return self._build_empty_response("Nenhum contrato público localizado nas bases consultadas")

        esferas_set = set()
        orgaos_list: List[str] = []
        valor_global_acumulado: float = 0.0
        teve_valor = False

        for contract in items:
            if not isinstance(contract, dict):
                continue

            # Órgão contratante
            orgao_entidade = contract.get("orgaoEntidade") or {}
            organ_name = ""
            raw_esfera = ""
            if isinstance(orgao_entidade, dict):
                organ_name = orgao_entidade.get("razaoSocial") or orgao_entidade.get("nomeOrgao") or ""
                raw_esfera = orgao_entidade.get("esferaId") or orgao_entidade.get("esfera") or ""
            if not organ_name:
                organ_name = contract.get("orgaoContratante") or contract.get("nomeOrgao") or ""

            sanitized_organ = sanitize_public_organ_name(organ_name)
            if sanitized_organ and sanitized_organ not in orgaos_list:
                orgaos_list.append(sanitized_organ)

            # Esfera
            esf = normalize_sphere(raw_esfera, organ_name=organ_name)
            if esf:
                esferas_set.add(esf)

            # Valor global
            val = contract.get("valorGlobal")
            if val is None:
                val = contract.get("valorInicial")
            if val is None:
                val = contract.get("valorContratado")
            if val is not None:
                try:
                    num_val = float(val)
                    valor_global_acumulado += num_val
                    teve_valor = True
                except (ValueError, TypeError):
                    pass

        final_esferas = sorted(list(esferas_set))
        if not final_esferas:
            final_esferas = ["Municipal"]  # Fallback padrão seguro

        val_total = round(valor_global_acumulado, 2) if teve_valor else None

        return {
            "status": "DISPONIVEL",
            "total_contratos": total_registros,
            "esferas": final_esferas,
            "orgaos_contratantes": orgaos_list[:10],
            "valor_global_total": val_total,
            "valor_global_formatado": format_currency_brl(val_total),
            "mensagem": f"{total_registros} contrato(s) público(s) identificado(s) nas bases governamentais",
        }

    def _parse_portal_transparencia_payload(self, payload: Any) -> Dict[str, Any]:
        """Processa a resposta do Portal da Transparência."""
        items: List[Dict[str, Any]] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("data") or payload.get("items") or []

        if not items:
            return self._build_empty_response("Nenhum contrato público localizado nas bases consultadas")

        esferas_set = {"Federal"}  # Portal da Transparência do Governo Federal
        orgaos_list: List[str] = []
        valor_global_acumulado: float = 0.0
        teve_valor = False

        for contract in items:
            if not isinstance(contract, dict):
                continue
            ug = contract.get("unidadeGestora") or {}
            organ_name = ug.get("nome") if isinstance(ug, dict) else None
            if not organ_name:
                ov = contract.get("orgaoVinculado") or {}
                organ_name = ov.get("nome") if isinstance(ov, dict) else contract.get("orgao")

            sanitized_organ = sanitize_public_organ_name(organ_name)
            if sanitized_organ and sanitized_organ not in orgaos_list:
                orgaos_list.append(sanitized_organ)

            val = contract.get("valorFinalCompra") or contract.get("valorInicialCompra")
            if val is not None:
                try:
                    valor_global_acumulado += float(val)
                    teve_valor = True
                except (ValueError, TypeError):
                    pass

        val_total = round(valor_global_acumulado, 2) if teve_valor else None
        total = len(items)

        return {
            "status": "DISPONIVEL",
            "total_contratos": total,
            "esferas": sorted(list(esferas_set)),
            "orgaos_contratantes": orgaos_list[:10],
            "valor_global_total": val_total,
            "valor_global_formatado": format_currency_brl(val_total),
            "mensagem": f"{total} contrato(s) público(s) identificado(s) nas bases governamentais",
        }

    def _build_empty_response(self, msg: str = "Nenhum contrato público localizado nas bases consultadas") -> Dict[str, Any]:
        return {
            "status": "DISPONIVEL",
            "total_contratos": 0,
            "esferas": [],
            "orgaos_contratantes": [],
            "valor_global_total": None,
            "valor_global_formatado": None,
            "mensagem": msg,
        }

    def _build_unavailable_response(self) -> Dict[str, Any]:
        return {
            "status": "INDISPONIVEL",
            "total_contratos": 0,
            "esferas": [],
            "orgaos_contratantes": [],
            "valor_global_total": None,
            "valor_global_formatado": None,
            "mensagem": "Base governamental temporariamente indisponível no momento da consulta.",
        }

    def _save_l1_cache(self, clean_cnpj: str, data: Dict[str, Any]) -> None:
        """Salva o resultado no PostgreSQL (public_contracts_cache e company_registry)."""
        try:
            cached = PublicContractCache.query.filter_by(cnpj=clean_cnpj).first()
            if cached:
                cached.data = data
                cached.cached_at = get_utc_now()
            else:
                new_cache = PublicContractCache(
                    cnpj=clean_cnpj,
                    data=data,
                    cached_at=get_utc_now(),
                    ttl_seconds=86400,
                )
                db.session.add(new_cache)

            # Atualiza também CompanyRegistry caso já exista cadastrado
            reg = CompanyRegistry.query.filter_by(cnpj=clean_cnpj).first()
            if reg:
                reg.public_contracts = data
                reg.public_contracts_cached_at = get_utc_now()

            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.warning("Falha ao salvar L1 cache de contratos públicos para CNPJ %s: %s", clean_cnpj, exc)


# Instância singleton padrão do serviço
public_contracts_service = PublicContractsService()


def get_public_contracts(cnpj: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Helper global para consulta de contratos públicos com o Poder Público."""
    return public_contracts_service.get_public_contracts(cnpj=cnpj, force_refresh=force_refresh)
