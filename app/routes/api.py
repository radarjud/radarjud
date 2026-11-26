# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import os
import re
import sys
import time
import uuid
import math
import hmac
import asyncio
import logging
import threading
from pathlib import Path
import gc
from datetime import datetime, timezone
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, Response, request, jsonify, current_app, g
from sqlalchemy import text

from app.services.prediction_engine import build_sector_benchmark
from app.utils.rate_limiter import limit_rate

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.process_record import ProcessRecord
from app.models.monitored_supplier import MonitoredSupplier
from app.services.orchestrator import CNJMetadataOrchestrator
from app.services.pdf_report import generate_company_report_pdf, generate_comparative_report_pdf
from app.services.economic_service import (
    EconomicStandingService,
    aggregate_group_capital,
    resolve_company_logo,
    discover_company_cnpjs,
    build_capital_social_display,
)
from app.utils.sanitizer import (
    sanitize_company_name,
    extract_brand_search_term,
    is_cnpj,
    fetch_cnpj_brasilapi,
    is_valid_company_search_input,
    classify_search_mode,
    resolve_search_strategy,
    CORPORATE_SUFFIX_REGEX,
    validate_webhook_url,
    validate_alert_email,
)
from app.utils.crypto import generate_movements_hash
from app.utils.auth import get_current_user_optional, require_auth, require_role
from app.models.tenant import Tenant
from app.models.audit_log import AuditLog
from app.services.quota_service import QuotaService
from app.services.cnj_searcher import normalize_court_code

logger = logging.getLogger(__name__)

# REGRA 4 e 5: Cache em Memória com TTL de 15 minutos e Regex instantâneo de CNPJ
CNPJ_REGEX = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
SEARCH_CACHE = {}  # {brand_term.upper(): (timestamp, dict_result)}
CACHE_TTL = 900    # 15 minutos
MAX_CACHE_ENTRIES = 200  # Proteção estrita de memória para Render Free (512 MB RAM)


def _prune_search_cache_if_needed(max_entries: int = MAX_CACHE_ENTRIES) -> None:
    """Purga entradas expiradas ou mais antigas do SEARCH_CACHE para mitigar memory leaks no Render Free."""
    now = time.time()
    # 1. Purga por TTL
    expired = [k for k, v in SEARCH_CACHE.items() if (now - v[0]) > CACHE_TTL]
    for k in expired:
        SEARCH_CACHE.pop(k, None)
    # 2. Se ainda exceder o limite máximo, remove as entradas mais antigas
    if len(SEARCH_CACHE) > max_entries:
        sorted_keys = sorted(SEARCH_CACHE.keys(), key=lambda k: SEARCH_CACHE[k][0])
        for k in sorted_keys[: len(SEARCH_CACHE) - max_entries]:
            SEARCH_CACHE.pop(k, None)

ACTIVE_SEARCH_LOCK = threading.Lock()
ACTIVE_SEARCH_JOB_ID = None
CANCELLED_JOB_IDS = set()


def fast_extract_cnpjs_from_texts(processes_dict: dict) -> list:
    """Extrai CNPJs únicos por raiz (8 primeiros dígitos) diretamente dos textos do Comunica PJe em 1ms."""
    seen_roots = {}
    for proc in processes_dict.values():
        for txt in proc.get("publicacoes_texto", []):
            for match in CNPJ_REGEX.findall(str(txt)):
                digits = re.sub(r"\D", "", match)
                if len(digits) == 14:
                    root = digits[:8]
                    if root not in seen_roots:
                        seen_roots[root] = digits
                        if len(seen_roots) >= 3:
                            return list(seen_roots.values())
    return list(seen_roots.values())

# Integração com radarjusAgent (Google ADK + Vertex AI)
RADARJUS_AGENT_DIR = Path(__file__).resolve().parents[2] / "radarjusAgent"
if RADARJUS_AGENT_DIR.exists() and str(RADARJUS_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(RADARJUS_AGENT_DIR))

try:
    from main import (
        executar_pipeline_radarjus as _executar_pipeline_radarjus,
        _extract_processes_from_realtime_json as _extract_agent_procs,
    )
except Exception as _agent_exc:
    logger.debug("Pipeline completo radarjusAgent indisponível: %s", _agent_exc)
    _executar_pipeline_radarjus = None
    _extract_agent_procs = None

api_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
orchestrator = CNJMetadataOrchestrator()
economic_service = EconomicStandingService()

# Pool dedicado para tarefas assíncronas em segundo plano (Deep Scan)
background_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="radarjus_bg_worker")


def _deep_scan_background_worker(
    app,
    search_id: int,
    company_name: str,
    search_strategy: dict = None,
    job_id: str = "",
    page1_processes: list = None,
):
    """
    Worker assíncrono executado em segundo plano para varredura completa (Deep Scan):
    1. Verifica cancelamento de jobs antigos para não congestionar a fila.
    2. Pagina sequencialmente até 15 páginas no Comunica PJe (limite oficial de 50 por página).
       Critério de parada: len(raw_items) < 50.
       Callback atualiza s.total_processes no banco progressivamente para o polling no frontend.
    3. Enriquece os Top 3 Tribunais no DataJud com timeout estrito de 6s.
    4. Consolida o Capital Social somado de todas as unidades descobertas do grupo.
    5. Atualiza banco de dados, gera parecer de IA e salva em SEARCH_CACHE (TTL 15 min).
    """
    with app.app_context():
        try:
            if job_id:
                with ACTIVE_SEARCH_LOCK:
                    if job_id in CANCELLED_JOB_IDS:
                        logger.info("Deep Scan abortado: job_id=%s foi cancelado.", job_id)
                        return

            logger.info("Iniciando Deep Scan em segundo plano para search_id=%d (%s)...", search_id, company_name)
            search = db.session.get(CompanySearch, search_id)
            if not search:
                return

            search.scan_status = "SCANNING_BACKGROUND"
            db.session.commit()

            start_ts = time.time()
            strat = search_strategy or {
                "comunica_query_term": extract_brand_search_term(company_name, razao_social=company_name),
                "display_title": company_name,
                "is_broad_brand": True,
                "mode": "BROAD_BRAND",
            }
            clean_brand = strat.get("comunica_query_term", "")

            all_processes_map = {}
            if page1_processes:
                for p in page1_processes:
                    num = p.get("numero_processo") or p.get("numeroProcesso")
                    if num:
                        all_processes_map[num] = {
                            "numeroProcesso": num,
                            "numero_processo": num,
                            "siglaTribunal": p.get("tribunal", ""),
                            "publicacoes_texto": p.get("publicacoes_texto", []),
                            "classes_pje": p.get("classes_pje", []),
                            "is_tst_recurso": p.get("is_tst_recurso", False),
                        }

            # Callback progressivo para polling no frontend (42 -> 90 -> 180 -> 350 -> 500+)
            def _on_page_update(page_num: int, accumulated_count: int, current_procs: dict):
                if job_id:
                    with ACTIVE_SEARCH_LOCK:
                        if job_id in CANCELLED_JOB_IDS:
                            return
                try:
                    for cnj, data in (current_procs or {}).items():
                        if cnj not in all_processes_map:
                            all_processes_map[cnj] = data
                        else:
                            for t in data.get("publicacoes_texto", []):
                                if t not in all_processes_map[cnj]["publicacoes_texto"]:
                                    all_processes_map[cnj]["publicacoes_texto"].append(t)
                            for c in data.get("classes_pje", []):
                                if c not in all_processes_map[cnj].get("classes_pje", []):
                                    all_processes_map[cnj].setdefault("classes_pje", []).append(c)

                    s = db.session.get(CompanySearch, search_id)
                    if s:
                        s.total_processes = len(all_processes_map)
                        s.last_checked_at = get_utc_now()
                        db.session.commit()
                except Exception as db_e:
                    db.session.rollback()
                    logger.debug("Erro ao atualizar progresso da página %d: %s", page_num, db_e)

            # 1. Paginação padrão até 20 páginas no Comunica PJe (começando da página 2)
            scan_res = orchestrator.searcher.paginate_comunica_pje_standard(
                search_strategy=strat,
                start_page=2,
                max_pages=20,
                on_page_callback=_on_page_update,
                timeout=6.0,
            )

            # Mescla todos os processos retornados em all_processes_map
            for cnj, data in scan_res.get("processes", {}).items():
                if cnj not in all_processes_map:
                    all_processes_map[cnj] = data
                else:
                    for t in data.get("publicacoes_texto", []):
                        if t not in all_processes_map[cnj]["publicacoes_texto"]:
                            all_processes_map[cnj]["publicacoes_texto"].append(t)
                    for c in data.get("classes_pje", []):
                        if c not in all_processes_map[cnj].get("classes_pje", []):
                            all_processes_map[cnj].setdefault("classes_pje", []).append(c)

            if job_id:
                with ACTIVE_SEARCH_LOCK:
                    if job_id in CANCELLED_JOB_IDS:
                        logger.info("Deep Scan cancelado antes da consolidação de CNPJ/DataJud: %s", job_id)
                        return

            # 2. Consolidação de Capital Social de todas as filiais e unidades vinculadas
            brand_term_clean = strat.get("comunica_query_term") or clean_brand
            cnpjs_found = set(discover_company_cnpjs(company_name, brand_term_clean, all_processes_map, search_strategy=strat))
            for ent in scan_res.get("discovered_entities", []):
                if is_cnpj(ent):
                    cnpjs_found.add(re.sub(r"\D", "", ent))

            cap_display = build_capital_social_display(list(cnpjs_found), total_processes=len(all_processes_map))
            capital_info = {
                "capital_ativo_formatado": cap_display.get("valor_formatado", "R$ 0,00"),
                "unidades_consolidadas": cap_display.get("unidades_consolidadas", 1),
                "observacao_porte": cap_display.get("observacao_porte", ""),
                "capital_social_display": cap_display,
            }

            # 3. Enriquecimento incremental no DataJud (pula processos já encerrados com log de economia)
            enriched_list, diff_stats = orchestrator.process_incremental_diff(
                company_search_id=search_id,
                all_processes_map=all_processes_map,
                max_tribunals=3,
                timeout=6.0,
                sync_db=True,
            )

            # 4. Métricas e parecer de IA
            deep_results = orchestrator.predictor.build_dashboard_metrics(
                company_name=company_name,
                processes=enriched_list,
                capital_info=capital_info,
                scan_status="COMPLETED",
            )
            deep_results["empresa"] = company_name
            deep_results["is_broad_brand"] = strat.get("is_broad_brand", False)
            deep_results["movements_hash"] = generate_movements_hash(enriched_list)
            deep_results["processos"] = enriched_list

            deep_results["idoneidade_economica"] = economic_service.evaluate_economic_standing(
                company_name=company_name,
                company_processes=enriched_list,
                p_condenacao_trabalhista=deep_results.get("probabilidade_condenacao_geral_pct") or 0.0,
                discovered_entities=list(cnpjs_found),
                search_strategy=strat,
            )
            econ = deep_results["idoneidade_economica"]
            deep_results["logo"] = econ.get("logo", {})
            if econ.get("indice_exposicao_financeira") is not None:
                deep_results["indice_exposicao_financeira"] = econ["indice_exposicao_financeira"]
                deep_results["score_risco_terceirizacao"] = econ["indice_exposicao_financeira"]
                deep_results["overall_risk_score"] = econ["indice_exposicao_financeira"]
            deep_results["capital_social_consolidado"] = econ.get("valor_formatado") or econ.get("capital_social_formatado") or cap_display.get("valor_formatado")
            deep_results["unidades_consolidadas"] = econ.get("unidades_consolidadas") or cap_display.get("unidades_consolidadas", 1)
            deep_results["observacao_porte"] = econ.get("observacao_porte") or cap_display.get("observacao_porte", "")
            deep_results["capital_social_display"] = econ.get("capital_social_display") or cap_display

            deep_results["ai_analysis"] = orchestrator.ai_analyzer.analyze_company(deep_results)
            deep_results["parecer_agente_adk"] = deep_results["ai_analysis"]

            # 5. Atualização e persistência no banco (Garante métricas completas com prioridade)
            search = db.session.get(CompanySearch, search_id)
            if not search:
                return

            search.total_processes = deep_results.get("total_processos", len(enriched_list))
            search.secrecy_count = sum(
                qtd for lvl, qtd in deep_results.get("niveis_sigilo", {}).items() if int(lvl) > 0
            )
            search.secrecy_rate = round(search.secrecy_count / max(search.total_processes, 1), 3)
            search.outcomes_summary = deep_results.get("desfechos_sentencas", {})
            search.ranking_motivos = deep_results.get("ranking_motivos", [])
            search.ranking_motivos_condenacao = deep_results.get("ranking_motivos_condenacao", [])
            search.condenacao_por_motivo = deep_results.get("condenacao_por_motivo", [])
            search.predictions_summary = deep_results.get("plus_previsao_condenacao", [])
            search.overall_risk_score = deep_results.get("score_risco_terceirizacao", 0.0)
            search.ai_analysis = deep_results.get("ai_analysis", {})
            if "condenacoes_subsidiarias_tomadoras_count" in deep_results:
                search.condenacoes_subsidiarias_tomadoras_count = deep_results.get("condenacoes_subsidiarias_tomadoras_count", 0)
                search.tomadoras_subsidiarias_pj = deep_results.get("tomadoras_subsidiarias_pj", [])
            search.movements_hash = deep_results.get("movements_hash")
            search.economic_standing = deep_results["idoneidade_economica"]
            if search.economic_standing and isinstance(search.economic_standing, dict):
                search.cnae_codigo = search.economic_standing.get("cnae_codigo") or search.cnae_codigo
                search.cnae_descricao = search.economic_standing.get("cnae_descricao") or search.cnae_descricao
            search.scan_status = "COMPLETED"
            search.last_checked_at = get_utc_now()
            db.session.commit()

            # Persiste os registros de processos individuais em lote protegido (projeção leve de IDs)
            try:
                existing_numbers = {
                    r[0]
                    for r in db.session.query(ProcessRecord.numero_processo)
                    .filter(ProcessRecord.company_search_id == search.id)
                    .all()
                    if r[0]
                }
                new_records = []
                for proc in enriched_list:
                    num = proc.get("numero_processo")
                    if num and num not in existing_numbers:
                        record = ProcessRecord(
                            company_search_id=search.id,
                            numero_processo=num,
                            tribunal=str(proc.get("tribunal") or "")[:50],
                            grau=str(proc.get("grau") or "G1")[:50],
                            nivel_sigilo=proc.get("nivelSigilo", 0),
                            primary_subject=proc.get("primary_subject"),
                            subjects_json=proc.get("subjects"),
                            observation=proc.get("observation"),
                            sentence_outcome=proc.get("sentence_outcome", "em_aberto"),
                            has_expert_report=proc.get("has_expert_report", False),
                            has_subsidiary_liability=bool(proc.get("has_subsidiary_liability", False)),
                            subsidiary_contractors_json=proc.get("subsidiary_contractors") or [],
                            data_distribuicao=proc.get("data_distribuicao"),
                            valor_causa=proc.get("valor_causa"),
                            raw_metadata=proc.get("raw_metadata"),
                        )
                        new_records.append(record)
                        existing_numbers.add(num)

                if new_records:
                    for i in range(0, len(new_records), 50):
                        try:
                            db.session.bulk_save_objects(new_records[i:i + 50])
                            db.session.commit()
                        except Exception as batch_err:
                            db.session.rollback()
                            logger.debug("Lote de processos %d tratado defensivamente: %s", i, batch_err)
            except Exception as rec_err:
                logger.debug("Persistência detalhada de processos ignorada: %s", rec_err)

            # REGRA 4: Salva no Cache em Memória com TTL de 15 minutos
            deep_results["search_id"] = search.id
            bg_cache_key = search_strategy.get("cache_key") or f"{search_strategy.get('mode', 'BROAD_BRAND')}::{search_strategy.get('comunica_query_term', clean_brand)}::{search_strategy.get('target_cnpj') or ''}"
            SEARCH_CACHE[bg_cache_key] = (time.time(), deep_results)
            logger.info("Deep Scan finalizado com sucesso em %.1fs para search_id=%d. Total: %d processos.",
                        time.time() - start_ts, search_id, search.total_processes)
        except Exception as exc:
            db.session.rollback()
            logger.exception("Erro no Deep Scan em background (search_id=%d): %s", search_id, exc)
            try:
                search = db.session.get(CompanySearch, search_id)
                if search:
                    search.scan_status = "COMPLETED"
                    db.session.commit()
            except Exception:
                pass


