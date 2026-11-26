import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.extensions import db
from app.models.company_search import CompanySearch
from app.models.public_contract_cache import PublicContractCache
from app.models.tenant import Tenant
from app.models.user import User
from app.services.pdf_report import (
    _generate_pdf_pure_python,
    generate_company_report_pdf,
    normalize_report_payload,
)
from app.services.public_contracts_service import (
    PublicContractsService,
    normalize_sphere,
    sanitize_cnpj,
    sanitize_public_organ_name,
)
from app.utils.jwt import create_access_token


@pytest.fixture
def app_instance():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app_instance):
    return app_instance.test_client()


def test_sanitize_cnpj_and_helper_functions():
    """Valida funções auxiliares de sanitização de CNPJ, esferas e órgãos públicos."""
    assert sanitize_cnpj("11.222.333/0001-99") == "11222333000199"
    assert sanitize_cnpj(None) == ""
    assert sanitize_cnpj("") == ""

    # Normalização de Esferas
    assert normalize_sphere("M") == "Municipal"
    assert normalize_sphere("MUNICIPAL") == "Municipal"
    assert normalize_sphere("E") == "Estadual"
    assert normalize_sphere("ESTADUAL") == "Estadual"
    assert normalize_sphere("F") == "Federal"
    assert normalize_sphere("FEDERAL") == "Federal"
    assert normalize_sphere("", organ_name="PREFEITURA MUNICIPAL DE PAULISTA") == "Municipal"
    assert normalize_sphere("", organ_name="GOVERNO DO ESTADO DE PERNAMBUCO") == "Estadual"
    assert normalize_sphere("", organ_name="MINISTERIO DA SAUDE") == "Federal"

    # Sanitização LGPD de Órgãos Públicos (remoção de CPFs acidentais)
    cleaned_organ = sanitize_public_organ_name("Prefeitura do Recife - Resp: 123.456.789-00")
    assert "123.456.789-00" not in cleaned_organ
    assert "PREFEITURA DO RECIFE" in cleaned_organ


