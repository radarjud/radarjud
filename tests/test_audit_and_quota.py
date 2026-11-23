from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.company_search import CompanySearch
from app.models.tenant import Tenant
from app.models.user import User
from app.services.quota_service import QuotaService


def _create_authenticated_user_and_tenant(client, name="Analista Compliance", email="compliance@empresa.com", plan=Tenant.PLAN_ESSENCIAL, quota=30):
    reg_resp = client.post("/api/v1/auth/register", json={
        "name": name,
        "email": email,
        "password": "senhaForteSegura123!",  # gitleaks:allow
        "tenant_name": f"Org {name}",
        "role": User.ROLE_ANALISTA,
    })
    assert reg_resp.status_code == 201
    data = reg_resp.get_json()
    token = data["access_token"]
    tenant_id = data["tenant"]["id"]
    user_id = data["user"]["id"]

    # Ajusta plano e cota do tenant se especificado
    tenant = db.session.get(Tenant, tenant_id)
    tenant.plan = plan
    tenant.limite_mensal_consultas = quota
    db.session.commit()

    return {
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
        "tenant_id": tenant_id,
        "user_id": user_id,
    }


def test_audit_log_decision_and_frozen_snapshot(client, app):
    """
    Critério 1:
    - Registrar decisão REPROVADO com justificativa via POST /api/v1/searches/{id}/decision.
    - Alterar posteriormente os dados da busca original no banco.
    - Validar no GET /api/v1/audit-logs que o snapshot (score e Cenário A/B) permaneceu congelado.
    - Validar nome do analista, data e integridade.
    """
    user_info = _create_authenticated_user_and_tenant(client, name="Juliana Ramos", email="juliana@auditoria.com")
    headers = user_info["headers"]
    tenant_id = user_info["tenant_id"]

    with app.app_context():
        search = CompanySearch(
            tenant_id=tenant_id,
            company_name="FORNECEDOR LOGISTICA LTDA",
            normalized_name="FORNECEDOR LOGISTICA LTDA",
            total_processes=10,
            overall_risk_score=78.5,
            economic_standing={"cenario": "CENARIO_B", "observacao_porte": "Médio Porte"},
            scan_status="COMPLETED",
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

    # 1. Envio da decisão de compliance
    payload = {
        "decision": "REPROVADO",
        "justification": "Alto índice de condenações trabalhistas sem lastro de capital suficiente (Cenário B).",
    }
    resp = client.post(f"/api/v1/searches/{search_id}/decision", json=payload, headers=headers)
    assert resp.status_code == 201
    res_data = resp.get_json()
    assert res_data["status"] == "success"
    audit = res_data["audit_log"]
    assert audit["decision"] == "REPROVADO"
    assert audit["analyst_name"] == "Juliana Ramos"
    assert audit["company_name"] == "FORNECEDOR LOGISTICA LTDA"
    assert audit["risk_score_snapshot"]["overall_risk_score"] == 78.5
    assert audit["risk_score_snapshot"]["cenario"] == "CENARIO_B"

    # 2. Altera a busca original para simular mutação de dados subsequente
    with app.app_context():
        s_mutate = db.session.get(CompanySearch, search_id)
        s_mutate.overall_risk_score = 15.0
        s_mutate.economic_standing = {"cenario": "CENARIO_A"}
        db.session.commit()

    # 3. Consulta GET /api/v1/audit-logs e valida que o snapshot permaneceu inalterado (congelado)
    list_resp = client.get("/api/v1/audit-logs", headers=headers)
    assert list_resp.status_code == 200
    logs_data = list_resp.get_json()
    assert logs_data["total"] == 1
    log_item = logs_data["items"][0]
    assert log_item["decision"] == "REPROVADO"
    assert log_item["analyst_name"] == "Juliana Ramos"
    assert log_item["risk_score_snapshot"]["overall_risk_score"] == 78.5
    assert log_item["risk_score_snapshot"]["cenario"] == "CENARIO_B"
    assert log_item["justification"] == payload["justification"]


def test_audit_log_immutability_and_api_mutation_block(client, app):
    """
    Critério 2:
    - Validar que tentativas de DELETE, PUT ou PATCH na API de audit logs retornam 405.
    - Validar que mutações diretas via SQLAlchemy disparam exceção (before_update e before_delete).
    """
    user_info = _create_authenticated_user_and_tenant(client, name="Auditor Chefe", email="auditor@seguranca.com")
    headers = user_info["headers"]
    tenant_id = user_info["tenant_id"]

    with app.app_context():
        search = CompanySearch(
            tenant_id=tenant_id,
            company_name="EMPRESA AUDITADA S.A.",
            normalized_name="EMPRESA AUDITADA S.A.",
            total_processes=5,
            overall_risk_score=20.0,
            scan_status="COMPLETED",
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

    client.post(f"/api/v1/searches/{search_id}/decision", json={
        "decision": "APROVADO",
        "justification": "Fornecedor homologado sem apontamentos impeditivos.",
    }, headers=headers)

    audit_entry = AuditLog.query.filter_by(company_search_id=search_id).first()
    assert audit_entry is not None
    audit_id = audit_entry.id

    # 1. API blocks PUT, PATCH, DELETE with 405
    assert client.put(f"/api/v1/audit-logs/{audit_id}", json={"decision": "REPROVADO"}, headers=headers).status_code == 405
    assert client.patch(f"/api/v1/audit-logs/{audit_id}", json={"decision": "REPROVADO"}, headers=headers).status_code == 405
    assert client.delete(f"/api/v1/audit-logs/{audit_id}", headers=headers).status_code == 405

    # 2. ORM blocks direct mutation
    with app.app_context():
        entry = db.session.get(AuditLog, audit_id)
        entry.decision = "REPROVADO"
        with pytest.raises(PermissionError, match="AuditLog é imutável: atualizações não são permitidas"):
            db.session.commit()
        db.session.rollback()

    # 3. ORM blocks direct deletion
    with app.app_context():
        entry = db.session.get(AuditLog, audit_id)
        db.session.delete(entry)
        with pytest.raises(PermissionError, match="AuditLog é imutável: exclusões não são permitidas"):
            db.session.commit()
        db.session.rollback()


def test_audit_log_tenant_isolation_and_anti_idor(client, app):
    """
    Critério 3:
    - Tenant A registra busca.
    - Tenant B tenta registrar decisão sobre a busca do Tenant A -> HTTP 403 Forbidden.
    - Tenant B não enxerga logs de auditoria do Tenant A no GET /api/v1/audit-logs.
    """
    user_a = _create_authenticated_user_and_tenant(client, name="Analista Tenant A", email="analista@tenant-a.com")
    user_b = _create_authenticated_user_and_tenant(client, name="Analista Tenant B", email="analista@tenant-b.com")

    with app.app_context():
        search_a = CompanySearch(
            tenant_id=user_a["tenant_id"],
            company_name="EMPRESA EXCLUSIVA TENANT A",
            normalized_name="EMPRESA EXCLUSIVA TENANT A",
            total_processes=3,
            overall_risk_score=10.0,
            scan_status="COMPLETED",
        )
        db.session.add(search_a)
        db.session.commit()
        search_a_id = search_a.id

    # Tenant B tenta registrar decisão sobre search do Tenant A -> 403
    resp_cross = client.post(f"/api/v1/searches/{search_a_id}/decision", json={
        "decision": "RETENCAO",
        "justification": "Tentativa de injeção cross-tenant.",
    }, headers=user_b["headers"])
    assert resp_cross.status_code == 403
    assert "Acesso negado" in resp_cross.get_json()["error"]

    # Tenant A registra decisão válida
    resp_owner = client.post(f"/api/v1/searches/{search_a_id}/decision", json={
        "decision": "RETENCAO",
        "justification": "Retenção técnica preventiva de 10%.",
    }, headers=user_a["headers"])
    assert resp_owner.status_code == 201

    # Tenant B lista /audit-logs -> total 0 (vazio)
    resp_list_b = client.get("/api/v1/audit-logs", headers=user_b["headers"])
    assert resp_list_b.status_code == 200
    assert resp_list_b.get_json()["total"] == 0

    # Tenant A lista /audit-logs -> total 1
    resp_list_a = client.get("/api/v1/audit-logs", headers=user_a["headers"])
    assert resp_list_a.status_code == 200
    assert resp_list_a.get_json()["total"] == 1


def test_quota_service_monthly_limits_and_reopen_cache_exemption(client, app):
    """
    Critério 4:
    - Tenant com limite_mensal_consultas = 2.
    - 1ª busca nova: consome cota, X-Quota-Remaining = 1.
    - 2ª busca nova: consome cota, X-Quota-Remaining = 0.
    - Reabertura da 1ª busca via histórico/cache: passa sem consumir cota (X-Quota-Remaining: 0).
    - 3ª busca nova: bloqueada com HTTP 402 e mensagem clara em português.
    """
    user_info = _create_authenticated_user_and_tenant(
        client, name="Gestor Cota", email="cota@empresa.com", plan=Tenant.PLAN_ESSENCIAL, quota=2
    )
    headers = user_info["headers"]

    mock_metrics_1 = {
        "empresa": "PRIMEIRA EMPRESA TESTE LTDA",
        "total_processos": 2,
        "ranking_motivos": [],
        "ranking_motivos_condenacao": [],
        "scan_status": "COMPLETED",
        "desfechos_sentencas": {"condenacoes": 0},
    }
    mock_metrics_2 = {
        "empresa": "SEGUNDA EMPRESA TESTE LTDA",
        "total_processos": 3,
        "ranking_motivos": [],
        "ranking_motivos_condenacao": [],
        "scan_status": "COMPLETED",
        "desfechos_sentencas": {"condenacoes": 0},
    }
    mock_metrics_3 = {
        "empresa": "TERCEIRA EMPRESA TESTE LTDA",
        "total_processos": 4,
        "ranking_motivos": [],
        "ranking_motivos_condenacao": [],
        "scan_status": "COMPLETED",
        "desfechos_sentencas": {"condenacoes": 0},
    }

    # 1ª Consulta Nova
    with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_metrics_1):
        res1 = client.post("/api/v1/search-company", json={"company_name": "PRIMEIRA EMPRESA TESTE LTDA"}, headers=headers)
        assert res1.status_code == 200
        assert res1.headers.get("X-Quota-Remaining") == "1"

    # 2ª Consulta Nova
    with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_metrics_2):
        res2 = client.post("/api/v1/search-company", json={"company_name": "SEGUNDA EMPRESA TESTE LTDA"}, headers=headers)
        assert res2.status_code == 200
        assert res2.headers.get("X-Quota-Remaining") == "0"

    # Reabertura da 1ª consulta (atende via Curto-Circuito L1 no banco ou L0 sem chamar CNJ)
    # Não deve consumir cota nem ser bloqueada
    res_reopen = client.post("/api/v1/search-company", json={"company_name": "PRIMEIRA EMPRESA TESTE LTDA"}, headers=headers)
    assert res_reopen.status_code == 200
    assert res_reopen.get_json().get("cached") is True
    assert res_reopen.headers.get("X-Quota-Remaining") == "0"

    # 3ª Consulta Nova (termo inédito que exigiria busca externa) -> Deve ser bloqueada com 402
    with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_metrics_3):
        res3 = client.post("/api/v1/search-company", json={"company_name": "TERCEIRA EMPRESA TESTE LTDA"}, headers=headers)
        assert res3.status_code == 402
        body = res3.get_json()
        assert "Cota mensal de consultas esgotada" in body["error"]
        assert body["used"] == 2
        assert body["limit"] == 2
        assert res3.headers.get("X-Quota-Remaining") == "0"