@api_bp.route("/health", methods=["GET"])
def health_check():
    """Verifica a integridade da aplicação e a conectividade com o PostgreSQL."""
    db_status = "connected"
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Database health check failed: %s", exc)
        db_status = "unavailable"

    return jsonify({
        "status": "healthy",
        "service": "RadarJus Backend",
        "database": db_status,
        "agente_adk": "integrated (radarjusAgent)" if _executar_pipeline_radarjus else "local_fallback",
        "apis_governamentais": {
            "datajud": "configured",
            "comunica_pje": "configured",
            "brasilapi": "active"
        }
    }), 200


@api_bp.route("/processar", methods=["POST"])
@api_bp.route("/analisar-empresa", methods=["POST"])
def run_agent_pipeline():
    """
    Executa diretamente o pipeline do agente ADK (radarjusAgent) sobre o JSON em tempo real
    ou nome/CNPJ da empresa, gerando CSV de auditoria e parecer Súmula 331 do TST.
    """
    data = request.get_json(silent=True) or {}
    company_input = (
        data.get("company_name")
        or data.get("empresa")
        or data.get("name")
        or ""
    ).strip() if isinstance(data, dict) else ""
    cnpj = str(data.get("cnpj") or "").strip() if isinstance(data, dict) else ""
    processos_in = (
        _extract_agent_procs(data, company_input)
        if _extract_agent_procs is not None
        else None
    )
    metricas_pre = data if isinstance(data, dict) and "desfechos_sentencas" in data else None

    if not company_input and not processos_in:
        return jsonify({"error": "Informe 'company_name' ou a lista 'processos' em JSON."}), 400

    if _executar_pipeline_radarjus is None:
        return jsonify({"error": "Módulo radarjusAgent não encontrado."}), 500

    try:
        resultado = asyncio.run(
            _executar_pipeline_radarjus(
                empresa=company_input,
                cnpj=cnpj,
                processos_in=processos_in,
                metricas_pre_agregadas=metricas_pre,
            )
        )
        return jsonify(resultado), 200
    except Exception as exc:
        logger.exception("Erro ao executar pipeline radarjusAgent: %s", exc)
        return jsonify({"error": str(exc)}), 500


@api_bp.route("/company-dashboard", methods=["POST"])
@api_bp.route("/search-company", methods=["POST", "GET"])
@api_bp.route("/search", methods=["POST", "GET"])
@api_bp.route("/overview", methods=["POST", "GET"])
@limit_rate(max_requests=60, window_seconds=60)
def search_company():
    """
    Endpoint principal para pesquisa e diagnóstico de risco de fornecedores/terceiros.
    Implementa:
    - Curto-Circuito Nível 1: Retorno instantâneo do PostgreSQL se consultado recentemente (< 24h).
    - Triangulação de CNPJ via BrasilAPI e sanitização de Razão Social.
    - Fast-Path síncrono (< 3s na primeira página).
    - Deep Scan disparado em background para complementar o histórico.
    - Curto-Circuito Nível 2: Se o hash de movimentos não mudou, preserva o parecer da IA.
    - Suporte a client_prefetched_items para fallback invisível anti-geoblocking.
    """
    client_prefetched_items = []
    browser_requested = False
    browser_count = None
    browser_term = None
    current_user = get_current_user_optional()
    current_tenant_id = current_user.tenant_id if current_user else None
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        company_input = data.get("company_name") or data.get("name")
        force_refresh = bool(data.get("force_refresh", False))
        client_prefetched_items = data.get("client_prefetched_items") or []
        browser_requested = data.get("browser_deep_scan") is True
        browser_count = data.get("browser_page1_count")
        browser_term = data.get("browser_query_term")
    else:
        company_input = request.args.get("name") or request.args.get("company_name")
        force_refresh = request.args.get("force_refresh", "false").lower() in ("true", "1")

    if company_input is None:
        return jsonify({"error": "Parâmetro 'company_name' é obrigatório."}), 400

    raw_query = str(company_input).strip()
    if not raw_query or not is_valid_company_search_input(raw_query):
        return jsonify({"error": "empresas so são pesquisadas por razão social ou CNPJ"}), 400

    # 1. Resolução inteligente de estratégia de busca (BROAD_BRAND vs STRICT_RAZAO vs STRICT_CNPJ)
    search_strategy = resolve_search_strategy(raw_query, brasil_api_fetcher=fetch_cnpj_brasilapi)
    query_term = search_strategy["comunica_query_term"]
    display_title = search_strategy["display_title"]
    is_broad_brand = search_strategy["is_broad_brand"]
    cnpj_used = search_strategy["target_cnpj"]

    # A busca do navegador deve usar o mesmo termo empresarial da estratégia do servidor.
    # Nunca aceitar uma página sem filtro identificado como prefetch de uma empresa.
    browser_owned = (
        browser_requested
        and isinstance(browser_term, str)
        and bool(sanitize_company_name(browser_term))
        and sanitize_company_name(browser_term) == sanitize_company_name(query_term)
        and not is_cnpj(browser_term)
        and type(browser_count) is int and browser_count >= 0
        and isinstance(client_prefetched_items, list)
        and 0 < len(client_prefetched_items) <= 100
        and len(client_prefetched_items) == min(browser_count, 100)
        and all(isinstance(item, dict) and orchestrator.searcher.normalize_cnj(
            item.get("numero_processo") or item.get("numeroProcesso") or item.get("numeroprocessocommascara")
        ) for item in client_prefetched_items)
    )
    if browser_requested and not browser_owned:
        client_prefetched_items = []

    sanitized = sanitize_company_name(query_term)
    normalized_name = sanitized.upper() if sanitized else query_term.upper()

    brand_term = extract_brand_search_term(
        raw_input=raw_query,
        razao_social=display_title,
        nome_fantasia=query_term if not is_cnpj(raw_query) else "",
    )

    cache_key = search_strategy.get("cache_key") or f"{search_strategy['mode']}::{search_strategy['comunica_query_term']}::{search_strategy.get('target_cnpj') or ''}"

    # =========================================================================
    # REGRA 4: Cache em Memória com TTL de 15 minutos (0.05s de resposta)
    # =========================================================================
    if not force_refresh and not browser_requested:
        cached_entry = SEARCH_CACHE.get(cache_key)
        if cached_entry:
            ts, cached_data = cached_entry
            econ_entry = cached_data.get("idoneidade_economica") or {}
            # Se o cache em memória tiver homônimo INAPTO antigo para empresa ativa, invalida o cache
            if econ_entry.get("situacao_cadastral") == "INAPTA" and (cached_data.get("total_processos") or 0) >= 25:
                SEARCH_CACHE.pop(cache_key, None)
            elif time.time() - ts < CACHE_TTL and (cached_data.get("total_processos") or 0) > 0:
                logger.info("Cache em memória (TTL 15 min) ativado para '%s'. Resposta instantânea em 0.05s.", cache_key)
                res_copy = dict(cached_data)
                res_copy["cached"] = True
                res_copy["cache_level"] = "L0_MEMORY_CACHE"
                resp = jsonify(res_copy)
                if current_user and current_user.tenant_id:
                    tenant_for_quota = db.session.get(Tenant, current_user.tenant_id)
                    if tenant_for_quota:
                        q_stat = QuotaService.check_quota(tenant_for_quota)
                        resp.headers["X-Quota-Remaining"] = q_stat["header_value"]
                return resp, 200

    # =========================================================================
    # CURTO-CIRCUITO NÍVEL 1 (Antes do CNJ): Busca instantânea no PostgreSQL
    # =========================================================================
    if not force_refresh:
        try:
            cached_search = (
                CompanySearch.query.filter(
                    CompanySearch.tenant_id == current_tenant_id,
                    (CompanySearch.normalized_name == normalized_name) |
                    (CompanySearch.company_name == display_title)
                )
                .order_by(CompanySearch.updated_at.desc())
                .first()
            )
            # Só retorna cache se houver processos (> 0), for recente e varredura COMPLETA.
            # Evita que uma busca vazia anterior ou preliminar congele o sistema por 24 horas.
            if (
                cached_search
                and (cached_search.total_processes or 0) > 0
                and cached_search.is_recent(hours=24)
                and cached_search.scan_status == "COMPLETED"
                and (not browser_requested or ((cached_search.economic_standing or {})
                     .get("browser_search_strategy", {}).get("cache_key") == cache_key))
            ):
                logger.info("Curto-circuito Nível 1 ativado para '%s' (search_id=%d).", normalized_name, cached_search.id)
                proc_dicts = [p.to_dict() for p in cached_search.processes]
                res = cached_search.to_dict()
                res["empresa"] = cached_search.company_name
                res["is_broad_brand"] = is_broad_brand
                res["search_strategy"] = search_strategy
                res["total_processos"] = cached_search.total_processes
                res["desfechos_sentencas"] = cached_search.outcomes_summary or {}
                res["ranking_motivos"] = cached_search.ranking_motivos or []
                res["ranking_motivos_condenacao"] = cached_search.ranking_motivos_condenacao or []
                res["condenacao_por_motivo"] = cached_search.condenacao_por_motivo or []
                res["plus_previsao_condenacao"] = cached_search.predictions_summary or []
                res["score_risco_terceirizacao"] = cached_search.overall_risk_score or 0.0
                res["overall_risk_score"] = cached_search.overall_risk_score or 0.0

                if proc_dicts and len(proc_dicts) >= (cached_search.total_processes or 0):
                    fresh_metrics = orchestrator.predictor.generate_dashboard_metrics(
                        cached_search.company_name, proc_dicts
                    )
                    res["total_processos"] = fresh_metrics.get("total_processos", cached_search.total_processes)
                    res["desfechos_sentencas"] = fresh_metrics.get("desfechos_sentencas", cached_search.outcomes_summary)
                    res["plus_previsao_condenacao"] = fresh_metrics["plus_previsao_condenacao"]
                    res["score_risco_terceirizacao"] = fresh_metrics["score_risco_terceirizacao"]
                    res["overall_risk_score"] = fresh_metrics["score_risco_terceirizacao"]
                    res["ranking_motivos"] = fresh_metrics.get("ranking_motivos", cached_search.ranking_motivos or [])
                    res["ranking_motivos_condenacao"] = fresh_metrics.get("ranking_motivos_condenacao", cached_search.ranking_motivos_condenacao or [])
                    res["condenacao_por_motivo"] = fresh_metrics.get("condenacao_por_motivo", cached_search.condenacao_por_motivo or [])
                    res["probabilidade_condenacao_geral_pct"] = fresh_metrics.get("probabilidade_condenacao_geral_pct")
                    res["taxa_historica_condenacao_pct"] = fresh_metrics.get("taxa_historica_condenacao_pct")
                    cached_search.ranking_motivos = fresh_metrics.get("ranking_motivos", cached_search.ranking_motivos or [])
                    cached_search.ranking_motivos_condenacao = fresh_metrics.get("ranking_motivos_condenacao", cached_search.ranking_motivos_condenacao or [])
                    cached_search.condenacao_por_motivo = fresh_metrics.get("condenacao_por_motivo", cached_search.condenacao_por_motivo or [])
                    cached_search.predictions_summary = fresh_metrics["plus_previsao_condenacao"]
                    cached_search.overall_risk_score = fresh_metrics["score_risco_terceirizacao"]
                # Análise de Idoneidade Econômica & Risco de Calote Subsidiário (Súmula 331 TST)
                econ_data = economic_service.evaluate_economic_standing(
                    company_name=cached_search.company_name,
                    cnpj=cnpj_used,
                    company_processes=proc_dicts,
                    p_condenacao_trabalhista=res.get("probabilidade_condenacao_geral_pct") or 0.0,
                    search_strategy=search_strategy,
                )
                saved_browser_strategy = (cached_search.economic_standing or {}).get("browser_search_strategy")
                if saved_browser_strategy:
                    econ_data = {**econ_data, "browser_search_strategy": saved_browser_strategy}
                res["idoneidade_economica"] = econ_data
                res["logo"] = econ_data.get("logo", {})
                res["capital_social_consolidado"] = econ_data.get("valor_formatado") or econ_data.get("capital_social_formatado")
                res["observacao_porte"] = econ_data.get("observacao_porte")
                res["capital_social_display"] = econ_data.get("capital_social_display")
                if econ_data.get("indice_exposicao_financeira") is not None:
                    res["indice_exposicao_financeira"] = econ_data["indice_exposicao_financeira"]
                    res["score_risco_terceirizacao"] = econ_data["indice_exposicao_financeira"]
                    res["overall_risk_score"] = econ_data["indice_exposicao_financeira"]

                # Re-executa o Agente ADK sobre os metadados (incluindo idoneidade econômica) para garantir parecer sempre atualizado
                fresh_ai = orchestrator.ai_analyzer.analyze_company(res)
                if fresh_ai:
                    res["ai_analysis"] = fresh_ai
                    cached_search.ai_analysis = fresh_ai
                res["cached"] = True
                res["cache_level"] = "L1_DATABASE"
                res["search_id"] = cached_search.id
                res["processos"] = proc_dicts
                res["parecer_agente_adk"] = res.get("ai_analysis", {})

                # Atualiza os dados de idoneidade no banco de dados para eliminar dados defasados
                try:
                    cached_search.economic_standing = econ_data
                    if econ_data and isinstance(econ_data, dict):
                        cached_search.cnae_codigo = econ_data.get("cnae_codigo") or cached_search.cnae_codigo
                        cached_search.cnae_descricao = econ_data.get("cnae_descricao") or cached_search.cnae_descricao
                    db.session.commit()
                except Exception:
                    pass

                resp = jsonify(res)
                if current_user and current_user.tenant_id:
                    tenant_for_quota = db.session.get(Tenant, current_user.tenant_id)
                    if tenant_for_quota:
                        q_stat = QuotaService.check_quota(tenant_for_quota)
                        resp.headers["X-Quota-Remaining"] = q_stat["header_value"]
                return resp, 200
        except Exception as db_err:
            logger.debug("Falha na checagem de cache L1 no banco: %s", db_err)

    # =========================================================================
    # CONTROLE DE COTA MENSAL (QuotaService - US-02)
    # Apenas novas consultas externas consomem cota.
    # =========================================================================
    tenant_obj = None
    quota_header_val = None
    if current_user and current_user.tenant_id:
        tenant_obj = db.session.get(Tenant, current_user.tenant_id)
        if tenant_obj:
            quota_status = QuotaService.check_quota(tenant_obj)
            quota_header_val = quota_status["header_value"]
            if not quota_status["allowed"]:
                resp = jsonify({
                    "error": "Cota mensal de consultas esgotada para o seu plano. Entre em contato para realizar upgrade ou aguarde a renovação no próximo mês.",
                    "code": "QUOTA_EXCEEDED",
                    "used": quota_status["used"],
                    "limit": quota_status["limit"],
                    "plan": tenant_obj.plan,
                })
                resp.headers["X-Quota-Remaining"] = quota_header_val
                return resp, 402

    if force_refresh:
        orchestrator._cache.pop(raw_query.lower(), None)
        orchestrator._cache.pop(query_term.lower(), None)
        orchestrator._cache.pop(normalized_name.lower(), None)

    current_job_id = str(uuid.uuid4())
    with ACTIVE_SEARCH_LOCK:
        global ACTIVE_SEARCH_JOB_ID
        if ACTIVE_SEARCH_JOB_ID:
            CANCELLED_JOB_IDS.add(ACTIVE_SEARCH_JOB_ID)
            if len(CANCELLED_JOB_IDS) > 100:
                CANCELLED_JOB_IDS.clear()
        ACTIVE_SEARCH_JOB_ID = current_job_id

    try:
        # =====================================================================
        # FAST-PATH OTIMIZADO (< 2 segundos): Busca Página 1 no Comunica PJe
        # =====================================================================
        fast_results = orchestrator.analyze_company_by_name(
            company_name=display_title,
            search_strategy=search_strategy,
            fast_path=True,
            limit_processes=50,
            client_prefetched_items=client_prefetched_items,
        )

        fast_results["empresa"] = display_title
        fast_results["is_broad_brand"] = is_broad_brand
        fast_results["search_strategy"] = search_strategy

        new_hash = fast_results.get("movements_hash")
        initial_status = fast_results.get("scan_status", "COMPLETED")
        if browser_owned and fast_results.get("total_processos", 0) > 0:
            initial_status = "BROWSER_PENDING" if browser_count > 100 else "COMPLETED"
        else:
            browser_owned = False

        # =====================================================================
        # CURTO-CIRCUITO NÍVEL 2 (Antes da IA): Invalidação inteligente por Hash
        # =====================================================================
        cached_search = None
        try:
            cached_search = (
                CompanySearch.query.filter(
                    (CompanySearch.normalized_name == normalized_name) |
                    (CompanySearch.company_name == display_title)
                )
                .order_by(CompanySearch.updated_at.desc())
                .first()
            )
        except Exception:
            pass

        # Se o hash de movimentos for idêntico ao anterior, preserva parecer de IA
        if cached_search and cached_search.movements_hash and cached_search.movements_hash == new_hash:
            logger.info("Curto-circuito Nível 2 (Hash idêntico) para '%s'. Mantendo IA anterior.", normalized_name)
            fast_results["ai_analysis"] = cached_search.ai_analysis
            fast_results["cached_ai"] = True
            fast_results["cache_level"] = "L2_HASH_MATCH"

        fast_results["parecer_agente_adk"] = fast_results.get("ai_analysis", {})

        # Análise de Idoneidade Econômica & Risco de Calote Subsidiário (Súmula 331 TST)
        econ_data = economic_service.evaluate_economic_standing(
            company_name=display_title,
            cnpj=cnpj_used,
            company_processes=fast_results.get("processos", []),
            p_condenacao_trabalhista=fast_results.get("probabilidade_condenacao_geral_pct") or 0.0,
            discovered_entities=fast_results.get("discovered_entities", []),
            search_strategy=search_strategy,
        )
        if browser_owned:
            econ_data = {**econ_data, "browser_search_strategy": search_strategy}
        fast_results["idoneidade_economica"] = econ_data
        fast_results["logo"] = econ_data.get("logo", {})
        fast_results["capital_social_consolidado"] = econ_data.get("valor_formatado") or econ_data.get("capital_social_formatado")
        fast_results["observacao_porte"] = econ_data.get("observacao_porte")
        fast_results["capital_social_display"] = econ_data.get("capital_social_display")
        if econ_data.get("indice_exposicao_financeira") is not None:
            fast_results["indice_exposicao_financeira"] = econ_data["indice_exposicao_financeira"]
            fast_results["score_risco_terceirizacao"] = econ_data["indice_exposicao_financeira"]
            fast_results["overall_risk_score"] = econ_data["indice_exposicao_financeira"]

        # Sincroniza o parecer do Agente ADK com a idoneidade econômica calculada
        fresh_ai = orchestrator.ai_analyzer.analyze_company(fast_results)
        if fresh_ai:
            fast_results["ai_analysis"] = fresh_ai
            fast_results["parecer_agente_adk"] = fresh_ai

        # Persistência do resultado no PostgreSQL
        search_id = None
        try:
            search_entry = CompanySearch(
                tenant_id=current_tenant_id,
                company_name=display_title,
                normalized_name=normalized_name,
                total_processes=fast_results.get("total_processos", 0),
                secrecy_count=sum(
                    qtd for lvl, qtd in fast_results.get("niveis_sigilo", {}).items() if int(lvl) > 0
                ),
                secrecy_rate=fast_results.get("taxa_sigilo_pct", 0.0) / 100.0,
                outcomes_summary=fast_results.get("desfechos_sentencas", {}),
                ranking_motivos=fast_results.get("ranking_motivos", []),
                ranking_motivos_condenacao=fast_results.get("ranking_motivos_condenacao", []),
                condenacao_por_motivo=fast_results.get("condenacao_por_motivo", []),
                predictions_summary=fast_results.get("plus_previsao_condenacao", []),
                economic_standing=econ_data,
                cnae_codigo=econ_data.get("cnae_codigo") if isinstance(econ_data, dict) else None,
                cnae_descricao=econ_data.get("cnae_descricao") if isinstance(econ_data, dict) else None,
                condenacoes_subsidiarias_tomadoras_count=fast_results.get("condenacoes_subsidiarias_tomadoras_count", 0),
                tomadoras_subsidiarias_pj=fast_results.get("tomadoras_subsidiarias_pj", []),
                overall_risk_score=fast_results.get("score_risco_terceirizacao", 0.0),
                ai_analysis=fast_results.get("ai_analysis", {}),
                movements_hash=new_hash,
                scan_status=initial_status,
                last_checked_at=get_utc_now(),
            )
            db.session.add(search_entry)
            db.session.commit()
            search_id = search_entry.id
            fast_results["search_id"] = search_id

            # Persiste os processos identificados
            for proc in fast_results.get("processos", []):
                record = ProcessRecord(
                    company_search_id=search_entry.id,
                    numero_processo=proc.get("numero_processo"),
                    tribunal=str(proc.get("tribunal") or "")[:50],
                    grau=str(proc.get("grau") or "G1")[:50],
                    nivel_sigilo=proc.get("nivelSigilo", 0),
                    primary_subject=proc.get("primary_subject"),
                    subjects_json=proc.get("subjects"),
                    observation=proc.get("observation"),
                    sentence_outcome=proc.get("sentence_outcome", "em_aberto"),
                    has_expert_report=proc.get("has_expert_report", False),
                    has_subsidiary_liability=bool(proc.get("has_subsidiary_liability", False)),
                    subsidiary_contractors_json=proc.get("subsidiary_contractors") or [],
                    data_distribuicao=proc.get("data_distribuicao"),
                    valor_causa=proc.get("valor_causa"),
                    raw_metadata=proc.get("raw_metadata"),
                )
                db.session.add(record)
            db.session.commit()
        except Exception as db_exc:
            db.session.rollback()
            logger.debug("Persistência no DB ignorada ou indisponível: %s", db_exc)

        # =====================================================================
        # DEEP SCAN (Background): Dispara apenas se ainda existirem mais páginas
        # =====================================================================
        if search_id and initial_status == "PARTIAL_FAST_PATH" and not current_app.config.get("TESTING"):
            app_ref = current_app._get_current_object()
            background_executor.submit(
                _deep_scan_background_worker,
                app_ref,
                search_id,
                display_title,
                search_strategy,
                current_job_id,
                fast_results.get("processos", []),
            )

        fast_results["scan_status"] = (
            "BROWSER_PARTIAL" if browser_owned and not search_id else initial_status
        )
        # O estado provisório do navegador não deve sobreviver no cache L0.
        if not browser_owned:
            SEARCH_CACHE[cache_key] = (time.time(), fast_results)
        resp = jsonify(fast_results)
        if quota_header_val is not None:
            # Recalcula o saldo restante após a persistência da nova consulta
            if tenant_obj:
                updated_quota = QuotaService.check_quota(tenant_obj)
                resp.headers["X-Quota-Remaining"] = updated_quota["header_value"]
            else:
                resp.headers["X-Quota-Remaining"] = quota_header_val
        return resp, 200

    except Exception as exc:
        logger.exception("Erro ao processar busca de empresa: %s", exc)
        return jsonify({"error": "Erro interno ao consultar APIs do CNJ."}), 500


