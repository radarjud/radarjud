import pytest
from app.extensions import db
from app.models.tenant import Tenant
from app.models.user import User
from app.models.company_search import CompanySearch
from app.utils.auth import require_auth, require_role
from flask import jsonify


def test_auth_register_and_login_flow(client):
    """Testa cadastro de novo Tenant e Admin, seguido de login e emissão de tokens."""
    # 1. Cadastro
    reg_payload = {
        "name": "Administrador Tenant A",
        "email": "admin@empresa-a.com.br",
        "password": "senha-segura-123",
        "tenant_name": "Empresa A Tecnologia",
        "cnpj": "11222333000199",
        "role": "ADMIN",
    }
    resp = client.post("/api/v1/auth/register", json=reg_payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "success"
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["user"]["email"] == "admin@empresa-a.com.br"
    assert data["user"]["role"] == "ADMIN"
    assert data["tenant"]["name"] == "Empresa A Tecnologia"
    assert data["tenant"]["cnpj"] == "11222333000199"

    # 2. Tentativa de cadastro com email duplicado
    resp_dup = client.post("/api/v1/auth/register", json=reg_payload)
    assert resp_dup.status_code == 409
    assert "já cadastrado" in resp_dup.get_json()["error"]

    # 3. Login com sucesso
    login_resp = client.post("/api/v1/auth/login", json={
        "email": "admin@empresa-a.com.br",
        "password": "senha-segura-123",
    })
    assert login_resp.status_code == 200
    login_data = login_resp.get_json()
    assert "access_token" in login_data
    assert "refresh_token" in login_data

    # 4. Login com senha errada
    bad_resp = client.post("/api/v1/auth/login", json={
        "email": "admin@empresa-a.com.br",
        "password": "senha-incorreta",
    })
    assert bad_resp.status_code == 401


def test_auth_me_endpoint(client):
    """Testa endpoint GET /api/v1/auth/me protegido por JWT."""
    # Sem token -> 401
    resp_no_auth = client.get("/api/v1/auth/me")
    assert resp_no_auth.status_code == 401

    # Cadastro e login
    reg_resp = client.post("/api/v1/auth/register", json={
        "name": "Maria Analista",
        "email": "maria@analise.com",
        "password": "senha-forte-123",  # gitleaks:allow
        "tenant_name": "Consultoria Compliance",
        "role": "ANALISTA",
    })
    assert reg_resp.status_code == 201
    access_token = reg_resp.get_json()["access_token"]

    # Com token válido -> 200
    resp_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert resp_me.status_code == 200
    me_data = resp_me.get_json()
    assert me_data["user"]["email"] == "maria@analise.com"
    assert me_data["user"]["role"] == "ANALISTA"
    assert me_data["tenant"]["name"] == "Consultoria Compliance"


def test_auth_logout_endpoint(client):
    """Testa endpoint de logout (/api/v1/auth/logout e alias /api/auth/logout)."""
    # 1. Logout anônimo ou sem token retorna 200
    resp_anon = client.post("/api/v1/auth/logout")
    assert resp_anon.status_code == 200
    assert resp_anon.get_json()["status"] == "success"

    # 2. Cadastro de usuário para obter token
    reg_resp = client.post("/api/v1/auth/register", json={
        "name": "Pedro Auditor",
        "email": "pedro@auditor.com",
        "password": "senha-segura-logout",
        "tenant_name": "Auditoria Corp",
    })
    access_token = reg_resp.get_json()["access_token"]

    # 3. Logout autenticado
    resp_auth = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert resp_auth.status_code == 200
    assert resp_auth.get_json()["status"] == "success"

    # 4. Rota alias /api/auth/logout
    resp_alias = client.post("/api/auth/logout")
    assert resp_alias.status_code == 200
    assert resp_alias.get_json()["status"] == "success"


def test_auth_refresh_token_endpoint(client):
    """Testa renovação de access_token usando refresh_token válido."""
    reg_resp = client.post("/api/v1/auth/register", json={
        "name": "Carlos Diretor",
        "email": "carlos@holding.com",
        "password": "segredo-corporativo",
    })
    tokens = reg_resp.get_json()
    refresh_token = tokens["refresh_token"]

    # Refresh com sucesso
    resp_ref = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp_ref.status_code == 200
    new_tokens = resp_ref.get_json()
    assert "access_token" in new_tokens
    assert "refresh_token" in new_tokens

    # Teste com token inválido
    resp_bad = client.post("/api/v1/auth/refresh", json={"refresh_token": "token-invalido.xyz"})
    assert resp_bad.status_code == 401

    # Teste usando access_token como refresh_token -> deve rejeitar
    resp_wrong_type = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert resp_wrong_type.status_code == 401


def test_rbac_decorators(app, client):
    """Garante que @require_role bloqueia perfis não autorizados (ex: ANALISTA tentando rota ADMIN)."""
    @app.route("/api/v1/test/admin-only", methods=["GET"])
    @require_role("ADMIN")
    def admin_only():
        return jsonify({"message": "Acesso concedido a ADMIN."}), 200

    @app.route("/api/v1/test/analista-or-admin", methods=["GET"])
    @require_role("ADMIN", "ANALISTA")
    def analista_or_admin():
        return jsonify({"message": "Acesso concedido a ambos."}), 200

    # Cria Admin e Analista
    admin_tokens = client.post("/api/v1/auth/register", json={
        "name": "Super Admin",
        "email": "admin@rbac.com",
        "password": "password123",
        "role": "ADMIN",
    }).get_json()

    analista_tokens = client.post("/api/v1/auth/register", json={
        "name": "Junior Analista",
        "email": "analista@rbac.com",
        "password": "password123",
        "role": "ANALISTA",
    }).get_json()

    # 1. Admin acessa rota exclusiva de admin -> 200
    res_admin = client.get("/api/v1/test/admin-only", headers={"Authorization": f"Bearer {admin_tokens['access_token']}"})
    assert res_admin.status_code == 200

    # 2. Analista tenta acessar rota exclusiva de admin -> 403 Forbidden
    res_forbidden = client.get("/api/v1/test/admin-only", headers={"Authorization": f"Bearer {analista_tokens['access_token']}"})
    assert res_forbidden.status_code == 403
    assert "Acesso não autorizado" in res_forbidden.get_json()["error"]

    # 3. Analista acessa rota permitida para analista -> 200
    res_ok = client.get("/api/v1/test/analista-or-admin", headers={"Authorization": f"Bearer {analista_tokens['access_token']}"})
    assert res_ok.status_code == 200


def test_tenant_isolation_and_anti_idor(client, app):
    """
    Validação estrita anti-IDOR e isolamento de dados por Tenant:
    - Tenant A cria busca salva no banco.
    - Tenant B tenta acessar /history/<id> e /search_status/<id> da busca do Tenant A.
    - Sistema bloqueia com HTTP 403.
    - Tenant B consulta /history e não visualiza histórico do Tenant A.
    """
    # 1. Cadastro do Tenant A
    res_a = client.post("/api/v1/auth/register", json={
        "name": "Usuario Tenant A",
        "email": "user@tenant-a.com",
        "password": "passwordA123",
        "tenant_name": "Corporação Alfa",
    }).get_json()
    token_a = res_a["access_token"]
    tenant_a_id = res_a["tenant"]["id"]

    # 2. Cadastro do Tenant B
    res_b = client.post("/api/v1/auth/register", json={
        "name": "Usuario Tenant B",
        "email": "user@tenant-b.com",
        "password": "passwordB123",
        "tenant_name": "Corporação Beta",
    }).get_json()
    token_b = res_b["access_token"]
    tenant_b_id = res_b["tenant"]["id"]

    assert tenant_a_id != tenant_b_id

    # 3. Cria uma busca pertencente ao Tenant A
    with app.app_context():
        search_a = CompanySearch(
            tenant_id=tenant_a_id,
            company_name="EMPRESA CONFIDENCIAL ALFA S.A.",
            normalized_name="EMPRESA CONFIDENCIAL ALFA S.A.",
            total_processes=15,
            scan_status="COMPLETED",
        )
        db.session.add(search_a)
        db.session.commit()
        search_a_id = search_a.id

    # 4. Tenant A acessa sua própria busca -> 200 OK
    res_owner_history = client.get(f"/api/v1/history/{search_a_id}", headers={"Authorization": f"Bearer {token_a}"})
    assert res_owner_history.status_code == 200
    assert res_owner_history.get_json()["empresa"] == "EMPRESA CONFIDENCIAL ALFA S.A."

    res_owner_status = client.get(f"/api/v1/search_status/{search_a_id}", headers={"Authorization": f"Bearer {token_a}"})
    assert res_owner_status.status_code == 200

    # 5. ANTI-IDOR: Tenant B tenta acessar os dados da busca do Tenant A -> Bloqueado com 403 Forbidden!
    res_idor_detail = client.get(f"/api/v1/history/{search_a_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert res_idor_detail.status_code == 403
    assert "Acesso negado" in res_idor_detail.get_json()["error"]

    res_idor_status = client.get(f"/api/v1/search_status/{search_a_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert res_idor_status.status_code == 403
    assert "Acesso negado" in res_idor_status.get_json()["error"]

    res_idor_delete = client.delete(f"/api/v1/history/{search_a_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert res_idor_delete.status_code == 403

    res_idor_pdf = client.get(f"/api/v1/history/{search_a_id}/summary.pdf", headers={"Authorization": f"Bearer {token_b}"})
    assert res_idor_pdf.status_code == 403

    # 6. Tenant B lista /api/v1/history -> Deve ver 0 itens (nenhum dado vazado do Tenant A)
    res_b_list = client.get("/api/v1/history", headers={"Authorization": f"Bearer {token_b}"})
    assert res_b_list.status_code == 200
    assert res_b_list.get_json()["total"] == 0
    assert len(res_b_list.get_json()["items"]) == 0

    # 7. Tenant A lista /api/v1/history -> Deve ver seu próprio item
    res_a_list = client.get("/api/v1/history", headers={"Authorization": f"Bearer {token_a}"})
    assert res_a_list.status_code == 200
    assert res_a_list.get_json()["total"] == 1
    assert res_a_list.get_json()["items"][0]["id"] == search_a_id

    # 8. Usuário anônimo sem token -> Histórico público não vaza registros confidenciais de nenhum tenant
    res_anon_list = client.get("/api/v1/history")
    assert res_anon_list.status_code == 200
    anon_items = res_anon_list.get_json()["items"]
    assert all(item.get("tenant_id") is None for item in anon_items)
    assert not any(item["id"] == search_a_id for item in anon_items)

    # 9. Usuário anônimo tentando acessar diretamente busca protegida por IDOR -> 403 Forbidden
    res_anon_detail = client.get(f"/api/v1/history/{search_a_id}")
    assert res_anon_detail.status_code == 403
    assert "Acesso negado" in res_anon_detail.get_json()["error"]

    res_anon_status = client.get(f"/api/v1/search_status/{search_a_id}")
    assert res_anon_status.status_code == 403

    res_anon_pdf = client.get(f"/api/v1/history/{search_a_id}/summary.pdf")
    assert res_anon_pdf.status_code == 403
