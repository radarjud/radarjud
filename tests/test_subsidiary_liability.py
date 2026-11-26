import json
import pytest
from app import create_app
from app.extensions import db
from app.models.company_search import CompanySearch
from app.models.process_record import ProcessRecord
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai_analyzer import (
    detect_subsidiary_liability_in_processes,
    sanitize_contractor_pj_name,
    extract_pj_contractors_from_process,
    process_has_subsidiary_text,
)
from app.services.pdf_report import (
    generate_company_report_pdf,
    normalize_report_payload,
    _generate_pdf_pure_python,
)


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


def test_sanitize_contractor_pj_name_and_lgpd_filters():
    """Valida que pessoas físicas e CPFs são 100% descartados (LGPD by Design)."""
    # Nomes de pessoas físicas sem indicador de PJ -> devem ser rejeitados
    assert sanitize_contractor_pj_name("João da Silva Sauro") is None
    assert sanitize_contractor_pj_name("Maria Eduarda dos Santos") is None
    assert sanitize_contractor_pj_name("Carlos Alberto de Oliveira") is None

    # CPFs isolados ou atrelados -> devem ser descartados ou sanitizados
    assert sanitize_contractor_pj_name("123.456.789-00") is None
    assert sanitize_contractor_pj_name("CPF 123.456.789-00") is None

    # PJs legítimas -> devem ser aceitas e normalizadas em uppercase
    pj_clean = sanitize_contractor_pj_name("Construtora Horizonte Ltda.")
    assert pj_clean == "CONSTRUTORA HORIZONTE LTDA"

    orgao_clean = sanitize_contractor_pj_name("Município de Recife - PE")
    assert "MUNICIPIO" in orgao_clean

    sa_clean = sanitize_contractor_pj_name("BANCO DO BRASIL S/A")
    assert sa_clean == "BANCO DO BRASIL S/A"

    # Exclusão da própria empresa avaliada (por nome ou CNPJ raiz)
    assert sanitize_contractor_pj_name(
        "ALPHA SERVICOS TERCEIRIZADOS LTDA",
        evaluated_company="Alpha Serviços Terceirizados Ltda"
    ) is None

    assert sanitize_contractor_pj_name(
        "ALPHA SERVICOS LTDA 11.222.333/0001-99",
        evaluated_company="Alpha Serviços Ltda",
        evaluated_cnpj="11.222.333/0002-88"
    ) is None


def test_positive_detection_and_process_deduplication():
    """
    Testa detecção positiva de responsabilidade subsidiária de tomadora PJ (Súmula 331 TST)
    e garante que o mesmo processo com múltiplas publicações seja contabilizado exatamente 1 vez.
    """
    evaluated_name = "TERCEIRIZA MAIS SERVICOS LTDA"

    processes = [
        # Processo 1: multipolo passivo PJ + 2 publicações citando a Súmula 331 TST
        {
            "numero_processo": "0000100-20.2023.5.06.0001",
            "publicacoes_texto": [
                "Publicação 1: Reconhecida a responsabilidade subsidiária da 2ª reclamada nos termos da Súmula 331 do TST.",
                "Publicação 2: Intimação da devedora subsidiária CONSTRUTORA HORIZONTE LTDA para pagamento.",
            ],
            "destinatarios_nomes": [
                evaluated_name,
                "CONSTRUTORA HORIZONTE LTDA",
                "João Reclamante Sauro",
            ],
            "observation": "Condenação subsidiária da tomadora",
        },
        # Processo 2: multipolo passivo PJ + padrão textual de tomadora de serviços
        {
            "numero_processo": "0000200-30.2023.5.06.0002",
            "publicacoes_texto": [
                "Julgo procedente em parte e condeno subsidiariamente a tomadora dos serviços COMPANHIA PERNAMBUCANA DE SANEAMENTO COMPESA.",
            ],
            "destinatarios_nomes": [
                evaluated_name,
                "COMPANHIA PERNAMBUCANA DE SANEAMENTO COMPESA",
            ],
        },
        # Processo 3: processo trabalhista comum sem condenação subsidiária nem tomadora PJ
        {
            "numero_processo": "0000300-40.2023.5.06.0003",
            "publicacoes_texto": [
                "Notificação para audiência inaugural de conciliação sobre horas extras.",
            ],
            "destinatarios_nomes": [
                evaluated_name,
                "Pedro Trabalhador da Silva",
            ],
        },
    ]

    result = detect_subsidiary_liability_in_processes(processes, evaluated_company=evaluated_name)

    # Critério de contagem exata (2 processos únicos afetados)
    assert result["condenacoes_subsidiarias_tomadoras_count"] == 2
    assert len(result["processos_afetados"]) == 2
    assert "0000100-20.2023.5.06.0001" in result["processos_afetados"]
    assert "0000200-30.2023.5.06.0002" in result["processos_afetados"]
    assert "0000300-40.2023.5.06.0003" not in result["processos_afetados"]

    # Validação da lista de tomadoras PJ identificadas (sem reclamantes pessoas físicas)
    tomadoras = result["tomadoras_subsidiarias_pj"]
    assert "CONSTRUTORA HORIZONTE LTDA" in tomadoras
    assert "COMPANHIA PERNAMBUCANA DE SANEAMENTO COMPESA" in tomadoras
    assert "João Reclamante Sauro" not in tomadoras
    assert "Pedro Trabalhador da Silva" not in tomadoras
    assert evaluated_name not in tomadoras

    # Mensagem padronizada
    expected_msg = "Histórico de Responsabilidade Subsidiária: 2 processos com condenação subsidiária atribuída a contratantes anteriores"
    assert result["mensagem_responsabilidade_subsidiaria"] == expected_msg


