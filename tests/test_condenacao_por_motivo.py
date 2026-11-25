import json
import re
from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.services.prediction_engine import (
    BayesianRiskPredictor,
    classify_process_canonical_motive,
    build_condenacao_por_motivo,
)


def test_multiplos_temas_e_invariante_de_soma():
    """
    Valida a invariante matemática de 100% dos processos mapeados:
    sum(item['total_processos'] for item in condenacao_por_motivo) == total_processos_mapeados.
    Nenhum corte artificial (como [:50] ou [:100]) é permitido.
    Verifica também taxas individuais coerentes e ordenação decrescente por condenações.
    """
    predictor = BayesianRiskPredictor()

    # Base de teste com 20 processos distribuídos em múltiplos temas canônicos
    processes = [
        # 6 de Verbas Rescisórias (4 condenações, 2 em aberto)
        {"primary_subject": "Rescisão do Contrato de Trabalho / Verbas Rescisórias", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Multa do Artigo 477 da CLT", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Aviso Prévio e Saldo de Salário", "sentence_outcome": "condenacoes"},
        {"primary_subject": "13º Salário e Férias Proporcionais", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Verbas Rescisórias", "sentence_outcome": "em_aberto"},
        {"primary_subject": "Rescisão Indireta", "sentence_outcome": "em_aberto"},

        # 4 de Horas Extras / Jornada (2 condenações, 2 improcedentes)
        {"primary_subject": "Horas Extras e Intervalo Intrajornada", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Banco de Horas e Sobrejornada", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Cartão de Ponto e Controle de Frequência", "sentence_outcome": "improcedentes"},
        {"primary_subject": "Trabalho Noturno e DSR", "sentence_outcome": "improcedentes"},

        # 3 de FGTS / Multa 40% (3 condenações)
        {"primary_subject": "Depósito de FGTS e Multa de 40%", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Liberação das Guias do FGTS", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Fundo de Garantia por Tempo de Serviço", "sentence_outcome": "condenacoes"},

        # 3 de Adicionais (Insalubridade / Periculosidade) (1 condenação, 2 acordos)
        {"primary_subject": "Adicional de Insalubridade em Grau Máximo", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Adicional de Periculosidade", "sentence_outcome": "acordos"},
        {"primary_subject": "Fornecimento de EPI e Insalubridade", "sentence_outcome": "acordos"},

        # 2 de Danos Morais / Assédio (0 condenações, 2 improcedentes)
        {"primary_subject": "Indenização por Dano Moral", "sentence_outcome": "improcedentes"},
        {"primary_subject": "Assédio Moral no Trabalho", "sentence_outcome": "improcedentes"},

        # 2 de Vínculo Empregatício (1 condenação, 1 em aberto)
        {"primary_subject": "Reconhecimento de Vínculo Empregatício", "sentence_outcome": "condenacoes"},
        {"primary_subject": "Anotação na CTPS e Relação de Emprego", "sentence_outcome": "em_aberto"},
    ]

    total_esperado = len(processes)
    assert total_esperado == 20

    metrics = predictor.generate_dashboard_metrics("EMPRESA MULTITEMAS S.A.", processes)
    cpm = metrics.get("condenacao_por_motivo", [])

    assert len(cpm) > 0

    # Invariante de conservação estrita de volume: exatamente 100% dos processos avaliados
    soma_processos = sum(item["total_processos"] for item in cpm)
    assert soma_processos == total_esperado, (
        f"Invariante violada: soma dos processos por motivo ({soma_processos}) "
        f"não confere com o total ({total_esperado})."
    )

    # Verifica ordenação determinística: maior número de condenações primeiro
    for i in range(len(cpm) - 1):
        assert (cpm[i]["condenacoes"], cpm[i]["total_processos"]) >= (
            cpm[i + 1]["condenacoes"],
            cpm[i + 1]["total_processos"],
        ), "Lista de condenações por motivo deve estar ordenada por condenações desc e total_processos desc"

    # Verifica cálculo coerente da taxa empírica de perda em cada motivo
    for item in cpm:
        total = item["total_processos"]
        cond = item["condenacoes"]
        taxa = item["taxa_condenacao"]
        faixa = item["faixa_variacao"]

        if cond == 0:
            assert taxa == 0.0
            assert faixa == "0,0% a 0,0%"
        else:
            taxa_calculada = round((cond / total) * 100.0, 1)
            assert taxa == taxa_calculada
            # Formato amigável em pt-BR com vírgula e percentual
            assert re.match(r"^\d+,\d% a \d+,\d%$", faixa)


def test_regra_de_bloqueio_zero_condenacoes():
    """
    Valida a Regra de Bloqueio (0.0%):
    Quando um motivo não possui condenações, a taxa retornada deve ser obrigatoriamente 0.0
    e a faixa de variação deve ser '0,0% a 0,0%'.
    """
    processes = [
        {"primary_subject": "Horas Extras e Intervalo", "sentence_outcome": "improcedentes"},
        {"primary_subject": "Horas Extras Noturnas", "sentence_outcome": "improcedentes"},
        {"primary_subject": "Jornada de Trabalho", "sentence_outcome": "acordos"},
        {"primary_subject": "Controle de Ponto", "sentence_outcome": "em_aberto"},
    ]

    cpm = build_condenacao_por_motivo(processes)
    assert len(cpm) == 1
    motivo_data = cpm[0]

    assert motivo_data["motivo"] == "Horas Extras / Jornada"
    assert motivo_data["total_processos"] == 4
    assert motivo_data["condenacoes"] == 0
    assert motivo_data["taxa_condenacao"] == 0.0
    assert motivo_data["faixa_variacao"] == "0,0% a 0,0%"


def test_fallback_seguro_assunto_nao_especificado():
    """
    Valida que processos sem assunto, com campos nulos ou sem metadados suficientes
    são agrupados de forma determinística em 'Assunto não especificado' sem quebras de execução.
    """
    processes = [
        {},  # Totalmente vazio
        {"primary_subject": None, "subjects": []},  # Nulos
        {"primary_subject": "   ", "subjects": [""]},  # Espaços em branco
        {"assuntos": []},  # Lista vazia
    ]

    for p in processes:
        assert classify_process_canonical_motive(p) == "Assunto não especificado"

    cpm = build_condenacao_por_motivo(processes)
    assert len(cpm) == 1
    assert cpm[0]["motivo"] == "Assunto não especificado"
    assert cpm[0]["total_processos"] == 4
    assert cpm[0]["condenacoes"] == 0
    assert cpm[0]["taxa_condenacao"] == 0.0
    assert cpm[0]["faixa_variacao"] == "0,0% a 0,0%"


def test_sanitizacao_lgpd_estrita_sem_vazamento_de_pii():
    """
    Valida que nenhum dado pessoal sensível (nome, CPF, documento de reclamante)
    é persistido, logado ou exposto dentro do payload de condenacao_por_motivo.
    """
    reclamante_nome = "Severino dos Ramos Pereira"
    reclamante_cpf = "098.765.432-11"

    processes = [
        {
            "primary_subject": "Verbas Rescisórias",
            "sentence_outcome": "condenacoes",
            "reclamante_nome": reclamante_nome,
            "reclamante_cpf": reclamante_cpf,
            "autor": reclamante_nome,
            "documento": "09876543211",
            "partes": [f"{reclamante_nome} (Reclamante)"],
        },
        {
            "primary_subject": "Adicional de Insalubridade",
            "sentence_outcome": "improcedentes",
            "reclamante_nome": "Maria Madalena da Silva",
            "reclamante_cpf": "123.456.789-00",
        },
    ]

    cpm = build_condenacao_por_motivo(processes)
    cpm_json = json.dumps(cpm, ensure_ascii=False)

    # Garante ausência total de identificadores de pessoas físicas
    assert reclamante_nome not in cpm_json
    assert reclamante_cpf not in cpm_json
    assert "Maria Madalena" not in cpm_json
    assert "123.456.789-00" not in cpm_json
    assert "09876543211" not in cpm_json

    # Garante estrita conformidade de chaves do contrato JSON
    allowed_keys = {"motivo", "total_processos", "condenacoes", "taxa_condenacao", "faixa_variacao"}
    for item in cpm:
        assert set(item.keys()) == allowed_keys


def test_payload_endpoints_overview_history_e_export_pdf(client):
    """
    Valida que os endpoints /overview, /history/<id> e /export-pdf
    retornam condenacao_por_motivo corretamente formatado no contrato de resposta.
    """
    # 1. Registra busca no banco com condenacao_por_motivo preenchido
    cpm_data = [
        {
            "motivo": "Verbas Rescisórias",
            "total_processos": 20,
            "condenacoes": 14,
            "taxa_condenacao": 70.0,
            "faixa_variacao": "52,0% a 84,5%",
        },
        {
            "motivo": "Horas Extras / Jornada",
            "total_processos": 10,
            "condenacoes": 0,
            "taxa_condenacao": 0.0,
            "faixa_variacao": "0,0% a 0,0%",
        },
    ]

    search = CompanySearch(
        company_name="TESTE FORNECEDOR INTEGRACAO S.A.",
        normalized_name="TESTE FORNECEDOR INTEGRACAO SA",
        total_processes=30,
        condenacao_por_motivo=cpm_data,
        outcomes_summary={"condenacoes": 14, "acordos": 6, "improcedentes": 10, "em_aberto": 0},
        ranking_motivos=[{"motivo": "Verbas Rescisórias", "quantidade": 20}],
        overall_risk_score=25.0,
        scan_status="COMPLETED",
        last_checked_at=get_utc_now(),
    )
    db.session.add(search)
    db.session.commit()
    search_id = search.id

    # 2. Teste do endpoint /history/<id>
    res_history = client.get(f"/api/v1/history/{search_id}")
    assert res_history.status_code == 200
    data_hist = res_history.get_json()
    assert "condenacao_por_motivo" in data_hist
    assert len(data_hist["condenacao_por_motivo"]) == 2
    assert data_hist["condenacao_por_motivo"][0]["motivo"] == "Verbas Rescisórias"
    assert data_hist["condenacao_por_motivo"][0]["taxa_condenacao"] == 70.0
    assert data_hist["condenacao_por_motivo"][0]["faixa_variacao"] == "52,0% a 84,5%"

    # 3. Teste do endpoint /overview (alias de consulta)
    res_overview = client.get(
        "/api/v1/overview",
        query_string={"name": "TESTE FORNECEDOR INTEGRACAO S.A."},
    )
    assert res_overview.status_code == 200
    data_overview = res_overview.get_json()
    assert "condenacao_por_motivo" in data_overview
    assert len(data_overview["condenacao_por_motivo"]) == 2

    # 4. Teste do endpoint /export-pdf via POST (contexto com condenacao_por_motivo)
    pdf_payload = {
        "empresa": "TESTE FORNECEDOR INTEGRACAO S.A.",
        "total_processos": 30,
        "desfechos_sentencas": {"condenacoes": 14, "acordos": 6, "improcedentes": 10, "em_aberto": 0},
        "condenacao_por_motivo": cpm_data,
        "score_risco_terceirizacao": 25.0,
    }
    res_pdf_post = client.post(
        "/api/v1/export-pdf",
        json=pdf_payload,
        content_type="application/json",
    )
    assert res_pdf_post.status_code == 200
    assert res_pdf_post.content_type == "application/pdf"
    assert res_pdf_post.data.startswith(b"%PDF")

    # 5. Teste do endpoint /export-pdf via GET buscando pelo registro existente
    res_pdf_get = client.get(f"/api/v1/export-pdf?search_id={search_id}")
    assert res_pdf_get.status_code == 200
    assert res_pdf_get.content_type == "application/pdf"
    assert res_pdf_get.data.startswith(b"%PDF")
