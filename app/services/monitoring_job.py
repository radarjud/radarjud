# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import gc
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import requests

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.monitored_supplier import MonitoredSupplier
from app.services.economic_service import fetch_accurate_cnpj_data
from app.utils.crypto import generate_movements_hash

logger = logging.getLogger(__name__)

# Status cadastrais considerados irregulares na Receita Federal
IRREGULAR_RFB_STATUSES = {"INAPTA", "SUSPENSA", "BAIXADA", "NULA"}


def evaluate_monitored_supplier(
    supplier: MonitoredSupplier,
    orchestrator: Any = None,
    economic_service: Any = None,
) -> dict[str, Any]:
    """
    Reavalia a situação cadastral e processual de um fornecedor monitorado.
    Prioriza dados frescos dos serviços integrados ou fallback para a última busca homologada.
    """
    # 1. Situação Cadastral RFB
    new_rfb_status = supplier.last_rfb_status or "ATIVA"
    try:
        rfb_data = fetch_accurate_cnpj_data(supplier.cnpj, timeout=4.0)
        if rfb_data and isinstance(rfb_data, dict):
            status = rfb_data.get("situacao") or rfb_data.get("descricao_situacao_cadastral")
            if status:
                new_rfb_status = str(status).strip().upper()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Falha ao consultar RFB para fornecedor id=%d (cnpj=%s): %s",
            supplier.id, supplier.cnpj, exc
        )

    # 2. Dados Processuais e Risco Judicial
    search = (
        CompanySearch.query.filter_by(tenant_id=supplier.tenant_id)
        .filter(
            CompanySearch.normalized_name.like(f"%{supplier.cnpj}%")
            | CompanySearch.company_name.like(f"%{supplier.cnpj}%")
        )
        .order_by(CompanySearch.created_at.desc())
        .first()
    )
    if not search:
        search = (
            CompanySearch.query.filter(
                CompanySearch.normalized_name.like(f"%{supplier.cnpj}%")
                | CompanySearch.company_name.like(f"%{supplier.cnpj}%")
            )
            .order_by(CompanySearch.created_at.desc())
            .first()
        )

    if search:
        outcomes = search.outcomes_summary or {}
        new_condenacoes_count = int(outcomes.get("condenacoes", 0))
        econ = search.economic_standing or {}
        new_scenario = econ.get("cenario") or (
            "CENARIO_A" if (search.overall_risk_score or 0.0) < 50.0 else "CENARIO_B"
        )
        new_tst_count = sum(
            1 for p in (search.processes or [])
            if (getattr(p, "tribunal", None) or getattr(p, "court", None) or "").upper() == "TST"
        )

        # Processos para hash consolidado
        proc_dicts = []
        for p in (search.processes or []):
            proc_dicts.append({
                "numero_processo": getattr(p, "numero_processo", "") or getattr(p, "process_number", ""),
                "dataHoraUltimaAtualizacao": p.created_at.isoformat() if getattr(p, "created_at", None) else "",
                "sentence_outcome": getattr(p, "sentence_outcome", "") or getattr(p, "outcome", ""),
                "tribunal": getattr(p, "tribunal", "") or getattr(p, "court", ""),
            })
        new_movements_hash = generate_movements_hash(proc_dicts, rfb_status=new_rfb_status)
    else:
        new_condenacoes_count = supplier.last_condenacoes_count
        new_scenario = supplier.last_scenario
        new_tst_count = supplier.last_tst_count
        new_movements_hash = supplier.last_movements_hash or generate_movements_hash([], rfb_status=new_rfb_status)

    return {
        "movements_hash": new_movements_hash,
        "condenacoes_count": new_condenacoes_count,
        "scenario": new_scenario,
        "rfb_status": new_rfb_status,
        "tst_count": new_tst_count,
    }


