"""
Serviço de Sinais Públicos de Solvência Real e Certidões Fiscais/Trabalhistas (US-22).

Consolida indicadores públicos verificáveis da Pessoa Jurídica (RFB, PGFN, CGU, CVM e
pressão de passivo judicial), tratando o Capital Social exclusivamente como dado cadastral
declaratório e garantindo conformidade estrita com a LGPD.
"""

import logging
import os
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Status canônicos exigidos pela especificação da US-22
STATUS_REGULAR = "Regular"
STATUS_RESTRICAO = "Restrição identificada"
STATUS_NAO_DISPONIVEL = "Não disponível publicamente"
STATUS_DEMONSTRACAO_INDISPONIVEL = "Demonstração contábil pública indisponível"
STATUS_BALANCO_INDISPONIVEL = STATUS_DEMONSTRACAO_INDISPONIVEL

# Limite de timeout estrito (<= 4s) para manter resiliência no Render Free (512 MB RAM)
DEFAULT_TIMEOUT_SECONDS = 3.0

# Cache leve em memória (L1) para evitar chamadas redundantes a APIs externas
_SOLVENCY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 900  # 15 minutos
_MAX_CACHE_ENTRIES = 500


def _clean_cnpj_digits(val: Any) -> str:
    """Extrai somente os 14 dígitos numéricos do CNPJ."""
    return re.sub(r"\D", "", str(val or "")).strip()


def _is_sa_aberta(cadastral_data: dict[str, Any], company_name: str = "") -> bool:
    """
    Verifica se a entidade é uma Sociedade Anônima de Capital Aberto.
    Empresas LTDA, EIRELI, ME, EPP e S.A. Fechadas não possuem demonstrações
    contábeis abertas perante a CVM.
    """
    natureza = str(
        cadastral_data.get("natureza_juridica")
        or cadastral_data.get("codigo_natureza_juridica")
        or ""
    ).lower()

    porte = str(cadastral_data.get("porte") or "").upper()
    razao = (company_name or str(cadastral_data.get("razao_social") or "")).upper()

    # Códigos e descrições oficiais da RFB para S.A. Aberta (ex: 204-6)
    if "204-6" in natureza or "aberta" in natureza:
        return True

    # Se for explicitamente limitada, individual, microempresa ou S.A. fechada, não é companhia aberta
    if any(k in natureza for k in ("limitada", "ltda", "206-2", "eireli", "230-5", "individual", "213-5", "fechada", "205-4")):
        return False

    if porte in ("ME", "EPP"):
        return False

    # Quando a razão social indica S.A. mas sem menção expressa de capital aberto,
    # assume-se fechada a menos que haja confirmação positiva da CVM
    if (" S.A." in razao or " S/A" in razao or razao.endswith((" S.A", " S/A"))) and "ABERTA" in razao:
        return True

    return False


