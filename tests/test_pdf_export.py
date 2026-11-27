import time
import tracemalloc
from unittest.mock import MagicMock
from urllib.parse import quote

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.tenant import Tenant
from app.models.user import User
from app.routes.api import SEARCH_CACHE
from app.services.pdf_report import (
    _generate_pdf_pure_python,
    generate_company_report_pdf,
    generate_comparative_report_pdf,
    normalize_report_payload,
)
from app.services.pdf_svg_charts import (
    generate_bayes_vs_sumula331_svg,
    generate_comparative_bars_svg,
    generate_outcomes_simplex_svg,
)
from app.utils.jwt import create_access_token


def _sample_bradesco_payload():
    return {
        "empresa": "BRADESCO (GRUPO CONSOLIDADO / FILIAIS)",
        "total_processos": 942,
        "indice_exposicao_financeira": 15.9,
        "score_risco_terceirizacao": 15.9,
        "probabilidade_condenacao_geral_pct": 88.2,
        "desfechos_sentencas": {
            "condenacoes": 410,
            "acordos": 180,
            "improcedentes": 55,
            "em_aberto": 297,
        },
        "ranking_motivos": [
            {"motivo": "Horas Extras e Intervalo Intrajornada", "quantidade": 210, "percentual": 22.3},
            {"motivo": "Direitos da Categoria Bancária (7ª e 8ª Horas)", "quantidade": 165, "percentual": 17.5},
        ],
        "ranking_motivos_condenacao": [
            {"motivo": "Horas Extras e Intervalo Intrajornada", "quantidade_condenacoes": 145, "percentual": 35.4},
            {"motivo": "Equiparação Salarial e Desvio de Função", "quantidade_condenacoes": 92, "percentual": 22.4},
        ],
        "plus_previsao_condenacao": [
            {
                "assunto": "Horas Extras e Intervalo Intrajornada",
                "probabilidade_prevista_pct": 78.4,
                "confiabilidade": "Alta",
                "amostra_julgada": 645,
            }
        ],
        "idoneidade_economica": {
            "cenario": "CENARIO_A",
            "cenario_tipo": "CENARIO_A",
            "cenario_badge": "Cenário A · Solvência Comprovada",
            "cenario_titulo": "Cenário A: Idoneidade Econômica Robusta • Risco de Calote Subsidiário Mitigado",
            "cenario_descricao": "A empresa apresenta situação cadastral ATIVA e Capital Social consolidado de R$ 115.187.056.914,72.",
            "capital_social_num": 115187056914.72,
            "capital_social_formatado": "R$ 115.187.056.914,72",
            "badge_compatibilidade_porte": "Alta Solidez Patrimonial • Compatível com o porte (DEMAIS)",
            "subtitulo_unidades": "Capital Social consolidado cobrindo 51 unidades/filiais mapeadas.",
            "situacao_cadastral": "ATIVA",
            "probabilidade_insolvencia": 0.8,
            "risco_calote_subsidiario": 0.7,
            "possui_sancoes_cgu": False,
            "possui_divida_ativa": False,
            "cnpj_utilizado": "60746948000112",
            "cnpjs_consolidados": ["60746948000112", "33055146000193"],
        },
        "ai_analysis": {
            "perfil_risco_consolidado": "Baixo / Solvente",
            "recomendacao_homologacao": "HOMOLOGAR COM MONITORAMENTO",
            "resumo_executivo": "Conglomerado financeiro com solvência comprovada sob a Súmula 331 do TST.",
            "recomendacoes_compliance": [
                "Exigir certidões negativas periódicas e comprovantes de FGTS/INSS."
            ],
        },
        "scan_status": "COMPLETED",
    }


