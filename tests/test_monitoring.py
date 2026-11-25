# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

from unittest.mock import MagicMock, patch

import requests

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.monitored_supplier import MonitoredSupplier
from app.models.process_record import ProcessRecord
from app.models.user import User
from app.services.monitoring_job import (
    run_monitoring_job,
)


def _create_tenant_and_user(client, name="Compliance Alpha", email="compliance@alpha.com"):
    """Helper para criar tenant e usuário autenticado com Bearer Token."""
    resp = client.post("/api/v1/auth/register", json={
        "name": name,
        "email": email,
        "password": "SenhaForteSegura123!",  # gitleaks:allow
        "tenant_name": f"Tenant {name}",
        "role": User.ROLE_ANALISTA,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    return {
        "token": data["access_token"],
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "tenant_id": data["tenant"]["id"],
        "user_id": data["user"]["id"],
    }


def test_create_monitored_supplier_crud_and_baseline(client, app):
    """
    Valida a criação do fornecedor monitorado com captura automática do baseline snapshot
    a partir de pesquisa prévia no tenant.
    """
    user_info = _create_tenant_and_user(client, "Empresa A", "user_a@empresa.com")
    tenant_id = user_info["tenant_id"]
    headers = user_info["headers"]
    cnpj = "12345678000195"

    with app.app_context():
        # Cria pesquisa prévia para a empresa no tenant
        search = CompanySearch(
            tenant_id=tenant_id,
            company_name="Fornecedor Alpha Serviços Ltda",
            normalized_name=f"FORNECEDOR ALPHA {cnpj}",
            total_processes=5,
            movements_hash="hash_baseline_original_123",
            outcomes_summary={"condenacoes": 2, "acordos": 1},
            overall_risk_score=25.0,
            economic_standing={"cenario": "CENARIO_A", "situacao": "ATIVA"},
        )
        db.session.add(search)
        db.session.commit()

        proc_tst = ProcessRecord(
            company_search_id=search.id,
            numero_processo="0001234-55.2024.5.00.0000",
            tribunal="TST",
            sentence_outcome="Condenação",
            created_at=get_utc_now(),
        )
        db.session.add(proc_tst)
        db.session.commit()

    # Dispara ativação do monitoramento via API
    resp = client.post("/api/v1/monitoring", headers=headers, json={
        "cnpj": cnpj,
        "webhook_url": "https://webhook.empresa.com/alerts",
        "alert_email": "alerta@empresa.com",
    })
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["status"] == "success"
    supplier = payload["supplier"]
    assert supplier["tenant_id"] == tenant_id
    assert supplier["cnpj"] == cnpj
    assert supplier["last_condenacoes_count"] == 2
    assert supplier["last_scenario"] == "CENARIO_A"
    assert supplier["last_rfb_status"] == "ATIVA"
    assert supplier["last_tst_count"] == 1
    assert supplier["active"] is True


def test_create_monitored_supplier_duplicate_conflict(client):
    """Garante que o mesmo tenant não pode cadastrar o mesmo CNPJ em duplicidade (409 Conflict)."""
    user_info = _create_tenant_and_user(client, "Empresa Dup", "user_dup@empresa.com")
    headers = user_info["headers"]
    cnpj = "98765432000188"

    resp1 = client.post("/api/v1/monitoring", headers=headers, json={"cnpj": cnpj})
    assert resp1.status_code == 201

    resp2 = client.post("/api/v1/monitoring", headers=headers, json={"cnpj": cnpj})
    assert resp2.status_code == 409
    assert "já cadastrado" in resp2.get_json()["error"]


def test_webhook_ssrf_and_email_validation(client):
    """
    Testa rigorosamente a proteção contra SSRF (Server-Side Request Forgery)
    e validação de formato de e-mail na ativação do monitoramento.
    """
    user_info = _create_tenant_and_user(client, "Empresa Sec", "user_sec@empresa.com")
    headers = user_info["headers"]
    cnpj = "11222333000144"

    # 1. Bloqueio de esquema não HTTPS (quando não testing/em produção)
    resp = client.post("/api/v1/monitoring", headers=headers, json={
        "cnpj": cnpj,
        "webhook_url": "ftp://malicious.com/webhook",
    })
    assert resp.status_code == 400

    # 2. Bloqueio de Loopback e Metadata em produção/modo estrito
    from app.utils.sanitizer import validate_webhook_url
    valid_http, msg_http = validate_webhook_url("http://127.0.0.1:8000/hook", allow_local_for_testing=False)
    assert not valid_http
    assert "HTTPS" in msg_http

    valid, msg = validate_webhook_url("https://127.0.0.1:8000/hook", allow_local_for_testing=False)
    assert not valid
    assert "inseguro" in msg or "loopback" in msg

    valid_meta, msg_meta = validate_webhook_url("https://169.254.169.254/latest", allow_local_for_testing=False)
    assert not valid_meta
    assert "inseguro" in msg_meta or "loopback" in msg_meta

    valid_local, msg_local = validate_webhook_url("https://localhost:5000/hook", allow_local_for_testing=False)
    assert not valid_local
    assert "inseguro" in msg_local or "loopback" in msg_local

    # 3. Validação de formato de e-mail
    resp_bad_email = client.post("/api/v1/monitoring", headers=headers, json={
        "cnpj": cnpj,
        "alert_email": "email_invalido_sem_arroba",
    })
    assert resp_bad_email.status_code == 400
    assert "e-mail" in resp_bad_email.get_json()["error"]


def test_anti_idor_supplier_isolation(client):
    """
    Valida Anti-IDOR: Usuário do Tenant A não pode listar, alterar ou deletar
    registros de fornecedores monitorados pertencentes ao Tenant B.
    """
    user_a = _create_tenant_and_user(client, "Tenant A", "user_a@tenant.com")
    user_b = _create_tenant_and_user(client, "Tenant B", "user_b@tenant.com")

    # Tenant A cria fornecedor
    resp_create = client.post("/api/v1/monitoring", headers=user_a["headers"], json={
        "cnpj": "44555666000177",
        "company_name": "Fornecedor Exclusivo A",
    })
    assert resp_create.status_code == 201
    supplier_id_a = resp_create.get_json()["supplier"]["id"]

    # Tenant B lista seus fornecedores: não deve constar o fornecedor do Tenant A
    resp_list_b = client.get("/api/v1/monitoring", headers=user_b["headers"])
    assert resp_list_b.status_code == 200
    b_ids = [s["id"] for s in resp_list_b.get_json()["suppliers"]]
    assert supplier_id_a not in b_ids

    # Tenant B tenta alterar o fornecedor do Tenant A (Anti-IDOR -> 403 Forbidden)
    resp_patch = client.patch(
        f"/api/v1/monitoring/{supplier_id_a}",
        headers=user_b["headers"],
        json={"active": False},
    )
    assert resp_patch.status_code == 403
    assert "outra organização" in resp_patch.get_json()["error"]

    # Tenant B tenta deletar o fornecedor do Tenant A (Anti-IDOR -> 403 Forbidden)
    resp_del = client.delete(
        f"/api/v1/monitoring/{supplier_id_a}",
        headers=user_b["headers"],
    )
    assert resp_del.status_code == 403

    # Tenant A consegue atualizar e deletar seu próprio fornecedor
    resp_patch_a = client.patch(
        f"/api/v1/monitoring/{supplier_id_a}",
        headers=user_a["headers"],
        json={"active": False},
    )
    assert resp_patch_a.status_code == 200
    assert resp_patch_a.get_json()["supplier"]["active"] is False

    resp_del_a = client.delete(
        f"/api/v1/monitoring/{supplier_id_a}",
        headers=user_a["headers"],
    )
    assert resp_del_a.status_code == 200


def test_delta_detection_new_convictions(app, client):
    """
    Critério DoD 1:
    Simular fornecedor monitorado que passa de 2 para 4 condenações (+2 delta):
    Job detecta o delta exato (+2), gera evento de alerta e atualiza baseline persistido.
    """
    user_info = _create_tenant_and_user(client, "Empresa Delta 1", "delta1@empresa.com")
    tenant_id = user_info["tenant_id"]

    with app.app_context():
        supplier = MonitoredSupplier(
            tenant_id=tenant_id,
            cnpj="11111222000133",
            company_name="Fornecedor Delta Teste",
            last_movements_hash="hash_baseline_v1",
            last_condenacoes_count=2,
            last_scenario="CENARIO_A",
            last_rfb_status="ATIVA",
            last_tst_count=0,
            webhook_url="https://webhook.tenanta.com/alert",
            alert_email="compliance@tenanta.com",
            active=True,
        )
        db.session.add(supplier)
        db.session.commit()
        supplier_id = supplier.id

    # Simula avaliador que detecta 4 condenações (+2 de delta)
    def fake_evaluator(sup):
        return {
            "movements_hash": "hash_baseline_v2_com_novas_condenacoes",
            "condenacoes_count": 4,
            "scenario": "CENARIO_A",
            "rfb_status": "ATIVA",
            "tst_count": 0,
        }

    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        with app.app_context():
            summary = run_monitoring_job(tenant_id=tenant_id, evaluator_fn=fake_evaluator)
            assert summary["processed"] == 1
            assert summary["alerts_emitted"] == 1
            assert len(summary["alert_events"]) == 1

            event = summary["alert_events"][0]
            payload = event["payload"]
            assert "NEW_CONVICTIONS" in payload["triggers"]
            assert payload["deltas"]["condenacoes"]["before"] == 2
            assert payload["deltas"]["condenacoes"]["after"] == 4
            assert payload["deltas"]["condenacoes"]["delta"] == 2

            # Verifica atualização do baseline persistido
            updated_supplier = db.session.get(MonitoredSupplier, supplier_id)
            assert updated_supplier.last_condenacoes_count == 4
            assert updated_supplier.last_movements_hash == "hash_baseline_v2_com_novas_condenacoes"


def test_delta_detection_rfb_status_and_scenario_and_tst(app, client):
    """
    Critério DoD 2:
    Simular fornecedor cuja situação RFB muda de 'ATIVA' para 'INAPTA',
    surge novo processo no TST e degradação de Cenário A -> Cenário B.
    """
    user_info = _create_tenant_and_user(client, "Empresa RFB", "rfb@empresa.com")
    tenant_id = user_info["tenant_id"]

    with app.app_context():
        supplier = MonitoredSupplier(
            tenant_id=tenant_id,
            cnpj="55555666000199",
            company_name="Fornecedor Risco Critico",
            last_movements_hash="hash_init",
            last_condenacoes_count=0,
            last_scenario="CENARIO_A",
            last_rfb_status="ATIVA",
            last_tst_count=0,
            webhook_url="https://webhook.tenanta.com/alert",
            active=True,
        )
        db.session.add(supplier)
        db.session.commit()
        supplier_id = supplier.id

    def fake_critical_evaluator(sup):
        return {
            "movements_hash": "hash_critical_updated",
            "condenacoes_count": 1,
            "scenario": "CENARIO_B",
            "rfb_status": "INAPTA",
            "tst_count": 1,
        }

    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        with app.app_context():
            summary = run_monitoring_job(tenant_id=tenant_id, evaluator_fn=fake_critical_evaluator)
            assert summary["processed"] == 1
            assert summary["alerts_emitted"] == 1

            event = summary["alert_events"][0]
            payload = event["payload"]
            assert "RFB_STATUS_CHANGE" in payload["triggers"]
            assert "NEW_TST_PROCESS" in payload["triggers"]
            assert "SCENARIO_DEGRADATION" in payload["triggers"]
            assert "NEW_CONVICTIONS" in payload["triggers"]

            assert payload["deltas"]["rfb_status"]["before"] == "ATIVA"
            assert payload["deltas"]["rfb_status"]["after"] == "INAPTA"
            assert payload["deltas"]["scenario"]["before"] == "CENARIO_A"
            assert payload["deltas"]["scenario"]["after"] == "CENARIO_B"
            assert payload["deltas"]["tst"]["delta"] == 1

            # Baseline persistido no banco
            s_db = db.session.get(MonitoredSupplier, supplier_id)
            assert s_db.last_rfb_status == "INAPTA"
            assert s_db.last_scenario == "CENARIO_B"
            assert s_db.last_tst_count == 1


def test_idempotency_zero_duplicate_alerts(app, client):
    """
    Critério DoD 3:
    Executar o monitoring_job duas vezes consecutivas para o mesmo fornecedor sem mudanças:
    Na 2ª execução nenhum evento de alerta duplicado é emitido (idempotência estrita).
    """
    user_info = _create_tenant_and_user(client, "Empresa Idempotente", "idem@empresa.com")
    tenant_id = user_info["tenant_id"]

    with app.app_context():
        supplier = MonitoredSupplier(
            tenant_id=tenant_id,
            cnpj="77777888000100",
            company_name="Fornecedor Idempotente",
            last_movements_hash="hash_baseline_v1",
            last_condenacoes_count=1,
            last_scenario="CENARIO_A",
            last_rfb_status="ATIVA",
            last_tst_count=0,
            webhook_url="https://webhook.tenanta.com/alert",
            active=True,
        )
        db.session.add(supplier)
        db.session.commit()

    # 1ª execução: gera delta (+1 condenação)
    def eval_run1(sup):
        return {
            "movements_hash": "hash_baseline_v2",
            "condenacoes_count": 2,
            "scenario": "CENARIO_A",
            "rfb_status": "ATIVA",
            "tst_count": 0,
        }

    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200

        with app.app_context():
            # Execução 1: detecta alteração e emite 1 alerta
            res1 = run_monitoring_job(tenant_id=tenant_id, evaluator_fn=eval_run1)
            assert res1["alerts_emitted"] == 1
            assert mock_post.call_count == 1

            # 2ª execução: mesmo estado retornado pelo avaliador (nenhum novo evento)
            def eval_run2(sup):
                return {
                    "movements_hash": "hash_baseline_v2",
                    "condenacoes_count": 2,
                    "scenario": "CENARIO_A",
                    "rfb_status": "ATIVA",
                    "tst_count": 0,
                }

            res2 = run_monitoring_job(tenant_id=tenant_id, evaluator_fn=eval_run2)
            assert res2["alerts_emitted"] == 0
            assert len(res2["alert_events"]) == 0
            # Nenhuma nova chamada HTTP disparada na 2ª execução
            assert mock_post.call_count == 1


def test_multi_tenant_isolation_no_recipient_leakage(app, client):
    """
    Critério DoD 4:
    Configurar Tenant A e Tenant B monitorando o mesmo CNPJ com destinatários distintos:
    Validar que o alerta do Tenant A é enviado exclusivamente aos destinatários do Tenant A,
    sem qualquer vazamento de dados ou destinatários entre tenants.
    """
    user_a = _create_tenant_and_user(client, "Tenant Org A", "admin@tenanta.com")
    user_b = _create_tenant_and_user(client, "Tenant Org B", "admin@tenantb.com")

    cnpj_compartilhado = "33333444000155"

    with app.app_context():
        # Tenant A
        sup_a = MonitoredSupplier(
            tenant_id=user_a["tenant_id"],
            cnpj=cnpj_compartilhado,
            company_name="Empresa Compartilhada A",
            last_movements_hash="hash_init",
            last_condenacoes_count=1,
            last_scenario="CENARIO_A",
            last_rfb_status="ATIVA",
            webhook_url="https://hooks.tenanta.com/alert",
            alert_email="compliance@tenanta.com",
            active=True,
        )
        # Tenant B
        sup_b = MonitoredSupplier(
            tenant_id=user_b["tenant_id"],
            cnpj=cnpj_compartilhado,
            company_name="Empresa Compartilhada B",
            last_movements_hash="hash_init",
            last_condenacoes_count=1,
            last_scenario="CENARIO_A",
            last_rfb_status="ATIVA",
            webhook_url="https://hooks.tenantb.com/alert",
            alert_email="risco@tenantb.com",
            active=True,
        )
        db.session.add_all([sup_a, sup_b])
        db.session.commit()

    # Avaliador com delta (+2 condenações)
    def delta_evaluator(sup):
        return {
            "movements_hash": "hash_updated",
            "condenacoes_count": 3,
            "scenario": "CENARIO_A",
            "rfb_status": "ATIVA",
            "tst_count": 0,
        }

    dispatched_calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        dispatched_calls.append({"url": url, "payload": json})
        mock = MagicMock()
        mock.status_code = 200
        return mock

    with patch("requests.post", side_effect=fake_post), app.app_context():
        summary = run_monitoring_job(evaluator_fn=delta_evaluator)
        assert summary["processed"] == 2
        assert summary["alerts_emitted"] == 2
        assert len(dispatched_calls) == 2

        # Analisa chamada 1 (Tenant A)
        call_a = next(c for c in dispatched_calls if c["url"] == "https://hooks.tenanta.com/alert")
        assert call_a["payload"]["tenant_id"] == user_a["tenant_id"]
        assert call_a["payload"]["alert_email"] == "compliance@tenanta.com"
        assert "tenantb" not in str(call_a["payload"]).lower()

        # Analisa chamada 2 (Tenant B)
        call_b = next(c for c in dispatched_calls if c["url"] == "https://hooks.tenantb.com/alert")
        assert call_b["payload"]["tenant_id"] == user_b["tenant_id"]
        assert call_b["payload"]["alert_email"] == "risco@tenantb.com"
        assert "tenanta" not in str(call_b["payload"]).lower()


def test_batch_processing_and_resilience_on_failure(app, client):
    """
    Critério DoD 5:
    Validar que o job processa múltiplos fornecedores respeitando o batch_size configurado
    e continua executando caso o disparo de um item falhe (resiliência).
    """
    user_info = _create_tenant_and_user(client, "Empresa Batch", "batch@empresa.com")
    tenant_id = user_info["tenant_id"]

    with app.app_context():
        # Cria 6 fornecedores
        suppliers = []
        for i in range(6):
            s = MonitoredSupplier(
                tenant_id=tenant_id,
                cnpj=f"10000000000{i:02d}1",
                company_name=f"Fornecedor Lote {i}",
                last_movements_hash="hash_0",
                last_condenacoes_count=0,
                last_scenario="CENARIO_A",
                last_rfb_status="ATIVA",
                webhook_url=f"https://webhook.batch.com/alert/{i}",
                active=True,
            )
            suppliers.append(s)
        db.session.add_all(suppliers)
        db.session.commit()
        supplier_ids = [s.id for s in suppliers]

    def delta_eval(sup):
        return {
            "movements_hash": "hash_1",
            "condenacoes_count": 1,
            "scenario": "CENARIO_A",
            "rfb_status": "ATIVA",
            "tst_count": 0,
        }

    # Simula falha apenas no envio do webhook para o fornecedor #2
    def fake_post_with_flaky(url, json=None, headers=None, timeout=None):
        if "/alert/2" in url:
            raise requests.RequestException("Conexão recusada pelo endpoint de webhook remoto")
        mock = MagicMock()
        mock.status_code = 200
        return mock

    with patch("requests.post", side_effect=fake_post_with_flaky), app.app_context():
        # Executa com batch_size pequeno (2 itens por lote)
        summary = run_monitoring_job(batch_size=2, tenant_id=tenant_id, evaluator_fn=delta_eval)
        assert summary["processed"] == 6
        assert summary["alerts_emitted"] == 6

        # O fornecedor #2 registrou erro no disparo do webhook, mas o loop não travou
        events = summary["alert_events"]
        failed_ev = next(e for e in events if e["supplier_id"] == supplier_ids[2])
        assert failed_ev["webhook_sent"] is False
        assert "Conexão recusada" in failed_ev["error"]

        successful_evs = [e for e in events if e["webhook_sent"] is True]
        assert len(successful_evs) == 5


def test_trigger_monitoring_job_endpoint(client, app):
    """Valida o disparo manual sob demanda via POST /api/v1/monitoring/run."""
    user = _create_tenant_and_user(client, "Empresa Trigger", "trigger@empresa.com")
    headers = user["headers"]

    # Cria fornecedor ativo
    resp_create = client.post("/api/v1/monitoring", headers=headers, json={
        "cnpj": "99888777000166",
        "company_name": "Fornecedor Trigger Job",
    })
    assert resp_create.status_code == 201

    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        resp_run = client.post("/api/v1/monitoring/run", headers=headers, json={"batch_size": 5})
        assert resp_run.status_code == 200
        data = resp_run.get_json()
        assert data["status"] == "success"
        assert data["processed"] >= 1


def test_monitoring_pagination_and_patch_validation(client):
    """Valida paginação no GET /api/v1/monitoring e validações defensivas no PATCH."""
    user = _create_tenant_and_user(client, "Empresa Pag", "pag@empresa.com")
    headers = user["headers"]

    # Cadastra 3 fornecedores
    created_ids = []
    for i in range(3):
        r = client.post("/api/v1/monitoring", headers=headers, json={
            "cnpj": f"22333444000{i:02d}9",
            "company_name": f"Fornecedor Pag {i}",
            "active": i < 2,  # 2 ativos, 1 inativo
        })
        assert r.status_code == 201
        created_ids.append(r.get_json()["supplier"]["id"])

    # 1. Listagem paginada (per_page=2)
    resp_page = client.get("/api/v1/monitoring?page=1&per_page=2", headers=headers)
    assert resp_page.status_code == 200
    page_data = resp_page.get_json()
    assert len(page_data["suppliers"]) == 2
    assert page_data["total"] == 3

    # 2. Filtro por active=true
    resp_active = client.get("/api/v1/monitoring?active=true", headers=headers)
    assert resp_active.status_code == 200
    active_data = resp_active.get_json()
    assert len(active_data["suppliers"]) == 2

    # 3. PATCH com email inválido
    resp_bad_email = client.patch(
        f"/api/v1/monitoring/{created_ids[0]}",
        headers=headers,
        json={"alert_email": "invalido@@email"},
    )
    assert resp_bad_email.status_code == 400

    # 4. PATCH com webhook SSRF inválido
    resp_bad_wh = client.patch(
        f"/api/v1/monitoring/{created_ids[0]}",
        headers=headers,
        json={"webhook_url": "ftp://malicious.com"},
    )
    assert resp_bad_wh.status_code == 400

    # 5. PATCH com campos válidos
    resp_ok = client.patch(
        f"/api/v1/monitoring/{created_ids[0]}",
        headers=headers,
        json={
            "company_name": "Fornecedor Pag Atualizado",
            "alert_email": "valido@compliance.com",
            "active": False,
        },
    )
    assert resp_ok.status_code == 200
    s_updated = resp_ok.get_json()["supplier"]
    assert s_updated["company_name"] == "Fornecedor Pag Atualizado"
    assert s_updated["alert_email"] == "valido@compliance.com"
    assert s_updated["active"] is False

