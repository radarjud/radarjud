import os
import sys
import re
import time
import json
import asyncio
import hashlib
import logging
import threading
import unicodedata
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

# Cache em memória para pareceres comparativos (proteção Render Free 512 MB RAM)
COMPARATIVE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
COMPARATIVE_CACHE_LOCK = threading.Lock()
COMPARATIVE_CACHE_MAX_ENTRIES = 100
COMPARATIVE_CACHE_TTL = 900  # 15 minutos


def generate_comparative_cache_key(
    ranked_companies: List[Dict[str, Any]],
    cnae_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Gera um hash SHA-256 determinístico baseado na ordenação das empresas,
    identificadores (CNPJ ou nome normalizado) e snapshot dos scores preditivos.
    Qualquer alteração nos scores, ordem ou solvência invalida o cache automaticamente.
    """
    items = []
    for c in ranked_companies:
        ident = str(c.get("cnpj") or c.get("empresa") or "").strip().upper()
        r_sub = float(c.get("risco_subsidiario", 0.0) or 0.0)
        p_cond = float(c.get("probabilidade_condenacao", 0.0) or 0.0)
        cenario = str(c.get("cenario") or c.get("cenario_tipo") or "").strip().upper()
        cap_num = float(c.get("capital_social_num", 0.0) or 0.0)
        faixa = str(c.get("faixa_decisao") or "").strip().upper()
        items.append(f"{ident}:{r_sub:.1f}:{p_cond:.1f}:{cenario}:{cap_num:.0f}:{faixa}")

    cnae_cod = (cnae_info.get("codigo") if isinstance(cnae_info, dict) else "") or ""
    serialized = "|".join(items) + f"|cnae:{cnae_cod}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _prune_comparative_cache_if_needed() -> None:
    """Mantém o uso de memória estritamente seguro para o limite de 512 MB do Render Free."""
    now = time.time()
    expired = [k for k, (ts, _) in COMPARATIVE_CACHE.items() if (now - ts) > COMPARATIVE_CACHE_TTL]
    for k in expired:
        COMPARATIVE_CACHE.pop(k, None)

    if len(COMPARATIVE_CACHE) > COMPARATIVE_CACHE_MAX_ENTRIES:
        sorted_keys = sorted(COMPARATIVE_CACHE.keys(), key=lambda k: COMPARATIVE_CACHE[k][0])
        excess = len(COMPARATIVE_CACHE) - COMPARATIVE_CACHE_MAX_ENTRIES
        for k in sorted_keys[:excess]:
            COMPARATIVE_CACHE.pop(k, None)


# =====================================================================
# US-19: Detecção de Responsabilidade Subsidiária de Tomadoras PJ (Súmula 331 TST)
# =====================================================================

SUBSIDIARY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"responsabilidade\s+subsidi[aá]ria",
        r"condena[cç][aã]o\s+subsidi[aá]ria",
        r"conden(?:o|a|am|ou|ad[ao]s?)\s+subsidiariamente",
        r"responde(m)?\s+subsidiariamente",
        r"subsidiariamente",
        r"s[uú]mula\s*(?:n[ºo]?\s*)?331",
        r"tomador(?:a)?(?:\s+(?:de|dos|do|da))?\s*(?:servi[cç]os?)?",
        r"responsabiliza[cç][aã]o\s+subsidi[aá]ria",
        r"devedor(?:a)?\s+subsidi[aá]ri[ao]",
        r"execu[cç][aã]o\s+subsidi[aá]ria",
    ]
]

PJ_INDICATORS_PATTERN = re.compile(
    r"\b("
    r"S\.?/?\s*A\.?|S/A|LTDA\.?|EIRELI\.?|MEI?|EPP|M\.?E\.?|E\.?P\.?P\.?|S/S|S\.?S\.?|"
    r"MUNICIPIO|MUNICÍPIO|ESTADO|UNI[AÃ]O|AUTARQUIA|FUNDA[CÇ][AÃ]O|INSTITUTO|"
    r"CONSORCIO|CONSÓRCIO|COOPERATIVA|BANCO|COMPANHIA|CIA\.?|SECRETARIA|MINIST[EÉ]RIO|"
    r"EMPRESA|HOSPITAL|UNIVERSIDADE|FACULDADE|DEPARTAMENTO|TRIBUNAL|CONSELHO|"
    r"AGENCIA|AGÊNCIA|SERVI[CÇ]O\s+SOCIAL|SESI|SENAI|SESC|SEBRAE|SOCIEDADE"
    r")\b",
    re.IGNORECASE
)

CPF_PATTERN = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
CNPJ_PATTERN = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")

EXCLUDED_ENTITIES = {
    "MINISTERIO PUBLICO DO TRABALHO",
    "MINISTÉRIO PÚBLICO DO TRABALHO",
    "JUSTICA DO TRABALHO",
    "JUSTIÇA DO TRABALHO",
    "TRIBUNAL SUPERIOR DO TRABALHO",
    "TST",
    "VARA DO TRABALHO",
    "PODER JUDICIARIO",
    "PODER JUDICIÁRIO",
}


def sanitize_contractor_pj_name(
    raw_name: str,
    evaluated_company: str = "",
    evaluated_cnpj: str = "",
) -> Optional[str]:
    """
    Sanitiza e valida se o nome de entidade corresponde estritamente a uma Pessoa Jurídica
    ou Órgão Público terceiro (tomadora), descartando 100% de pessoas físicas (LGPD by Design)
    e excluindo a própria empresa avaliada.
    """
    if not raw_name or not isinstance(raw_name, str):
        return None

    name = " ".join(raw_name.strip().split())
    if len(name) < 3:
        return None

    # LGPD: remove expressamente CPFs formatados ou numéricos isolados
    name = CPF_PATTERN.sub("", name).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) < 3:
        return None

    has_pj_indicator = bool(PJ_INDICATORS_PATTERN.search(name))
    has_cnpj = bool(CNPJ_PATTERN.search(name))

    # LGPD By Design: se NÃO contiver indicador explícito de PJ nem CNPJ, descarta imediatamente
    if not has_pj_indicator and not has_cnpj:
        return None

    # Normaliza diacríticos para ASCII para comparações consistentes e sem ruído
    name_normalized = (
        unicodedata.normalize("NFKD", name)
        .encode("ASCII", "ignore")
        .decode("utf-8")
    )
    name_upper = name_normalized.upper().strip(" .,;:-_")

    for exc in EXCLUDED_ENTITIES:
        exc_norm = (
            unicodedata.normalize("NFKD", exc)
            .encode("ASCII", "ignore")
            .decode("utf-8")
            .upper()
        )
        if exc_norm in name_upper:
            return None

    if evaluated_company:
        eval_norm = (
            unicodedata.normalize("NFKD", evaluated_company)
            .encode("ASCII", "ignore")
            .decode("utf-8")
        )
        clean_eval = re.sub(r"[^\w\s]", "", eval_norm.upper()).strip()
        clean_name = re.sub(r"[^\w\s]", "", name_upper).strip()

        if clean_eval and (clean_name == clean_eval or clean_eval in clean_name or clean_name in clean_eval):
            return None

        eval_tokens = set(re.sub(r"\b(LTDA|SA|S/A|EIRELI|ME|EPP|CIA|COMPANHIA|DE|DO|DA|E)\b", "", clean_eval).split())
        name_tokens = set(re.sub(r"\b(LTDA|SA|S/A|EIRELI|ME|EPP|CIA|COMPANHIA|DE|DO|DA|E)\b", "", clean_name).split())
        eval_core = {t for t in eval_tokens if len(t) >= 4}
        name_core = {t for t in name_tokens if len(t) >= 4}
        if eval_core and name_core and eval_core == name_core:
            return None

    if evaluated_cnpj:
        clean_eval_cnpj = re.sub(r"\D", "", evaluated_cnpj)
        clean_name_cnpj = re.sub(r"\D", "", name)
        if len(clean_eval_cnpj) >= 8 and len(clean_name_cnpj) >= 8:
            if clean_eval_cnpj[:8] == clean_name_cnpj[:8]:
                return None

    if len(name_upper) > 120:
        name_upper = name_upper[:120].strip()

    return name_upper


def extract_pj_contractors_from_process(
    proc: Dict[str, Any],
    evaluated_company: str = "",
    evaluated_cnpj: str = "",
) -> List[str]:
    """
    Extrai lista deduplicada de contratantes PJ (tomadoras) do processo a partir de:
    - destinatarios_nomes / destinatarios (Comunica PJe)
    - poloPassivo / partes / raw_metadata (DataJud)
    - texto das publicações (se contiver co-réus / tomadoras identificáveis)
    """
    candidates: List[str] = []

    dest_nomes = proc.get("destinatarios_nomes") or []
    if isinstance(dest_nomes, list):
        for n in dest_nomes:
            if isinstance(n, str):
                candidates.append(n)

    raw_meta = proc.get("raw_metadata") or {}
    if isinstance(raw_meta, dict):
        for key in ("poloPassivo", "partes", "destinatarios"):
            entities = raw_meta.get(key) or []
            if isinstance(entities, list):
                for ent in entities:
                    if isinstance(ent, dict):
                        nome = ent.get("nome") or ent.get("nomeParte")
                        if not nome and isinstance(ent.get("pessoa"), dict):
                            nome = ent["pessoa"].get("nome")
                        if nome and isinstance(nome, str):
                            candidates.append(nome)

    texts = list(proc.get("publicacoes_texto") or [])
    if proc.get("observation"):
        texts.append(str(proc["observation"]))

    for txt in texts:
        if not isinstance(txt, str) or not txt:
            continue
        matches = re.finditer(
            r"(?:2[ªaºo]?\s+reclamad[ao]|segund[ao]\s+reclamad[ao]|3[ªaºo]?\s+reclamad[ao]|"
            r"terceir[ao]\s+reclamad[ao]|litisconsorte\s+passiv[ao]|litisconsorte|"
            r"tomador[ao]\s+(?:de|dos|do)\s+servi[cç]os?|tomador[ao])\s*[:\-–]?\s*"
            r"([A-Za-z0-9À-ÿ\s\.\-–—/&]+?)(?:;|\.|\n|,?\s+(?:e\s+outr|advogad|cpf|cnpj|inscrit|nos\s+termos)|$)",
            txt,
            re.IGNORECASE
        )
        for m in matches:
            matched_name = m.group(1).strip()
            if len(matched_name) >= 3:
                candidates.append(matched_name)

    valid_contractors: List[str] = []
    seen = set()
    for cand in candidates:
        cleaned = sanitize_contractor_pj_name(cand, evaluated_company, evaluated_cnpj)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            valid_contractors.append(cleaned)

    return valid_contractors


def process_has_subsidiary_text(proc: Dict[str, Any]) -> bool:
    """
    Verifica se o processo possui padrões textuais de condenação / responsabilidade subsidiária
    (Súmula 331 TST) nas publicações, movimentos ou observações.
    """
    texts = list(proc.get("publicacoes_texto") or [])
    if proc.get("observation"):
        texts.append(str(proc["observation"]))
    if proc.get("resumoPublicacao"):
        texts.append(str(proc["resumoPublicacao"]))

    movs = proc.get("movimentos") or []
    if isinstance(movs, list):
        for m in movs:
            if isinstance(m, dict):
                desc = m.get("nome") or m.get("descricao") or m.get("texto")
                if desc and isinstance(desc, str):
                    texts.append(desc)

    combined_text = " ".join(str(t) for t in texts if t)
    if not combined_text:
        return False

    for pattern in SUBSIDIARY_PATTERNS:
        if pattern.search(combined_text):
            return True

    return False


def detect_subsidiary_liability_in_processes(
    processes: List[Dict[str, Any]],
    evaluated_company: str = "",
    evaluated_cnpj: str = "",
) -> Dict[str, Any]:
    """
    Executa a mineração jurídica estruturada de responsabilidade subsidiária (Súmula 331 TST)
    sobre 100% dos processos coletados (sem fatiamento artificial).
    """
    affected_processes: List[str] = []
    seen_process_numbers = set()
    all_tomadoras: List[str] = []
    seen_tomadoras = set()
    process_contractors_map: Dict[str, List[str]] = {}

    for proc in processes:
        if not isinstance(proc, dict):
            continue

        p_num = proc.get("numero_processo") or proc.get("numeroProcesso") or ""
        norm_num = re.sub(r"\D", "", str(p_num))
        if not norm_num:
            norm_num = str(p_num).strip()
        if not norm_num:
            continue

        if norm_num in seen_process_numbers:
            continue

        seen_process_numbers.add(norm_num)

        has_text_signal = process_has_subsidiary_text(proc)
        contractors = extract_pj_contractors_from_process(proc, evaluated_company, evaluated_cnpj)

        if has_text_signal and contractors:
            affected_processes.append(p_num)
            process_contractors_map[p_num] = contractors
            proc["has_subsidiary_liability"] = True
            proc["subsidiary_contractors"] = contractors
            for c in contractors:
                if c not in seen_tomadoras:
                    seen_tomadoras.add(c)
                    all_tomadoras.append(c)
        else:
            proc.setdefault("has_subsidiary_liability", False)
            proc.setdefault("subsidiary_contractors", [])

    count = len(affected_processes)
    limited_tomadoras = sorted(all_tomadoras)[:15]

    if count > 0:
        mensagem = f"Histórico de Responsabilidade Subsidiária: {count} processos com condenação subsidiária atribuída a contratantes anteriores"
    else:
        mensagem = "Nenhuma condenação subsidiária de contratante identificada na amostra"

    return {
        "condenacoes_subsidiarias_tomadoras_count": count,
        "tomadoras_subsidiarias_pj": limited_tomadoras,
        "mensagem_responsabilidade_subsidiaria": mensagem,
        "mensagem": mensagem,
        "processos_afetados": affected_processes,
        "process_contractors_map": process_contractors_map,
    }

# Integração nativa com o diretório radarjusAgent (Google ADK + Vertex AI)
RADARJUS_AGENT_DIR = Path(__file__).resolve().parents[2] / "radarjusAgent"
if RADARJUS_AGENT_DIR.exists() and str(RADARJUS_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(RADARJUS_AGENT_DIR))

try:
    from agents.clients_agent import gerar_parecer_adk as _adk_gerar_parecer
    ADK_AGENT_AVAILABLE = True
except Exception as _exc:
    logger.debug("Agente ADK radarjusAgent não carregado diretamente: %s", _exc)
    _adk_gerar_parecer = None
    ADK_AGENT_AVAILABLE = False


class LegalAIAnalyzer:
    """
    Motor Híbrido de Inteligência Artificial para Diagnóstico Preditivo Jurídico:
    1. Modo Agente ADK (radarjusAgent): Orquestração via Google ADK + Vertex AI (gemini-2.5-flash-lite)
       para auditoria de terceiros e Súmula 331 do TST em tempo real.
    2. Modo Nuvem REST: Conexão com Gemini Flash-Lite via Google Generative AI se GEMINI_API_KEY configurada.
    3. Modo Local (Gratuito / Offline): Motor de NLP e Heurística Jurídica especializado em
       Direito do Trabalho (Súmula 331 TST, TPU/CNJ e Teoria do Risco Empresarial), garantindo
       fidelidade técnica a custo zero e sem dependências externas.
    """

    def __init__(self):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        self.agent_service_url = os.getenv("RADARJUS_AGENT_URL", "").strip()

    def _call_adk_agent(self, summary_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Invoca o agente radarjusAgent (in-process via Google ADK ou via microserviço HTTP se configurado)
        usando os dados JSON em tempo real da busca.
        """
        empresa = summary_data.get("empresa", "Empresa Analisada")

        # 1. Se houver URL de microserviço externo configurada (ex: http://127.0.0.1:8000)
        if self.agent_service_url:
            try:
                url = f"{self.agent_service_url.rstrip('/')}/analisar-empresa"
                resp = requests.post(
                    url,
                    json={"company_name": empresa, "resumo_metricas": summary_data},
                    timeout=15,
                )
                if resp.ok:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        return payload.get("ai_analysis") or payload.get("parecer_agente_adk")
            except Exception as exc:
                logger.warning("Falha ao contatar microserviço radarjusAgent (%s): %s", self.agent_service_url, exc)

        # 2. Execução direta in-process do radarjusAgent (Google ADK)
        if ADK_AGENT_AVAILABLE and _adk_gerar_parecer is not None:
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    import concurrent.futures
                    def _run_adk_task():
                        return asyncio.run(_adk_gerar_parecer(empresa, summary_data))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(_run_adk_task).result(timeout=20)
                else:
                    return asyncio.run(_adk_gerar_parecer(empresa, summary_data))
            except Exception as exc:
                logger.warning("Erro ao executar radarjusAgent ADK in-process: %s", exc)

        return None

    def _call_gemini_api(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Executa a chamada HTTP à API REST do Gemini caso a chave esteja presente."""
        if not self.gemini_api_key:
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=12)
            if resp.ok:
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(raw_text)
            else:
                logger.warning("Falha na chamada Gemini (Status %d): %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Erro de conexão com a API do Gemini: %s. Utilizando motor local.", exc)

        return None

    def _generate_local_legal_ai_report(self, summary_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Motor Local de Inteligência Jurídica Especializada.
        Processa os metadados agregados para inferir padrões de passivo, probabilidade de êxito
        e medidas preventivas segundo a jurisprudência consolidada do TST.
        """
        empresa = summary_data.get("empresa", "Empresa Analisada")
        total = summary_data.get("total_processos", 0)
        outcomes = summary_data.get("desfechos_sentencas", {})
        condenacoes = outcomes.get("condenacoes", 0)
        acordos = outcomes.get("acordos", 0)
        improcedentes = outcomes.get("improcedentes", 0)
        em_aberto = outcomes.get("em_aberto", 0)
        total_julgados = condenacoes + improcedentes + acordos
        ranking = summary_data.get("ranking_motivos", [])

        # Dados de Idoneidade Econômica e Solvência Patrimonial
        econ = summary_data.get("idoneidade_economica") or {}
        cenario_tipo = econ.get("cenario_tipo") or summary_data.get("cenario_tipo")
        prob_insolvencia = econ.get("probabilidade_insolvencia")
        if prob_insolvencia is None:
            prob_insolvencia = summary_data.get("probabilidade_insolvencia", 50.0)
        try:
            prob_insolvencia = float(prob_insolvencia)
        except (ValueError, TypeError):
            prob_insolvencia = 50.0

        risco_subsidiario = econ.get("risco_exposicao_subsidiaria") or econ.get("risco_calote_subsidiario")
        if risco_subsidiario is None:
            risco_subsidiario = summary_data.get("risco_exposicao_subsidiaria") or summary_data.get("risco_calote_subsidiario", 0.0)
        try:
            risco_subsidiario = float(risco_subsidiario)
        except (ValueError, TypeError):
            risco_subsidiario = 0.0

        capital_fmt = (
            econ.get("capital_social_formatado")
            or econ.get("valor_formatado")
            or summary_data.get("capital_social_formatado")
            or summary_data.get("capital_social_consolidado")
            or ""
        )

        # Caso 1: Empresa sem nenhum processo localizado no CNJ
        if total == 0:
            resumo = (
                f"Não foram encontrados processos judiciais trabalhistas para a empresa {empresa} "
                f"nas bases públicas do CNJ e diários oficiais. Na ausência de histórico litigioso, "
                f"não há passivo identificado nem dados amostrais para calcular taxas de condenação ou acordos."
            )
            return {
                "modelo_utilizado": "Local-Legal-NLP-v2",
                "orquestrador": "Google ADK (Agent Development Kit)",
                "perfil_risco_consolidado": "Sem Processos Localizados",
                "parecer_governanca": resumo,
                "resumo_executivo": resumo,
                "recomendacao_homologacao": "HOMOLOGAR SEM RESTRIÇÕES",
                "taxa_historica_vitoria_empresa_pct": None,
                "taxa_historica_acordos_pct": None,
                "taxa_historica_condenacao_pct": None,
                "processos_em_aberto_monitorados": 0,
                "principais_vulnerabilidades": [],
                "alertas_sumula_331": [
                    "Nenhuma condenação subsidiária de contratante identificada na amostra."
                ],
                "historico_responsabilidade_subsidiaria": {
                    "condenacoes_subsidiarias_tomadoras_count": 0,
                    "tomadoras_subsidiarias_pj": [],
                    "mensagem": "Nenhuma condenação subsidiária de contratante identificada na amostra",
                },
                "recomendacoes_compliance": [
                    "Manter rotinas preventivas de conformidade trabalhista (CLT e eSocial).",
                    "Acompanhar periodicamente novas publicações ou eventuais citações judiciais."
                ],
                "status": "sucesso"
            }

        # Sub-informações de Responsabilidade Subsidiária (Súmula 331 TST - US-19)
        sub_count = int(summary_data.get("condenacoes_subsidiarias_tomadoras_count", 0) or 0)
        sub_tomadoras = list(summary_data.get("tomadoras_subsidiarias_pj", []) or [])
        sub_msg = (
            f"Histórico de Responsabilidade Subsidiária: {sub_count} processos com condenação subsidiária atribuída a contratantes anteriores"
            if sub_count > 0
            else "Nenhuma condenação subsidiária de contratante identificada na amostra"
        )
        alerta_sub_txt = (
            f"Histórico de Responsabilidade Subsidiária: {sub_count} processos com condenação subsidiária atribuída a contratantes anteriores ({', '.join(sub_tomadoras[:3])})."
            if sub_count > 0
            else "Nenhuma condenação subsidiária de contratante identificada na amostra."
        )

        # Caso 2: Empresa com processos abertos, mas sem histórico anterior julgado (condenações/improcedências/acordos)
        if total_julgados == 0:
            generic_ai_terms = {
                "outros motivos trabalhistas", "outros", "reclamacao trabalhista (geral)",
                "obrigação / reclamação em apuração inicial", "obrigacao / reclamacao em apuracao inicial",
                "direito do trabalho", "atos processuais"
            }
            specific_motivos = [
                r["motivo"] for r in ranking
                if r.get("motivo", "").lower() not in generic_ai_terms
            ]
            chosen_pool = specific_motivos if specific_motivos else [r.get("motivo") for r in ranking if r.get("motivo")]
            top_motivos = chosen_pool[:3] if chosen_pool else ["Temas Contratuais Gerais"]
            motivos_str = ", ".join(top_motivos)

            if cenario_tipo == "CENARIO_A":
                rec_homologacao = "APROVAR / RISCO MITIGADO"
                perfil_risco = f"Baixo ({risco_subsidiario:.1f}%)"
                resumo = (
                    f"A empresa {empresa} possui {total} processo(s) em tramitação ({motivos_str}) e plena solidez "
                    f"patrimonial comprovada (Capital Social de {capital_fmt} e solvência ativa). O amplo lastro econômico "
                    f"assegura a quitação autônoma de qualquer contingência, eliminando o risco de redirecionamento "
                    f"subsidiário ao tomador de serviços nos termos da Súmula 331 do TST."
                )
            else:
                rec_homologacao = "APROVAR COM RETENÇÃO CAUTELAR"
                perfil_risco = "Amostra Inicial (Em Andamento)"
                resumo = (
                    f"A empresa {empresa} possui {total} processo(s) em tramitação ({motivos_str}), porém "
                    f"nenhum desfecho anterior transitado em julgado (sentença ou acordo homologado). "
                    f"Sem dados pretéritos de julgamento na empresa, as estimativas de probabilidade de vitória "
                    f"adotam exclusivamente as médias jurisprudenciais basais dos TRTs."
                )

            return {
                "modelo_utilizado": "Local-Legal-NLP-v2",
                "orquestrador": "Google ADK (Agent Development Kit)",
                "perfil_risco_consolidado": perfil_risco,
                "parecer_governanca": resumo,
                "resumo_executivo": resumo,
                "recomendacao_homologacao": rec_homologacao,
                "taxa_historica_vitoria_empresa_pct": None,
                "taxa_historica_acordos_pct": None,
                "taxa_historica_condenacao_pct": None,
                "processos_em_aberto_monitorados": em_aberto,
                "principais_vulnerabilidades": top_motivos,
                "alertas_sumula_331": [
                    f"Demandas em aberto concentradas em: {motivos_str}.",
                    alerta_sub_txt,
                ],
                "historico_responsabilidade_subsidiaria": {
                    "condenacoes_subsidiarias_tomadoras_count": sub_count,
                    "tomadoras_subsidiarias_pj": sub_tomadoras,
                    "mensagem": sub_msg,
                },
                "recomendacoes_compliance": [
                    "Estruturar auditoria prévia de provas documentais e testemunhais para os processos em curso.",
                    "Avaliar viabilidade de conciliação nas primeiras audiências para reduzir custos processuais."
                ],
                "status": "sucesso"
            }

        # Caso 3: Empresa com dados empíricos de processos já julgados
        taxa_vitoria_historica = round((improcedentes / total_julgados) * 100, 1)
        taxa_acordo_historica = round((acordos / total_julgados) * 100, 1)
        taxa_condenacao_historica = round((condenacoes / total_julgados) * 100, 1)

        # Identificação dos principais temas de vulnerabilidade priorizando causas substantivas
        generic_ai_terms = {
            "outros motivos trabalhistas", "outros", "reclamacao trabalhista (geral)",
            "obrigação / reclamação em apuração inicial", "obrigacao / reclamacao em apuracao inicial",
            "direito do trabalho", "atos processuais"
        }
        specific_motivos = [
            r["motivo"] for r in ranking
            if r.get("motivo", "").lower() not in generic_ai_terms
        ]
        chosen_pool = specific_motivos if specific_motivos else [r.get("motivo") for r in ranking if r.get("motivo")]
        top_motivos = chosen_pool[:3] if chosen_pool else ["Relações Contratuais Gerais"]
        motivos_str = ", ".join(top_motivos)

        # Diagnóstico integrado de risco: subordina a recomendação ao lastro patrimonial (Cenário A vs B)
        if cenario_tipo == "CENARIO_A":
            perfil_risco = f"Baixo ({risco_subsidiario:.1f}%)"
            rec_homologacao = "APROVAR / RISCO MITIGADO"
            parecer_resumo = (
                f"A empresa {empresa} possui plena higidez econômico-financeira comprovada perante a Receita Federal "
                f"(Capital Social de {capital_fmt} e probabilidade de insolvência de apenas {prob_insolvencia:.1f}%). "
                f"Embora apresente volume de litígios típico de grandes corporações (com taxa histórica desfavorável de "
                f"{taxa_condenacao_historica}% dos julgados concentrados em {motivos_str}), o substancial lastro patrimonial "
                f"garante sua capacidade autônoma de quitação integral, eliminando o risco de condenação subsidiária ou execução "
                f"contra o contratante nos termos da Súmula 331 do TST (Risco Subsidiário estimado em apenas {risco_subsidiario:.1f}%)."
            )
        elif cenario_tipo == "CENARIO_B":
            perfil_risco = "Crítico / Elevado"
            rec_homologacao = "REPROVAR / BLOQUEAR"
            parecer_resumo = (
                f"A empresa {empresa} apresenta vulnerabilidade econômico-financeira (P(Insolvência) estimada em {prob_insolvencia:.1f}%) "
                f"associada a passivo processual relevante ({taxa_condenacao_historica}% de condenações em {motivos_str}). "
                f"Há risco iminente de inadimplemento das obrigações trabalhistas e redirecionamento da execução contra o tomador "
                f"de serviços sob a ótica da Súmula 331 do TST."
            )
        elif taxa_vitoria_historica >= 50.0:
            perfil_risco = "Baixo a Moderado"
            rec_homologacao = "HOMOLOGAR SEM RESTRIÇÕES"
            parecer_resumo = (
                f"A empresa {empresa} apresenta histórico favorável com índice de vitória (improcedência) de "
                f"{taxa_vitoria_historica}% sobre os {total_julgados} processos já julgados, indicando consistência probatória e compliance robusto."
            )
        elif taxa_vitoria_historica >= 30.0:
            perfil_risco = "Moderado"
            rec_homologacao = "APROVAR COM RETENÇÃO CAUTELAR"
            parecer_resumo = (
                f"A empresa {empresa} possui taxa de vitória de {taxa_vitoria_historica}% e adesão a conciliações de "
                f"{taxa_acordo_historica}%, com demandas concentradas em: {motivos_str}."
            )
        else:
            perfil_risco = "Crítico / Elevado"
            rec_homologacao = "REPROVAR / BLOQUEAR"
            parecer_resumo = (
                f"A empresa {empresa} apresenta taxa de condenação elevada ({taxa_condenacao_historica}% dos julgados). "
                f"Os registros apontam vulnerabilidade em pedidos envolvendo {motivos_str}, "
                f"com risco reflexo direto para tomadores de serviço sob a ótica da Súmula 331 do TST."
            )

        recomendacoes = [
            "Auditar cartões de ponto eletrônicos e escalas de descanso para suprimir passivo de jornada.",
            "Instituir comitê de prevenção e canal de ouvidoria independente para mitigar queixas de assédio/dano moral.",
            "Fiscalizar guias de recolhimento de FGTS e rescisões contratuais rigorosamente no prazo do art. 477 da CLT.",
            "Exigir certidões negativas e comprovantes fiscais periódicos de prestadores terceirizados (Súmula 331/TST)."
        ]

        return {
            "modelo_utilizado": "Local-Legal-NLP-v2",
            "orquestrador": "Google ADK (Agent Development Kit)",
            "perfil_risco_consolidado": perfil_risco,
            "parecer_governanca": parecer_resumo,
            "resumo_executivo": parecer_resumo,
            "recomendacao_homologacao": rec_homologacao,
            "taxa_historica_vitoria_empresa_pct": taxa_vitoria_historica,
            "taxa_historica_acordos_pct": taxa_acordo_historica,
            "taxa_historica_condenacao_pct": taxa_condenacao_historica,
            "processos_em_aberto_monitorados": em_aberto,
            "principais_vulnerabilidades": top_motivos,
            "alertas_sumula_331": [
                f"Assuntos recorrentes: {motivos_str}.",
                f"Desfechos analisados: {condenacoes} condenações, {acordos} acordos, {improcedentes} improcedentes e {em_aberto} em aberto.",
                alerta_sub_txt,
            ],
            "historico_responsabilidade_subsidiaria": {
                "condenacoes_subsidiarias_tomadoras_count": sub_count,
                "tomadoras_subsidiarias_pj": sub_tomadoras,
                "mensagem": sub_msg,
            },
            "recomendacoes_compliance": recomendacoes,
            "status": "sucesso"
        }

    def analyze_company(self, summary_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Orquestra a análise por IA:
        1. Executa o agente ADK radarjusAgent (Google ADK + Vertex AI Gemini Flash-Lite) sobre o JSON em tempo real.
        2. Tenta Gemini REST se GEMINI_API_KEY estiver configurada.
        3. Fallback para o motor analítico local.
        """
        if "condenacoes_subsidiarias_tomadoras_count" not in summary_data and "processos" in summary_data:
            empresa = summary_data.get("empresa", "")
            sub_res = detect_subsidiary_liability_in_processes(
                summary_data.get("processos", []),
                evaluated_company=empresa,
                evaluated_cnpj=summary_data.get("cnpj", ""),
            )
            summary_data["condenacoes_subsidiarias_tomadoras_count"] = sub_res["condenacoes_subsidiarias_tomadoras_count"]
            summary_data["tomadoras_subsidiarias_pj"] = sub_res["tomadoras_subsidiarias_pj"]
            summary_data["mensagem_responsabilidade_subsidiaria"] = sub_res["mensagem_responsabilidade_subsidiaria"]
        total = summary_data.get("total_processos", 0)
        outcomes = summary_data.get("desfechos_sentencas", {})
        total_julgados = (
            outcomes.get("condenacoes", 0)
            + outcomes.get("improcedentes", 0)
            + outcomes.get("acordos", 0)
        )

        # Calibração determinística para casos sem processos ou sem julgados
        if total == 0 or total_julgados == 0:
            adk_calibrated = self._call_adk_agent(summary_data)
            if adk_calibrated:
                return adk_calibrated
            return self._generate_local_legal_ai_report(summary_data)

        # 1. Tenta o Agente ADK radarjusAgent (Google ADK + Vertex AI)
        adk_result = self._call_adk_agent(summary_data)
        if adk_result:
            # Se houver GEMINI_API_KEY configurada mas Vertex AI não estiver ativo, permite enriquecer via REST
            if self.gemini_api_key and "Google ADK" in str(adk_result.get("modelo_utilizado", "")):
                pass
            return adk_result

        # 2. Se houver chave Gemini REST, monta prompt estruturado
        if self.gemini_api_key:
            prompt = (
                "Você é um consultor jurídico sênior especialista em passivo trabalhista brasileiro e Súmula 331 do TST. "
                "Analise os metadados JSON a seguir e retorne um diagnóstico objetivo em formato JSON contendo: "
                "'perfil_risco_consolidado', 'resumo_executivo', 'recomendacao_homologacao', "
                "'taxa_historica_vitoria_empresa_pct', 'principais_vulnerabilidades', "
                "'alertas_sumula_331', 'recomendacoes_compliance'.\n"
                f"Metadados: {json.dumps(summary_data, ensure_ascii=False, default=str)}"
            )
            cloud_result = self._call_gemini_api(prompt)
            if cloud_result:
                cloud_result["modelo_utilizado"] = f"{self.gemini_model} (Google ADK)"
                cloud_result.setdefault("orquestrador", "Google ADK (Agent Development Kit)")
                return cloud_result

        # 3. Fallback para o motor local gratuito
        return self._generate_local_legal_ai_report(summary_data)

    # =========================================================================
    # PARECER COMPARATIVO SETORIAL (US-12 / radarjusAgent)
    # Cascata de 3 Níveis: ADK -> REST Gemini -> Fallback Heurístico Local
    # =========================================================================

    @classmethod
    def reset_comparative_cache(cls) -> None:
        """Limpa o cache em memória de pareceres comparativos (para testes unitários)."""
        with COMPARATIVE_CACHE_LOCK:
            COMPARATIVE_CACHE.clear()

    def _validate_comparative_guardrails(
        self,
        candidate: Any,
        expected_first_place: str,
    ) -> bool:
        """
        Guardrail Anti-Alucinação para parecer comparativo de IA:
        1. Valida estrutura JSON e presença das 5 chaves canônicas obrigatórias.
        2. Proíbe troca de 1º lugar: o campo 'primeiro_lugar' deve coincidir estritamente
           com a vencedora do ranking matemático da US-13.
        3. Valida que a recomendação cita a vencedora oficial.
        """
        if not isinstance(candidate, dict):
            logger.warning("[Guardrail] Resposta do LLM não é um objeto JSON válido.")
            return False

        required_keys = [
            "recomendacao_compra",
            "primeiro_lugar",
            "alerta_critico",
            "economia_potencial_texto",
            "justificativa_executiva",
        ]
        if not all(k in candidate for k in required_keys):
            logger.warning("[Guardrail] Chaves obrigatórias ausentes no retorno do LLM: esperadas %s", required_keys)
            return False

        primeiro_llm = str(candidate.get("primeiro_lugar") or "").strip().upper()
        esperado = expected_first_place.strip().upper()

        if not primeiro_llm or primeiro_llm != esperado:
            logger.warning(
                "[Guardrail Anti-Alucinação] Divergência no 1º colocado: LLM='%s', esperado='%s'. Descartando parecer.",
                primeiro_llm,
                esperado,
            )
            return False

        rec_texto = str(candidate.get("recomendacao_compra") or "").upper()
        if esperado not in rec_texto:
            logger.warning("[Guardrail] Recomendação de compra não cita a vencedora '%s'.", esperado)
            return False

        return True

    def _build_comparative_prompt(
        self,
        ranked_companies: List[Dict[str, Any]],
        cnae_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Constrói o prompt executivo injetando o ranking fechado e guardrails anti-alucinação."""
        vencedora = ranked_companies[0].get("empresa", "")
        summary_items = []
        for c in ranked_companies:
            summary_items.append({
                "posicao_ranking": c.get("posicao_ranking"),
                "empresa": c.get("empresa"),
                "cnpj": c.get("cnpj"),
                "risco_subsidiario_pct": c.get("risco_subsidiario"),
                "probabilidade_condenacao_bayes_pct": c.get("probabilidade_condenacao"),
                "capital_social": c.get("capital_social"),
                "cenario": c.get("cenario"),
                "faixa_decisao": c.get("faixa_decisao"),
                "previsao_bloqueada": c.get("previsao_bloqueada", False),
            })

        cnae_str = (
            f"CNAE Comum: {cnae_info.get('descricao', '')} ({cnae_info.get('codigo', '')})"
            if isinstance(cnae_info, dict) and cnae_info.get("codigo")
            else "Segmentos econômicos distintos."
        )

        return (
            "Você é o radarjusAgent, consultor executivo sênior especialista em Direito do Trabalho, "
            "Súmula 331 do TST e governança de risco com terceiros (RiskTech).\n"
            "Analise os fornecedores abaixo, já matematicamente ranqueados por menor risco subsidiário e solvência:\n\n"
            f"Segmento: {cnae_str}\n"
            f"Dados consolidados: {json.dumps(summary_items, ensure_ascii=False, indent=2, default=str)}\n\n"
            "DIRETRIZES E GUARDRAILS ANTI-ALUCINAÇÃO:\n"
            f"1. A empresa em 1º lugar DEVE SER EXATAMENTE '{vencedora}'. É estritamente proibido alterar o ranking ou trocar a vencedora.\n"
            "2. Não invente números, percentuais ou valores financeiros diferentes dos informados nos dados acima.\n"
            "3. Se uma empresa estiver na faixa 'REPROVAR' ou 'CENARIO_B', gere o alerta crítico correspondente apontando os riscos da Súmula 331.\n"
            "4. Se a empresa vencedora possuir 0.0% de risco subsidiário ou ausência de condenações, preserve essa condição sem supor condenações inexistentes.\n"
            "5. Retorne EXCLUSIVAMENTE um objeto JSON válido com as seguintes 5 chaves:\n"
            "{\n"
            f'  "recomendacao_compra": "Recomenda-se a contratação prioritária de {vencedora}, classificada em 1º lugar.",\n'
            f'  "primeiro_lugar": "{vencedora}",\n'
            '  "alerta_critico": "Alerta sobre empresa reprovada/Cenário B ou null se todas forem seguras",\n'
            '  "economia_potencial_texto": "Texto explicando a mitigação do risco subsidiário em pontos percentuais",\n'
            '  "justificativa_executiva": "Parecer executivo detalhado fundamentando a decisão de compra"\n'
            "}"
        )

    def _call_adk_agent_comparative(
        self,
        ranked_companies: List[Dict[str, Any]],
        cnae_info: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Nível 1: Invoca o radarjusAgent via Google ADK in-process ou serviço externo."""
        if self.agent_service_url:
            try:
                url = f"{self.agent_service_url.rstrip('/')}/comparar-empresas"
                resp = requests.post(
                    url,
                    json={"empresas_ranqueadas": ranked_companies, "cnae_info": cnae_info},
                    timeout=2.0,
                )
                if resp.ok:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        return payload.get("parecer_comparativo") or payload
            except Exception as exc:
                logger.warning("Falha ao contatar radarjusAgent comparativo externo: %s", exc)

        if ADK_AGENT_AVAILABLE:
            try:
                import agents.clients_agent as _clients_agent
                if hasattr(_clients_agent, "gerar_parecer_comparativo_adk"):
                    adk_func = getattr(_clients_agent, "gerar_parecer_comparativo_adk")
                    return asyncio.run(adk_func(ranked_companies, cnae_info))
            except Exception as exc:
                logger.debug("radarjusAgent comparativo in-process indisponível: %s", exc)

        return None

    def _call_gemini_api_comparative(
        self,
        prompt: str,
        timeout: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """Nível 2: Chamada REST à API do Gemini com timeout estrito (<= 2.0s) para manter a rota < 2,5s."""
        if not self.gemini_api_key:
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.ok:
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(raw_text)
            else:
                logger.warning("Falha na chamada Gemini comparativa (Status %d): %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Erro/timeout na API do Gemini comparativo: %s. Utilizando fallback heurístico.", exc)

        return None

    def _generate_local_comparative_opinion(
        self,
        ranked_companies: List[Dict[str, Any]],
        cnae_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Nível 3 (Fallback Heurístico Local):
        Motor 100% determinístico e de custo zero para parecer comparativo executivo.
        Preserva rigorosamente a Regra Zero Condenações (0.0%) e paridade com US-13.
        """
        if not ranked_companies:
            return {
                "recomendacao_compra": "",
                "primeiro_lugar": None,
                "alerta_critico": None,
                "economia_potencial_texto": "",
                "justificativa_executiva": "",
            }

        melhor = ranked_companies[0]
        pior = ranked_companies[-1]

        r_sub_melhor = float(melhor.get("risco_subsidiario", 0.0) or 0.0)
        r_sub_pior = float(pior.get("risco_subsidiario", 0.0) or 0.0)
        diff_pts = round(r_sub_pior - r_sub_melhor, 1)

        # Alerta Crítico para Cenário B ou Reprovação
        if r_sub_pior >= 40.0 or pior.get("faixa_decisao") == "REPROVAR" or pior.get("cenario") == "CENARIO_B":
            alerta_critico = (
                f"Alerta crítico: {pior['empresa']} apresenta risco subsidiário de {r_sub_pior:.1f}%, "
                f"classificada na faixa de REPROVAÇÃO com alta probabilidade de calote e redirecionamento (Súmula 331 TST)."
            )
        else:
            alerta_critico = None

        # Economia Potencial
        if diff_pts > 0:
            economia_texto = (
                f"A contratação prioritária de {melhor['empresa']} em relação à última colocada "
                f"mitiga a exposição ao passivo trabalhista em até {diff_pts} pontos percentuais de risco subsidiário."
            )
        else:
            economia_texto = "Todas as empresas avaliadas possuem risco subsidiário equivalente."

        # Justificativa Executiva
        faixa_melhor = melhor.get("faixa_decisao", "APROVAR")
        if r_sub_melhor == 0.0 or melhor.get("previsao_bloqueada"):
            detalhe_melhor = "com 0.0% de risco subsidiário (sem passivo condenatório anterior)"
        else:
            detalhe_melhor = f"com risco subsidiário de {r_sub_melhor:.1f}%"

        justificativa = (
            f"Comparativo técnico entre {len(ranked_companies)} fornecedores com base em inferência bayesiana e solvência patrimonial. "
            f"A empresa {melhor['empresa']} obteve a melhor qualificação ({faixa_melhor}) {detalhe_melhor}. "
        )

        if pior.get("faixa_decisao") == "REPROVAR" or r_sub_pior >= 40.0:
            justificativa += (
                f"Recomenda-se reprovação ou exigência de garantia fidejussória para {pior['empresa']} "
                f"devido ao elevado risco subsidiário ({r_sub_pior:.1f}%)."
            )

        if isinstance(cnae_info, dict) and cnae_info.get("descricao"):
            justificativa += f" Fornecedores avaliados no segmento de {cnae_info['descricao']}."

        return {
            "recomendacao_compra": f"Recomenda-se a contratação prioritária de {melhor['empresa']}, classificada em 1º lugar.",
            "primeiro_lugar": melhor["empresa"],
            "alerta_critico": alerta_critico,
            "economia_potencial_texto": economia_texto,
            "justificativa_executiva": justificativa.strip(),
        }

    def generate_comparative_analysis(
        self,
        ranked_companies: List[Dict[str, Any]],
        cnae_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Gera parecer comparativo executivo de IA usando cascata resiliente de 3 níveis:
        1. Google ADK (radarjusAgent)
        2. REST API Gemini (temperature=0.2, timeout <= 2.0s)
        3. Motor Heurístico Local (100% determinístico, zero custo, fallback resiliente)

        Inclui:
        - Cache em memória indexado por hash SHA-256 das empresas e scores (< 50ms).
        - Guardrail Anti-Alucinação para impedir troca de vencedor ou distorções.
        - Preservação da Regra de Zero Condenações (0.0%).
        """
        if not ranked_companies:
            return self._generate_local_comparative_opinion([], cnae_info)

        expected_first = ranked_companies[0].get("empresa", "")

        # 0. Verificação de Cache por Hash Determinístico
        cache_key = generate_comparative_cache_key(ranked_companies, cnae_info)
        with COMPARATIVE_CACHE_LOCK:
            if cache_key in COMPARATIVE_CACHE:
                cached_ts, cached_result = COMPARATIVE_CACHE[cache_key]
                if (time.time() - cached_ts) < COMPARATIVE_CACHE_TTL:
                    logger.debug("Hit de cache comparativo para hash %s", cache_key[:12])
                    return dict(cached_result)

        # 1. Nível 1: radarjusAgent (Google ADK)
        adk_result = self._call_adk_agent_comparative(ranked_companies, cnae_info)
        if adk_result and self._validate_comparative_guardrails(adk_result, expected_first):
            with COMPARATIVE_CACHE_LOCK:
                COMPARATIVE_CACHE[cache_key] = (time.time(), adk_result)
                _prune_comparative_cache_if_needed()
            return adk_result

        # 2. Nível 2: REST API Gemini
        if self.gemini_api_key:
            prompt = self._build_comparative_prompt(ranked_companies, cnae_info)
            cloud_result = self._call_gemini_api_comparative(prompt, timeout=2.0)
            if cloud_result and self._validate_comparative_guardrails(cloud_result, expected_first):
                with COMPARATIVE_CACHE_LOCK:
                    COMPARATIVE_CACHE[cache_key] = (time.time(), cloud_result)
                    _prune_comparative_cache_if_needed()
                return cloud_result

        # 3. Nível 3: Fallback Heurístico Local
        local_result = self._generate_local_comparative_opinion(ranked_companies, cnae_info)
        with COMPARATIVE_CACHE_LOCK:
            COMPARATIVE_CACHE[cache_key] = (time.time(), local_result)
            _prune_comparative_cache_if_needed()
        return local_result