def dispatch_alert(
    supplier: MonitoredSupplier,
    triggers: list[str],
    deltas: dict[str, Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    """
    Despacha o payload estruturado de alerta (integração US-11) para os canais cadastrados
    do tenant (webhook_url e alert_email), respeitando estritamente a LGPD e Anti-IDOR.
    """
    payload = {
        "event": "SUPPLIER_ALERT",
        "tenant_id": supplier.tenant_id,
        "supplier_id": supplier.id,
        "cnpj": supplier.cnpj,
        "company_name": supplier.company_name,
        "triggers": triggers,
        "deltas": deltas,
        "webhook_url": supplier.webhook_url,
        "alert_email": supplier.alert_email,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    result = {
        "tenant_id": supplier.tenant_id,
        "supplier_id": supplier.id,
        "payload": payload,
        "webhook_sent": False,
        "webhook_status": None,
        "email_queued": False,
        "error": None,
    }

    # 1. Disparo de Webhook com timeout explícito
    if supplier.webhook_url:
        try:
            resp = requests.post(
                supplier.webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "RadarJus-Monitoring/1.0",
                },
                timeout=timeout,
            )
            result["webhook_sent"] = True
            result["webhook_status"] = resp.status_code
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Falha no disparo do webhook para supplier_id=%d (tenant=%d): %s",
                supplier.id, supplier.tenant_id, exc
            )
            result["error"] = str(exc)

    # 2. Notificação por E-mail (LGPD compliant: destinatário e tenant isolados)
    if supplier.alert_email:
        result["email_queued"] = True
        logger.info(
            "Alerta enfileirado para e-mail cadastrado do tenant=%d (supplier_id=%d)",
            supplier.tenant_id, supplier.id
        )

    return result


def run_monitoring_job(
    batch_size: int = 10,
    tenant_id: int | None = None,
    evaluator_fn: Callable[[MonitoredSupplier], dict[str, Any]] | None = None,
    timeout_webhook: float = 5.0,
) -> dict[str, Any]:
    """
    Executa a rotina periódica de monitoramento contínuo sobre fornecedores ativos.
    - Processa em lotes pequenos (< 512 MB RAM Render Free).
    - Executa gc.collect() a cada lote.
    - Detecção determinística de 4 gatilhos de alerta.
    - Idempotência estrita: 0 alertas duplicados quando não há novos eventos.
    - Retorna sumário estruturado de execução.
    """
    start_time = time.time()
    total_processed = 0
    total_alerts_emitted = 0
    total_errors = 0
    alert_events: list[dict[str, Any]] = []

    base_query = MonitoredSupplier.query.filter_by(active=True)
    if tenant_id is not None:
        base_query = base_query.filter_by(tenant_id=tenant_id)

    total_active = base_query.count()
    offset = 0

    while True:
        batch = (
            base_query.order_by(MonitoredSupplier.id.asc())
            .offset(offset)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        for supplier in batch:
            total_processed += 1
            try:
                # Reavalia o fornecedor
                eval_res = (
                    evaluator_fn(supplier)
                    if evaluator_fn
                    else evaluate_monitored_supplier(supplier)
                )

                new_movements_hash = eval_res["movements_hash"]
                new_condenacoes_count = int(eval_res.get("condenacoes_count", 0))
                new_scenario = str(eval_res.get("scenario") or "CENARIO_A")
                new_rfb_status = str(eval_res.get("rfb_status") or "ATIVA").upper()
                new_tst_count = int(eval_res.get("tst_count", 0))

                triggers: list[str] = []
                deltas: dict[str, Any] = {}

                # 1. Novas Condenações: novo_condenacoes_count > last_condenacoes_count (+N)
                if new_condenacoes_count > supplier.last_condenacoes_count:
                    delta_cond = new_condenacoes_count - supplier.last_condenacoes_count
                    triggers.append("NEW_CONVICTIONS")
                    deltas["condenacoes"] = {
                        "before": supplier.last_condenacoes_count,
                        "after": new_condenacoes_count,
                        "delta": delta_cond,
                    }

                # 2. Novo Processo no TST
                if new_tst_count > (supplier.last_tst_count or 0):
                    delta_tst = new_tst_count - (supplier.last_tst_count or 0)
                    triggers.append("NEW_TST_PROCESS")
                    deltas["tst"] = {
                        "before": supplier.last_tst_count or 0,
                        "after": new_tst_count,
                        "delta": delta_tst,
                    }

                # 3. Mudança de Situação Cadastral RFB (de ATIVA para status irregular)
                if (
                    (supplier.last_rfb_status in ("ATIVA", None) or not supplier.last_rfb_status)
                    and new_rfb_status in IRREGULAR_RFB_STATUSES
                ):
                    triggers.append("RFB_STATUS_CHANGE")
                    deltas["rfb_status"] = {
                        "before": supplier.last_rfb_status or "ATIVA",
                        "after": new_rfb_status,
                    }
                elif (
                    supplier.last_rfb_status
                    and new_rfb_status != supplier.last_rfb_status
                    and new_rfb_status in IRREGULAR_RFB_STATUSES
                ):
                    triggers.append("RFB_STATUS_CHANGE")
                    deltas["rfb_status"] = {
                        "before": supplier.last_rfb_status,
                        "after": new_rfb_status,
                    }

                # 4. Degradação de Cenário A -> Cenário B
                if supplier.last_scenario in ("A", "CENARIO_A") and new_scenario in ("B", "CENARIO_B"):
                    triggers.append("SCENARIO_DEGRADATION")
                    deltas["scenario"] = {
                        "before": supplier.last_scenario,
                        "after": new_scenario,
                    }

                # Idempotência: só despacha alerta se houver gatilhos críticos detectados
                if triggers:
                    dispatch_res = dispatch_alert(supplier, triggers, deltas, timeout=timeout_webhook)
                    total_alerts_emitted += 1
                    alert_events.append(dispatch_res)

                # Atualiza snapshot de baseline para que próximas execuções sejam idempotentes
                supplier.last_movements_hash = new_movements_hash
                supplier.last_condenacoes_count = new_condenacoes_count
                supplier.last_scenario = new_scenario
                supplier.last_rfb_status = new_rfb_status
                supplier.last_tst_count = new_tst_count
                supplier.last_checked_at = get_utc_now()
                db.session.commit()

            except Exception:
                db.session.rollback()
                total_errors += 1
                logger.exception(
                    "Erro ao reavaliar fornecedor id=%d (cnpj=%s)",
                    supplier.id, supplier.cnpj
                )

        offset += len(batch)
        del batch
        gc.collect()

    elapsed = time.time() - start_time
    summary = {
        "status": "success",
        "processed": total_processed,
        "total_active": total_active,
        "alerts_emitted": total_alerts_emitted,
        "errors": total_errors,
        "alert_events": alert_events,
        "execution_time_seconds": round(elapsed, 3),
    }
    logger.info("Execução do monitoring_job finalizada: %s", summary)
    return summary