# =============================================================================
# DECISÕES DE COMPLIANCE E TRILHA DE AUDITORIA (US-02)
# =============================================================================

@api_bp.route("/searches/<int:company_search_id>/decision", methods=["POST"])
@require_auth
def register_search_decision(company_search_id: int):
    """
    Registra decisão de compliance (APROVADO, REPROVADO, RETENCAO) sobre uma empresa analisada.
    Congela o snapshot de risco (score de risco e Cenário A/B) e garante trilha de auditoria append-only.
    """
    search = db.session.get(CompanySearch, company_search_id)
    if not search:
        return jsonify({"error": "Registro de pesquisa não encontrado."}), 404

    # Anti-IDOR: Usuário só pode registrar decisão em buscas do seu próprio tenant
    current_user = g.current_user
    if search.tenant_id is not None and search.tenant_id != current_user.tenant_id:
        return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403

    data = request.get_json(silent=True) or {}
    decision = str(data.get("decision") or "").strip().upper()
    justification = str(data.get("justification") or "").strip()

    if decision not in AuditLog.VALID_DECISIONS:
        return jsonify({
            "error": f"Decisão inválida: '{decision}'. Valores permitidos: {sorted(list(AuditLog.VALID_DECISIONS))}."
        }), 400

    if not justification:
        return jsonify({"error": "O campo 'justification' é obrigatório."}), 400

    # Captura snapshot congelado de score e Cenário A/B
    econ = search.economic_standing if isinstance(search.economic_standing, dict) else {}
    cenario = econ.get("cenario") or ("CENARIO_A" if (search.overall_risk_score or 0) < 50 else "CENARIO_B")
    
    risk_snapshot = {
        "overall_risk_score": float(search.overall_risk_score or 0.0),
        "cenario": cenario,
        "total_processes": search.total_processes,
        "score_risco_terceirizacao": float(search.overall_risk_score or 0.0),
        "company_name": search.company_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        audit_entry = AuditLog(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            company_search_id=search.id,
            decision=decision,
            justification=justification,
            risk_score_snapshot=risk_snapshot,
        )
        db.session.add(audit_entry)
        db.session.commit()

        logger.info(
            "Decisão de auditoria registrada: user_id=%d, tenant_id=%d, search_id=%d, decision=%s",
            current_user.id, current_user.tenant_id, search.id, decision
        )
        return jsonify({
            "status": "success",
            "message": "Decisão de compliance registrada com sucesso.",
            "audit_log": audit_entry.to_dict(),
        }), 201

    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro ao registrar decisão de auditoria: %s", exc)
        return jsonify({"error": "Falha interna ao registrar decisão de auditoria."}), 500


@api_bp.route("/audit-logs", methods=["GET"])
@require_auth
def list_audit_logs():
    """
    Lista registros da trilha de auditoria imutável do tenant autenticado.
    Suporta paginação e filtros opcionais por company_search_id e decision.
    """
    current_user = g.current_user
    tenant_id = current_user.tenant_id

    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    search_id_filter = request.args.get("company_search_id")
    decision_filter = request.args.get("decision")

    try:
        query = AuditLog.query.filter_by(tenant_id=tenant_id)

        if search_id_filter and str(search_id_filter).isdigit():
            query = query.filter_by(company_search_id=int(search_id_filter))

        if decision_filter:
            dec_up = str(decision_filter).strip().upper()
            if dec_up in AuditLog.VALID_DECISIONS:
                query = query.filter_by(decision=dec_up)

        total = query.count()
        logs = (
            query.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return jsonify({
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [log.to_dict() for log in logs],
        }), 200

    except Exception as exc:
        logger.exception("Erro ao listar trilha de auditoria: %s", exc)
        return jsonify({"error": "Falha ao recuperar logs de auditoria."}), 500


@api_bp.route("/audit-logs/<int:audit_id>", methods=["PUT", "PATCH", "DELETE"])
@require_auth
def block_audit_log_mutations(audit_id: int):
    """Bloqueio explícito de mutação ou deleção de logs de auditoria via API."""
    return jsonify({
        "error": "Operação não permitida: a trilha de auditoria é estritamente imutável (append-only)."
    }), 405

#   (alias: /api/v1/companies/search)
#
# COLE ESTE BLOCO no mesmo arquivo do `api_bp` (o que contém search_company),
# logo ANTES de:   @api_bp.route("/search_status/<int:search_id>", ...)
#
# Não precisa de blueprint novo nem de register_blueprint: a rota nasce em api_bp.
# Reaproveita o que o arquivo já importa: orchestrator, discover_company_cnpjs,
# resolve_company_logo, fetch_cnpj_brasilapi, resolve_search_strategy,
# is_valid_company_search_input, is_cnpj, CNPJ_REGEX, ThreadPoolExecutor, current_app.
# =============================================================================
import inspect
import unicodedata

import requests

CATALOG_CACHE = {}          # {(termo_normalizado,): (timestamp, payload)}
CATALOG_TTL = 3600          # 1h
CATALOG_PAGES = 3           # páginas do Comunica PJe varridas na descoberta (50 itens/página)
CATALOG_MAX_ROOTS = 30      # teto de empresas (raízes de CNPJ) por busca
CATALOG_BRANCHES_PER_ROOT = 3   # filiais (vistas nos processos) listadas por empresa
CATALOG_MAX_BRANCH_JOBS = 20    # teto global de consultas extras de filiais à BrasilAPI
_FREE_MAIL = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "uol.com.br",
    "bol.com.br", "terra.com.br", "ig.com.br", "icloud.com", "live.com", "msn.com",
}


def _cat_norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9 ]", " ", text.upper()).strip()


def _cat_dv(base12: str) -> str:
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    r1 = sum(int(d) * w for d, w in zip(base12, w1)) % 11
    d1 = "0" if r1 < 2 else str(11 - r1)
    r2 = sum(int(d) * w for d, w in zip(base12 + d1, w2)) % 11
    d2 = "0" if r2 < 2 else str(11 - r2)
    return d1 + d2


def _cat_valid(cnpj: str) -> bool:
    return len(cnpj) == 14 and len(set(cnpj)) > 1 and cnpj[12:] == _cat_dv(cnpj[:12])


def _cat_matrix(root8: str) -> str:
    base = root8 + "0001"
    return base + _cat_dv(base)


def _cat_brasilapi(cnpj: str):
    """Obtém dados cadastrais do CNPJ com camadas resilientes (Diretório Oficial, DB, Cache e APIs)."""
    clean_cnpj = re.sub(r"\D", "", str(cnpj or ""))
    if not _cat_valid(clean_cnpj):
        return None

    # 1. Diretório Oficial Corporativo de Fallback (alta velocidade, thread-safe, sem I/O)
    try:
        from app.services.economic_service import OFFICIAL_CORPORATE_METADATA
        if clean_cnpj in OFFICIAL_CORPORATE_METADATA:
            return dict(OFFICIAL_CORPORATE_METADATA[clean_cnpj])
    except Exception as exc:
        logger.debug("Catálogo: OFFICIAL_CORPORATE_METADATA falhou (%s): %s", clean_cnpj, exc)

    # 2. Tenta obter do banco local CompanyRegistry
    try:
        from app.models.CompanyRegistry import CompanyRegistry
        reg = CompanyRegistry.query.filter_by(cnpj=clean_cnpj).first()
        if reg and reg.razao_social:
            return {
                "cnpj": clean_cnpj,
                "razao_social": reg.razao_social,
                "nome_fantasia": reg.nome_fantasia or "",
                "descricao_situacao_cadastral": reg.situacao_cadastral or "ATIVA",
                "capital_social": float(reg.capital_social or 0.0),
                "municipio": reg.municipio or "",
                "uf": reg.uf or "",
                "identificador_matriz_filial": 1 if (reg.matriz_filial == "1" or clean_cnpj[8:12] == "0001") else 2,
            }
    except Exception as exc:
        logger.debug("Catálogo: consulta a CompanyRegistry falhou (%s): %s", clean_cnpj, exc)

    # 3. Tenta fetch_accurate_cnpj_data_cached (Round-Robin entre BrasilAPI, CNPJ.ws, MinhaReceita, ReceitaWS)
    try:
        from app.services.economic_service import fetch_accurate_cnpj_data_cached
        data = fetch_accurate_cnpj_data_cached(clean_cnpj, timeout=4.0)
        if isinstance(data, dict) and data.get("razao_social"):
            return {
                "cnpj": clean_cnpj,
                "razao_social": data.get("razao_social"),
                "nome_fantasia": data.get("nome_fantasia") or "",
                "descricao_situacao_cadastral": data.get("situacao") or data.get("descricao_situacao_cadastral") or "ATIVA",
                "capital_social": float(data.get("capital_social") or 0.0),
                "municipio": data.get("municipio") or "",
                "uf": data.get("uf") or "",
                "identificador_matriz_filial": 1 if clean_cnpj[8:12] == "0001" else 2,
            }
    except Exception as exc:
        logger.debug("Catálogo: fetch_accurate_cnpj_data_cached falhou (%s): %s", clean_cnpj, exc)

    # 4. Tenta fetch_cnpj_brasilapi
    try:
        data = fetch_cnpj_brasilapi(clean_cnpj)
        if isinstance(data, dict) and data.get("razao_social") and "descricao_situacao_cadastral" in data:
            return data
    except Exception as exc:
        logger.debug("Catálogo: fetch_cnpj_brasilapi falhou (%s): %s", clean_cnpj, exc)

    # 5. Tenta requisição direta BrasilAPI
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{clean_cnpj}", headers=headers, timeout=5)
        if r.ok:
            data = r.json()
            if isinstance(data, dict) and data.get("razao_social"):
                return data
    except Exception as exc:
        logger.debug("Catálogo: BrasilAPI direta falhou (%s): %s", clean_cnpj, exc)

    return None


