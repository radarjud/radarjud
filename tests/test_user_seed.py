# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
import pytest
from app import create_app
from app.extensions import db
from app.models.tenant import Tenant
from app.models.user import User
from app.seeds.user_seed import seed_default_users, DEFAULT_USERS, DEFAULT_ORGANIZATION


@pytest.fixture
def app():
    app = create_app("testing", seed_catalog=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_seed_default_users_initial_creation(app):
    """
    Valida a criação inicial dos usuários padrão de sistema (Admin e Analista),
    assegurando que ambos são vinculados à organização padrão com seus respectivos perfis.
    """
    with app.app_context():
        # Limpa qualquer dado existente para teste isolado
        User.query.delete()
        Tenant.query.delete()
        db.session.commit()

        res = seed_default_users(commit=True)

        assert res["status"] == "success"
        assert len(res["created"]) == 2
        assert "admin@radarjus.com.br" in res["created"]
        assert "analista@radarjus.com.br" in res["created"]

        # Valida tenant
        tenant = Tenant.query.filter_by(name=DEFAULT_ORGANIZATION["name"]).first()
        assert tenant is not None
        assert tenant.cnpj == DEFAULT_ORGANIZATION["cnpj"]
        assert tenant.plan == Tenant.PLAN_ENTERPRISE

        # Valida usuário Admin
        admin = User.query.filter_by(email="admin@radarjus.com.br").first()
        assert admin is not None
        assert admin.name == "Administrador RadarJus"
        assert admin.role == User.ROLE_ADMIN
        assert admin.tenant_id == tenant.id
        assert admin.is_active is True
        assert admin.check_password("admin123") is True

        # Valida usuário Analista
        analista = User.query.filter_by(email="analista@radarjus.com.br").first()
        assert analista is not None
        assert analista.name == "Analista de Compliance"
        assert analista.role == User.ROLE_ANALISTA
        assert analista.tenant_id == tenant.id
        assert analista.is_active is True
        assert analista.check_password("analista123") is True


def test_seed_default_users_idempotency_multiple_runs(app):
    """
    Garante idempotência estrita: executar o seed múltiplas vezes
    não gera registros duplicados nem erros no banco.
    """
    with app.app_context():
        User.query.delete()
        Tenant.query.delete()
        db.session.commit()

        # 1ª Execução -> Cria os 2 usuários
        res1 = seed_default_users(commit=True)
        assert len(res1["created"]) == 2
        assert len(res1["existing"]) == 0
        assert User.query.count() == 2
        assert Tenant.query.count() == 1

        # 2ª Execução -> Não cria nada, apenas detecta existentes
        res2 = seed_default_users(commit=True)
        assert len(res2["created"]) == 0
        assert len(res2["existing"]) == 2
        assert "admin@radarjus.com.br" in res2["existing"]
        assert "analista@radarjus.com.br" in res2["existing"]
        assert User.query.count() == 2
        assert Tenant.query.count() == 1

        # 3ª Execução -> Continua 100% idempotente
        res3 = seed_default_users(commit=True)
        assert len(res3["created"]) == 0
        assert len(res3["existing"]) == 2
        assert User.query.count() == 2
        assert Tenant.query.count() == 1


def test_default_users_authentication_via_login_api(app, client):
    """
    Valida que os usuários padrão criados pelo seed conseguem autenticar
    normalmente através do endpoint REST oficial /api/v1/auth/login.
    """
    with app.app_context():
        User.query.delete()
        Tenant.query.delete()
        db.session.commit()
        seed_default_users(commit=True)

    # 1. Login com o Administrador
    resp_admin = client.post("/api/v1/auth/login", json={
        "email": "admin@radarjus.com.br",
        "password": "admin123",
    })
    assert resp_admin.status_code == 200
    data_admin = resp_admin.get_json()
    assert data_admin["status"] == "success"
    assert "access_token" in data_admin
    assert data_admin["user"]["role"] == "ADMIN"
    assert data_admin["user"]["email"] == "admin@radarjus.com.br"

    # 2. Login com o Analista
    resp_analista = client.post("/api/v1/auth/login", json={
        "email": "analista@radarjus.com.br",
        "password": "analista123",
    })
    assert resp_analista.status_code == 200
    data_analista = resp_analista.get_json()
    assert data_analista["status"] == "success"
    assert "access_token" in data_analista
    assert data_analista["user"]["role"] == "ANALISTA"
    assert data_analista["user"]["email"] == "analista@radarjus.com.br"

    # 3. Senha incorreta deve falhar com 401
    resp_fail = client.post("/api/v1/auth/login", json={
        "email": "admin@radarjus.com.br",
        "password": "senha_errada",
    })
    assert resp_fail.status_code == 401


def test_seed_reuses_existing_tenant_if_present(app):
    """
    Se já existir uma organização no banco de dados, os usuários padrão
    são vinculados a ela em vez de criar um tenant duplicado.
    """
    with app.app_context():
        User.query.delete()
        Tenant.query.delete()
        db.session.commit()

        # Cria um tenant pré-existente
        existing_tenant = Tenant(name="Empresa Cliente S/A", cnpj="11222333000199")
        db.session.add(existing_tenant)
        db.session.commit()
        tenant_id = existing_tenant.id

        # Executa o seed
        res = seed_default_users(commit=True)
        assert res["status"] == "success"
        assert res["tenant_id"] == tenant_id
        assert Tenant.query.count() == 1

        # Usuários devem pertencer ao tenant existente
        users = User.query.all()
        assert len(users) == 2
        for u in users:
            assert u.tenant_id == tenant_id