def test_empty_state_zero_subsidiary_liability():
    """Valida o estado de ausência de condenações subsidiárias de contratantes."""
    processes = [
        {
            "numero_processo": "0000999-99.2023.5.06.0099",
            "publicacoes_texto": ["Audiência de instrução sobre adicional de periculosidade."],
            "destinatarios_nomes": ["EMPRESA LIMPA LTDA"],
        }
    ]

    result = detect_subsidiary_liability_in_processes(processes, evaluated_company="EMPRESA LIMPA LTDA")

    assert result["condenacoes_subsidiarias_tomadoras_count"] == 0
    assert result["tomadoras_subsidiarias_pj"] == []
    assert result["processos_afetados"] == []
    assert result["mensagem_responsabilidade_subsidiaria"] == "Nenhuma condenação subsidiária de contratante identificada na amostra"

    # Caso com lista totalmente vazia
    empty_res = detect_subsidiary_liability_in_processes([], evaluated_company="EMPRESA QUALQUER")
    assert empty_res["condenacoes_subsidiarias_tomadoras_count"] == 0
    assert empty_res["tomadoras_subsidiarias_pj"] == []
    assert empty_res["mensagem_responsabilidade_subsidiaria"] == "Nenhuma condenação subsidiária de contratante identificada na amostra"


def test_strict_lgpd_sanitization_no_claimants_in_db_payload_or_pdf(app_instance):
    """
    Garante que nomes e CPFs de reclamantes propositalmente inseridos nunca vazem
    no banco de dados, no payload JSON ou no PDF gerado.
    """
    sensitive_claimant = "João da Silva Sauro"
    sensitive_cpf = "123.456.789-00"

    processes = [
        {
            "numero_processo": "0000777-77.2023.5.06.0077",
            "publicacoes_texto": [
                f"Condeno subsidiariamente a tomadora MUNICIPIO DE RECIFE. Reclamante: {sensitive_claimant}, CPF: {sensitive_cpf}."
            ],
            "destinatarios_nomes": [
                "PRESTADORA DE SERVICOS LTDA",
                "MUNICIPIO DE RECIFE",
                f"{sensitive_claimant} (CPF: {sensitive_cpf})",
            ],
        }
    ]

    sub_res = detect_subsidiary_liability_in_processes(
        processes, evaluated_company="PRESTADORA DE SERVICOS LTDA"
    )

    # 1. Validação no detector
    assert sensitive_claimant not in str(sub_res["tomadoras_subsidiarias_pj"])
    assert sensitive_cpf not in str(sub_res["tomadoras_subsidiarias_pj"])
    assert "MUNICIPIO DE RECIFE" in sub_res["tomadoras_subsidiarias_pj"]

    # 2. Validação no modelo persistido
    search = CompanySearch(
        company_name="PRESTADORA DE SERVICOS LTDA",
        normalized_name="PRESTADORA DE SERVICOS LTDA",
        total_processes=1,
        condenacoes_subsidiarias_tomadoras_count=sub_res["condenacoes_subsidiarias_tomadoras_count"],
        tomadoras_subsidiarias_pj=sub_res["tomadoras_subsidiarias_pj"],
    )
    db.session.add(search)
    db.session.commit()

    search_dict = search.to_dict()
    serialized_json = json.dumps(search_dict)
    assert sensitive_claimant not in serialized_json
    assert sensitive_cpf not in serialized_json

    # 3. Validação no PDF gerado
    pdf_bytes = generate_company_report_pdf(search_dict)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 500
    assert sensitive_claimant.encode("latin-1") not in pdf_bytes
    assert sensitive_cpf.encode("latin-1") not in pdf_bytes