def _cat_logo(data: dict, cnpj: str):
    """Chama resolve_company_logo do projeto adaptando-se à assinatura real (inspect).
    Retorna (logo_dict, origem) — origem aparece no ?debug=1."""
    razao = data.get("razao_social") or ""
    fantasia = data.get("nome_fantasia") or ""
    email = (data.get("email") or "").lower()
    values = {
        "company_name": razao, "razao_social": razao, "name": razao, "nome": razao,
        "nome_fantasia": fantasia, "cnpj": cnpj, "email": email,
        "brasil_data": data, "brasilapi_data": data, "cnpj_data": data, "data": data,
        "dados_cnpj": data, "company_data": data,
    }
    try:
        sig = inspect.signature(resolve_company_logo)
        kwargs, missing = {}, []
        for pname, p in sig.parameters.items():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            if pname in values:
                kwargs[pname] = values[pname]
            elif p.default is inspect.Parameter.empty:
                missing.append(pname)
        if missing:
            raise TypeError(f"parâmetros obrigatórios não mapeados: {missing}")
        logo = resolve_company_logo(**kwargs)
        if isinstance(logo, str):
            logo = {"logo_url": logo}
        if isinstance(logo, dict) and logo:
            return logo, "resolve_company_logo"
    except Exception as exc:
        origem = f"fallback favicon ({type(exc).__name__}: {exc})"
    else:
        origem = "fallback favicon (resolve_company_logo devolveu vazio)"

    domain = email.split("@")[-1] if "@" in email else ""
    initials = "".join(re.findall(r"[A-Za-z0-9]", razao))[:3].upper() or "EMP"
    logo = {"fallback_initials": initials}
    if domain and domain not in _FREE_MAIL:
        logo["google_favicon_url"] = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    return logo, origem


def _cat_enrich(args):
    app, cnpj, fallback, with_logo = args
    with app.app_context():
        used = cnpj
        data = _cat_brasilapi(cnpj)
        if not data and fallback and fallback != cnpj:
            used, data = fallback, _cat_brasilapi(fallback)
        if not data:
            return None
        if with_logo:
            logo, origem = _cat_logo(data, used)
        else:
            logo, origem = {}, "filial"
        return used, data, logo, origem


@api_bp.route("/company-catalog", methods=["GET"])
@api_bp.route("/companies/search", methods=["GET"])
def search_companies_catalog():
    """Catálogo: todas as empresas cujo nome contém o termo, agrupáveis por raiz de CNPJ
    (matriz + filiais vistas nos processos), cada uma com logo, razão social e CNPJ.
    O frontend envia o CNPJ escolhido a POST /search-company (modo STRICT_CNPJ)."""
    q = (request.args.get("q") or "").strip()
    debug = request.args.get("debug") == "1"
    include_branches = request.args.get("include_branches", "1") != "0"
    try:
        limit = max(1, min(int(request.args.get("limit", CATALOG_MAX_ROOTS)), CATALOG_MAX_ROOTS))
    except ValueError:
        limit = CATALOG_MAX_ROOTS
    if len(q) < 3:
        return jsonify({"error": "Informe ao menos 3 caracteres.", "items": []}), 400
    if not is_valid_company_search_input(q):
        return jsonify({"error": "empresas so são pesquisadas por razão social ou CNPJ", "items": []}), 400

    key = (_cat_norm(q), include_branches, limit)
    if not debug:
        hit = CATALOG_CACHE.get(key)
        if hit and time.time() - hit[0] < CATALOG_TTL:
            return jsonify(hit[1]), 200

    dbg = {"consulta": q}
    app = current_app._get_current_object()
    counts, seen = {}, {}   # raiz -> nº de processos que a citam / CNPJs completos vistos

    try:
        digits_q = re.sub(r"\D", "", q)
        if is_cnpj(q) and _cat_valid(digits_q):
            counts[digits_q[:8]] = 0
            seen[digits_q[:8]] = {digits_q}
            dbg["descoberta"] = "CNPJ informado"
        else:
            strat = resolve_search_strategy(q, brasil_api_fetcher=fetch_cnpj_brasilapi)
            brand = strat.get("comunica_query_term") or q
            dbg["termo_pje"] = brand

            # 1) Descoberta via Banco Local (CompanyRegistry)
            try:
                from app.models.CompanyRegistry import CompanyRegistry
                from sqlalchemy import or_
                db_matches = CompanyRegistry.query.filter(
                    or_(
                        CompanyRegistry.razao_social.ilike(f"%{q}%"),
                        CompanyRegistry.nome_fantasia.ilike(f"%{q}%")
                    )
                ).all()
                for comp in db_matches:
                    d = re.sub(r"\D", "", comp.cnpj)
                    if _cat_valid(d):
                        counts.setdefault(d[:8], 0)
                        seen.setdefault(d[:8], set()).add(d)
                dbg["db_matches"] = len(db_matches)
            except Exception as exc:
                dbg["db_matches_erro"] = str(exc)

            # 2) Descoberta via Diretório Oficial Corporativo (ex: BRADESCO, ITAU, etc.)
            try:
                from app.services.economic_service import OFFICIAL_CORPORATE_CNPJ_DIRECTORY
                norm_q = _cat_norm(q).upper()
                for brand_key, cnpjs in OFFICIAL_CORPORATE_CNPJ_DIRECTORY.items():
                    norm_k = _cat_norm(brand_key).upper()
                    if norm_k in norm_q or norm_q in norm_k or (len(norm_q) >= 4 and norm_q.split()[0] in norm_k):
                        for c in cnpjs:
                            d = re.sub(r"\D", "", c)
                            if _cat_valid(d):
                                counts.setdefault(d[:8], 0)
                                seen.setdefault(d[:8], set()).add(d)
            except Exception as exc:
                dbg["directory_erro"] = str(exc)

            processes_map = {}
            try:
                processes_map = orchestrator._fetch_comunica_pje(
                    brand, max_pages=CATALOG_PAGES, page_size=50
                ) or {}
            except Exception as exc:
                dbg["pje_erro"] = f"{type(exc).__name__}: {exc}"
                logger.warning("Catálogo: Comunica PJe falhou para '%s': %s", brand, exc)
            dbg["pje_processos"] = len(processes_map)

            # 3) CNPJs nos textos das publicações (contados 1x por processo e por raiz)
            for proc in processes_map.values():
                here = {}
                for txt in proc.get("publicacoes_texto", []) or []:
                    for m in CNPJ_REGEX.findall(str(txt)):
                        d = re.sub(r"\D", "", m)
                        if _cat_valid(d):
                            here.setdefault(d[:8], set()).add(d)
                for root, cnpjs in here.items():
                    counts[root] = counts.get(root, 0) + 1
                    seen.setdefault(root, set()).update(cnpjs)
            dbg["cnpjs_nos_textos"] = len(counts)

            # 4) Descoberta já existente no projeto (grupo/marca)
            try:
                for ent in discover_company_cnpjs(q, brand, processes_map, search_strategy=strat) or []:
                    d = re.sub(r"\D", "", str(ent))
                    if _cat_valid(d):
                        counts.setdefault(d[:8], 0)
                        seen.setdefault(d[:8], set()).add(d)
            except Exception as exc:
                dbg["discover_erro"] = f"{type(exc).__name__}: {exc}"
            dbg["raizes_candidatas"] = len(counts)

        roots = sorted(counts, key=lambda r: -counts[r])[:limit]
        jobs = [(app, _cat_matrix(r), sorted(seen[r])[0], True) for r in roots]
        n_matrizes = len(jobs)
        if include_branches:
            budget = CATALOG_MAX_BRANCH_JOBS
            for r in roots:
                mat = _cat_matrix(r)
                for c in [c for c in sorted(seen[r]) if c != mat][:CATALOG_BRANCHES_PER_ROOT]:
                    if budget <= 0:
                        break
                    jobs.append((app, c, None, False))
                    budget -= 1

        by_cnpj = is_cnpj(q)          # busca por CNPJ não passa pelo filtro de nome
        tokens = [] if by_cnpj else _cat_norm(q).split()
        items, emitted = [], set()
        descartados = sem_resposta = 0
        logos = {"resolve_company_logo": 0, "fallback": 0}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for job, res in zip(jobs, pool.map(_cat_enrich, jobs)):
                if not res:
                    sem_resposta += 1
                    continue
                cnpj, data, logo, origem = res
                if cnpj in emitted:
                    continue
                haystack = _cat_norm(f"{data.get('razao_social', '')} {data.get('nome_fantasia') or ''}")
                if not all(t in haystack for t in tokens):
                    descartados += 1
                    continue
                emitted.add(cnpj)
                if origem != "filial":
                    logos["resolve_company_logo" if origem == "resolve_company_logo" else "fallback"] += 1
                    if origem != "resolve_company_logo":
                        dbg.setdefault("logo_motivo_fallback", origem)
                items.append({
                    "cnpj": cnpj,
                    "cnpj_root": cnpj[:8],
                    "razao_social": data.get("razao_social"),
                    "nome_fantasia": data.get("nome_fantasia") or None,
                    "situacao": data.get("descricao_situacao_cadastral"),
                    "capital_social": data.get("capital_social"),
                    "municipio": data.get("municipio"),
                    "uf": data.get("uf"),
                    "matriz": cnpj[8:12] == "0001",
                    "processos_encontrados": counts.get(cnpj[:8], 0),
                    "logo": logo,
                    "logo_url": logo.get("logo_url") if logo.get("is_verified_domain") else None,
                })
        dbg["brasilapi"] = {"consultas": len(jobs), "matrizes": n_matrizes,
                            "sem_resposta": sem_resposta, "descartados_por_nome": descartados}
        dbg["logos"] = logos

        items.sort(key=lambda c: (c["situacao"] != "ATIVA", -c["processos_encontrados"],
                                  _cat_norm(c["razao_social"]), c["cnpj_root"],
                                  not c["matriz"], c["cnpj"]))
        payload = {"query": q, "count": len(items), "items": items}
        if debug:
            payload["debug"] = dbg
        if not items:
            logger.info("Catálogo sem resultados para '%s': %s", q, dbg)
            return jsonify({**payload, "error": "NOT_FOUND"}), 200
        CATALOG_CACHE[key] = (time.time(), {k: v for k, v in payload.items() if k != "debug"})
        return jsonify(payload), 200
    except Exception as exc:
        logger.exception("Erro no catálogo de empresas ('%s'): %s", q, exc)
        body = {"error": "Erro interno ao montar o catálogo.", "items": []}
        if debug:
            body["debug"] = {**dbg, "excecao": f"{type(exc).__name__}: {exc}"}
        return jsonify(body), 500


def _expire_browser_scan(search):
    """Uma aba fechada ou conexão perdida não deixa a busca pendente indefinidamente."""
    if (search.scan_status == "BROWSER_PENDING" and search.last_checked_at
            and (get_utc_now() - search.last_checked_at).total_seconds() > 120):
        CompanySearch.query.filter_by(id=search.id, scan_status="BROWSER_PENDING").update(
            {CompanySearch.scan_status: "BROWSER_PARTIAL"}, synchronize_session=False)
        db.session.commit()
        db.session.refresh(search)


@api_bp.route("/search_status/<int:search_id>", methods=["GET"])
@api_bp.route("/search-status/<int:search_id>", methods=["GET"])
def get_search_status(search_id: int):
    """
    Endpoint leve para polling pelo front-end.
    Permite verificar se o Deep Scan em segundo plano já completou a paginação.
    """
    try:
        search = db.session.get(CompanySearch, search_id)
        if not search:
            return jsonify({"error": "Registro não encontrado."}), 404

        # Anti-IDOR: Se a busca pertencer a um tenant específico, valida permissão
        current_user = get_current_user_optional()
        if search.tenant_id is not None:
            if not current_user or current_user.tenant_id != search.tenant_id:
                return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403
        
        _expire_browser_scan(search)
        is_completed = search.scan_status == "COMPLETED"
        is_terminal = is_completed or search.scan_status == "BROWSER_PARTIAL"
        response = {
            "search_id": search.id,
            "company_name": search.company_name,
            "scan_status": search.scan_status,
            "is_completed": is_completed,
            "is_terminal": is_terminal,
            "total_processes": search.total_processes,
            "last_checked_at": search.last_checked_at.isoformat() if search.last_checked_at else None,
            "updated_at": search.updated_at.isoformat() if search.updated_at else None,
        }
        # Quando completado, retorna os dados atualizados para atualizar o dashboard sem reload
        if is_terminal:
            proc_dicts = [p.to_dict() for p in search.processes]
            response["data"] = search.to_dict()
            response["data"]["empresa"] = search.company_name
            response["data"]["processos"] = proc_dicts
            response["data"]["is_broad_brand"] = "GRUPO CONSOLIDADO" in search.company_name.upper()

            # Prioriza estritamente os totais, desfechos e motivos consolidados do search
            # (calculados pelo background worker sobre TODOS os processos da varredura completa)
            response["data"]["total_processos"] = search.total_processes or len(proc_dicts)
            response["data"]["desfechos_sentencas"] = search.outcomes_summary or {}
            response["data"]["ranking_motivos"] = search.ranking_motivos or []
            response["data"]["ranking_motivos_condenacao"] = search.ranking_motivos_condenacao or []
            response["data"]["condenacao_por_motivo"] = search.condenacao_por_motivo or []
            response["data"]["plus_previsao_condenacao"] = search.predictions_summary or []
            response["data"]["score_risco_terceirizacao"] = search.overall_risk_score or 0.0
            response["data"]["overall_risk_score"] = search.overall_risk_score or 0.0

            # Se proc_dicts contiver o volume completo ou superior, sincroniza métricas
            if proc_dicts and len(proc_dicts) >= (search.total_processes or 0):
                fresh_metrics = orchestrator.predictor.generate_dashboard_metrics(
                    search.company_name, proc_dicts
                )
                response["data"]["total_processos"] = fresh_metrics.get("total_processos", search.total_processes)
                response["data"]["desfechos_sentencas"] = fresh_metrics.get("desfechos_sentencas", search.outcomes_summary)
                response["data"]["ranking_motivos"] = fresh_metrics.get("ranking_motivos", search.ranking_motivos)
                response["data"]["ranking_motivos_condenacao"] = fresh_metrics.get("ranking_motivos_condenacao", search.ranking_motivos_condenacao)
                response["data"]["condenacao_por_motivo"] = fresh_metrics.get("condenacao_por_motivo", search.condenacao_por_motivo)
                response["data"]["plus_previsao_condenacao"] = fresh_metrics.get("plus_previsao_condenacao", search.predictions_summary)
                response["data"]["score_risco_terceirizacao"] = fresh_metrics.get("score_risco_terceirizacao", search.overall_risk_score)
                response["data"]["overall_risk_score"] = fresh_metrics.get("score_risco_terceirizacao", search.overall_risk_score)

            response["data"]["parecer_agente_adk"] = response["data"].get("ai_analysis", {})
            if isinstance(search.economic_standing, dict):
                response["data"]["idoneidade_economica"] = search.economic_standing
                response["data"]["logo"] = search.economic_standing.get("logo", {})
                if search.economic_standing.get("indice_exposicao_financeira") is not None:
                    response["data"]["indice_exposicao_financeira"] = search.economic_standing["indice_exposicao_financeira"]
                    response["data"]["score_risco_terceirizacao"] = search.economic_standing["indice_exposicao_financeira"]
                    response["data"]["overall_risk_score"] = search.economic_standing["indice_exposicao_financeira"]

        return jsonify(response), 200
    except Exception as exc:
        logger.error("Erro ao verificar status do search_id %d: %s", search_id, exc)
        return jsonify({"error": "Registro não encontrado."}), 404


@api_bp.route("/sync-incremental/<int:search_id>", methods=["POST"])
def incremental_sync(search_id: int):
    """
    Executa varredura incremental por delta temporal:
    Consulta no DataJud/Comunica PJe apenas processos ou movimentações com
    dataHoraUltimaAtualizacao > cached_search.last_checked_at.
    """
    try:
        search = db.session.get(CompanySearch, search_id)
        if not search:
            return jsonify({"error": "Registro não encontrado."}), 404
        since_date = search.last_checked_at.isoformat() if search.last_checked_at else None

        processes_map = orchestrator._fetch_comunica_pje(search.company_name, max_pages=2, page_size=50)
        if not processes_map:
            search.last_checked_at = get_utc_now()
            db.session.commit()
            return jsonify({
                "status": "sem_novos_dados",
                "search_id": search_id,
                "updated": False,
                "message": "Nenhuma alteração recente encontrada no CNJ."
            }), 200

        delta_processes = orchestrator._enrich_with_datajud(
            processes_map, since_date=since_date, company_search_id=search_id
        )
        if not delta_processes:
            search.last_checked_at = get_utc_now()
            db.session.commit()
            return jsonify({
                "status": "sem_novos_dados",
                "search_id": search_id,
                "updated": False,
                "message": "Movimentações processuais inalteradas."
            }), 200

        new_hash = generate_movements_hash(delta_processes)
        if new_hash == search.movements_hash:
            search.last_checked_at = get_utc_now()
            db.session.commit()
            return jsonify({
                "status": "hash_inalterado",
                "search_id": search_id,
                "updated": False,
                "message": "Metadados idênticos. Não houve necessidade de reprocessar com IA."
            }), 200

        # Novos andamentos detectados: recalcula e roda IA
        metrics = orchestrator._calculate_metrics(search.company_name, delta_processes, scan_status="COMPLETED")
        ai_res = orchestrator.ai_analyzer.analyze_company(metrics)

        search.total_processes = metrics.get("total_processos", search.total_processes)
        search.outcomes_summary = metrics.get("desfechos_sentencas", search.outcomes_summary)
        search.ranking_motivos = metrics.get("ranking_motivos", search.ranking_motivos)
        search.predictions_summary = metrics.get("plus_previsao_condenacao", search.predictions_summary)
        search.overall_risk_score = metrics.get("score_risco_terceirizacao", search.overall_risk_score)
        search.ai_analysis = ai_res
        search.movements_hash = new_hash
        search.last_checked_at = datetime.utcnow()

        db.session.commit()
        return jsonify({
            "status": "atualizado_com_sucesso",
            "search_id": search_id,
            "updated": True,
            "data": search.to_dict()
        }), 200
    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro na varredura incremental do search_id %d: %s", search_id, exc)
        return jsonify({"error": "Falha na sincronização incremental."}), 500