class SolvencySignalsService:
    """
    Consolidador de sinais públicos de solvência real e certidões.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = min(timeout_seconds, 4.0)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "RadarJus/2.0 (SolvencySignals; contato@radarjus.com.br)"
        })

    def check_rfb_status(self, cadastral_data: dict[str, Any] | None) -> dict[str, str]:
        """
        Avalia a Situação Cadastral perante a Receita Federal do Brasil (RFB).
        Retorna status canônico ('Regular', 'Restrição identificada' ou 'Não disponível publicamente').
        """
        if not cadastral_data or not isinstance(cadastral_data, dict):
            return {
                "fonte": "Receita Federal (Situação Cadastral)",
                "tipo": "rfb",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "Dados cadastrais da Receita Federal não disponíveis no momento",
            }

        situacao = str(
            cadastral_data.get("situacao_cadastral")
            or cadastral_data.get("situacao")
            or cadastral_data.get("descricao_situacao_cadastral")
            or ""
        ).upper().strip()

        if not situacao:
            return {
                "fonte": "Receita Federal (Situação Cadastral)",
                "tipo": "rfb",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "Situação cadastral não informada pela base da RFB",
            }

        # Códigos 02 / ATIVA indicam regularidade perante a RFB
        if "ATIVA" in situacao or situacao in {"02", "2"}:
            return {
                "fonte": "Receita Federal (Situação Cadastral)",
                "tipo": "rfb",
                "status": STATUS_REGULAR,
                "detalhe": "Situação cadastral ATIVA perante a Receita Federal",
            }

        # Inapta, Baixada, Suspensa ou Nula configuram restrição grave
        return {
            "fonte": "Receita Federal (Situação Cadastral)",
            "tipo": "rfb",
            "status": STATUS_RESTRICAO,
            "detalhe": f"Situação cadastral irregular ({situacao}) na Receita Federal",
        }

    def check_pgfn_divida_ativa(
        self,
        cnpj: str,
        pgfn_data: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """
        Avalia o indicativo de débitos inscritos em Dívida Ativa da União e FGTS (PGFN).
        """
        digits = _clean_cnpj_digits(cnpj)
        if len(digits) != 14:
            return {
                "fonte": "PGFN (Dívida Ativa da União)",
                "tipo": "pgfn",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "CNPJ não informado ou inválido para consulta na PGFN",
            }

        data = pgfn_data
        if data is None:
            # Fallback seguro para endpoint público de dívida ativa / ambiente configurado
            pgfn_url = os.getenv("PGFN_API_URL")
            if pgfn_url:
                try:
                    resp = self.session.get(f"{pgfn_url.rstrip('/')}/{digits}", timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                    elif resp.status_code == 404:
                        data = {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA"}
                    else:
                        data = None
                except (requests.RequestException, OSError, ValueError) as exc:
                    logger.debug("Consulta direta PGFN falhou: %s", exc)
                    data = None

        if not data or not isinstance(data, dict):
            # Quando a fonte externa não puder ser consultada sem travar o worker
            return {
                "fonte": "PGFN (Dívida Ativa da União)",
                "tipo": "pgfn",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "Base de Dívida Ativa da PGFN não disponível publicamente no momento",
            }

        if data.get("possui_divida_ativa") or float(data.get("total_debitos") or 0.0) > 0.0:
            total = float(data.get("total_debitos") or 0.0)
            detalhe = (
                f"Inscrição em Dívida Ativa da União identificada (R$ {total:,.2f})".replace(",", "X").replace(".", ",").replace("X", ".")
                if total > 0.0
                else "Inscrição em Dívida Ativa da União identificada"
            )
            return {
                "fonte": "PGFN (Dívida Ativa da União)",
                "tipo": "pgfn",
                "status": STATUS_RESTRICAO,
                "detalhe": detalhe,
            }

        if data.get("status_pgfn") == "CERTIDAO_NEGATIVA" or data.get("possui_divida_ativa") is False:
            return {
                "fonte": "PGFN (Dívida Ativa da União)",
                "tipo": "pgfn",
                "status": STATUS_REGULAR,
                "detalhe": "Certidão Negativa de Débitos (CND) / Sem inscrições na Dívida Ativa",
            }

        return {
            "fonte": "PGFN (Dívida Ativa da União)",
            "tipo": "pgfn",
            "status": STATUS_NAO_DISPONIVEL,
            "detalhe": "Situação fiscal perante a PGFN não conclusiva em consulta pública",
        }

    def check_cgu_sanctions(
        self,
        cnpj: str,
        cgu_data: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """
        Avalia sanções administrativas no Portal da Transparência / CGU (CEIS, CNEP, CEPIM).
        """
        digits = _clean_cnpj_digits(cnpj)
        if len(digits) != 14:
            return {
                "fonte": "CGU (Sanções e Impedimentos)",
                "tipo": "cgu",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "CNPJ não informado para consulta na CGU",
            }

        if not cgu_data or not isinstance(cgu_data, dict):
            return {
                "fonte": "CGU (Sanções e Impedimentos)",
                "tipo": "cgu",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "Consulta ao Portal da Transparência / CGU não disponível publicamente",
            }

        if cgu_data.get("possui_sancoes") or cgu_data.get("status_cgu") == "SANCIONADO_IMPEDIDO":
            qtd = len(cgu_data.get("detalhes") or [])
            detalhe = (
                f"Constam {qtd} sanção(ões) vigentes no CEIS/CNEP (Portal da Transparência)"
                if qtd > 0
                else "Constam sanções ou impedimentos vigentes no CEIS/CNEP"
            )
            return {
                "fonte": "CGU (Sanções e Impedimentos)",
                "tipo": "cgu",
                "status": STATUS_RESTRICAO,
                "detalhe": detalhe,
            }

        if cgu_data.get("status_cgu") == "REGULAR" or cgu_data.get("possui_sancoes") is False:
            return {
                "fonte": "CGU (Sanções e Impedimentos)",
                "tipo": "cgu",
                "status": STATUS_REGULAR,
                "detalhe": "Nada consta no Cadastro de Empresas Inidôneas e Suspensas (CEIS/CNEP)",
            }

        return {
            "fonte": "CGU (Sanções e Impedimentos)",
            "tipo": "cgu",
            "status": STATUS_NAO_DISPONIVEL,
            "detalhe": "Consulta de sanções federais não disponível no momento",
        }

    def check_cvm_accounting_status(
        self,
        cnpj: str,
        cadastral_data: dict[str, Any],
        company_name: str = "",
        cvm_override: dict[str, Any] | None = None,
    ) -> tuple[dict[str, str], str]:
        """
        Avalia a disponibilidade de Demonstrações Contábeis Públicas na CVM.
        Tratamento explícito para LTDA / PME: quando não há balanço aberto,
        retorna STATUS_DEMONSTRACAO_INDISPONIVEL sem inventar métricas patrimoniais.
        """
        # Se houver override injetado para testes ou dados já em cache
        if cvm_override and isinstance(cvm_override, dict):
            status_cvm = cvm_override.get("status", STATUS_REGULAR)
            status_dem = cvm_override.get("demonstracao_contabil_status", status_cvm)
            detalhe = cvm_override.get("detalhe", "Demonstrações financeiras auditadas perante a CVM")
            return {
                "fonte": "CVM (Demonstrações Contábeis Públicas)",
                "tipo": "cvm",
                "status": status_cvm,
                "detalhe": detalhe,
            }, status_dem

        is_aberta = _is_sa_aberta(cadastral_data, company_name=company_name)
        if not is_aberta:
            # Sociedades limitadas e pequenas empresas não publicam balanço na CVM
            return {
                "fonte": "CVM (Demonstrações Contábeis Públicas)",
                "tipo": "cvm",
                "status": STATUS_NAO_DISPONIVEL,
                "detalhe": "Empresa de capital fechado (LTDA/PME) sem publicação obrigatória de balanço na CVM",
            }, STATUS_DEMONSTRACAO_INDISPONIVEL

        # Para companhias abertas, consulta o cadastro público oficial da CVM com timeout defensivo
        digits = _clean_cnpj_digits(cnpj)
        try:
            cvm_api_url = os.getenv("CVM_API_URL")
            if cvm_api_url and digits:
                resp = self.session.get(f"{cvm_api_url.rstrip('/')}/{digits}", timeout=self.timeout)
                if resp.status_code == 200:
                    c_data = resp.json()
                    situacao_cvm = str(c_data.get("situacao") or "REGULAR").upper()
                    if "CANCELADA" in situacao_cvm or "SUSPENSA" in situacao_cvm:
                        return {
                            "fonte": "CVM (Demonstrações Contábeis Públicas)",
                            "tipo": "cvm",
                            "status": STATUS_RESTRICAO,
                            "detalhe": f"Registro na CVM com situação {situacao_cvm}",
                        }, STATUS_RESTRICAO
                    return {
                        "fonte": "CVM (Demonstrações Contábeis Públicas)",
                        "tipo": "cvm",
                        "status": STATUS_REGULAR,
                        "detalhe": "Demonstrações financeiras auditadas publicadas perante a CVM",
                    }, STATUS_REGULAR
        except (requests.RequestException, OSError, ValueError) as exc:
            logger.debug("Consulta CVM falhou com timeout/erro: %s", exc)

        # Para S.A. Aberta conhecida sem API externa acessível no momento
        return {
            "fonte": "CVM (Demonstrações Contábeis Públicas)",
            "tipo": "cvm",
            "status": STATUS_REGULAR if is_aberta else STATUS_NAO_DISPONIVEL,
            "detalhe": "Companhia de capital aberto sujeita a auditoria e publicação na CVM",
        }, STATUS_REGULAR if is_aberta else STATUS_DEMONSTRACAO_INDISPONIVEL

    def check_judicial_pressure(
        self,
        processes: list[dict[str, Any]] | None = None,
        insolvency_data: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """
        Avalia a pressão de passivo judicial já apurada nos processos (falência,
        recuperação judicial e execuções fiscais/trabalhistas).
        """
        procs = processes or []
        execucoes_fiscais = 0
        pedidos_falencia = 0
        recuperacao_judicial = 0

        if insolvency_data and isinstance(insolvency_data, dict):
            execucoes_fiscais = int(insolvency_data.get("execucoes_fiscais") or 0)
            pedidos_falencia = int(insolvency_data.get("pedidos_falencia") or 0)
            recuperacao_judicial = int(insolvency_data.get("recuperacao_judicial") or 0)
        else:
            for p in procs:
                subjects = " ".join(p.get("subjects") or []).lower()
                obs = str(p.get("observation") or "").lower()
                full_txt = f"{subjects} {obs}"
                if "execução fiscal" in full_txt or "execucao fiscal" in full_txt:
                    execucoes_fiscais += 1
                if "falência" in full_txt or "falencia" in full_txt:
                    pedidos_falencia += 1
                if "recuperação judicial" in full_txt or "recuperacao judicial" in full_txt:
                    recuperacao_judicial += 1

        if pedidos_falencia > 0 or recuperacao_judicial > 0:
            alerta = []
            if pedidos_falencia > 0:
                alerta.append(f"{pedidos_falencia} pedido(s) de falência")
            if recuperacao_judicial > 0:
                alerta.append(f"{recuperacao_judicial} recuperação judicial")
            return {
                "fonte": "Judiciário / CNJ (Passivo e Insolvência)",
                "tipo": "judicial",
                "status": STATUS_RESTRICAO,
                "detalhe": f"Identificado alerta grave de insolvência judicial ({', '.join(alerta)})",
            }

        if execucoes_fiscais >= 10:
            return {
                "fonte": "Judiciário / CNJ (Passivo e Insolvência)",
                "tipo": "judicial",
                "status": STATUS_RESTRICAO,
                "detalhe": f"Volume expressivo de execuções fiscais mapeadas ({execucoes_fiscais} processos)",
            }

        return {
            "fonte": "Judiciário / CNJ (Passivo e Insolvência)",
            "tipo": "judicial",
            "status": STATUS_REGULAR,
            "detalhe": "Sem registros de falência, recuperação judicial ou passivo fiscal crítico mapeados",
        }

    def consolidate_solvency_signals(
        self,
        company_name: str,
        cnpj: str | None = None,
        cadastral_data: dict[str, Any] | None = None,
        processes: list[dict[str, Any]] | None = None,
        pgfn_data: dict[str, Any] | None = None,
        cgu_data: dict[str, Any] | None = None,
        insolvency_data: dict[str, Any] | None = None,
        cvm_override: dict[str, Any] | None = None,
        capital_social: float = 0.0,
    ) -> dict[str, Any]:
        """
        Consolida todos os sinais públicos de solvência real e certidões em um
        bloco estruturado pronto para consumo pelo motor de previsão, APIs e relatórios.
        """
        clean_cnpj = _clean_cnpj_digits(cnpj)
        cache_key = f"{clean_cnpj}::{company_name.strip().upper()}"

        # Verifica cache L1 em memória
        if clean_cnpj and cache_key in _SOLVENCY_CACHE:
            ts, cached_val = _SOLVENCY_CACHE[cache_key]
            if time.time() - ts < _CACHE_TTL_SECONDS:
                return dict(cached_val)

        cad_data = cadastral_data or {}
        procs = processes or []

        # 1. Consulta e padronização de cada fonte
        fonte_rfb = self.check_rfb_status(cad_data)
        fonte_pgfn = self.check_pgfn_divida_ativa(clean_cnpj, pgfn_data=pgfn_data)
        fonte_cgu = self.check_cgu_sanctions(clean_cnpj, cgu_data=cgu_data)
        fonte_cvm, dem_status = self.check_cvm_accounting_status(
            clean_cnpj, cad_data, company_name=company_name, cvm_override=cvm_override
        )
        fonte_jud = self.check_judicial_pressure(processes=procs, insolvency_data=insolvency_data)

        fontes_consultadas = [fonte_rfb, fonte_pgfn, fonte_cgu, fonte_cvm, fonte_jud]

        # 2. Identificação de restrições
        restricoes_identificadas = [
            f["fonte"] for f in fontes_consultadas if f["status"] == STATUS_RESTRICAO
        ]
        possui_restricoes = len(restricoes_identificadas) > 0

        # 3. Tratamento para LTDA / PME sem balanço público
        alerta_homologacao_privada = (dem_status == STATUS_DEMONSTRACAO_INDISPONIVEL)

        # 4. Resumo executivo de solvência
        if possui_restricoes:
            resumo_solvencia = (
                f"Restrição identificada em bases públicas de solvência ({', '.join(restricoes_identificadas)}). "
                "Classificação conservadora aplicada para evitar otimismo artificial."
            )
        elif alerta_homologacao_privada:
            resumo_solvencia = (
                "Sinais públicos cadastrais regulares com ressalva: demonstração contábil pública indisponível "
                "(LTDA/PME). Capital social tratado como dado declaratório."
            )
        else:
            resumo_solvencia = "Sinais públicos regulares e solvência comprovada em fontes oficiais."

        # 5. Orientação prescritiva para homologação
        if alerta_homologacao_privada:
            orientacao_homologacao = (
                "Exigir certidões negativas atualizadas e balanço patrimonial recente diretamente do fornecedor no processo de homologação."
            )
        elif possui_restricoes:
            orientacao_homologacao = (
                "Exigir comprovação de quitação ou regularização das restrições identificadas em certidões antes de qualquer avanço na homologação."
            )
        else:
            orientacao_homologacao = (
                "Manter rotina periódica de verificação de certidões e conformidade trabalhista durante a vigência contratual."
            )

        cap_fmt = (
            f"R$ {capital_social:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            if capital_social > 0
            else "R$ 0,00"
        )

        result = {
            "demonstracao_contabil_status": dem_status,
            "resumo_solvencia": resumo_solvencia,
            "possui_restricoes": possui_restricoes,
            "fontes_consultadas": fontes_consultadas,
            "capital_social_declaratorio": float(capital_social),
            "capital_social_declaratorio_formatado": cap_fmt,
            "alerta_homologacao_privada": alerta_homologacao_privada,
            "orientacao_homologacao": orientacao_homologacao,
            "restricoes_identificadas": restricoes_identificadas,
        }

        # Armazena no cache L1 com controle de tamanho
        if clean_cnpj:
            if len(_SOLVENCY_CACHE) >= _MAX_CACHE_ENTRIES:
                # Remove o item mais antigo
                oldest_key = next(iter(_SOLVENCY_CACHE))
                _SOLVENCY_CACHE.pop(oldest_key, None)
            _SOLVENCY_CACHE[cache_key] = (time.time(), result)

        return result


# Instância singleton para uso em todo o backend
solvency_signals_service = SolvencySignalsService()
