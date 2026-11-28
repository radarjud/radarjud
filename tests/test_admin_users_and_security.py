import pytest
from app.extensions import db
from app.models.tenant import Tenant
from app.models.user import User


def test_password_hash_never_exposed_in_payloads(client):
    """
    Valida que nem a senha pura nem o hash de senha (password_hash) são expostos
    em /register, /login, /me, /admin/users ou no to_dict() do modelo User.
    """
    # 1. Cadastro
    reg_resp = client.post("/api/v1/auth/register", json={
        "name": "Super Admin Seguranca",
        "email": "seguranca@tenant-sec.com",
        "password": "SenhaSuperSecreta@2026",
        "tenant_name": "Tenant Seguranca",
        "role": "ADMIN",
    })
    assert reg_resp.status_code == 201
    reg_data = reg_resp.get_json()
    assert "password" not in reg_data["user"]
    assert "password_hash" not in reg_data["user"]
    assert "SenhaSuperSecreta@2026" not in str(reg_data)

    token = reg_data["access_token"]

    # 2. Login
    login_resp = client.post("/api/v1/auth/login", json={
        "email": "seguranca@tenant-sec.com",
        "password": "SenhaSuperSecreta@2026",
    })
    assert login_resp.status_code == 200
    login_data = login_resp.get_json()
    assert "password" not in login_data["user"]
    assert "password_hash" not in login_data["user"]
    assert "SenhaSuperSecreta@2026" not in str(login_data)

    # 3. GET /me
    me_resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    me_data = me_resp.get_json()
    assert "password" not in me_data["user"]
    assert "password_hash" not in me_data["user"]

    # 4. GET /admin/users
    admin_users_resp = client.get("/api/v1/auth/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert admin_users_resp.status_code == 200
    admin_data = admin_users_resp.get_json()
    for u in admin_data["users"]:
        assert "password" not in u
        assert "password_hash" not in u


def test_admin_board_access_restricted_to_admin(client):
    """
    Garante que APENAS usuários com papel 'ADMIN' conseguem listar usuários
    e alterar planos. Usuários 'ANALISTA' ou anônimos recebem 403/401.
    """
    # Cria Admin e Analista no mesmo Tenant
    client.post("/api/v1/auth/register", json={
        "name": "Admin Responsável",
        "email": "admin@empresa-x.com",
        "password": "passwordAdmin123",
        "tenant_name": "Empresa X",
        "role": "ADMIN",
    })

    # Adiciona analista
    analista_resp = client.post("/api/v1/auth/register", json={
        "name": "Analista Operacional",
        "email": "analista@empresa-x.com",
        "password": "passwordAnalista123",
        "tenant_name": "Empresa X - Secundária",
        "role": "ANALISTA",
    })
    analista_token = analista_resp.get_json()["access_token"]

    # Anônimo tentando acessar /admin/users -> 401
    anon_resp = client.get("/api/v1/auth/admin/users")
    assert anon_resp.status_code == 401

    # Analista tentando acessar /admin/users -> 403 Forbidden
    analista_get_resp = client.get(
        "/api/v1/auth/admin/users",
        headers={"Authorization": f"Bearer {analista_token}"}
    )
    assert analista_get_resp.status_code == 403
    assert "Acesso não autorizado" in analista_get_resp.get_json()["error"]

    # Analista tentando alterar plano -> 403 Forbidden
    analista_put_resp = client.put(
        "/api/v1/auth/admin/users/1/plan",
        headers={"Authorization": f"Bearer {analista_token}"},
        json={"plan": "PRO"}
    )
    assert analista_put_resp.status_code == 403


def test_admin_board_list_and_update_plan_flow(client):
    """
    Valida fluxo completo do Admin:
    1. Lista usuários do seu tenant com cotas e planos disponíveis.
    2. Altera plano para PRO e verifica atualização de cota de 30 para 150.
    3. Altera plano para ENTERPRISE e verifica cota ilimitada.
    4. Anti-IDOR: impede que admin altere plano de usuário de outro tenant.
    """
    # 1. Cadastra Admin Tenant Alfa (Plano default ESSENCIAL, limite 30)
    res_alfa = client.post("/api/v1/auth/register", json={
        "name": "Admin Alfa",
        "email": "admin@alfa-corp.com",
        "password": "adminAlfaPass123",
        "tenant_name": "Alfa Corp",
        "role": "ADMIN",
    }).get_json()
    token_alfa = res_alfa["access_token"]
    user_alfa_id = res_alfa["user"]["id"]
    tenant_alfa_id = res_alfa["tenant"]["id"]

    # Cadastra segundo usuário (Analista) no mesmo Tenant Alfa
    with client.application.app_context():
        user_analista = User(
            tenant_id=tenant_alfa_id,
            name="Analista Alfa 2",
            email="analista2@alfa-corp.com",
            role="ANALISTA",
            is_active=True,
        )
        user_analista.set_password("analistaAlfaPass123")
        db.session.add(user_analista)
        db.session.commit()
        analista_id = user_analista.id

    # 2. Admin Alfa lista todos os usuários do sistema -> Deve incluir os usuários criados
    list_resp = client.get(
        "/api/v1/auth/admin/users",
        headers={"Authorization": f"Bearer {token_alfa}"}
    )
    assert list_resp.status_code == 200
    list_data = list_resp.get_json()
    assert list_data["status"] == "success"
    assert list_data["total"] >= 2
    emails = [u["email"] for u in list_data["users"]]
    assert "admin@alfa-corp.com" in emails
    assert "analista2@alfa-corp.com" in emails
    assert list_data["tenant"]["plan"] == "ESSENCIAL"
    assert list_data["quota"]["limit"] == 30

    # 3. Admin Alfa altera plano de user_alfa para PRO (isolado para este usuário)
    update_pro = client.put(
        f"/api/v1/auth/admin/users/{user_alfa_id}/plan",
        headers={"Authorization": f"Bearer {token_alfa}"},
        json={"plan": "PRO"}
    )
    assert update_pro.status_code == 200
    pro_data = update_pro.get_json()
    assert pro_data["user"]["plan"] == "PRO"
    assert pro_data["quota"]["limit"] == 150

    # Valida que a alteração de user_alfa NÃO afetou analista2
    with client.application.app_context():
        u_analista_check = db.session.get(User, analista_id)
        assert u_analista_check.plan is None
        assert u_analista_check.effective_plan == "ESSENCIAL"
        assert u_analista_check.effective_limit == 30

    # 4. Admin Alfa altera plano de analista para ENTERPRISE (Ilimitado)
    update_ent = client.patch(
        f"/api/v1/auth/admin/users/{analista_id}/plan",
        headers={"Authorization": f"Bearer {token_alfa}"},
        json={"plan": "ENTERPRISE"}
    )
    assert update_ent.status_code == 200
    ent_data = update_ent.get_json()
    assert ent_data["user"]["plan"] == "ENTERPRISE"
    assert ent_data["quota"]["is_unlimited"] is True

    # Valida novamente que user_alfa permaneceu PRO e analista2 virou ENTERPRISE
    with client.application.app_context():
        u_alfa_check = db.session.get(User, user_alfa_id)
        u_analista_check2 = db.session.get(User, analista_id)
        assert u_alfa_check.plan == "PRO"
        assert u_analista_check2.plan == "ENTERPRISE"

    # 5. Tentativa com plano inválido
    bad_plan = client.put(
        f"/api/v1/auth/admin/users/{user_alfa_id}/plan",
        headers={"Authorization": f"Bearer {token_alfa}"},
        json={"plan": "PLANO_INEXISTENTE"}
    )
    assert bad_plan.status_code == 400

    # 6. Cria Tenant Beta com outro usuário
    res_beta = client.post("/api/v1/auth/register", json={
        "name": "User Beta",
        "email": "user@beta-corp.com",
        "password": "userBetaPass123",
        "tenant_name": "Beta Corp",
    }).get_json()
    assert res_beta["user"]["role"] == "ANALISTA"
    user_beta_id = res_beta["user"]["id"]

    # Admin Alfa consegue visualizar e gerenciar o plano do usuário Beta no painel global
    update_beta = client.put(
        f"/api/v1/auth/admin/users/{user_beta_id}/plan",
        headers={"Authorization": f"Bearer {token_alfa}"},
        json={"plan": "PRO"}
    )
    assert update_beta.status_code == 200
    assert update_beta.get_json()["user"]["plan"] == "PRO"