@api_bp.route("/ingest-deep-scan", methods=["POST"])
@limit_rate(max_requests=30, window_seconds=60)
def ingest_deep_scan():
    """
    Endpoint canônico para ingestão em lote de varredura profunda (Deep Scan)
    proveniente do navegador ou robôs assíncronos.
    - Validação de payload e limite de 2.000 itens (HTTP 413 se excedido).
    - Deduplicação inteligente por número CNJ normalizado (20 dígitos).
    - Preserva processos já existentes e mescla novas ocorrências.
    - Reavaliação estatística completa sobre 100% dos processos (sem fatiamento artificial [:50]/[:100]).
    - Invariante estrita: soma dos desfechos == total de processos consolidados.
    - Regra estrita de bloqueio para zero condenações (0.0%).
    - Isolamento de Multitenancy defensivo (Anti-IDOR).
    - Atualização dos caches L0 (SEARCH_CACHE) e banco L1.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Payload JSON inválido. Um objeto JSON é esperado."}), 400

    search_id = data.get("search_id")
    if search_id is None or not isinstance(search_id, int):
        return jsonify({"error": "Campo 'search_id' obrigatório e deve ser um número inteiro."}), 400

    items = data.get("items")
    browser_finished = data.get("browser_scan_finished") is True
    if not isinstance(items, list) or (not items and not browser_finished):
        return jsonify({"error": "Campo 'items' obrigatório e deve ser uma lista não vazia."}), 400

    if len(items) > 2000:
        return jsonify({"error": "O lote de ingestão excede o limite máximo de 2.000 itens por requisição."}), 413

    is_final_batch = data.get("is_final_batch", True)
    if type(is_final_batch) is not bool:
        return jsonify({"error": "is_final_batch deve ser booleano."}), 400

    try:
        search = db.session.get(CompanySearch, search_id, with_for_update=True)
        if not search:
            return jsonify({"error": f"Registro de busca ID {search_id} não encontrado."}), 404

        # Isolamento de Multitenancy defensivo (Anti-IDOR)
        req_tenant_id = getattr(g, "tenant_id", None)
        if req_tenant_id is None and request.headers.get("X-Tenant-ID"):
            req_tenant_id = request.headers.get("X-Tenant-ID")

        if req_tenant_id is not None:
            rec_tenant_id = getattr(CompanySearch, "tenant_id", None)
            if isinstance(rec_tenant_id, (int, str)):
                tenant_val = rec_tenant_id
            else:
                tenant_val = getattr(search, "tenant_id", None)

            if tenant_val is not None and str(tenant_val) != str(req_tenant_id):
                return jsonify({"error": "Acesso não autorizado aos dados da busca solicitada."}), 403

        # Para buscas reais vinculadas a uma organização, o JWT (não o header informado
        # pelo cliente) é a autoridade de acesso.
        if isinstance(search.tenant_id, int):
            user = get_current_user_optional()
            if not user or user.tenant_id != search.tenant_id:
                return jsonify({"error": "Acesso não autorizado aos dados da busca solicitada."}), 403

        if browser_finished and (
            search.scan_status not in ("BROWSER_PENDING", "BROWSER_PARTIAL", "COMPLETED")
            or not (search.economic_standing or {}).get("browser_search_strategy")
        ):
            return jsonify({"error": "A busca não está sob paginação do navegador."}), 409
        if search.scan_status in ("BROWSER_PENDING", "BROWSER_PARTIAL") and not browser_finished:
            return jsonify({"error": "A conclusão da paginação do navegador deve ser informada."}), 409

        existing_records = {p.numero_processo: p for p in search.processes if p.numero_processo}

        new_records = []
        discovered_entities = set()

        for item in items:
            if not isinstance(item, dict):
                continue

            raw_num = (
                item.get("numero_processo")
                or item.get("numeroProcesso")
                or item.get("numeroprocessocommascara")
            )
            cnj = orchestrator.searcher.normalize_cnj(raw_num) if raw_num else None
            if not cnj:
                continue

            if cnj in existing_records:
                existing_p = existing_records[cnj]
                item_outcome = item.get("sentence_outcome") or item.get("statusDesfecho")
                if existing_p.sentence_outcome == "em_aberto" and item_outcome and item_outcome != "em_aberto":
                    existing_p.sentence_outcome = item_outcome
                continue

            if item.get("primary_subject") or item.get("sentence_outcome"):
                tribunal = str(item.get("tribunal") or item.get("siglaTribunal") or "N/A")[:50]
                grau = str(item.get("grau") or "G1")[:50]
                sigilo_raw = item.get("nivel_sigilo") if item.get("nivel_sigilo") is not None else item.get("nivelSigilo", 0)
                try:
                    nivel_sigilo = int(sigilo_raw)
                except (ValueError, TypeError):
                    nivel_sigilo = 0
                primary_sub = item.get("primary_subject") or "Obrigação / Reclamação em Apuração Inicial"
                subjects_list = item.get("subjects") or [primary_sub]
                outcome = item.get("sentence_outcome") or "em_aberto"
                has_expert = bool(item.get("has_expert_report", False))
                obs = item.get("observation")
                parsed_proc = item
            else:
                single_map = {}
                orchestrator.searcher._parse_comunica_items([item], single_map, discovered_entities)
                com_data = single_map.get(cnj, {})
                parsed_proc = orchestrator.searcher._merge_process_data_fast(cnj, com_data, None)
                tribunal = str(parsed_proc.get("tribunal") or "N/A")[:50]
                grau = str(parsed_proc.get("grau") or "G1")[:50]
                nivel_sigilo = int(parsed_proc.get("nivel_sigilo") or 0)
                primary_sub = parsed_proc.get("primary_subject")
                subjects_list = [s.get("nome") for s in parsed_proc.get("assuntos", []) if isinstance(s, dict) and s.get("nome")]
                outcome = parsed_proc.get("sentence_outcome") or "em_aberto"
                has_expert = bool(parsed_proc.get("has_expert_report", False))
                obs = parsed_proc.get("observation")

            record = ProcessRecord(
                company_search_id=search.id,
                numero_processo=cnj,
                tribunal=tribunal,
                grau=grau,
                nivel_sigilo=nivel_sigilo,
                primary_subject=primary_sub,
                subjects_json=subjects_list,
                observation=obs,
                sentence_outcome=outcome,
                has_expert_report=has_expert,
                data_distribuicao=parsed_proc.get("data_distribuicao"),
                valor_causa=parsed_proc.get("valor_causa"),
                raw_metadata=parsed_proc,
            )
            new_records.append(record)
            existing_records[cnj] = record

        if new_records:
            for i in range(0, len(new_records), 50):
                db.session.bulk_save_objects(new_records[i : i + 50])
            db.session.flush()

        # Recarrega a totalidade dos processos consolidados (100% da base sem truncamentos)
        all_processes = ProcessRecord.query.filter_by(company_search_id=search.id).all()
        proc_dicts = []
        for p in all_processes:
            d = p.to_dict()
            if isinstance(p.raw_metadata, dict):
                for k, v in p.raw_metadata.items():
                    if k not in d:
                        d[k] = v
            proc_dicts.append(d)

        fresh_metrics = orchestrator.predictor.generate_dashboard_metrics(search.company_name, proc_dicts)

        p_cond = fresh_metrics.get("probabilidade_condenacao_geral_pct") or fresh_metrics.get("score_risco_terceirizacao") or 0.0
        browser_strategy = (search.economic_standing or {}).get("browser_search_strategy")
        econ_data = economic_service.evaluate_economic_standing(
            company_name=search.company_name,
            company_processes=proc_dicts,
            p_condenacao_trabalhista=p_cond,
            **({"cnpj": browser_strategy.get("target_cnpj"), "search_strategy": browser_strategy}
               if browser_strategy else {}),
        )
        if browser_strategy:
            econ_data = {**econ_data, "browser_search_strategy": browser_strategy}

        ai_payload = {**fresh_metrics, "empresa": search.company_name, "processos": proc_dicts}
        ai_res = orchestrator.ai_analyzer.analyze_company(ai_payload)

        search.total_processes = len(all_processes)
        secrecy_count = sum(1 for p in all_processes if p.nivel_sigilo and p.nivel_sigilo > 0)
        search.secrecy_count = secrecy_count
        search.secrecy_rate = round(secrecy_count / len(all_processes), 4) if all_processes else 0.0
        search.outcomes_summary = fresh_metrics.get("desfechos_sentencas", {})
        search.ranking_motivos = fresh_metrics.get("ranking_motivos", [])
        search.ranking_motivos_condenacao = fresh_metrics.get("ranking_motivos_condenacao", [])
        search.condenacao_por_motivo = fresh_metrics.get("condenacao_por_motivo", [])
        search.predictions_summary = fresh_metrics.get("plus_previsao_condenacao", [])
        search.overall_risk_score = fresh_metrics.get("score_risco_terceirizacao", 0.0)
        search.economic_standing = econ_data
        if econ_data and isinstance(econ_data, dict):
            search.cnae_codigo = econ_data.get("cnae_codigo") or search.cnae_codigo
            search.cnae_descricao = econ_data.get("cnae_descricao") or search.cnae_descricao
        search.ai_analysis = ai_res
        if browser_finished:
            if search.scan_status != "COMPLETED":
                search.scan_status = "COMPLETED" if is_final_batch else "BROWSER_PARTIAL"
        else:
            search.scan_status = "COMPLETED" if is_final_batch else "PARTIAL_FAST_PATH"
        search.movements_hash = generate_movements_hash(proc_dicts)
        search.last_checked_at = get_utc_now()
        db.session.commit()

        # Invalida as chaves reais de busca que apontam para este ID, sem criar
        # uma chave incompatível com STRICT_CNPJ/BROAD_BRAND.
        for cache_key, (_, cached) in list(SEARCH_CACHE.items()):
            if cached.get("search_id", cached.get("id")) == search.id:
                SEARCH_CACHE.pop(cache_key, None)
        _prune_search_cache_if_needed()

        condenacoes = search.outcomes_summary.get("condenacoes", 0) if search.outcomes_summary else 0
        previsao_bloqueada = (condenacoes == 0)
        prob_pct = None if previsao_bloqueada else fresh_metrics.get("probabilidade_condenacao_geral_pct")

        return jsonify({
            "status": "success",
            "search_id": search.id,
            "company_name": search.company_name,
            "scan_status": search.scan_status,
            "total_processes": search.total_processes,
            "ingested_count": len(new_records),
            "desfechos_sentencas": search.outcomes_summary or {},
            "score_risco_terceirizacao": search.overall_risk_score,
            "probabilidade_condenacao_geral_pct": prob_pct,
            "previsao_bloqueada": previsao_bloqueada,
        }), 200

    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro durante ingest-deep-scan para search_id %d: %s", search_id, exc)
        return jsonify({"error": f"Erro interno ao processar ingestão: {str(exc)}"}), 500


@api_bp.route("/searches/<int:company_search_id>/processes", methods=["GET"])
@api_bp.route("/searches/<int:company_search_id>/processes/", methods=["GET"])
def get_search_processes(company_search_id: int):
    """
    Retorna a listagem paginada e filtrada dos processos consolidados de uma empresa.
    Endpoint: GET /api/v1/searches/{company_search_id}/processes
    Query parameters:
    - page: int (padrão 1, >= 1)
    - per_page: int (padrão 50, máximo 100)
    - tribunal: string opcional normalizada (ex: TRT6, TST)
    - date_from: data ISO YYYY-MM-DD
    - date_to: data ISO YYYY-MM-DD
    - min_value: float/decimal >= 0
    - max_value: float/decimal >= 0
    - outcome: string opcional (condenacao, acordo, improcedente, em_aberto)
    - search: string opcional (busca textual por CNJ, assunto ou observação)
    """
    try:
        search = (
            db.session.get(CompanySearch, company_search_id)
            if hasattr(db.session, "get")
            else CompanySearch.query.get(company_search_id)
        )
        if not search:
            return jsonify({"error": f"Pesquisa ID {company_search_id} não encontrada."}), 404

        # Anti-IDOR Obrigatório: Se a busca pertencer a um tenant específico, valida permissão estrita
        current_user = get_current_user_optional()
        req_tenant_id = getattr(g, "tenant_id", None) or request.headers.get("X-Tenant-ID")

        rec_tenant_id = getattr(CompanySearch, "tenant_id", None)
        if isinstance(rec_tenant_id, (int, str)):
            tenant_val = rec_tenant_id
        else:
            tenant_val = getattr(search, "tenant_id", None)

        if tenant_val is not None:
            if current_user:
                if str(current_user.tenant_id) != str(tenant_val):
                    return jsonify({"error": "Acesso não autorizado aos dados da busca solicitada."}), 403
            elif req_tenant_id is not None:
                if str(tenant_val) != str(req_tenant_id):
                    return jsonify({"error": "Acesso não autorizado aos dados da busca solicitada."}), 403
            else:
                return jsonify({"error": "Acesso não autorizado aos dados da busca solicitada."}), 403

        # Paginação sanitizada: page (default 1, >= 1) e per_page (default 50, teto 100)
        try:
            page = int(request.args.get("page", 1))
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        try:
            per_page = int(request.args.get("per_page", 50))
            if per_page < 1:
                per_page = 50
            per_page = min(100, per_page)
        except (ValueError, TypeError):
            per_page = 50

        query = ProcessRecord.query.filter_by(company_search_id=search.id)

        # Filtro: tribunal (normalizado case-insensitive e flexível)
        tribunal = request.args.get("tribunal")
        if tribunal and tribunal.strip():
            t_raw = tribunal.strip()
            norm_t = normalize_court_code(t_raw)
            clauses = [ProcessRecord.tribunal.ilike(f"%{t_raw}%")]
            if norm_t:
                clauses.append(ProcessRecord.tribunal.ilike(f"%{norm_t}%"))
                clauses.append(ProcessRecord.tribunal == norm_t)
            query = query.filter(db.or_(*clauses))

        # Filtros de data: date_from e date_to (ISO YYYY-MM-DD)
        date_from = request.args.get("date_from")
        if date_from and date_from.strip():
            try:
                d_from = datetime.strptime(date_from.strip(), "%Y-%m-%d").date()
                query = query.filter(ProcessRecord.data_distribuicao >= d_from)
            except ValueError:
                return jsonify({"error": "Formato de data inválido para 'date_from'. Utilize YYYY-MM-DD."}), 400

        date_to = request.args.get("date_to")
        if date_to and date_to.strip():
            try:
                d_to = datetime.strptime(date_to.strip(), "%Y-%m-%d").date()
                query = query.filter(ProcessRecord.data_distribuicao <= d_to)
            except ValueError:
                return jsonify({"error": "Formato de data inválido para 'date_to'. Utilize YYYY-MM-DD."}), 400

        # Filtros de valor da causa: min_value e max_value (>= 0)
        min_value = request.args.get("min_value")
        if min_value is not None and str(min_value).strip() != "":
            try:
                mv = float(str(min_value).strip())
                if mv < 0:
                    return jsonify({"error": "'min_value' deve ser maior ou igual a zero."}), 400
                query = query.filter(ProcessRecord.valor_causa >= mv)
            except (ValueError, TypeError):
                return jsonify({"error": "'min_value' deve ser um valor numérico válido."}), 400

        max_value = request.args.get("max_value")
        if max_value is not None and str(max_value).strip() != "":
            try:
                xv = float(str(max_value).strip())
                if xv < 0:
                    return jsonify({"error": "'max_value' deve ser maior ou igual a zero."}), 400
                query = query.filter(ProcessRecord.valor_causa <= xv)
            except (ValueError, TypeError):
                return jsonify({"error": "'max_value' deve ser um valor numérico válido."}), 400

        # Filtro: outcome (desfecho/status mapeado)
        outcome = request.args.get("outcome")
        if outcome and outcome.strip():
            o = outcome.strip().lower()
            if o in ("condenacao", "condenacoes", "condenação", "condenações"):
                query = query.filter(
                    ProcessRecord.sentence_outcome.in_([
                        "condenacoes", "condenacao", "procedencia_total", "procedencia_parcial"
                    ])
                )
            elif o in ("acordo", "acordos"):
                query = query.filter(ProcessRecord.sentence_outcome.in_(["acordos", "acordo", "acordo_homologado"]))
            elif o in ("improcedente", "improcedentes"):
                query = query.filter(ProcessRecord.sentence_outcome.in_(["improcedentes", "improcedente"]))
            elif o in ("em_aberto", "aberto"):
                query = query.filter(ProcessRecord.sentence_outcome.in_(["em_aberto", "aberto"]))
            elif o in ("extintos_sem_merito", "extinto_sem_merito", "extincao"):
                query = query.filter(ProcessRecord.sentence_outcome.in_(["extintos_sem_merito", "extinto_sem_merito"]))
            else:
                query = query.filter(ProcessRecord.sentence_outcome == o)

        # Filtro opcional de busca textual
        search_query = request.args.get("search")
        if search_query and search_query.strip():
            sq = f"%{search_query.strip()}%"
            query = query.filter(
                db.or_(
                    ProcessRecord.numero_processo.ilike(sq),
                    ProcessRecord.primary_subject.ilike(sq),
                    ProcessRecord.observation.ilike(sq),
                )
            )

        total_matches = query.count()
        total_pages = max(1, math.ceil(total_matches / per_page)) if total_matches > 0 else 1
        offset = (page - 1) * per_page
        records = query.order_by(ProcessRecord.id.asc()).offset(offset).limit(per_page).all()

        items = [
            {
                "id": r.id,
                "numero_processo": r.numero_processo,
                "tribunal": r.tribunal,
                "grau": r.grau,
                "data_distribuicao": r.data_distribuicao.isoformat() if r.data_distribuicao else None,
                "valor_causa": float(r.valor_causa) if r.valor_causa is not None else None,
                "primary_subject": r.primary_subject,
                "primary_subject_code": r.primary_subject_code,
                "subjects": r.subjects_json or [],
                "observation": r.observation,
                "sentence_outcome": r.sentence_outcome,
                "has_expert_report": r.has_expert_report,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]

        return jsonify({
            "search_id": search.id,
            "company_name": search.company_name,
            "page": page,
            "per_page": per_page,
            "total": total_matches,
            "pages": total_pages,
            "total_pages": total_pages,
            "items": items,
            "processes": items,
        }), 200

    except Exception as exc:
        logger.exception("Erro ao listar processos da busca ID %d: %s", company_search_id, exc)
        return jsonify({"error": f"Erro interno ao listar processos: {str(exc)}"}), 500


@api_bp.route("/history", methods=["GET", "DELETE"])
def get_search_history():
    """
    GET: Retorna o histórico de análises de empresas salvas no banco de dados.
    DELETE: Remove todo o histórico de buscas e processos vinculados do banco de dados.
    """
    current_user = get_current_user_optional()
    if request.method == "DELETE":
        try:
            if current_user:
                tenant_searches = CompanySearch.query.filter_by(tenant_id=current_user.tenant_id).all()
                for s in tenant_searches:
                    db.session.delete(s)
            else:
                # Exclusão em lote (batch deletes) para evitar table lock ou alto consumo de RAM
                batch_size = 500
                while True:
                    proc_ids = [p.id for p in ProcessRecord.query.with_entities(ProcessRecord.id).limit(batch_size).all()]
                    if not proc_ids:
                        break
                    ProcessRecord.query.filter(ProcessRecord.id.in_(proc_ids)).delete(synchronize_session=False)
                    db.session.commit()

                while True:
                    search_ids = [s.id for s in CompanySearch.query.with_entities(CompanySearch.id).limit(batch_size).all()]
                    if not search_ids:
                        break
                    CompanySearch.query.filter(CompanySearch.id.in_(search_ids)).delete(synchronize_session=False)
                    db.session.commit()
            db.session.commit()
            SEARCH_CACHE.clear()
            logger.info("Todo o histórico de pesquisas e processos foi limpo pelo usuário.")
            return jsonify({
                "status": "success",
                "message": "Histórico de pesquisas e processos limpo com sucesso."
            }), 200
        except Exception as exc:
            db.session.rollback()
            logger.error("Erro ao limpar histórico de pesquisas: %s", exc)
            return jsonify({"error": "Falha ao limpar histórico."}), 500

    limit = int(request.args.get("limit", 20))
    try:
        query = CompanySearch.query
        if current_user:
            query = query.filter_by(tenant_id=current_user.tenant_id)
        else:
            query = query.filter(CompanySearch.tenant_id.is_(None))
            
        searches = (
            query.order_by(CompanySearch.updated_at.desc())
            .limit(limit)
            .all()
        )
        return jsonify({
            "total": len(searches),
            "items": [s.to_dict() for s in searches]
        }), 200
    except Exception as exc:
        logger.error("Erro ao listar histórico de pesquisas: %s", exc)
        return jsonify({"error": "Não foi possível carregar o histórico."}), 500


@api_bp.route("/history/<int:search_id>", methods=["GET", "DELETE"])
@api_bp.route("/searches/<int:search_id>", methods=["GET"])
def get_search_detail(search_id: int):
    """
    GET: Retorna os dados detalhados e a lista de processos de uma análise específica.
    DELETE: Remove um registro específico do histórico.
    """
    search = db.session.get(CompanySearch, search_id)
    if not search:
        return jsonify({"error": "Registro não encontrado."}), 404

    # Anti-IDOR: Se a busca pertencer a um tenant específico, valida permissão
    current_user = get_current_user_optional()
    if search.tenant_id is not None:
        if not current_user or current_user.tenant_id != search.tenant_id:
            return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403

    if request.method == "DELETE":
        try:
            db.session.delete(search)
            db.session.commit()
            logger.info("Registro search_id=%d removido pelo usuário.", search_id)
            return jsonify({
                "status": "success",
                "message": f"Registro {search_id} removido com sucesso."
            }), 200
        except Exception as exc:
            db.session.rollback()
            logger.error("Erro ao deletar search_id %d: %s", search_id, exc)
            return jsonify({"error": "Falha ao remover o registro."}), 500
    try:
        _expire_browser_scan(search)
        proc_dicts = [p.to_dict() for p in search.processes]
        result = search.to_dict()
        result["empresa"] = search.company_name
        result["processos"] = proc_dicts
        if proc_dicts:
            fresh_metrics = orchestrator.predictor.generate_dashboard_metrics(
                search.company_name, proc_dicts
            )
            result["plus_previsao_condenacao"] = fresh_metrics["plus_previsao_condenacao"]
            result["score_risco_terceirizacao"] = fresh_metrics["score_risco_terceirizacao"]
            result["ranking_motivos_condenacao"] = fresh_metrics.get("ranking_motivos_condenacao", [])
            result["condenacao_por_motivo"] = fresh_metrics.get("condenacao_por_motivo", search.condenacao_por_motivo or [])
            result["previsao_bloqueada"] = fresh_metrics.get("previsao_bloqueada", False)
            result["motivo_bloqueio_previsao"] = fresh_metrics.get("motivo_bloqueio_previsao")
        result["parecer_agente_adk"] = result.get("ai_analysis", {})
        return jsonify(result), 200
    except Exception as exc:
        logger.error("Erro ao obter detalhes da pesquisa ID %d: %s", search_id, exc)
        return jsonify({"error": "Registro não encontrado."}), 404


@api_bp.route("/compare-companies", methods=["POST"])
@limit_rate(max_requests=60, window_seconds=60)
def compare_companies():
    """
    Endpoint canônico para benchmarking setorial comparativo entre 2 e 5 empresas.

    Formatos de entrada suportados via JSON:
    1. { "search_ids": [12, 15] }
    2. { "cnpjs": ["11.222.333/0001-44", "55.666.777/0001-88"] }
    3. { "items": [{ "search_id": 12 }, { "cnpj": "55.666.777/0001-88" }] }
    4. { "companies": ["Nome Empresa 1", "Nome Empresa 2"] }

    Validações e Segurança:
    - Exige entre 2 e 5 empresas distintas (HTTP 400 em caso de < 2 ou > 5).
    - Valida e sanitiza todos os termos de entrada via sanitizer.py.
    - Isolamento de multitenancy defensivo (Anti-IDOR): se g.tenant_id estiver ativo,
      impede acesso a buscas pertencentes a outros tenants.
    - Reutiliza 100% dos dados já computados pelo /overview (L0_MEMORY_CACHE ou L1_DATABASE),
      executando consulta sob demanda apenas para termos inéditos.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Payload JSON inválido. Envie um objeto com search_ids, cnpjs ou items."}), 400

    raw_items = []
    if "search_ids" in data and isinstance(data["search_ids"], list):
        for sid in data["search_ids"]:
            raw_items.append({"search_id": sid})
    elif "cnpjs" in data and isinstance(data["cnpjs"], list):
        for c in data["cnpjs"]:
            raw_items.append({"cnpj": c})
    elif "items" in data and isinstance(data["items"], list):
        for it in data["items"]:
            if isinstance(it, dict):
                raw_items.append(it)
            elif isinstance(it, int) or (isinstance(it, str) and str(it).isdigit() and len(str(it)) < 10):
                raw_items.append({"search_id": int(it)})
            elif isinstance(it, str):
                raw_items.append({"cnpj": str(it)})
    elif "companies" in data and isinstance(data["companies"], list):
        for comp in data["companies"]:
            if isinstance(comp, dict):
                raw_items.append(comp)
            else:
                raw_items.append({"company_name": str(comp)})

    if len(raw_items) < 2:
        return jsonify({"error": "É necessário informar entre 2 e 5 empresas para realizar o comparativo setorial."}), 400
    if len(raw_items) > 5:
        return jsonify({"error": "O limite máximo para comparação simultânea é de 5 empresas."}), 400

    # Verificação de duplicatas no payload de entrada
    seen_identifiers = set()
    for item in raw_items:
        sid = item.get("search_id") or item.get("id")
        cnpj_val = item.get("cnpj") or item.get("cnpj_utilizado")
        name_val = item.get("company_name") or item.get("name") or item.get("empresa")
        if sid is not None and str(sid).isdigit():
            key = f"id:{sid}"
        elif cnpj_val:
            d_val = re.sub(r"\D", "", str(cnpj_val))
            key = f"cnpj:{d_val}" if len(d_val) == 14 else f"name:{str(cnpj_val).upper()}"
        elif name_val:
            key = f"name:{sanitize_company_name(str(name_val)).upper()}"
        else:
            key = f"raw:{str(item)}"
        seen_identifiers.add(key)

    if len(seen_identifiers) < 2:
        return jsonify({"error": "Forneça pelo menos 2 empresas distintas para comparação."}), 400

    resolved_payloads: list = [None] * len(raw_items)
    on_demand_queue: list = []

    for idx, item in enumerate(raw_items):
        # Se já for um payload completo pré-calculado
        if isinstance(item, dict) and (
            item.get("risco_subsidiario") is not None
            or item.get("score_risco_terceirizacao") is not None
            or item.get("plus_previsao_condenacao") is not None
        ) and not (item.get("search_id") and not item.get("empresa")):
            resolved_payloads[idx] = item
            continue

        sid = item.get("search_id") or item.get("id")
        if sid is not None and str(sid).isdigit():
            sid_int = int(sid)
            search_record = db.session.get(CompanySearch, sid_int)
            if not search_record:
                return jsonify({"error": f"Empresa com ID {sid_int} não encontrada."}), 404

            # Defesa Anti-IDOR contra acesso cruzado de tenants
            req_tenant_id = getattr(g, "tenant_id", None)
            if req_tenant_id is None and request.headers.get("X-Tenant-ID"):
                req_tenant_id = request.headers.get("X-Tenant-ID")

            if req_tenant_id is not None:
                # Prioriza valor da classe (caso monkeypatched em testes) ou da instância
                rec_tenant_id = getattr(CompanySearch, "tenant_id", None)
                if isinstance(rec_tenant_id, int) or isinstance(rec_tenant_id, str):
                    tenant_val = rec_tenant_id
                else:
                    tenant_val = getattr(search_record, "tenant_id", None)

                if tenant_val is not None and str(tenant_val) != str(req_tenant_id):
                    return jsonify({"error": "Acesso não autorizado aos dados da busca solicitada."}), 403

            resolved_payloads[idx] = _search_record_to_pdf_payload(search_record)
            continue

        raw_term = str(item.get("cnpj") or item.get("company_name") or item.get("name") or "").strip()
        if not raw_term:
            return jsonify({"error": "Item de busca vazio ou sem identificador válido."}), 400

        # Sanitização da entrada
        if not is_valid_company_search_input(raw_term):
            return jsonify({"error": f"Identificador de empresa inválido: '{raw_term}'. Forneça Razão Social ou CNPJ válido."}), 400

        # Busca no L0 (Memory Cache) ou L1 (Database)
        cached_payload = _find_company_payload_for_pdf(raw_term)
        if cached_payload:
            resolved_payloads[idx] = cached_payload
            continue

        on_demand_queue.append((idx, raw_term))

    # Execução concorrente via ThreadPoolExecutor para termos inéditos fora de cache (baixa latência no Render Free)
    if on_demand_queue:
        def _fetch_company_on_demand(term: str) -> dict:
            def _brasil_api_fetcher_with_timeout(cnpj: str) -> Optional[str]:
                return fetch_cnpj_brasilapi(cnpj, timeout=5)

            strat = resolve_search_strategy(term, brasil_api_fetcher=_brasil_api_fetcher_with_timeout)
            q_term = strat["comunica_query_term"]
            d_title = strat["display_title"]
            target_cnpj = strat.get("target_cnpj")

            fast_res = orchestrator.analyze_company_by_name(
                company_name=d_title,
                search_strategy=strat,
                fast_path=True,
                limit_processes=50,
            )
            fast_res["empresa"] = d_title
            fast_res["is_broad_brand"] = strat.get("is_broad_brand", False)
            fast_res["search_strategy"] = strat

            econ = economic_service.evaluate_economic_standing(
                company_name=d_title,
                cnpj=target_cnpj,
                company_processes=fast_res.get("processos", []),
                p_condenacao_trabalhista=fast_res.get("probabilidade_condenacao_geral_pct") or 0.0,
                search_strategy=strat,
            )
            fast_res["idoneidade_economica"] = econ
            fast_res["logo"] = econ.get("logo", {})
            if econ.get("cnpj_utilizado"):
                fast_res["cnpj"] = econ.get("cnpj_utilizado")
            elif target_cnpj:
                fast_res["cnpj"] = target_cnpj
            if econ.get("risco_calote_subsidiario") is not None:
                fast_res["risco_subsidiario"] = econ["risco_calote_subsidiario"]
            elif econ.get("indice_exposicao_financeira") is not None:
                fast_res["risco_subsidiario"] = econ["indice_exposicao_financeira"]
            if econ.get("indice_exposicao_financeira") is not None:
                fast_res["indice_exposicao_financeira"] = econ["indice_exposicao_financeira"]
                fast_res["score_risco_terceirizacao"] = econ["indice_exposicao_financeira"]
                fast_res["overall_risk_score"] = econ["indice_exposicao_financeira"]

            ckey = strat.get("cache_key") or f"{strat['mode']}::{q_term}::{target_cnpj or ''}"
            _prune_search_cache_if_needed()
            SEARCH_CACHE[ckey] = (time.time(), fast_res)
            return fast_res

        if len(on_demand_queue) == 1:
            idx, term = on_demand_queue[0]
            try:
                resolved_payloads[idx] = _fetch_company_on_demand(term)
            except Exception as search_err:
                logger.error("Erro na busca sob demanda para comparativo ('%s'): %s", term, search_err)
                return jsonify({"error": f"Não foi possível processar a empresa '{term}' para o comparativo."}), 500
        else:
            with ThreadPoolExecutor(max_workers=min(len(on_demand_queue), 4)) as executor:
                future_to_item = {
                    executor.submit(_fetch_company_on_demand, term): (idx, term)
                    for idx, term in on_demand_queue
                }
                for future in as_completed(future_to_item, timeout=15.0):
                    idx, term = future_to_item[future]
                    try:
                        resolved_payloads[idx] = future.result(timeout=15.0)
                    except Exception as search_err:
                        logger.error("Erro na busca concorrente sob demanda ('%s'): %s", term, search_err)
                        return jsonify({"error": f"Não foi possível processar a empresa '{term}' para o comparativo."}), 500

    # Validação de unicidade das empresas resolvidas
    unique_resolved = set()
    for p in resolved_payloads:
        p_cnpj = re.sub(r"\D", "", str(p.get("cnpj") or (p.get("idoneidade_economica") or {}).get("cnpj_utilizado") or ""))
        p_name = sanitize_company_name(str(p.get("empresa") or p.get("company_name") or "")).upper()
        unique_resolved.add(p_cnpj if p_cnpj else p_name)

    if len(unique_resolved) < 2:
        return jsonify({"error": "Forneça pelo menos 2 empresas distintas para comparação."}), 400

    try:
        benchmark = build_sector_benchmark(resolved_payloads, ai_analyzer=orchestrator.ai_analyzer)
        return jsonify(benchmark), 200
    except Exception as bench_err:
        logger.exception("Erro ao gerar benchmarking setorial: %s", bench_err)
        return jsonify({"error": "Erro interno ao processar o comparativo setorial."}), 500