def test_api_endpoints_expose_subsidiary_liability_and_anti_idor(app_instance, client):
    """Valida a exposição correta dos campos nas rotas da API e o isolamento Anti-IDOR."""
    tenant_a = Tenant(name="Tenant Alpha", cnpj="11111111000100")
    tenant_b = Tenant(name="Tenant Beta", cnpj="22222222000100")
    db.session.add_all([tenant_a, tenant_b])
    db.session.commit()

    user_a = User(email="user.a@alpha.com", name="User A", tenant_id=tenant_a.id)
    user_a.set_password("secret123")
    db.session.add(user_a)
    db.session.commit()

    # Cria busca associada ao tenant_a com condenação subsidiária
    search = CompanySearch(
        tenant_id=tenant_a.id,
        company_name="EMPRESA AUDITADA S/A",
        normalized_name="EMPRESA AUDITADA SA",
        total_processes=2,
        condenacoes_subsidiarias_tomadoras_count=1,
        tomadoras_subsidiarias_pj=["ESTADO DE PERNAMBUCO"],
    )
    db.session.add(search)
    db.session.commit()

    from app.utils.jwt import create_access_token
    token_a = create_access_token(user_a)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Consulta via /history/<id>
    resp = client.get(f"/api/v1/history/{search.id}", headers=headers_a)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["condenacoes_subsidiarias_tomadoras_count"] == 1
    assert data["tomadoras_subsidiarias_pj"] == ["ESTADO DE PERNAMBUCO"]
    assert "Histórico de Responsabilidade Subsidiária: 1 processos" in data["mensagem_responsabilidade_subsidiaria"]

    # Consulta via rota canônica /searches/<id>
    resp_search = client.get(f"/api/v1/searches/{search.id}", headers=headers_a)
    assert resp_search.status_code == 200
    data_search = resp_search.get_json()
    assert data_search["condenacoes_subsidiarias_tomadoras_count"] == 1
    assert data_search["tomadoras_subsidiarias_pj"] == ["ESTADO DE PERNAMBUCO"]

    # Validação Anti-IDOR: Usuário de outro tenant não pode acessar
    user_b = User(email="user.b@beta.com", name="User B", tenant_id=tenant_b.id)
    user_b.set_password("secret123")
    db.session.add(user_b)
    db.session.commit()
    token_b = create_access_token(user_b)
    headers_b = {"Authorization": f"Bearer {token_b}"}

    resp_forbidden = client.get(f"/api/v1/history/{search.id}", headers=headers_b)
    assert resp_forbidden.status_code == 403