def test_public_contracts_positive_pncp_and_l1_cache(app_instance):
    """
    Simula resposta positiva do PNCP com múltiplos contratos, órgãos e esferas distintas.
    Valida que os dados são consolidados e que a segunda chamada é servida via cache L1.
    """
    test_cnpj = "11222333000199"

    mock_pncp_response = {
        "totalRegistros": 2,
        "data": [
            {
                "numeroContratoEmpenho": "CT-001/2023",
                "objetoContrato": "Prestação de serviços de limpeza predial e conservação.",
                "valorGlobal": 150000.0,
                "orgaoEntidade": {
                    "razaoSocial": "PREFEITURA MUNICIPAL DE RECIFE",
                    "esferaId": "M",
                },
            },
            {
                "numeroContratoEmpenho": "CT-002/2024",
                "objetoContrato": "Manutenção preventiva em instalações elétricas.",
                "valorGlobal": 350000.0,
                "orgaoEntidade": {
                    "razaoSocial": "MINISTERIO DA EDUCACAO",
                    "esferaId": "F",
                },
            },
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_pncp_response

    service = PublicContractsService()

    with patch.object(service.session, "get", return_value=mock_resp) as mock_get:
        # 1ª consulta: dispara requisição HTTP externa
        res = service.get_public_contracts(test_cnpj)

        assert mock_get.call_count == 1
        assert res["status"] == "DISPONIVEL"
        assert res["total_contratos"] == 2
        assert sorted(res["esferas"]) == ["Federal", "Municipal"]
        assert "PREFEITURA MUNICIPAL DE RECIFE" in res["orgaos_contratantes"]
        assert "MINISTERIO DA EDUCACAO" in res["orgaos_contratantes"]
        assert res["valor_global_total"] == 500000.0
        assert "500.000,00" in res["valor_global_formatado"]
        assert "2 contrato(s) público(s) identificado(s)" in res["mensagem"]

        # Validação do cache L1 persistido no banco
        cached_entry = PublicContractCache.query.filter_by(cnpj=test_cnpj).first()
        assert cached_entry is not None
        assert cached_entry.data["total_contratos"] == 2
        assert cached_entry.is_valid(ttl_hours=24) is True

    # 2ª consulta: deve ser atendida pelo cache L1 sem disparar requisição HTTP
    with patch.object(service.session, "get") as mock_get_second:
        cached_res = service.get_public_contracts(test_cnpj)
        assert mock_get_second.call_count == 0
        assert cached_res["status"] == "DISPONIVEL"
        assert cached_res["total_contratos"] == 2
        assert cached_res["valor_global_total"] == 500000.0


def test_public_contracts_empty_results(app_instance):
    """Valida o comportamento com empresa sem contratos públicos (totalRegistros=0 ou lista vazia)."""
    test_cnpj = "44555666000188"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"totalRegistros": 0, "data": []}

    service = PublicContractsService()
    with patch.object(service.session, "get", return_value=mock_resp):
        res = service.get_public_contracts(test_cnpj)

        assert res["status"] == "DISPONIVEL"
        assert res["total_contratos"] == 0
        assert res["esferas"] == []
        assert res["orgaos_contratantes"] == []
        assert res["valor_global_total"] is None
        assert res["valor_global_formatado"] is None
        assert res["mensagem"] == "Nenhum contrato público localizado nas bases consultadas"


def test_public_contracts_resilience_timeout_and_500(app_instance):
    """
    Garante resiliência: timeout ou erro HTTP 500 no portal externo retorna status 'INDISPONIVEL'
    sem quebrar a aplicação e sem gravar cache permanente de erro.
    """
    test_cnpj = "77888999000100"
    service = PublicContractsService()

    # 1. Simulação de Timeout
    with patch.object(service.session, "get", side_effect=requests.exceptions.Timeout("Read timeout")):
        res_timeout = service.get_public_contracts(test_cnpj)
        assert res_timeout["status"] == "INDISPONIVEL"
        assert res_timeout["total_contratos"] == 0
        assert res_timeout["mensagem"] == "Base governamental temporariamente indisponível no momento da consulta."

        # Não deve salvar no cache L1 definitivo
        assert PublicContractCache.query.filter_by(cnpj=test_cnpj).first() is None

    # 2. Simulação de HTTP 500 Internal Server Error
    mock_500 = MagicMock()
    mock_500.status_code = 500
    with patch.object(service.session, "get", return_value=mock_500):
        res_500 = service.get_public_contracts(test_cnpj)
        assert res_500["status"] == "INDISPONIVEL"
        assert res_500["total_contratos"] == 0
        assert res_500["mensagem"] == "Base governamental temporariamente indisponível no momento da consulta."


def test_public_contracts_strict_lgpd_sanitization(app_instance):
    """
    Garante que nomes de gestores e CPFs acidentalmente inseridos nos metadados
    são 100% filtrados e jamais vazam no retorno estruturado.
    """
    test_cnpj = "99888777000166"
    sensitive_claimant = "João Gestor da Silva Sauro"
    sensitive_cpf = "123.456.789-00"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "totalRegistros": 1,
        "data": [
            {
                "numeroContratoEmpenho": "100/2023",
                "valorGlobal": 90000.0,
                "orgaoEntidade": {
                    "razaoSocial": f"SECRETARIA MUNICIPAL DE EDUCACAO - FISCAL: {sensitive_claimant} (CPF: {sensitive_cpf})",
                    "esferaId": "M",
                },
            }
        ],
    }

    service = PublicContractsService()
    with patch.object(service.session, "get", return_value=mock_resp):
        res = service.get_public_contracts(test_cnpj)

        serialized = json.dumps(res)
        assert sensitive_cpf not in serialized
        assert "SECRETARIA MUNICIPAL DE EDUCACAO" in str(res["orgaos_contratantes"])