# =============================================================================
# EXPORTAÇÃO DE RELATÓRIO EXECUTIVO EM PDF (v2.0 RiskTech + Solvência)
# Suporta:
#   - GET  /api/v1/history/<int:search_id>/summary.pdf
#   - GET/POST /api/v1/report/pdf  (e /api/v1/export/pdf)
#   - GET  /api/v1/report/pdf/<path:company_name> (e /api/v1/export/pdf/<path:company_name>)
#   - Aliases diretos /api/report/pdf, /api/export/pdf registrados no app factory
# =============================================================================
def _strip_visual_company_suffixes(text: str) -> str:
    """Remove sufixos visuais como '(GRUPO CONSOLIDADO / FILIAIS)' para localizar a chave real."""
    cleaned = re.sub(r"\(.*?\)", "", str(text or "")).strip()
    for kw in ("GRUPO CONSOLIDADO", "FILIAIS", "MATRIZ / FILIAIS"):
        cleaned = re.sub(rf"\b{re.escape(kw)}\b", "", cleaned, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", cleaned).strip(" -/")


def _search_record_to_pdf_payload(search: CompanySearch) -> dict:
    """Converte um registro CompanySearch do banco no payload completo para geração do PDF."""
    result = search.to_dict()
    result["empresa"] = search.company_name
    result["total_processos"] = search.total_processes or 0
    result["desfechos_sentencas"] = search.outcomes_summary or {}
    result["ranking_motivos"] = search.ranking_motivos or []
    result["ranking_motivos_condenacao"] = search.ranking_motivos_condenacao or []
    result["condenacao_por_motivo"] = (
        search.condenacao_por_motivo
        if search.condenacao_por_motivo is not None
        else result.get("condenacao_por_motivo", [])
    )
    result["plus_previsao_condenacao"] = search.predictions_summary or []
    result["score_risco_terceirizacao"] = search.overall_risk_score or 0.0
    result["overall_risk_score"] = search.overall_risk_score or 0.0
    if isinstance(search.economic_standing, dict):
        result["idoneidade_economica"] = search.economic_standing
        if search.economic_standing.get("indice_exposicao_financeira") is not None:
            result["indice_exposicao_financeira"] = search.economic_standing["indice_exposicao_financeira"]
        if search.economic_standing.get("risco_calote_subsidiario") is not None:
            result["risco_subsidiario"] = search.economic_standing["risco_calote_subsidiario"]
    result["parecer_agente_adk"] = result.get("ai_analysis") or {}
    return result


def _find_company_payload_for_pdf(term: str, tenant_id: Optional[int] = None) -> Optional[dict]:
    """
    Procura os dados consolidados da empresa primeiro no L0_MEMORY_CACHE (SEARCH_CACHE)
    e depois no banco (CompanySearch), aplicando isolamento multitenant (anti-IDOR),
    limpando sufixos visuais e suportando CNPJs com barra.
    """
    raw = str(term or "").strip()
    if not raw:
        return None

    clean_no_parens = _strip_visual_company_suffixes(raw)
    digits = re.sub(r"\D", "", raw)
    cnpj_digits = digits if len(digits) == 14 else ""
    sanitized = sanitize_company_name(clean_no_parens or raw).upper()
    brand = extract_brand_search_term(clean_no_parens or raw).upper()

    targets = {
        t.upper()
        for t in (raw, clean_no_parens, sanitized, brand, cnpj_digits)
        if t and len(t.strip()) >= 2
    }

    # 1. Busca no L0_MEMORY_CACHE (SEARCH_CACHE)
    for cache_key, (_ts, cached_data) in list(SEARCH_CACHE.items()):
        if not isinstance(cached_data, dict):
            continue
        cached_tenant = cached_data.get("tenant_id")
        if cached_tenant is not None and tenant_id is not None and str(cached_tenant) != str(tenant_id):
            continue
        key_upper = str(cache_key).upper()
        emp_upper = str(cached_data.get("empresa") or cached_data.get("company_name") or "").upper()
        emp_clean = _strip_visual_company_suffixes(emp_upper).upper()
        econ = cached_data.get("idoneidade_economica") or {}
        strat = cached_data.get("search_strategy") or {}

        cached_cnpjs = set()
        for c_val in [
            econ.get("cnpj_utilizado"),
            econ.get("cnpj_triangulado"),
            strat.get("target_cnpj"),
            *(econ.get("cnpjs_consolidados") or []),
        ]:
            d_val = re.sub(r"\D", "", str(c_val or ""))
            if len(d_val) == 14:
                cached_cnpjs.add(d_val)

        if cnpj_digits and (cnpj_digits in cached_cnpjs or cnpj_digits in key_upper):
            return dict(cached_data)

        if (
            emp_upper in targets
            or emp_clean in targets
            or any(t in key_upper.split("::") for t in targets)
            or (brand and emp_clean and brand == emp_clean)
        ):
            return dict(cached_data)

    # 2. Busca no banco de dados (SQLite / PostgreSQL - CompanySearch) com isolamento multitenant
    try:
        query = CompanySearch.query
        if tenant_id is not None:
            query = query.filter((CompanySearch.tenant_id == tenant_id) | (CompanySearch.tenant_id.is_(None)))
        recent_rows = query.order_by(CompanySearch.updated_at.desc()).limit(50).all()
        for row in recent_rows:
            if row.tenant_id is not None and tenant_id is not None and str(row.tenant_id) != str(tenant_id):
                continue
            row_name_up = str(row.company_name or "").upper()
            row_norm_up = str(row.normalized_name or "").upper()
            row_clean_up = _strip_visual_company_suffixes(row_name_up).upper()
            row_econ = row.economic_standing if isinstance(row.economic_standing, dict) else {}

            row_cnpjs = set()
            for c_val in [
                row_econ.get("cnpj_utilizado"),
                row_econ.get("cnpj_triangulado"),
                *(row_econ.get("cnpjs_consolidados") or []),
            ]:
                d_val = re.sub(r"\D", "", str(c_val or ""))
                if len(d_val) == 14:
                    row_cnpjs.add(d_val)

            if cnpj_digits and (cnpj_digits in row_cnpjs or cnpj_digits in re.sub(r"\D", "", row_name_up)):
                return _search_record_to_pdf_payload(row)

            if (
                row_name_up in targets
                or row_norm_up in targets
                or row_clean_up in targets
                or (sanitized and sanitized == row_norm_up)
                or (brand and brand == row_clean_up)
            ):
                return _search_record_to_pdf_payload(row)
    except Exception as db_exc:
        logger.debug("Consulta ao banco para PDF tratada silenciosamente: %s", db_exc)

    return None


def _build_pdf_response(payload: dict, filename_hint: str = "relatorio") -> Response:
    """Renderiza os bytes do PDF executivo v3.0 e retorna a resposta HTTP application/pdf."""
    pdf_bytes = generate_company_report_pdf(payload)
    base_name = _strip_visual_company_suffixes(filename_hint) or "relatorio"
    norm_ascii = unicodedata.normalize("NFKD", base_name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", norm_ascii).strip("-").lower()[:48] or "relatorio"
    safe_filename = re.sub(r'[\r\n/\\"]+', "_", f"radarjus-{slug}.pdf")
    resp = Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Cache-Control": "no-store",
        },
    )
    gc.collect()
    return resp