def test_enterprise_plan_has_unlimited_quota(client, app):
    """
    Critério 5:
    - Plano Enterprise possui cota ilimitada (limite_mensal_consultas = None).
    - Header X-Quota-Remaining retorna 'unlimited'.
    - Nenhuma restrição ou bloqueio HTTP 402 ocorre.
    """
    user_info = _create_authenticated_user_and_tenant(
        client, name="Diretor Enterprise", email="enterprise@grandeholding.com",
        plan=Tenant.PLAN_ENTERPRISE, quota=None
    )
    headers = user_info["headers"]

    mock_metrics = {
        "empresa": "EMPRESA CONSULTADA ENTERPRISE",
        "total_processos": 1,
        "ranking_motivos": [],
        "ranking_motivos_condenacao": [],
        "scan_status": "COMPLETED",
        "desfechos_sentencas": {"condenacoes": 0},
    }

    with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_metrics):
        for i in range(3):
            res = client.post(
                "/api/v1/search-company",
                json={"company_name": f"EMPRESA CONSULTADA ENTERPRISE {i}"},
                headers=headers
            )
            assert res.status_code == 200
            assert res.headers.get("X-Quota-Remaining") == "unlimited"


def test_previous_month_searches_do_not_count_in_current_month(app):
    """
    Critério 6:
    - Consultas realizadas em meses anteriores não devem descontar da cota do mês corrente.
    """
    with app.app_context():
        tenant = Tenant(name="Tenant Ciclo Mensal", plan=Tenant.PLAN_ESSENCIAL, limite_mensal_consultas=5)
        db.session.add(tenant)
        db.session.commit()

        # Cria busca no mês anterior
        past_search = CompanySearch(
            tenant_id=tenant.id,
            company_name="BUSCA MES PASSADO",
            normalized_name="BUSCA MES PASSADO",
            total_processes=1,
            created_at=datetime(2026, 1, 15, 12, 0, 0),
        )
        # Cria busca no mês corrente
        current_search = CompanySearch(
            tenant_id=tenant.id,
            company_name="BUSCA MES ATUAL",
            normalized_name="BUSCA MES ATUAL",
            total_processes=1,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.session.add_all([past_search, current_search])
        db.session.commit()

        # O uso do mês corrente deve ser estritamente 1
        usage = QuotaService.get_monthly_usage(tenant.id)
        assert usage == 1

        check = QuotaService.check_quota(tenant)
        assert check["used"] == 1
        assert check["remaining"] == 4
        assert check["allowed"] is True