def test_api_history_exposes_contratos_with_anti_idor(app_instance, client):
    """Valida a exposição do bloco contratos_poder_publico em /history/<id> e o isolamento Anti-IDOR."""
    tenant_a = Tenant(name="Tenant A", cnpj="11111111000100")
    tenant_b = Tenant(name="Tenant B", cnpj="22222222000100")
    db.session.add_all([tenant_a, tenant_b])
    db.session.commit()

    user_a = User(email="user.a@gov.com", name="User A", tenant_id=tenant_a.id)
    user_a.set_password("pass123")
    user_b = User(email="user.b@gov.com", name="User B", tenant_id=tenant_b.id)
    user_b.set_password("pass123")
    db.session.add_all([user_a, user_b])
    db.session.commit()

    contratos_mock = {
        "status": "DISPONIVEL",
        "total_contratos": 3,
        "esferas": ["Estadual", "Municipal"],
        "orgaos_contratantes": ["PREFEITURA DE CARUARU", "COMPESA"],
        "valor_global_total": 750000.0,
        "valor_global_formatado": "R$ 750.000,00",
        "mensagem": "3 contrato(s) público(s) identificado(s) nas bases governamentais",
    }

    search = CompanySearch(
        tenant_id=tenant_a.id,
        company_name="EMPRESA FORNECEDORA S/A",
        normalized_name="EMPRESA FORNECEDORA SA",
        total_processes=5,
        contratos_poder_publico=contratos_mock,
    )
    db.session.add(search)
    db.session.commit()

    token_a = create_access_token(user_a)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Acesso autorizado pelo tenant A
    resp = client.get(f"/api/v1/history/{search.id}", headers=headers_a)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "contratos_poder_publico" in data
    assert data["contratos_poder_publico"]["total_contratos"] == 3
    assert "COMPESA" in data["contratos_poder_publico"]["orgaos_contratantes"]

    # Consulta via rota canônica /searches/<id>
    resp_canon = client.get(f"/api/v1/searches/{search.id}", headers=headers_a)
    assert resp_canon.status_code == 200
    data_canon = resp_canon.get_json()
    assert data_canon["contratos_poder_publico"]["total_contratos"] == 3

    # Bloqueio Anti-IDOR para usuário do tenant B
    token_b = create_access_token(user_b)
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp_forbidden = client.get(f"/api/v1/history/{search.id}", headers=headers_b)
    assert resp_forbidden.status_code == 403


def test_pdf_rendering_with_public_contracts(app_instance):
    """Valida a renderização correta do bloco de Contratos com o Poder Público no PDF."""
    # 1. Cenário Positivo (com contratos)
    payload_pos = {
        "company_name": "FORNECEDOR GOV S/A",
        "total_processes": 4,
        "contratos_poder_publico": {
            "status": "DISPONIVEL",
            "total_contratos": 2,
            "esferas": ["Federal", "Municipal"],
            "orgaos_contratantes": ["PREFEITURA DO RECIFE", "MINISTERIO DA SAUDE"],
            "valor_global_total": 450000.0,
            "valor_global_formatado": "R$ 450.000,00",
            "mensagem": "2 contrato(s) público(s) identificado(s) nas bases governamentais",
        },
    }
    norm_pos = normalize_report_payload(payload_pos)
    assert norm_pos["contratos_poder_publico"]["total_contratos"] == 2

    pure_pdf_pos = _generate_pdf_pure_python(norm_pos)
    assert pure_pdf_pos.startswith(b"%PDF-1.4")
    assert b"Contratos com o Poder Publico" in pure_pdf_pos
    assert b"PREFEITURA DO RECIFE" in pure_pdf_pos

    pdf_pos = generate_company_report_pdf(payload_pos)
    assert isinstance(pdf_pos, bytes)
    assert pdf_pos.startswith(b"%PDF-")
    assert b"%%EOF" in pdf_pos

    # 2. Cenário Vazio (zero contratos)
    payload_empty = {
        "company_name": "EMPRESA PRIVADA LTDA",
        "total_processes": 2,
        "contratos_poder_publico": {
            "status": "DISPONIVEL",
            "total_contratos": 0,
            "esferas": [],
            "orgaos_contratantes": [],
            "valor_global_total": None,
            "valor_global_formatado": None,
            "mensagem": "Nenhum contrato público localizado nas bases consultadas",
        },
    }
    norm_empty = normalize_report_payload(payload_empty)
    pure_pdf_empty = _generate_pdf_pure_python(norm_empty)
    assert b"Nenhum contrato publico localizado nas bases consultadas" in pure_pdf_empty

    pdf_empty = generate_company_report_pdf(payload_empty)
    assert isinstance(pdf_empty, bytes)
    assert pdf_empty.startswith(b"%PDF-")
    assert b"%%EOF" in pdf_empty

    # 3. Cenário Indisponível
    payload_unavail = {
        "company_name": "EMPRESA INDISPONIVEL LTDA",
        "total_processes": 1,
        "contratos_poder_publico": {
            "status": "INDISPONIVEL",
            "total_contratos": 0,
            "esferas": [],
            "orgaos_contratantes": [],
            "valor_global_total": None,
            "valor_global_formatado": None,
            "mensagem": "Base governamental temporariamente indisponível no momento da consulta.",
        },
    }
    norm_unavail = normalize_report_payload(payload_unavail)
    pure_pdf_unavail = _generate_pdf_pure_python(norm_unavail)
    assert b"temporariamente indisponivel" in pure_pdf_unavail
