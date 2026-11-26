import io
import json
import re

import pytest

from app import create_app
from app.extensions import db
from app.models.company_search import CompanySearch
from app.models.pep_registry import (
    PEPComplianceCache,
    PEPRegistry,
    mask_cpf_lgpd,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.pdf_report import (
    _generate_pdf_pure_python,
    generate_company_report_pdf,
    normalize_report_payload,
)
from app.services.pep_compliance_service import (
    PEPComplianceService,
    extract_masked_digits,
    is_corporate_entity,
    normalize_pep_name,
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


def test_normalize_pep_name_and_lgpd_masking():
    """Valida normalização determinística de nomes e mascaramento estrito LGPD."""
    assert normalize_pep_name("  João da Silva Álvares! ") == "JOAO DA SILVA ALVARES"
    assert normalize_pep_name("MÁRCIA SOUZA & CIA") == "MARCIA SOUZA CIA"
    assert normalize_pep_name("") == ""
    assert normalize_pep_name(None) == ""

    # Mascaramento de CPF completo para formato oficial CGU
    assert mask_cpf_lgpd("123.456.789-01") == "***.456.789-**"
    assert mask_cpf_lgpd("12345678901") == "***.456.789-**"
    assert mask_cpf_lgpd("***123456**") == "***.123.456-**"
    assert mask_cpf_lgpd("***.123.456-**") == "***.123.456-**"
    assert mask_cpf_lgpd(None) is None

    # Extração de dígitos visíveis
    assert extract_masked_digits("***.123.456-**") == "123456"
    assert extract_masked_digits("") == ""

    # Identificação de Pessoa Jurídica no QSA (não deve ser tratada como pessoa física PEP)
    assert is_corporate_entity("ALPHA HOLDING E PARTICIPACOES LTDA") is True
    assert is_corporate_entity("BANCO SANTANDER S.A.") is True
    assert is_corporate_entity("CARLOS ROBERTO DA SILVA") is False


def test_pep_ingest_csv_stream_memory_efficiency_and_masking(app_instance):
    """
    Valida a ingestão da base oficial PEP em streaming/lotes pequenos (batch_size),
    garantindo que CPFs completos sejam mascarados no banco e estruturas volumosas não vazem na RAM.
    """
    csv_content = (
        "CPF_PEP;NOME_PEP;DESCRICAO_FUNCAO;NOME_ORGAO;DATA_INICIO_EXERCICIO;DATA_FIM_EXERCICIO;DATA_FIM_CARENCIA\n"
        "123.456.789-01;FERNANDO AUGUSTO MENDES;Deputado Federal;Câmara dos Deputados;01/02/2023;31/01/2027;\n"
        "98765432100;ANA PAULA NOGUEIRA;Secretária Municipal;Prefeitura de Curitiba;01/01/2021;31/12/2024;\n"
        "***.555.666-**;ROBERTO MARCIO DIAS;Ministro de Estado;Ministério da Fazenda;01/01/2023;;15/07/2025\n"
    )

    stream = io.StringIO(csv_content)
    service = PEPComplianceService()

    count = service.ingest_pep_csv_stream(stream, batch_size=2)
    assert count == 3

    # Confirmação de persistência indexada com mascaramento estrito LGPD
    record_fernando = PEPRegistry.query.filter_by(nome_normalizado="FERNANDO AUGUSTO MENDES").first()
    assert record_fernando is not None
    assert record_fernando.cargo_funcao == "Deputado Federal"
    assert record_fernando.esfera_orgao == "Câmara dos Deputados"
    # Jamais pode conter os 11 dígitos do CPF
    assert record_fernando.cpf_parcial_mascarado == "***.456.789-**"
    assert "123.456.789-01" not in record_fernando.cpf_parcial_mascarado

    record_ana = PEPRegistry.query.filter_by(nome_normalizado="ANA PAULA NOGUEIRA").first()
    assert record_ana is not None
    assert record_ana.cpf_parcial_mascarado == "***.654.321-**"
    assert "98765432100" not in record_ana.cpf_parcial_mascarado


def test_pep_compliance_positive_exact_match(app_instance):
    """
    Simula empresa com sócio com correspondência exata (nome e documento parcial coincidente).
    Valida status ALERTA_PEP, nível CORRESPONDENCIA_ALTA e recomendação de diligência.
    """
    # Popula PEP no banco com documento mascarado ***.456.789-**
    pep = PEPRegistry(
        nome_normalizado="CARLOS EDUARDO PEREIRA",
        cpf_parcial_mascarado="***.456.789-**",
        cargo_funcao="Diretor Executivo",
        esfera_orgao="Agência Nacional de Energia Elétrica",
        data_inicio_exercicio="01/01/2022",
    )
    db.session.add(pep)
    db.session.commit()

    qsa = [
        {
            "nome_socio": "Carlos Eduardo Pereira",
            "cnpj_cpf_do_socio": "***.456.789-**",
            "qualificacao_socio": "49-Sócio-Administrador",
        },
        {
            "nome_socio": "MARIA DAS DORES SANTOS",
            "cnpj_cpf_do_socio": "***.111.222-**",
            "qualificacao_socio": "22-Sócio",
        },
    ]

    service = PEPComplianceService()
    res = service.check_qsa_compliance(qsa=qsa, cnpj="11222333000199")

    assert res["status"] == "ALERTA_PEP"
    assert res["total_vinculos_identificados"] == 1
    assert len(res["ocorrencias"]) == 1
    oc = res["ocorrencias"][0]
    assert oc["nivel_correspondencia"] == "CORRESPONDENCIA_ALTA"
    assert "Agência Nacional de Energia Elétrica" in oc["esfera_orgao"]
    assert oc["cargo_funcao"] == "Diretor Executivo"
    assert "Correspondência exata por documento parcial mascarado" in oc["descricao_correspondencia"]
    assert "Recomenda-se diligência reforçada" in res["recomendacao"]

    # Validação do cache L1 persistido
    cached = PEPComplianceCache.query.filter_by(cnpj="11222333000199").first()
    assert cached is not None
    assert cached.data["status"] == "ALERTA_PEP"


def test_pep_compliance_positive_homonym_match(app_instance):
    """
    Simula sócio com mesmo nome completo, porém sem documento parcial ou com documento distinto.
    Valida classificação explícita como POSSIVEL_VINCULO_HOMONIMO.
    """
    pep = PEPRegistry(
        nome_normalizado="JOSE CARLOS BATISTA",
        cpf_parcial_mascarado="***.777.888-**",
        cargo_funcao="Vereador",
        esfera_orgao="Câmara Municipal de Campinas",
    )
    db.session.add(pep)
    db.session.commit()

    # Sócio possui o mesmo nome, porém documento parcial desconhecido ou divergente
    qsa = [
        {
            "nome_socio": "José Carlos Batista",
            "cnpj_cpf_do_socio": None,
        }
    ]

    service = PEPComplianceService()
    res = service.check_qsa_compliance(qsa=qsa, cnpj="22333444000188")

    assert res["status"] == "ALERTA_PEP"
    assert res["total_vinculos_identificados"] == 1
    oc = res["ocorrencias"][0]
    assert oc["nivel_correspondencia"] == "POSSIVEL_VINCULO_HOMONIMO"
    assert "Possível vínculo (homônimo) — requer validação documental" in oc["descricao_correspondencia"]
    assert oc["cargo_funcao"] == "Vereador"
    assert "Câmara Municipal de Campinas" in oc["esfera_orgao"]


def test_pep_compliance_clean_negative(app_instance):
    """Valida empresa com QSA limpo (sem correspondência na base PEP)."""
    pep = PEPRegistry(
        nome_normalizado="OUTRA PESSOA QUALQUER",
        cpf_parcial_mascarado="***.123.456-**",
        cargo_funcao="Secretário",
        esfera_orgao="Governo Estadual",
    )
    db.session.add(pep)
    db.session.commit()

    qsa = [
        {"nome_socio": "Juliana Silveira Ramos", "cnpj_cpf_do_socio": "***.999.888-**"},
        {"nome_socio": "HOLDING PARTICIPACOES LTDA", "cnpj_cpf_do_socio": "12345678000199"},
    ]

    service = PEPComplianceService()
    res = service.check_qsa_compliance(qsa=qsa, cnpj="33444555000177")

    assert res["status"] == "NAO_IDENTIFICADO"
    assert res["total_vinculos_identificados"] == 0
    assert res["ocorrencias"] == []
    assert "Nenhum vínculo com Pessoa Politicamente Exposta" in res["recomendacao"]


def test_pep_compliance_resilience_and_unavailable(app_instance, monkeypatch):
    """Valida resiliência em caso de falha de banco com fallback gracioso para INDISPONIVEL."""
    service = PEPComplianceService()

    def _failing_eval(*args, **kwargs):
        raise RuntimeError("Falha de conexão com banco de dados")

    monkeypatch.setattr(service, "_evaluate_qsa_matches", _failing_eval)

    res = service.check_qsa_compliance(qsa=[{"nome_socio": "Teste"}], cnpj="99888777000166")
    assert res["status"] == "INDISPONIVEL"
    assert res["total_vinculos_identificados"] == 0
    assert "temporariamente indisponível" in res["recomendacao"]


def test_pep_compliance_anti_vazamento_lgpd_zero_full_cpf(app_instance, client, caplog):
    """
    Auditoria rigorosa de Segurança e LGPD by Design:
    Passa deliberadamente CPFs completos de 11 dígitos ('123.456.789-01' e '98765432100')
    e verifica que em nenhum momento eles vazam em logs, no banco ou nos payloads JSON de API/PDF.
    """
    cpf_sensivel_1 = "123.456.789-01"
    cpf_sensivel_2 = "98765432100"

    # 1. Ingestão com CPF completo
    csv_raw = (
        f"CPF_PEP;NOME_PEP;DESCRICAO_FUNCAO;NOME_ORGAO\n"
        f"{cpf_sensivel_1};ALEXANDRE DE MORAIS SOUZA;Assessor Parlamentar;Senado Federal\n"
    )
    service = PEPComplianceService()
    service.ingest_pep_csv_stream(io.StringIO(csv_raw))

    # Verifica que no banco o CPF sensível 1 NÃO foi gravado de forma completa
    saved_pep = PEPRegistry.query.filter_by(nome_normalizado="ALEXANDRE DE MORAIS SOUZA").first()
    assert saved_pep is not None
    assert cpf_sensivel_1 not in (saved_pep.cpf_parcial_mascarado or "")
    assert cpf_sensivel_1.replace(".", "").replace("-", "") not in (saved_pep.cpf_parcial_mascarado or "")

    # 2. QSA contendo CPF completo 2
    qsa_com_cpf_completo = [
        {
            "nome_socio": "Alexandre de Morais Souza",
            "cnpj_cpf_do_socio": cpf_sensivel_2,
        }
    ]

    with caplog.at_level("DEBUG"):
        compliance_res = service.check_qsa_compliance(qsa=qsa_com_cpf_completo, cnpj="44555666000122")

    # Auditoria em logs capturados
    log_text = caplog.text
    assert cpf_sensivel_1 not in log_text
    assert cpf_sensivel_2 not in log_text

    # Auditoria no payload do serviço
    serialized = json.dumps(compliance_res)
    assert cpf_sensivel_1 not in serialized
    assert cpf_sensivel_2 not in serialized
    assert re.search(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", serialized) is None
    assert re.search(r"\b\d{11}\b", serialized) is None


def test_pep_compliance_anti_idor_and_history_exposure(app_instance, client):
    """Valida a exposição do bloco compliance_pep em /history/<id> e o isolamento Anti-IDOR."""
    tenant_a = Tenant(name="Tenant Compliance A", cnpj="11111111000100")
    tenant_b = Tenant(name="Tenant Compliance B", cnpj="22222222000100")
    db.session.add_all([tenant_a, tenant_b])
    db.session.commit()

    user_a = User(email="analista.a@compliance.com", name="User A", tenant_id=tenant_a.id)
    user_a.set_password("segredo123")
    user_b = User(email="analista.b@compliance.com", name="User B", tenant_id=tenant_b.id)
    user_b.set_password("segredo123")
    db.session.add_all([user_a, user_b])
    db.session.commit()

    pep_payload = {
        "status": "ALERTA_PEP",
        "total_vinculos_identificados": 1,
        "ocorrencias": [
            {
                "nivel_correspondencia": "POSSIVEL_VINCULO_HOMONIMO",
                "cargo_funcao": "Secretário de Estado",
                "esfera_orgao": "Governo do Estado de Pernambuco",
                "descricao_correspondencia": "Possível vínculo (homônimo) — requer validação documental",
                "vigencia": "31/12/2026",
            }
        ],
        "recomendacao": "Identificado possível vínculo com Pessoa Politicamente Exposta (PEP) no QSA.",
    }

    search = CompanySearch(
        tenant_id=tenant_a.id,
        company_name="FORNECEDOR PEP S/A",
        normalized_name="FORNECEDOR PEP SA",
        total_processes=2,
        compliance_pep=pep_payload,
    )
    db.session.add(search)
    db.session.commit()

    token_a = create_access_token(user_a)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Tenant A (proprietário) acessa com sucesso
    resp_a = client.get(f"/api/v1/history/{search.id}", headers=headers_a)
    assert resp_a.status_code == 200
    data_a = resp_a.get_json()
    assert "compliance_pep" in data_a
    assert data_a["compliance_pep"]["status"] == "ALERTA_PEP"
    assert data_a["compliance_pep"]["total_vinculos_identificados"] == 1
    assert "Secretário de Estado" in str(data_a["compliance_pep"]["ocorrencias"])

    # Tenant B recebe 403 Forbidden (Anti-IDOR)
    token_b = create_access_token(user_b)
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp_b = client.get(f"/api/v1/history/{search.id}", headers=headers_b)
    assert resp_b.status_code == 403


def test_pep_compliance_pdf_rendering(app_instance):
    """Valida a renderização correta do bloco de Compliance PEP no PDF (v3 HTML, ReportLab e Pure Python)."""
    # 1. Cenário Positivo (ALERTA_PEP)
    payload_pos = {
        "company_name": "FORNECEDOR COM PEP LTDA",
        "total_processes": 3,
        "compliance_pep": {
            "status": "ALERTA_PEP",
            "total_vinculos_identificados": 1,
            "ocorrencias": [
                {
                    "nivel_correspondencia": "CORRESPONDENCIA_ALTA",
                    "cargo_funcao": "Subsecretário de Obras",
                    "esfera_orgao": "Prefeitura de São Paulo",
                    "descricao_correspondencia": "Correspondência exata por documento parcial mascarado",
                }
            ],
            "recomendacao": "Identificado possível vínculo com Pessoa Politicamente Exposta (PEP) no QSA.",
        },
    }

    norm_pos = normalize_report_payload(payload_pos)
    assert norm_pos["compliance_pep"]["status"] == "ALERTA_PEP"
    assert norm_pos["compliance_pep"]["total_vinculos_identificados"] == 1

    pure_pdf_pos = _generate_pdf_pure_python(norm_pos)
    assert pure_pdf_pos.startswith(b"%PDF-1.4")
    assert b"Alerta Compliance PEP: 1 vinculo" in pure_pdf_pos

    pdf_pos = generate_company_report_pdf(payload_pos)
    assert isinstance(pdf_pos, bytes)
    assert pdf_pos.startswith(b"%PDF-")

    # 2. Cenário Negativo (NAO_IDENTIFICADO)
    payload_neg = {
        "company_name": "EMPRESA PRIVADA LIMPA LTDA",
        "total_processes": 1,
        "compliance_pep": {
            "status": "NAO_IDENTIFICADO",
            "total_vinculos_identificados": 0,
            "ocorrencias": [],
            "recomendacao": "Nenhum vínculo com Pessoa Politicamente Exposta (PEP) identificado no QSA consultado.",
        },
    }

    norm_neg = normalize_report_payload(payload_neg)
    pure_pdf_neg = _generate_pdf_pure_python(norm_neg)
    assert b"Nenhum vinculo com Pessoa Politicamente Exposta" in pure_pdf_neg

    pdf_neg = generate_company_report_pdf(payload_neg)
    assert isinstance(pdf_neg, bytes)
    assert pdf_neg.startswith(b"%PDF-")


def test_pep_compliance_baseline_and_roxel_participacoes(app, client):
    """
    Valida a detecção determinística de PEP para a empresa Roxel Participações Ltda,
    cujo QSA possui Gilmar Ferreira Mendes (Ministro do STF - PEP Direto) e
    Francisco Schertel Ferreira Mendes (Filho de Ministro do STF - Familiar de PEP 1º grau).
    """
    from app.services.economic_service import EconomicStandingService
    from app.services.pep_compliance_service import check_qsa_pep_compliance

    with app.app_context():
        # Limpa para garantir acionamento do seed_pep_baseline_if_empty
        PEPRegistry.query.delete()
        db.session.commit()

        econ = EconomicStandingService()
        data = econ.fetch_brasilapi_data("53259809000107")
        assert data.get("razao_social") == "ROXEL PARTICIPACOES LTDA"
        qsa = data.get("qsa")
        assert len(qsa) == 3

        res = check_qsa_pep_compliance(qsa=qsa, cnpj="53259809000107", force_refresh=True)
        assert res["status"] == "ALERTA_PEP"
        assert res["total_vinculos_identificados"] >= 2

        # Valida que Gilmar Mendes e Francisco Mendes foram identificados
        cargos = [oc["cargo_funcao"] for oc in res["ocorrencias"]]
        assert any("Ministro do Supremo Tribunal Federal" in c for c in cargos)
        assert any("Familiar de PEP (Filho de Ministro do STF)" in c for c in cargos)


def test_pep_compliance_zema_credito(app, client):
    """
    Valida a detecção de PEP para a Zema Crédito, Financiamento e Investimento S/A,
    identificando Romeu Zema Neto (Governador do Estado de Minas Gerais - PEP Direto).
    """
    from app.services.economic_service import EconomicStandingService
    from app.services.pep_compliance_service import check_qsa_pep_compliance

    with app.app_context():
        econ = EconomicStandingService()
        data = econ.fetch_brasilapi_data("05351887000186")
        assert data.get("razao_social") == "ZEMA CREDITO, FINANCIAMENTO E INVESTIMENTO S/A"
        qsa = data.get("qsa")
        assert any("ROMEU ZEMA" in s.get("nome_socio", "") for s in qsa)

        res = check_qsa_pep_compliance(qsa=qsa, cnpj="05351887000186", force_refresh=True)
        assert res["status"] == "ALERTA_PEP"
        assert res["total_vinculos_identificados"] >= 1

        cargos = [oc["cargo_funcao"] for oc in res["ocorrencias"]]
        assert any("Governador do Estado de Minas Gerais" in c for c in cargos)