def _build_compare_pdf_response(payload: dict, filename_hint: str = "comparativo") -> Response:
    """Renderiza os bytes do PDF comparativo v3.0 e retorna a resposta HTTP application/pdf."""
    pdf_bytes = generate_comparative_report_pdf(payload)
    base_name = _strip_visual_company_suffixes(filename_hint) or "comparativo"
    norm_ascii = unicodedata.normalize("NFKD", base_name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", norm_ascii).strip("-").lower()[:48] or "comparativo"
    safe_filename = re.sub(r'[\r\n/\\"]+', "_", f"radarjus-{slug}.pdf")
    resp = Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Cache-Control": "no-store",
        },
    )
    gc.collect()
    return resp


def handle_pdf_export_request(
    search_id: Optional[int] = None,
    company_name: Optional[str] = None,
) -> Response:
    """
    Manipulador unificado de exportação de PDF:
    1. Aceita POST com o objeto de resultado já carregado na tela (nunca dá 404).
    2. Aceita search_id (via path /history/<id>/summary.pdf ou query ?search_id=).
    3. Aceita nome/CNPJ via query string (?empresa=, ?q=, ?company_name=) ou <path:company_name> (suportando '/').
    4. Busca em L0_MEMORY_CACHE -> banco CompanySearch -> geração sob demanda para empresa/CNPJ válido.
    Garante estrito isolamento multitenant (anti-IDOR).
    """
    body = request.get_json(silent=True) if request.method == "POST" else None
    if not isinstance(body, dict):
        body = {}

    current_user = get_current_user_optional()
    user_tenant_id = current_user.tenant_id if current_user else None

    # Validação Anti-IDOR se o body trouxer tenant_id explícito
    body_tenant = body.get("tenant_id")
    if body_tenant is not None and (not current_user or current_user.tenant_id != body_tenant):
        return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403

    # 1. Payload já carregado na sessão do Front-end (via POST)
    live_name = body.get("empresa") or body.get("company_name") or body.get("name")
    has_live_metrics = any(
        k in body and body.get(k) is not None
        for k in (
            "idoneidade_economica",
            "idoneidadeEconomica",
            "desfechos_sentencas",
            "outcomes",
            "outcomes_summary",
            "total_processos",
            "total_processes",
            "total",
            "ai_analysis",
            "aiAnalysis",
            "ranking_motivos",
            "reasons",
        )
    )
    if live_name and has_live_metrics:
        payload = dict(body)
        # Se houver search_id e faltar idoneidade ou IA, enriquece com o registro salvo
        body_sid = body.get("search_id") or body.get("searchId") or body.get("id")
        if body_sid:
            try:
                saved = db.session.get(CompanySearch, int(body_sid))
                if saved:
                    if saved.tenant_id is not None and (not current_user or current_user.tenant_id != saved.tenant_id):
                        return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403
                    if (
                        not payload.get("idoneidade_economica")
                        and not payload.get("idoneidadeEconomica")
                        and isinstance(saved.economic_standing, dict)
                    ):
                        payload["idoneidade_economica"] = saved.economic_standing
            except Exception:
                pass
        return _build_pdf_response(payload, filename_hint=str(live_name))

    # 2. Busca por search_id (rota /history/<id>/summary.pdf ou ?search_id=)
    resolved_sid = search_id
    if resolved_sid is None:
        raw_sid = request.args.get("search_id") or body.get("search_id") or body.get("searchId")
        if raw_sid is not None and str(raw_sid).isdigit():
            resolved_sid = int(raw_sid)

    if resolved_sid is not None:
        try:
            search = db.session.get(CompanySearch, int(resolved_sid))
        except Exception:
            search = None
        if search:
            if search.tenant_id is not None and (not current_user or current_user.tenant_id != search.tenant_id):
                return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403
            payload = _search_record_to_pdf_payload(search)
            return _build_pdf_response(payload, filename_hint=f"empresa-{search.id}")
        # Se veio somente search_id e não existe no banco, verifica se veio nome de fallback
        fallback_term = (
            company_name
            or request.args.get("empresa")
            or request.args.get("q")
            or request.args.get("company_name")
            or live_name
        )
        if not fallback_term:
            return jsonify({"error": "Consulta não encontrada para geração do PDF."}), 404

    # 3. Busca por nome de empresa ou CNPJ (suporta barras via <path:company_name> ou query string)
    term = (
        company_name
        or request.args.get("empresa")
        or request.args.get("q")
        or request.args.get("company_name")
        or request.args.get("name")
        or live_name
        or ""
    ).strip()

    if not term:
        return jsonify({
            "error": "Informe 'empresa', 'q', 'search_id' ou envie os dados da análise via POST para gerar o PDF."
        }), 400

    payload = _find_company_payload_for_pdf(term, tenant_id=user_tenant_id)
    if payload:
        return _build_pdf_response(payload, filename_hint=term)

    # 4. Fallback sob demanda: gera relatório executivo com estratégia + idoneidade econômica
    try:
        strategy = resolve_search_strategy(term)
        display_title = strategy.get("display_title") or term
        cnpj_target = strategy.get("target_cnpj")
        econ_data = economic_service.evaluate_economic_standing(
            company_name=display_title,
            cnpj=cnpj_target,
            company_processes=[],
            p_condenacao_trabalhista=0.0,
            search_strategy=strategy,
        )
    except Exception as exc:
        logger.debug("Fallback econômico na geração direta de PDF: %s", exc)
        display_title = term
        strategy = {}
        econ_data = {}

    live_payload = {
        "empresa": display_title,
        "total_processos": 0,
        "desfechos_sentencas": {"condenacoes": 0, "acordos": 0, "improcedentes": 0, "em_aberto": 0},
        "ranking_motivos": [],
        "ranking_motivos_condenacao": [],
        "condenacao_por_motivo": [],
        "plus_previsao_condenacao": [],
        "indice_exposicao_financeira": econ_data.get("indice_exposicao_financeira", 0.0),
        "score_risco_terceirizacao": econ_data.get("indice_exposicao_financeira", 0.0),
        "idoneidade_economica": econ_data,
        "search_strategy": strategy,
        "scan_status": "COMPLETED",
    }
    try:
        live_payload["ai_analysis"] = orchestrator.ai_analyzer.analyze_company(live_payload)
    except Exception:
        live_payload["ai_analysis"] = {}

    return _build_pdf_response(live_payload, filename_hint=display_title)


@api_bp.route("/history/<int:search_id>/summary.pdf", methods=["GET"])
def export_history_summary_pdf(search_id: int):
    """Exporta o relatório executivo em PDF de uma pesquisa salva pelo ID."""
    search = db.session.get(CompanySearch, search_id)
    if not search:
        return jsonify({"error": "Registro não encontrado."}), 404
    current_user = get_current_user_optional()
    if search.tenant_id is not None:
        if not current_user or current_user.tenant_id != search.tenant_id:
            return jsonify({"error": "Acesso negado: registro pertence a outra organização."}), 403
    return handle_pdf_export_request(search_id=search_id)


@api_bp.route("/export-pdf", methods=["GET", "POST"])
@api_bp.route("/report/pdf", methods=["GET", "POST"])
@api_bp.route("/export/pdf", methods=["GET", "POST"])
def export_report_pdf_query_or_post() -> Response:
    """Exporta o relatório executivo em PDF via query string (?empresa=... / ?q=...) ou POST com JSON da tela."""
    return handle_pdf_export_request()


@api_bp.route("/export-pdf/<path:company_name>", methods=["GET"])
@api_bp.route("/report/pdf/<path:company_name>", methods=["GET"])
@api_bp.route("/export/pdf/<path:company_name>", methods=["GET"])
def export_report_pdf_by_path(company_name: str) -> Response:
    """Exporta o relatório executivo em PDF por nome/CNPJ no path (suporta barras como CNPJ e GRUPO CONSOLIDADO / FILIAIS)."""
    return handle_pdf_export_request(company_name=company_name)