def test_pdf_export_by_history_id_returns_200_and_valid_pdf(client, app):
    """Garante que GET /api/v1/history/<id>/summary.pdf retorna 200 e um PDF válido."""
    payload = _sample_bradesco_payload()
    with app.app_context():
        search = CompanySearch(
            company_name=payload["empresa"],
            normalized_name="BRADESCO",
            total_processes=payload["total_processos"],
            outcomes_summary=payload["desfechos_sentencas"],
            ranking_motivos=payload["ranking_motivos"],
            ranking_motivos_condenacao=payload["ranking_motivos_condenacao"],
            predictions_summary=payload["plus_previsao_condenacao"],
            economic_standing=payload["idoneidade_economica"],
            overall_risk_score=payload["score_risco_terceirizacao"],
            ai_analysis=payload["ai_analysis"],
            scan_status="COMPLETED",
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

    resp = client.get(f"/api/v1/history/{sid}/summary.pdf")
    assert resp.status_code == 200
    assert "application/pdf" in resp.content_type
    assert resp.data.startswith(b"%PDF-")
    assert len(resp.data) > 500


def test_pdf_export_with_slashes_in_company_name_and_cnpj_never_404(client, app):
    """
    Valida que barras '/' no nome ('BRADESCO (GRUPO CONSOLIDADO / FILIAIS)') e no CNPJ
    ('60.746.948/0001-12') NUNCA retornam 404, tanto via path quanto via query string.
    """
    payload = _sample_bradesco_payload()
    SEARCH_CACHE["BROAD_BRAND::BRADESCO::"] = (time.time(), payload)

    try:
        # 1. Path com barra no nome consolidado (/api/v1 e /api)
        resp_name_v1 = client.get("/api/v1/report/pdf/BRADESCO (GRUPO CONSOLIDADO / FILIAIS)")
        assert resp_name_v1.status_code == 200
        assert "application/pdf" in resp_name_v1.content_type
        assert resp_name_v1.data.startswith(b"%PDF-")

        resp_name_alias = client.get("/api/report/pdf/BRADESCO (GRUPO CONSOLIDADO / FILIAIS)")
        assert resp_name_alias.status_code == 200
        assert "application/pdf" in resp_name_alias.content_type
        assert resp_name_alias.data.startswith(b"%PDF-")

        # 2. Path com barra no CNPJ formatado
        resp_cnpj_v1 = client.get("/api/v1/report/pdf/60.746.948/0001-12")
        assert resp_cnpj_v1.status_code == 200
        assert "application/pdf" in resp_cnpj_v1.content_type
        assert resp_cnpj_v1.data.startswith(b"%PDF-")

        resp_cnpj_export = client.get("/api/export/pdf/60.746.948/0001-12")
        assert resp_cnpj_export.status_code == 200
        assert "application/pdf" in resp_cnpj_export.content_type
        assert resp_cnpj_export.data.startswith(b"%PDF-")

        # 3. Query string (?empresa=... e ?q=...)
        q_name = quote("BRADESCO (GRUPO CONSOLIDADO / FILIAIS)")
        resp_qs_name = client.get(f"/api/report/pdf?empresa={q_name}")
        assert resp_qs_name.status_code == 200
        assert "application/pdf" in resp_qs_name.content_type
        assert resp_qs_name.data.startswith(b"%PDF-")

        q_cnpj = quote("60.746.948/0001-12")
        resp_qs_cnpj = client.get(f"/api/export/pdf?q={q_cnpj}")
        assert resp_qs_cnpj.status_code == 200
        assert "application/pdf" in resp_qs_cnpj.content_type
        assert resp_qs_cnpj.data.startswith(b"%PDF-")
    finally:
        SEARCH_CACHE.pop("BROAD_BRAND::BRADESCO::", None)


def test_pdf_export_via_post_with_session_payload_works_without_db_record(client):
    """
    Garante que enviar o objeto já carregado na tela via POST /api/v1/report/pdf
    sempre gera o PDF com 200 OK, mesmo sem registro prévio no banco ou cache.
    """
    payload = _sample_bradesco_payload()
    resp = client.post("/api/v1/report/pdf", json=payload)
    assert resp.status_code == 200
    assert "application/pdf" in resp.content_type
    assert resp.data.startswith(b"%PDF-")
    assert "radarjus-bradesco.pdf" in resp.headers.get("Content-Disposition", "")


def test_pdf_report_contains_all_v2_risktech_and_solvency_fields():
    """
    Verifica que o normalizador e o gerador de PDF incluem todos os campos obrigatórios da v2.0:
    Nome/Grupo, CNPJ Matriz, Exposição Financeira, Eventos, Confiança Bayesiana,
    Capital Social Consolidado, Situação RFB, Insolvência, Risco Subsidiário, Cenário A/B,
    Ranking de Condenações e Parecer Súmula 331 TST.
    """
    payload = _sample_bradesco_payload()
    norm = normalize_report_payload(payload)

    assert norm["company_name"] == "BRADESCO (GRUPO CONSOLIDADO / FILIAIS)"
    assert norm["cnpj_formatted"] == "60.746.948/0001-12"
    assert norm["exposure_score"] == 15.9
    assert norm["total_processes"] == 942
    assert norm["conf_label"] == "Alta"
    assert norm["economic"]["capital_fmt"] == "R$ 115.187.056.914,72"
    assert norm["economic"]["situacao_rfb"] == "ATIVA"
    assert norm["economic"]["prob_insolv"] == 0.8
    assert norm["economic"]["risco_sub"] == 0.7
    assert norm["economic"]["cenario_tipo"] == "CENARIO_A"
    assert len(norm["ranking_condenacoes"]) == 2
    assert "HOMOLOGAR" in norm["compliance"]["recomendacao"]

    # Valida geração via ReportLab (ou fallback) e também diretamente no fallback puro
    pdf_bytes = generate_company_report_pdf(payload)
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"%%EOF" in pdf_bytes

    pure_pdf = _generate_pdf_pure_python(norm)
    assert pure_pdf.startswith(b"%PDF-1.4")
    assert b"BRADESCO \\(GRUPO CONSOLIDADO / FILIAIS\\)" in pure_pdf
    assert b"60.746.948/0001-12" in pure_pdf
    assert b"R$ 115.187.056.914,72" in pure_pdf
    assert b"HOMOLOGAR COM MONITORAMENTO" in pure_pdf
    assert b"Horas Extras e Intervalo Intrajornada" in pure_pdf


def test_pdf_export_endpoints_export_pdf_v1_and_alias(client):
    """
    Testa que os novos endpoints /api/v1/export-pdf e os aliases /api/export-pdf
    funcionam para POST e GET sanitizando o header Content-Disposition sem caracteres ilegais.
    """
    payload = _sample_bradesco_payload()

    # 1. POST /api/v1/export-pdf
    resp_post_v1 = client.post("/api/v1/export-pdf", json=payload)
    assert resp_post_v1.status_code == 200
    assert "application/pdf" in resp_post_v1.content_type
    assert resp_post_v1.data.startswith(b"%PDF-")
    cd_header = resp_post_v1.headers.get("Content-Disposition", "")
    assert "radarjus-" in cd_header
    assert "\n" not in cd_header
    assert "\r" not in cd_header
    assert "/" not in cd_header.split("filename=")[-1]

    # 2. POST alias /api/export-pdf
    resp_post_alias = client.post("/api/export-pdf", json=payload)
    assert resp_post_alias.status_code == 200
    assert resp_post_alias.data.startswith(b"%PDF-")

    # 3. GET /api/v1/export-pdf com query string
    q_name = quote("BRADESCO")
    resp_get_v1 = client.get(f"/api/v1/export-pdf?empresa={q_name}")
    assert resp_get_v1.status_code == 200
    assert resp_get_v1.data.startswith(b"%PDF-")


def test_pdf_export_single_cenario_b_zero_condemnation_block(client):
    """
    Testa que uma empresa no Cenário B com 0 condenações ativa a regra de bloqueio
    de predição bayesiana (0,0%) e gera o PDF v3 com sucesso sem erro 500.
    """
    cenario_b_payload = {
        "empresa": "SERVICOS TERCEIRIZADOS LTDA",
        "cnpj": "12.345.678/0001-90",
        "total_processos": 15,
        "score_risco_terceirizacao": 85.0,
        "desfechos_sentencas": {
            "condenacoes": 0,
            "acordos": 3,
            "improcedentes": 7,
            "em_aberto": 5,
        },
        "idoneidade_economica": {
            "cenario": "CENARIO_B",
            "cenario_tipo": "CENARIO_B",
            "cenario_badge": "Cenário B · Risco Crítico",
            "capital_social_num": 1000.0,
            "capital_social_formatado": "R$ 1.000,00",
            "situacao_cadastral": "ATIVA",
            "probabilidade_insolvencia": 92.0,
            "risco_calote_subsidiario": 85.0,
            "possui_sancoes_cgu": True,
            "possui_divida_ativa": True,
        },
        "ai_analysis": {
            "perfil_risco_consolidado": "Crítico / Elevado",
            "recomendacao_homologacao": "REPROVAR / BLOQUEAR",
            "resumo_executivo": "Empresa sem patrimônio suficiente para suportar passivo potencial sob Súmula 331 TST.",
        },
    }

    # Gera PDF diretamente
    pdf_bytes = generate_company_report_pdf(cenario_b_payload)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500

    # Testa via API
    resp = client.post("/api/v1/export-pdf", json=cenario_b_payload)
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF-")


def test_pdf_export_comparative_endpoints(client, app):
    """
    Testa a exportação de PDF comparativo via /api/v1/export-compare-pdf e /api/export-compare-pdf:
    1. POST com array de 3 empresas
    2. GET com query string
    3. Proteção Anti-IDOR para busca de outro tenant
    """
    comp1 = _sample_bradesco_payload()
    comp2 = {
        "empresa": "BANCO DO BRASIL S.A.",
        "cnpj": "00.000.000/0001-91",
        "total_processos": 520,
        "score_risco_terceirizacao": 18.0,
        "idoneidade_economica": {
            "cenario": "CENARIO_A",
            "capital_social_formatado": "R$ 90.000.000.000,00",
            "situacao_cadastral": "ATIVA",
        },
        "compliance": {"recomendacao": "HOMOLOGAR COM MONITORAMENTO"},
    }
    comp3 = {
        "empresa": "ITAU UNIBANCO S.A.",
        "cnpj": "60.701.190/0001-04",
        "total_processos": 780,
        "score_risco_terceirizacao": 22.0,
        "idoneidade_economica": {
            "cenario": "CENARIO_A",
            "capital_social_formatado": "R$ 100.000.000.000,00",
            "situacao_cadastral": "ATIVA",
        },
        "compliance": {"recomendacao": "HOMOLOGAR COM MONITORAMENTO"},
    }

    # 1. POST /api/v1/export-compare-pdf com 3 empresas
    resp_post = client.post("/api/v1/export-compare-pdf", json={"companies": [comp1, comp2, comp3]})
    assert resp_post.status_code == 200
    assert "application/pdf" in resp_post.content_type
    assert resp_post.data.startswith(b"%PDF-")
    assert "radarjus-comparativo" in resp_post.headers.get("Content-Disposition", "")

    # 2. GET /api/export-compare-pdf com empresas
    resp_get = client.get("/api/export-compare-pdf?empresas=Bradesco,Itau")
    assert resp_get.status_code == 200
    assert resp_get.data.startswith(b"%PDF-")

    # 3. Anti-IDOR check com Tenant diferente
    with app.app_context():
        t1 = Tenant(name="Tenant Alpha", cnpj="11111111000111", is_active=True)
        t2 = Tenant(name="Tenant Beta", cnpj="22222222000122", is_active=True)
        db.session.add_all([t1, t2])
        db.session.commit()

        user1 = User(
            tenant_id=t1.id,
            name="User Alpha",
            email="user1@alpha.com",
            password_hash="hash1",
            role="USER",
            is_active=True,
        )
        db.session.add(user1)
        db.session.commit()

        # Busca pertencente ao Tenant 2
        search_t2 = CompanySearch(
            tenant_id=t2.id,
            company_name="CONFIDENCIAL BETA LTDA",
            normalized_name="CONFIDENCIAL",
            total_processes=10,
        )
        db.session.add(search_t2)
        db.session.commit()
        sid_t2 = search_t2.id
        t1_id = t1.id
        t2_id = t2.id
        token_u1 = create_access_token(user1)

    headers = {"Authorization": f"Bearer {token_u1}"}

    # Tenta exportar comparativo acessando search_id do Tenant 2 com token do Tenant 1 -> 403 Forbidden
    resp_idor = client.post(
        "/api/v1/export-compare-pdf",
        json={"search_ids": [sid_t2]},
        headers=headers,
    )
    assert resp_idor.status_code == 403

    # Tenta exportar enviando objeto com tenant_id de outro tenant -> 403 Forbidden
    resp_idor_body = client.post(
        "/api/v1/export-compare-pdf",
        json={"companies": [{"company_name": "Invasora", "tenant_id": t2_id}]},
        headers=headers,
    )
    assert resp_idor_body.status_code == 403

    # 4. Validação de erro 400 quando menos de 2 empresas são fornecidas
    resp_insufficient = client.post(
        "/api/v1/export-compare-pdf",
        json={"companies": [comp1]},
        headers=headers,
    )
    assert resp_insufficient.status_code == 400
    assert "pelo menos 2 empresas" in resp_insufficient.get_json().get("error", "")

    # 5. Isolamento em _find_company_payload_for_pdf
    from app.routes.api import _find_company_payload_for_pdf
    with app.app_context():
        # Tenant 1 não enxerga busca do Tenant 2
        assert _find_company_payload_for_pdf("CONFIDENCIAL BETA LTDA", tenant_id=t1_id) is None
        # Tenant 2 enxerga normalmente
        assert _find_company_payload_for_pdf("CONFIDENCIAL BETA LTDA", tenant_id=t2_id) is not None


def test_pdf_export_weasyprint_mock_and_fallback(monkeypatch):
    """
    Testa explicitamente os dois caminhos:
    1. Renderização com WeasyPrint (quando as bibliotecas nativas estão disponíveis)
    2. Fallback gracioso para ReportLab (quando WeasyPrint levanta OSError ou ImportError)
    """
    payload = _sample_bradesco_payload()

    # Cenário 1: WeasyPrint bem-sucedido
    mock_weasyprint_pdf = b"%PDF-1.7-weasyprint-output-success"
    mock_html_instance = MagicMock()
    mock_html_instance.write_pdf.return_value = mock_weasyprint_pdf
    mock_html_cls = MagicMock(return_value=mock_html_instance)

    import sys
    mock_weasy_mod = MagicMock()
    mock_weasy_mod.HTML = mock_html_cls
    monkeypatch.setitem(sys.modules, "weasyprint", mock_weasy_mod)

    pdf_out = generate_company_report_pdf(payload)
    assert pdf_out == mock_weasyprint_pdf
    assert mock_html_cls.called

    # Cenário 2: WeasyPrint falha com OSError (ex: libgobject não encontrada no Windows)
    mock_html_cls.side_effect = OSError("cannot load library 'libgobject-2.0-0'")
    pdf_fallback = generate_company_report_pdf(payload)
    # Deve executar o fallback para ReportLab
    assert pdf_fallback.startswith(b"%PDF-")
    assert pdf_fallback != mock_weasyprint_pdf


def test_pdf_export_memory_profile_under_render_limits():
    """
    Perfil de memória: gera 3 PDFs consecutivos (individuais e comparativos)
    e valida que o pico alocado permanece estritamente abaixo do teto de 420 MB
    (Render Free 512 MB).
    """
    payload = _sample_bradesco_payload()
    tracemalloc.start()
    try:
        for _ in range(3):
            # Gera individual
            p1 = generate_company_report_pdf(payload)
            assert len(p1) > 0

            # Gera comparativo
            p2 = generate_comparative_report_pdf([payload, payload])
            assert len(p2) > 0

        _current, peak = tracemalloc.get_traced_memory()
        # 420 MB em bytes
        max_limit_bytes = 420 * 1024 * 1024
        assert peak < max_limit_bytes, f"Pico de memória ({peak / 1024 / 1024:.2f} MB) excedeu o teto de 420 MB."
    finally:
        tracemalloc.stop()


def test_pdf_svg_charts_generation():
    """
    Testa a geração dos gráficos SVG inline:
    - Simplex de desfechos (100%)
    - Inferência Bayesiana vs. Súmula 331 (com e sem bloqueio)
    - Barras comparativas multi-empresa
    """
    # 1. Simplex de desfechos
    empty_svg = generate_outcomes_simplex_svg({})
    assert "<svg" in empty_svg
    assert "Nenhum processo" in empty_svg

    data_simplex = {"condenacoes": 40, "acordos": 30, "improcedentes": 20, "em_aberto": 10}
    simplex_svg = generate_outcomes_simplex_svg(data_simplex)
    assert "<svg" in simplex_svg
    assert "xmlns=\"http://www.w3.org/2000/svg\"" in simplex_svg
    assert "Condenações: 40 (40.0%)" in simplex_svg
    assert "Acordos: 30 (30.0%)" in simplex_svg

    # 2. Bayes vs Súmula 331
    bayes_normal = generate_bayes_vs_sumula331_svg(score_bayes=45.0, risco_sumula=35.0, is_blocked=False)
    assert "<svg" in bayes_normal
    assert "45.0%" in bayes_normal
    assert "35.0%" in bayes_normal

    bayes_blocked = generate_bayes_vs_sumula331_svg(score_bayes=45.0, risco_sumula=80.0, is_blocked=True)
    assert "<svg" in bayes_blocked
    assert "REGRA DE BLOQUEIO DE PREVISÃO ATIVA" in bayes_blocked
    assert "0.0% (Bloqueado)" in bayes_blocked

    # 3. Barras comparativas
    comp_svg = generate_comparative_bars_svg([
        {"nome": "Empresa A", "score": 25.0, "processos": 100},
        {"nome": "Empresa B", "score": 75.0, "processos": 300},
    ])
    assert "<svg" in comp_svg
    assert "Empresa A" in comp_svg
    assert "Empresa B" in comp_svg
    assert "25.0%" in comp_svg
    assert "75.0%" in comp_svg


def test_pdf_separates_execution_phase_and_has_no_conflict_markers():
    """
    Garante que o relatório PDF e seu template HTML:
    1. Não possuem marcadores residuais de merge git;
    2. Separam rótulos de execução/cumprimento da tabela de motivos;
    3. Incluem o bloco dedicado 'Processos em Fase de Execução / Sentença Aplicada'.
    """
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader

    template_path = Path(__file__).resolve().parents[1] / "app" / "templates" / "pdf" / "report_single.html"
    raw_html = template_path.read_text(encoding="utf-8")
    assert "<<<<<<<" not in raw_html
    assert ">>>>>>>" not in raw_html

    payload = _sample_bradesco_payload()
    payload["ranking_motivos"].append({"motivo": "Execução de Sentença", "quantidade": 35, "percentual": 3.7})
    payload["condenacao_por_motivo"] = [
        {
            "motivo": "Execução / Cumprimento de Sentença",
            "total_processos": 35,
            "condenacoes": 35,
            "taxa_condenacao": 100.0,
            "faixa_variacao": "92,0% a 99,5%",
        },
        {
            "motivo": "Horas Extras / Jornada",
            "total_processos": 210,
            "condenacoes": 145,
            "taxa_condenacao": 69.0,
            "faixa_variacao": "63,5% a 74,0%",
        },
    ]

    norm = normalize_report_payload(payload)
    assert norm["execution_phase_summary"]["total_processos"] == 35
    assert all("Execução" not in r["motivo"] for r in norm["ranking_motivos"])
    assert all("Execução" not in c["motivo"] for c in norm["condenacao_por_motivo"])

    env = Environment(loader=FileSystemLoader(str(template_path.parent)))
    rendered_html = env.get_template("report_single.html").render(
        report=norm,
        econ=norm["economic"],
        comp=norm["compliance"],
        out=norm["outcomes"],
        outcomes_svg="<svg></svg>",
        bayes_svg="<svg></svg>",
        pct=lambda v: f"{v:.1f}%" if v is not None else "—",
        scan_label=lambda s: s,
    )
    assert "Processos em Fase de Execução / Sentença Aplicada" in rendered_html
    assert "35 processo(s)" in rendered_html

    pdf_bytes = generate_company_report_pdf(payload)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500