def test_pdf_rendering_exact_messages_positive_and_empty():
    """Valida os textos exatos renderizados no PDF para presença e ausência de registros."""
    # Cenário Positivo
    payload_pos = {
        "company_name": "CONSTRUTORA EXEMPLO S/A",
        "total_processes": 5,
        "condenacoes_subsidiarias_tomadoras_count": 3,
        "tomadoras_subsidiarias_pj": ["PETROBRAS S/A", "MUNICIPIO DE SUAPE"],
        "desfechos_sentencas": {"condenacoes": 2, "acordos": 1, "improcedentes": 2, "em_aberto": 0},
    }
    norm_pos = normalize_report_payload(payload_pos)
    assert norm_pos["compliance"]["condenacoes_subsidiarias_tomadoras_count"] == 3
    assert "Histórico de Responsabilidade Subsidiária: 3 processos" in norm_pos["compliance"]["mensagem_responsabilidade_subsidiaria"]
    assert norm_pos["compliance"]["tomadoras_subsidiarias_pj"] == ["PETROBRAS S/A", "MUNICIPIO DE SUAPE"]

    pure_pdf_pos = _generate_pdf_pure_python(norm_pos)
    assert pure_pdf_pos.startswith(b"%PDF-1.4")
    assert b"Responsabilidade Subsidi" in pure_pdf_pos
    assert b"PETROBRAS S/A" in pure_pdf_pos
    assert b"MUNICIPIO DE SUAPE" in pure_pdf_pos

    pdf_pos = generate_company_report_pdf(payload_pos)
    assert isinstance(pdf_pos, bytes)
    assert pdf_pos.startswith(b"%PDF-")
    assert b"%%EOF" in pdf_pos

    # Cenário Vazio (Empty State)
    payload_empty = {
        "company_name": "EMPRESA SEGURA LTDA",
        "total_processes": 2,
        "condenacoes_subsidiarias_tomadoras_count": 0,
        "tomadoras_subsidiarias_pj": [],
        "desfechos_sentencas": {"condenacoes": 0, "acordos": 1, "improcedentes": 1, "em_aberto": 0},
    }
    norm_empty = normalize_report_payload(payload_empty)
    assert norm_empty["compliance"]["condenacoes_subsidiarias_tomadoras_count"] == 0
    assert norm_empty["compliance"]["mensagem_responsabilidade_subsidiaria"] == "Nenhuma condenação subsidiária de contratante identificada na amostra"

    pure_pdf_empty = _generate_pdf_pure_python(norm_empty)
    assert pure_pdf_empty.startswith(b"%PDF-1.4")
    assert b"Nenhuma" in pure_pdf_empty
    assert b"subsidi" in pure_pdf_empty.lower()

    pdf_empty = generate_company_report_pdf(payload_empty)
    assert isinstance(pdf_empty, bytes)
    assert pdf_empty.startswith(b"%PDF-")
    assert b"%%EOF" in pdf_empty


def test_bayesian_simplex_and_blocking_rule_not_broken(app_instance):
    """
    Garante que a métrica complementar de responsabilidade subsidiária
    não afeta a Regra de Bloqueio de Predição (0 condenações = 0.0%) nem a soma do simplex bayesiano.
    """
    from app.services.prediction_engine import BayesianRiskPredictor

    predictor = BayesianRiskPredictor()

    # 1. Empresa sem condenações (Regra de Bloqueio)
    processes_zero_cond = [
        {
            "numero_processo": "0000001-00.2023.5.06.0001",
            "sentence_outcome": "improcedentes",
            "primary_subject": "Horas Extras",
        },
        {
            "numero_processo": "0000002-00.2023.5.06.0001",
            "sentence_outcome": "acordos",
            "primary_subject": "Horas Extras",
        },
    ]

    metrics = predictor.generate_dashboard_metrics("EMPRESA LIMPA LTDA", processes_zero_cond)
    assert metrics["previsao_bloqueada"] is True
    assert metrics["probabilidade_condenacao_geral_pct"] == 0.0

    # 2. Simplex bayesiano somando 100%
    processes_with_cond = [
        {
            "numero_processo": "0000003-00.2023.5.06.0001",
            "sentence_outcome": "condenacoes",
            "primary_subject": "Verbas Rescisórias",
        },
        {
            "numero_processo": "0000004-00.2023.5.06.0001",
            "sentence_outcome": "improcedentes",
            "primary_subject": "Verbas Rescisórias",
        },
        {
            "numero_processo": "0000005-00.2023.5.06.0001",
            "sentence_outcome": "acordos",
            "primary_subject": "Verbas Rescisórias",
        },
    ]
    metrics_active = predictor.generate_dashboard_metrics("EMPRESA CONDENADA LTDA", processes_with_cond)
    for p in metrics_active.get("plus_previsao_condenacao", []):
        soma_simplex = (
            p.get("probabilidade_condenacao_pct", 0.0)
            + p.get("probabilidade_acordo_pct", 0.0)
            + p.get("probabilidade_vencer_pct", 0.0)
        )
        assert round(soma_simplex, 1) == 100.0