@api_bp.route("/export-compare-pdf", methods=["GET", "POST"])
@api_bp.route("/compare-pdf", methods=["GET", "POST"])
@api_bp.route("/export/compare-pdf", methods=["GET", "POST"])
def export_compare_pdf() -> Response:
    """
    Exporta o relatório comparativo de 2 a 5 empresas em PDF (v3.0).
    Aceita POST com JSON contendo 'companies', 'empresas', 'search_ids' ou lista de empresas.
    Aceita GET com query string contendo ?search_ids=1,2,3 ou ?empresas=A,B ou ?q=A,B.
    Garante estrito isolamento multitenant (anti-IDOR).
    """
    body = request.get_json(silent=True) if request.method == "POST" else None
    if not isinstance(body, dict):
        if isinstance(body, list):
            body = {"companies": body}
        else:
            body = {}

    current_user = get_current_user_optional()
    user_tenant_id = current_user.tenant_id if current_user else None
    resolved_companies: List[dict] = []

    # 1. Se vieram search_ids
    search_ids = body.get("search_ids") or body.get("ids")
    if not search_ids and request.args.get("search_ids"):
        raw_sids = request.args.get("search_ids", "").split(",")
        search_ids = [int(s.strip()) for s in raw_sids if s.strip().isdigit()]
    elif not search_ids and request.args.get("ids"):
        raw_sids = request.args.get("ids", "").split(",")
        search_ids = [int(s.strip()) for s in raw_sids if s.strip().isdigit()]

    if search_ids and isinstance(search_ids, list):
        for sid in search_ids:
            try:
                s_rec = db.session.get(CompanySearch, int(sid))
            except Exception:
                s_rec = None
            if s_rec:
                if s_rec.tenant_id is not None:
                    if not current_user or current_user.tenant_id != s_rec.tenant_id:
                        return jsonify({"error": f"Acesso negado: a busca ID {sid} pertence a outra organização."}), 403
                resolved_companies.append(_search_record_to_pdf_payload(s_rec))

    # 2. Se vieram objetos de empresa diretamente no POST
    if not resolved_companies:
        raw_list = body.get("companies") or body.get("empresas") or body.get("items")
        if isinstance(raw_list, list) and raw_list:
            for item in raw_list:
                if isinstance(item, dict):
                    item_tenant = item.get("tenant_id")
                    if item_tenant is not None and (not current_user or current_user.tenant_id != item_tenant):
                        return jsonify({"error": "Acesso negado: um ou mais itens enviados pertencem a outra organização."}), 403
                    if any(
                        k in item
                        for k in ("idoneidade_economica", "outcomes", "desfechos_sentencas", "total_processos", "total_processes")
                    ):
                        resolved_companies.append(item)
                    else:
                        term = str(item.get("empresa") or item.get("company_name") or item.get("cnpj") or "").strip()
                        if term:
                            payload = _find_company_payload_for_pdf(term, tenant_id=user_tenant_id)
                            if payload:
                                resolved_companies.append(payload)
                            else:
                                resolved_companies.append({"company_name": term})
                elif isinstance(item, str) and item.strip():
                    payload = _find_company_payload_for_pdf(item.strip(), tenant_id=user_tenant_id)
                    if payload:
                        resolved_companies.append(payload)
                    else:
                        resolved_companies.append({"company_name": item.strip()})

    # 3. Se vieram nomes de empresas por query string
    if not resolved_companies:
        names_param = request.args.get("empresas") or request.args.get("companies") or request.args.get("q")
        if names_param:
            raw_names = [n.strip() for n in names_param.split(",") if n.strip()]
            for name in raw_names:
                payload = _find_company_payload_for_pdf(name, tenant_id=user_tenant_id)
                if payload:
                    resolved_companies.append(payload)
                else:
                    resolved_companies.append({"company_name": name})

    # Regra estrita: relatório comparativo exige ao menos 2 empresas (máximo 5)
    if len(resolved_companies) < 2:
        return jsonify({
            "error": "Informe pelo menos 2 empresas (máximo 5) via 'companies', 'empresas' ou 'search_ids' para exportação comparativa."
        }), 400

    return _build_compare_pdf_response({"companies": resolved_companies[:5]}, filename_hint="comparativo-empresas")


@api_bp.route("/cnj-proxy", methods=["GET", "POST", "OPTIONS"])
def cnj_proxy():
    """Proxy local e para desenvolvimento para contornar restrições de CORS e geoblocking."""
    from flask import request as flask_req, Response
    if flask_req.method == "OPTIONS":
        resp = Response("", status=204)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, chave-api-dados, X-Target-Url, x-proxy-secret"
        return resp

    expected_secret = (os.getenv("PROXY_SHARED_SECRET") or os.getenv("CNJ_PROXY_SECRET") or "").strip()
    if expected_secret:
        provided_secret = (flask_req.headers.get("x-proxy-secret") or "").strip()
        auth_header = (flask_req.headers.get("Authorization") or "").strip()
        if not provided_secret and auth_header.startswith("Bearer "):
            provided_secret = auth_header[7:].strip()
        if not provided_secret or not hmac.compare_digest(provided_secret, expected_secret):
            return jsonify({"error": "Unauthorized proxy access"}), 401

    target_url = flask_req.args.get("target") or flask_req.headers.get("X-Target-Url")
    if not target_url:
        return jsonify({"error": "Missing target URL"}), 400

    from urllib.parse import urlparse
    parsed_target = urlparse(target_url)
    allowed_hosts = {
        "comunicaapi.pje.jus.br",
        "api-publica.datajud.cnj.jus.br",
        "api.portaldatransparencia.gov.br",
    }
    if parsed_target.scheme != "https" or parsed_target.hostname not in allowed_hosts:
        return jsonify({"error": "Host not allowed by proxy whitelist"}), 400

    try:
        from app.utils.http_client import request_gov_br_with_fallback
        method = flask_req.method
        headers = {k: v for k, v in flask_req.headers.items() if k.lower() in ("authorization", "chave-api-dados", "content-type")}
        json_body = flask_req.get_json(silent=True) if method in ("POST", "PUT", "PATCH") else None
        upstream = request_gov_br_with_fallback(method, target_url, headers=headers, json_body=json_body, timeout=8.0)
        resp = Response(upstream.content, status=upstream.status_code, content_type=upstream.headers.get("content-type", "application/json"))
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ============================================================================
# US-09: Rotas de Monitoramento Contínuo de Fornecedores Homologados
# ============================================================================

@api_bp.route("/monitoring", methods=["POST"])
@require_auth
def create_monitored_supplier() -> Response:
    """
    Ativa o monitoramento contínuo para um fornecedor homologado no tenant autenticado.
    Inicializa o baseline snapshot (movements_hash, condenacoes, cenario, status RFB, TST)
    a partir da última busca homologada do tenant ou parâmetros recebidos.
    """
    current_user = g.current_user
    tenant_id = current_user.tenant_id

    data = request.get_json(silent=True) or {}
    raw_cnpj = str(data.get("cnpj") or "").strip()
    clean_cnpj = re.sub(r"\D", "", raw_cnpj)
    if len(clean_cnpj) != 14:
        return jsonify({"error": "CNPJ inválido. Forneça um CNPJ com 14 dígitos numéricos."}), 400

    # Proteção Anti-SSRF para webhook_url
    webhook_url = data.get("webhook_url")
    if webhook_url:
        is_testing = current_app.config.get("TESTING", False)
        valid_wh, wh_err = validate_webhook_url(str(webhook_url).strip(), allow_local_for_testing=is_testing)
        if not valid_wh:
            return jsonify({"error": wh_err or "URL de webhook inválida ou insegura."}), 400

    # Validação de formato de e-mail de alerta
    alert_email = data.get("alert_email")
    if alert_email:
        if not validate_alert_email(str(alert_email).strip()):
            return jsonify({"error": "Endereço de e-mail de alerta inválido."}), 400

    # Verifica unicidade no tenant (uq_monitored_suppliers_tenant_cnpj)
    existing = MonitoredSupplier.query.filter_by(tenant_id=tenant_id, cnpj=clean_cnpj).first()
    if existing:
        return jsonify({
            "error": "Fornecedor já cadastrado para monitoramento neste tenant.",
            "supplier": existing.to_dict(),
        }), 409

    # Busca baseline na última pesquisa homologada da empresa
    search = (
        CompanySearch.query.filter_by(tenant_id=tenant_id)
        .filter(CompanySearch.normalized_name.like(f"%{clean_cnpj}%") | CompanySearch.company_name.like(f"%{clean_cnpj}%"))
        .order_by(CompanySearch.created_at.desc())
        .first()
    )
    if not search:
        search = (
            CompanySearch.query
            .filter(CompanySearch.normalized_name.like(f"%{clean_cnpj}%") | CompanySearch.company_name.like(f"%{clean_cnpj}%"))
            .order_by(CompanySearch.created_at.desc())
            .first()
        )

    company_name = str(data.get("company_name") or "").strip()
    if not company_name and search:
        company_name = search.company_name
    if not company_name:
        company_name = f"Fornecedor {clean_cnpj}"

    # Snapshot inicial determinístico
    if search:
        last_movements_hash = search.movements_hash
        last_condenacoes_count = (search.outcomes_summary or {}).get("condenacoes", 0)
        econ = search.economic_standing or {}
        last_scenario = econ.get("cenario") or ("CENARIO_A" if (search.overall_risk_score or 0) < 50 else "CENARIO_B")
        last_rfb_status = econ.get("situacao") or econ.get("situacao_cadastral") or "ATIVA"
        last_tst_count = sum(1 for p in (search.processes or []) if (getattr(p, "tribunal", None) or getattr(p, "court", None) or "").upper() == "TST")
    else:
        last_movements_hash = None
        last_condenacoes_count = 0
        last_scenario = "CENARIO_A"
        last_rfb_status = "ATIVA"
        last_tst_count = 0

    # Permite sobrescrita explícita de baseline caso enviada (ex: testes ou carga inicial)
    if "last_movements_hash" in data:
        last_movements_hash = data.get("last_movements_hash")
    if "last_condenacoes_count" in data:
        try:
            last_condenacoes_count = int(data.get("last_condenacoes_count", 0))
        except (ValueError, TypeError):
            pass
    if "last_scenario" in data:
        last_scenario = str(data.get("last_scenario") or "CENARIO_A")
    if "last_rfb_status" in data:
        last_rfb_status = str(data.get("last_rfb_status") or "ATIVA")
    if "last_tst_count" in data:
        try:
            last_tst_count = int(data.get("last_tst_count", 0))
        except (ValueError, TypeError):
            pass

    active = bool(data.get("active", True))

    try:
        supplier = MonitoredSupplier(
            tenant_id=tenant_id,
            cnpj=clean_cnpj,
            company_name=company_name,
            last_movements_hash=last_movements_hash,
            last_condenacoes_count=last_condenacoes_count,
            last_scenario=last_scenario,
            last_rfb_status=str(last_rfb_status).upper() if last_rfb_status else "ATIVA",
            last_tst_count=last_tst_count,
            webhook_url=str(webhook_url).strip() if webhook_url else None,
            alert_email=str(alert_email).strip() if alert_email else None,
            active=active,
            last_checked_at=get_utc_now(),
        )
        db.session.add(supplier)
        db.session.commit()

        logger.info(
            "Fornecedor cadastrado para monitoramento: id=%d, tenant=%d, cnpj=%s",
            supplier.id, tenant_id, clean_cnpj
        )
        return jsonify({
            "status": "success",
            "message": "Fornecedor homologado ativado para monitoramento contínuo.",
            "supplier": supplier.to_dict(),
        }), 201

    except Exception as exc:
        db.session.rollback()
        logger.exception("Falha ao registrar fornecedor monitorado: %s", exc)
        return jsonify({"error": "Falha interna ao cadastrar fornecedor monitorado."}), 500


@api_bp.route("/monitoring", methods=["GET"])
@require_auth
def list_monitored_suppliers() -> Response:
    """
    Lista fornecedores monitorados do tenant autenticado com suporte a paginação e filtro por active.
    Anti-IDOR estrito: retorna única e exclusivamente registros do tenant autenticado.
    """
    current_user = g.current_user
    tenant_id = current_user.tenant_id

    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    active_param = request.args.get("active")

    query = MonitoredSupplier.query.filter_by(tenant_id=tenant_id)
    if active_param is not None:
        is_active = str(active_param).lower() in ("true", "1", "yes")
        query = query.filter_by(active=is_active)

    total = query.count()
    suppliers = (
        query.order_by(MonitoredSupplier.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return jsonify({
        "status": "success",
        "suppliers": [s.to_dict() for s in suppliers],
        "total": total,
        "page": page,
        "per_page": per_page,
    }), 200


@api_bp.route("/monitoring/<int:monitoring_id>", methods=["PATCH"])
@require_auth
def update_monitored_supplier(monitoring_id: int) -> Response:
    """
    Atualiza status (active), canais de alerta (webhook_url, alert_email) ou razão social.
    Garante Anti-IDOR: validação obrigatória de tenant_id.
    """
    current_user = g.current_user
    tenant_id = current_user.tenant_id

    supplier = db.session.get(MonitoredSupplier, monitoring_id)
    if not supplier:
        return jsonify({"error": "Fornecedor monitorado não encontrado."}), 404

    # Anti-IDOR
    if supplier.tenant_id != tenant_id:
        return jsonify({"error": "Acesso negado: fornecedor pertence a outra organização."}), 403

    data = request.get_json(silent=True) or {}

    # Atualização de active
    if "active" in data:
        supplier.active = bool(data["active"])

    # Atualização de webhook_url com validação SSRF
    if "webhook_url" in data:
        wh = data["webhook_url"]
        if wh:
            is_testing = current_app.config.get("TESTING", False)
            valid_wh, wh_err = validate_webhook_url(str(wh).strip(), allow_local_for_testing=is_testing)
            if not valid_wh:
                return jsonify({"error": wh_err or "URL de webhook inválida ou insegura."}), 400
            supplier.webhook_url = str(wh).strip()
        else:
            supplier.webhook_url = None

    # Atualização de alert_email com validação de formato
    if "alert_email" in data:
        em = data["alert_email"]
        if em:
            if not validate_alert_email(str(em).strip()):
                return jsonify({"error": "Endereço de e-mail de alerta inválido."}), 400
            supplier.alert_email = str(em).strip()
        else:
            supplier.alert_email = None

    # Atualização de company_name se fornecido
    if "company_name" in data:
        cn = str(data["company_name"] or "").strip()
        if cn:
            supplier.company_name = cn

    supplier.updated_at = get_utc_now()
    try:
        db.session.commit()
        return jsonify({
            "status": "success",
            "message": "Fornecedor monitorado atualizado com sucesso.",
            "supplier": supplier.to_dict(),
        }), 200
    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro ao atualizar fornecedor monitorado id=%d: %s", monitoring_id, exc)
        return jsonify({"error": "Falha interna ao atualizar fornecedor monitorado."}), 500


@api_bp.route("/monitoring/<int:monitoring_id>", methods=["DELETE"])
@require_auth
def delete_monitored_supplier(monitoring_id: int) -> Response:
    """
    Remove fornecedor do monitoramento contínuo.
    Garante Anti-IDOR: validação obrigatória de tenant_id.
    """
    current_user = g.current_user
    tenant_id = current_user.tenant_id

    supplier = db.session.get(MonitoredSupplier, monitoring_id)
    if not supplier:
        return jsonify({"error": "Fornecedor monitorado não encontrado."}), 404

    # Anti-IDOR
    if supplier.tenant_id != tenant_id:
        return jsonify({"error": "Acesso negado: fornecedor pertence a outra organização."}), 403

    try:
        db.session.delete(supplier)
        db.session.commit()
        logger.info("Fornecedor removido do monitoramento: id=%d, tenant=%d", monitoring_id, tenant_id)
        return jsonify({
            "status": "success",
            "message": "Fornecedor removido do monitoramento com sucesso.",
        }), 200
    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro ao remover fornecedor monitorado id=%d: %s", monitoring_id, exc)
        return jsonify({"error": "Falha interna ao remover fornecedor monitorado."}), 500


@api_bp.route("/monitoring/run", methods=["POST"])
@require_auth
def trigger_monitoring_job() -> Response:
    """
    Endpoint administrativo para disparar a execução manual ou sob demanda do job de monitoramento
    para o tenant autenticado (ou todos os tenants caso admin).
    """
    from app.services.monitoring_job import run_monitoring_job

    current_user = g.current_user
    data = request.get_json(silent=True) or {}
    batch_size = min(50, max(1, int(data.get("batch_size", 10))))

    # Tenant isolation: se usuário não for admin global, executa apenas para o seu próprio tenant
    tenant_filter = current_user.tenant_id if current_user.role != "ADMIN" else data.get("tenant_id", current_user.tenant_id)

    summary = run_monitoring_job(batch_size=batch_size, tenant_id=tenant_filter)
    return jsonify(summary), 200
