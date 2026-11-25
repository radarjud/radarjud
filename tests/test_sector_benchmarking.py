# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.services.prediction_engine import build_sector_benchmark


def test_distinct_profiles_ranking_and_relative_risk():
    """
    Critério 1:
    Valida perfis distintos (R_sub 1.4%, 18.6% e 61.8%):
    - Ordenação exata: 1º (1.4%) -> 2º (18.6%) -> 3º (61.8%)
    - Fator de risco relativo: round(61.8 / 1.4, 2) == 44.14 (~44.14x)
    - Faixas de decisão: APROVAR, CAUTELA, REPROVAR
    - Gráfico comparativo e parecer executivo
    """
    payload_a = {
        "empresa": "Fornecedora Verde Limpa LTDA",
        "cnpj": "11.111.111/0001-11",
        "capital_social": "R$ 50.000.000,00",
        "capital_social_num": 50000000.0,
        "risco_subsidiario": 1.4,
        "probabilidade_condenacao": 12.0,
        "cenario": "CENARIO_A",
        "ic_90": {"inferior": 8.0, "superior": 16.0},
    }
    payload_b = {
        "empresa": "Fornecedora Amarela Cautela S/A",
        "cnpj": "22.222.222/0001-22",
        "capital_social": "R$ 5.000.000,00",
        "capital_social_num": 5000000.0,
        "risco_subsidiario": 18.6,
        "probabilidade_condenacao": 32.5,
        "cenario": "CENARIO_A",
        "ic_90": {"inferior": 25.0, "superior": 40.0},
    }
    payload_c = {
        "empresa": "Fornecedora Vermelha Crítica ME",
        "cnpj": "33.333.333/0001-33",
        "capital_social": "R$ 100.000,00",
        "capital_social_num": 100000.0,
        "risco_subsidiario": 61.8,
        "probabilidade_condenacao": 72.0,
        "cenario": "CENARIO_B",
        "ic_90": {"inferior": 60.0, "superior": 84.0},
    }

    # Enviando fora de ordem propositalmente
    result = build_sector_benchmark([payload_c, payload_a, payload_b])

    ranked = result["empresas_ranqueadas"]
    assert len(ranked) == 3

    # 1º Lugar: Fornecedora Verde (1.4% -> APROVAR)
    assert ranked[0]["empresa"] == "Fornecedora Verde Limpa LTDA"
    assert ranked[0]["posicao_ranking"] == 1
    assert ranked[0]["risco_subsidiario"] == 1.4
    assert ranked[0]["faixa_decisao"] == "APROVAR"
    assert "Aprovar fornecedor" in ranked[0]["recomendacao"]

    # 2º Lugar: Fornecedora Amarela (18.6% -> CAUTELA)
    assert ranked[1]["empresa"] == "Fornecedora Amarela Cautela S/A"
    assert ranked[1]["posicao_ranking"] == 2
    assert ranked[1]["risco_subsidiario"] == 18.6
    assert ranked[1]["faixa_decisao"] == "CAUTELA"
    assert "10%" in ranked[1]["recomendacao"]

    # 3º Lugar: Fornecedora Vermelha (61.8% -> REPROVAR)
    assert ranked[2]["empresa"] == "Fornecedora Vermelha Crítica ME"
    assert ranked[2]["posicao_ranking"] == 3
    assert ranked[2]["risco_subsidiario"] == 61.8
    assert ranked[2]["faixa_decisao"] == "REPROVAR"

    # Fator de Risco Relativo: 61.8 / 1.4 == 44.14x
    resumo = result["resumo_setorial"]
    assert resumo["fator_risco_relativo"] == 44.14
    assert "44.14x" in resumo["fator_risco_relativo_texto"]
    assert resumo["melhor_opcao"]["empresa"] == "Fornecedora Verde Limpa LTDA"
    assert resumo["pior_opcao"]["empresa"] == "Fornecedora Vermelha Crítica ME"

    # Gráfico comparativo
    grafico = result["grafico_comparativo"]
    assert grafico["labels"] == [
        "Fornecedora Verde Limpa LTDA",
        "Fornecedora Amarela Cautela S/A",
        "Fornecedora Vermelha Crítica ME",
    ]
    assert grafico["series"]["risco_subsidiario_sumula_331"] == [1.4, 18.6, 61.8]
    assert grafico["series"]["probabilidade_condenacao_bayes"] == [12.0, 32.5, 72.0]

    # Parecer Comparativo (Contrato US-12)
    parecer = result["parecer_comparativo"]
    assert parecer["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"
    assert "Fornecedora Verde Limpa LTDA" in parecer["recomendacao_compra"]
    assert parecer["alerta_critico"] is not None
    assert "Fornecedora Vermelha Crítica ME" in parecer["alerta_critico"]
    assert "60.4 pontos percentuais" in parecer["economia_potencial_texto"]


def test_tiebreaker_criteria_capital_and_conviction_prob():
    """
    Critério 2:
    Valida critérios de desempate:
    1. Mesmo risco subsidiário -> desempata por MAIOR capital social
    2. Mesmo risco subsidiário E mesmo capital social -> desempata por MENOR probabilidade de condenação
    """
    # Caso 1: Mesmo risco subsidiário (12.5%), capital diferente
    comp_cap_menor = {
        "empresa": "Fornecedora Menor Capital",
        "cnpj": "10.000.000/0001-01",
        "capital_social_num": 1_000_000.0,
        "risco_subsidiario": 12.5,
        "probabilidade_condenacao": 25.0,
    }
    comp_cap_maior = {
        "empresa": "Fornecedora Maior Capital",
        "cnpj": "20.000.000/0001-02",
        "capital_social_num": 80_000_000.0,
        "risco_subsidiario": 12.5,
        "probabilidade_condenacao": 25.0,
    }

    res1 = build_sector_benchmark([comp_cap_menor, comp_cap_maior])
    ranked1 = res1["empresas_ranqueadas"]
    assert ranked1[0]["empresa"] == "Fornecedora Maior Capital"
    assert ranked1[1]["empresa"] == "Fornecedora Menor Capital"

    # Caso 2: Mesmo risco subsidiário E mesmo capital, probabilidade diferente
    comp_prob_maior = {
        "empresa": "Fornecedora Maior P_Cond",
        "cnpj": "30.000.000/0001-03",
        "capital_social_num": 10_000_000.0,
        "risco_subsidiario": 15.0,
        "probabilidade_condenacao": 45.0,
    }
    comp_prob_menor = {
        "empresa": "Fornecedora Menor P_Cond",
        "cnpj": "40.000.000/0001-04",
        "capital_social_num": 10_000_000.0,
        "risco_subsidiario": 15.0,
        "probabilidade_condenacao": 20.0,
    }

    res2 = build_sector_benchmark([comp_prob_maior, comp_prob_menor])
    ranked2 = res2["empresas_ranqueadas"]
    assert ranked2[0]["empresa"] == "Fornecedora Menor P_Cond"
    assert ranked2[1]["empresa"] == "Fornecedora Maior P_Cond"


def test_zero_conviction_rule_preserves_zero_without_div_zero():
    """
    Critério 3:
    Empresa com 0 condenações mantém 0.0% no comparativo sem distorção e sem divisão por zero
    no cálculo do fator de risco relativo.
    """
    comp_zero_1 = {
        "empresa": "Empresa Totalmente Sem Condenações 1",
        "cnpj": "00.111.222/0001-33",
        "total_processos": 8,
        "outcomes_summary": {"condenacoes": 0, "acordos": 2, "improcedentes": 6},
        "risco_subsidiario": 0.0,
        "probabilidade_condenacao": 0.0,
        "previsao_bloqueada": True,
        "capital_social_num": 10_000_000.0,
    }
    comp_zero_2 = {
        "empresa": "Empresa Totalmente Sem Condenações 2",
        "cnpj": "00.333.444/0001-55",
        "total_processos": 4,
        "outcomes_summary": {"condenacoes": 0, "acordos": 1, "improcedentes": 3},
        "risco_subsidiario": 0.0,
        "probabilidade_condenacao": 0.0,
        "previsao_bloqueada": True,
        "capital_social_num": 5_000_000.0,
    }

    result = build_sector_benchmark([comp_zero_1, comp_zero_2])
    ranked = result["empresas_ranqueadas"]
    assert ranked[0]["risco_subsidiario"] == 0.0
    assert ranked[0]["probabilidade_condenacao"] == 0.0
    assert ranked[0]["ic_90"]["inferior"] == 0.0
    assert ranked[0]["ic_90"]["superior"] == 0.0
    assert ranked[0]["faixa_decisao"] == "APROVAR"

    # Fator de risco relativo quando todas têm 0.0% é estritamente 1.0
    assert result["resumo_setorial"]["fator_risco_relativo"] == 1.0
    assert "0.0%" in result["resumo_setorial"]["fator_risco_relativo_texto"]


def test_endpoint_limit_validations_and_duplicates(client):
    """
    Critério 4:
    Validações obrigatórias no endpoint POST /api/v1/compare-companies:
    - Menos de 2 empresas -> HTTP 400
    - Mais de 5 empresas -> HTTP 400
    - Duplicatas que resultem em < 2 distintas -> HTTP 400
    """
    # 1 empresa
    res_1 = client.post("/api/v1/compare-companies", json={"search_ids": [101]})
    assert res_1.status_code == 400
    data_1 = res_1.get_json()
    assert "entre 2 e 5 empresas" in data_1["error"]

    # 6 empresas
    res_6 = client.post(
        "/api/v1/compare-companies",
        json={"search_ids": [101, 102, 103, 104, 105, 106]},
    )
    assert res_6.status_code == 400
    data_6 = res_6.get_json()
    assert "limite máximo" in data_6["error"]

    # Duplicatas (mesmo ID ou mesmo CNPJ)
    res_dup_id = client.post(
        "/api/v1/compare-companies",
        json={"search_ids": [101, 101]},
    )
    assert res_dup_id.status_code == 400
    assert "distintas" in res_dup_id.get_json()["error"]

    res_dup_cnpj = client.post(
        "/api/v1/compare-companies",
        json={"cnpjs": ["11.222.333/0001-44", "11.222.333/0001-44"]},
    )
    assert res_dup_cnpj.status_code == 400
    assert "distintas" in res_dup_cnpj.get_json()["error"]


def test_parity_invariant_with_overview_via_test_client(client, app):
    """
    Critério 5:
    Invariante de paridade: os scores (probabilidade_condenacao, risco_subsidiario,
    ic_90, cenario) retornados em /compare-companies devem ser 100% IDÊNTICOS aos
    retornados pelo /history/:id (que alimenta o /overview) para cada empresa.
    """
    with app.app_context():
        # Cria duas buscas simuladas no banco de testes
        s1 = CompanySearch(
            company_name="Empresa Paridade Alpha LTDA",
            normalized_name="EMPRESA PARIDADE ALPHA LTDA",
            total_processes=20,
            overall_risk_score=6.4,
            outcomes_summary={"condenacoes": 2, "acordos": 6, "improcedentes": 12},
            economic_standing={
                "cenario_tipo": "CENARIO_A",
                "capital_social_num": 30_000_000.0,
                "capital_social_formatado": "R$ 30.000.000,00",
                "cnae_codigo": "6201-5/00",
                "cnae_descricao": "Desenvolvimento de programas de computador",
            },
            predictions_summary=[
                {
                    "motivo": "Horas Extras",
                    "probabilidade_condenacao_pct": 18.5,
                    "ic_90_inferior_pct": 14.0,
                    "ic_90_superior_pct": 23.0,
                }
            ],
            scan_status="COMPLETED",
            last_checked_at=get_utc_now(),
        )
        s2 = CompanySearch(
            company_name="Empresa Paridade Beta S/A",
            normalized_name="EMPRESA PARIDADE BETA S/A",
            total_processes=40,
            overall_risk_score=48.2,
            outcomes_summary={"condenacoes": 18, "acordos": 12, "improcedentes": 10},
            economic_standing={
                "cenario_tipo": "CENARIO_B",
                "capital_social_num": 800_000.0,
                "capital_social_formatado": "R$ 800.000,00",
                "cnae_codigo": "6201-5/00",
                "cnae_descricao": "Desenvolvimento de programas de computador",
            },
            predictions_summary=[
                {
                    "motivo": "Adicional de Periculosidade",
                    "probabilidade_condenacao_pct": 55.0,
                    "ic_90_inferior_pct": 47.0,
                    "ic_90_superior_pct": 63.0,
                }
            ],
            scan_status="COMPLETED",
            last_checked_at=get_utc_now(),
        )
        db.session.add_all([s1, s2])
        db.session.commit()
        id1, id2 = s1.id, s2.id

    # 1. Consulta detalhes individuais via /history/<id> (overview individual)
    resp_ind1 = client.get(f"/api/v1/history/{id1}")
    assert resp_ind1.status_code == 200
    ov1 = resp_ind1.get_json()

    resp_ind2 = client.get(f"/api/v1/history/{id2}")
    assert resp_ind2.status_code == 200
    ov2 = resp_ind2.get_json()

    # 2. Consulta comparativa setorial via /compare-companies
    resp_comp = client.post("/api/v1/compare-companies", json={"search_ids": [id1, id2]})
    assert resp_comp.status_code == 200
    comp_data = resp_comp.get_json()

    by_name = {c["empresa"]: c for c in comp_data["empresas_ranqueadas"]}

    c1 = by_name["Empresa Paridade Alpha LTDA"]
    c2 = by_name["Empresa Paridade Beta S/A"]

    # Invariante de Paridade Absoluta:
    assert c1["risco_subsidiario"] == ov1["score_risco_terceirizacao"]
    assert c1["probabilidade_condenacao"] == ov1["probabilidade_condenacao_geral_pct"]
    assert c1["cenario"] == ov1["idoneidade_economica"]["cenario_tipo"]

    assert c2["risco_subsidiario"] == ov2["score_risco_terceirizacao"]
    assert c2["probabilidade_condenacao"] == ov2["probabilidade_condenacao_geral_pct"]
    assert c2["cenario"] == ov2["idoneidade_economica"]["cenario_tipo"]


def test_sector_validation_cnae_same_and_different():
    """
    Critério 6:
    Validação setorial (CNAE):
    - Mesmo CNAE: mesmo_segmento == True, alerta_setorial == None, cnae_comum preenchido
    - CNAEs distintos: mesmo_segmento == False, alerta_setorial com texto explicativo
    """
    # 1. Empresas com mesmo segmento econômico
    c1 = {
        "empresa": "Construtora A",
        "cnpj": "11.111.111/0001-11",
        "risco_subsidiario": 10.0,
        "cnae_codigo": "4120-4/00",
        "cnae_descricao": "Construção de edifícios",
    }
    c2 = {
        "empresa": "Construtora B",
        "cnpj": "22.222.222/0001-22",
        "risco_subsidiario": 15.0,
        "cnae_codigo": "41.20-4/00",
        "cnae_descricao": "Construção de edifícios",
    }
    res_same = build_sector_benchmark([c1, c2])
    resumo_same = res_same["resumo_setorial"]
    assert resumo_same["mesmo_segmento"] is True
    assert resumo_same["alerta_setorial"] is None
    assert resumo_same["cnae_comum"] is not None
    assert "Construção" in resumo_same["cnae_comum"]["descricao"]

    # 2. Empresas com segmentos econômicos distintos
    c3 = {
        "empresa": "Empresa de TI C",
        "cnpj": "33.333.333/0001-33",
        "risco_subsidiario": 20.0,
        "cnae_codigo": "6201-5/00",
        "cnae_descricao": "Desenvolvimento de software",
    }
    res_diff = build_sector_benchmark([c1, c3])
    resumo_diff = res_diff["resumo_setorial"]
    assert resumo_diff["mesmo_segmento"] is False
    assert resumo_diff["cnae_comum"] is None
    assert resumo_diff["alerta_setorial"] is not None
    assert "segmentos econômicos distintos" in resumo_diff["alerta_setorial"]


def test_security_sanitization_and_defensive_tenant_isolation(client, app, monkeypatch):
    """
    Critério 7:
    Isolamento e Segurança:
    - Sanitização de entrada rejeita ataques maliciosos (XSS, SQLi)
    - Isolamento multitenant defensivo (Anti-IDOR) com verificação de g.tenant_id
    """
    # Sanitização: termos com tags script ou caracteres proibidos
    res_bad = client.post(
        "/api/v1/compare-companies",
        json={"cnpjs": ["<script>alert(1)</script>", "DROP TABLE users;--"]},
    )
    assert res_bad.status_code == 400

    # Multi-tenant: se CompanySearch tiver tenant_id e tenant requisitante for diferente, bloqueia com 403
    with app.app_context():
        s_tenant = CompanySearch(
            company_name="Empresa Tenant Isolada",
            normalized_name="EMPRESA TENANT ISOLADA",
            total_processes=5,
            overall_risk_score=15.0,
            scan_status="COMPLETED",
        )
        s_other = CompanySearch(
            company_name="Empresa Outra LTDA",
            normalized_name="EMPRESA OUTRA LTDA",
            total_processes=2,
            overall_risk_score=10.0,
            scan_status="COMPLETED",
        )
        db.session.add_all([s_tenant, s_other])
        db.session.commit()
        tenant_search_id = s_tenant.id
        other_search_id = s_other.id

    # Simula atributo tenant_id presente no modelo (US-01) com tenant 999
    monkeypatch.setattr(CompanySearch, "tenant_id", 999, raising=False)

    # Requisição com tenant diferente (123 != 999) deve retornar 403 (Forbidden)
    res_cross_tenant = client.post(
        "/api/v1/compare-companies",
        json={"search_ids": [tenant_search_id, other_search_id]},
        headers={"X-Tenant-ID": "123"},
    )
    assert res_cross_tenant.status_code == 403
    assert "Acesso não autorizado" in res_cross_tenant.get_json()["error"]


def test_mixed_payload_inputs_combining_ids_cnpjs_and_names(client, app):
    """
    Valida suporte a payloads mistos combinando simultaneamente:
    - Empresa por search_id (banco de dados)
    - Empresa por CNPJ pré-computado ou objeto direto
    - Empresa por objeto com métricas e Razão Social
    """
    with app.app_context():
        s_db = CompanySearch(
            company_name="Empresa Cadastrada Banco LTDA",
            normalized_name="EMPRESA CADASTRADA BANCO LTDA",
            total_processes=10,
            overall_risk_score=5.0,
            outcomes_summary={"condenacoes": 1, "acordos": 3, "improcedentes": 6},
            economic_standing={
                "cenario_tipo": "CENARIO_A",
                "capital_social_num": 20_000_000.0,
                "capital_social_formatado": "R$ 20.000.000,00",
            },
            scan_status="COMPLETED",
            last_checked_at=get_utc_now(),
        )
        db.session.add(s_db)
        db.session.commit()
        db_id = s_db.id

    payload_mixed = {
        "items": [
            {"search_id": db_id},
            {
                "empresa": "Fornecedor Objeto Direto LTDA",
                "cnpj": "44.555.666/0001-77",
                "risco_subsidiario": 22.0,
                "capital_social_num": 5_000_000.0,
                "probabilidade_condenacao": 30.0,
            },
            {
                "empresa": "Fornecedor Terceiro Cautela S/A",
                "cnpj": "88.999.000/0001-11",
                "risco_subsidiario": 45.0,
                "capital_social_num": 500_000.0,
                "probabilidade_condenacao": 60.0,
            },
        ]
    }

    res = client.post("/api/v1/compare-companies", json=payload_mixed)
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["empresas_ranqueadas"]) == 3
    ranks = [c["posicao_ranking"] for c in data["empresas_ranqueadas"]]
    assert ranks == [1, 2, 3]
    assert data["empresas_ranqueadas"][0]["empresa"] == "Empresa Cadastrada Banco LTDA"


def test_endpoint_error_handling_edge_cases(client):
    """
    Cobre cenários defensivos adicionais de erro no endpoint /compare-companies:
    - Payload JSON inválido (não-objeto ou malformado)
    - Lista vazia de itens
    - Search_id inexistente no banco (retorno 404)
    - Itens vazios ou nulos
    """
    # 1. Payload não-dicionário
    res_list = client.post("/api/v1/compare-companies", json=["item1", "item2"])
    assert res_list.status_code == 400
    assert "Payload JSON inválido" in res_list.get_json()["error"]

    # 2. Lista vazia
    res_empty = client.post("/api/v1/compare-companies", json={"items": []})
    assert res_empty.status_code == 400
    assert "entre 2 e 5 empresas" in res_empty.get_json()["error"]

    # 3. Search ID inexistente
    res_404 = client.post(
        "/api/v1/compare-companies",
        json={"search_ids": [99999999, 88888888]},
    )
    assert res_404.status_code == 404
    assert "não encontrada" in res_404.get_json()["error"]

    # 4. Item com identificador vazio
    res_blank = client.post(
        "/api/v1/compare-companies",
        json={"items": [{"cnpj": ""}, {"company_name": "Empresa Teste"}]},
    )
    assert res_blank.status_code == 400
    assert "vazio ou sem identificador" in res_blank.get_json()["error"]


def test_scenario_a_solvent_large_company_approved_not_rejected():
    """
    Garante que empresas com solvência comprovada (Cenário A) e capital social expressivo
    (ex: Banco Bradesco com > R$ 100 Bilhões) são classificadas como 'APROVAR' (ou Cautela)
    e NUNCA como 'REPROVAR', respeitando a Súmula 331 IV TST (risco de calote mitigado).
    """
    bradesco_payload = {
        "empresa": "BANCO BRADESCO S.A.",
        "cnpj": "60.746.948/0001-12",
        "total_processos": 1015,
        "probabilidade_condenacao": 86.2,
        "score_risco_terceirizacao": 59.7,
        "overall_risk_score": 59.7,
        "idoneidade_economica": {
            "cenario_tipo": "CENARIO_A",
            "capital_social_num": 117_847_056_921.22,
            "capital_social_formatado": "R$ 117.847.056.921,22",
            "probabilidade_insolvencia": 0.8,
            "risco_calote_subsidiario": 0.7,
            "indice_exposicao_financeira": 16.3,
            "situacao_cadastral": "ATIVA",
        },
    }

    empresa_critica = {
        "empresa": "Prestadora Insolvente ME",
        "cnpj": "99.999.999/0001-99",
        "total_processos": 50,
        "probabilidade_condenacao": 70.0,
        "risco_subsidiario": 60.0,
        "capital_social_num": 10_000.0,
        "cenario": "CENARIO_B",
    }

    res = build_sector_benchmark([bradesco_payload, empresa_critica])
    ranked = res["empresas_ranqueadas"]

    by_name = {c["empresa"]: c for c in ranked}
    brad = by_name["BANCO BRADESCO S.A."]
    assert brad["faixa_decisao"] == "APROVAR"
    assert brad["posicao_ranking"] == 1
    assert brad["risco_subsidiario"] <= 1.0
    assert "Aprovar fornecedor" in brad["recomendacao"]
    assert "REPROVAR" not in brad["faixa_decisao"]


def test_sector_benchmark_preserves_logo_status_and_identifiers():
    """
    Verifica se o benchmarking propaga logo, situacao_cadastral e search_id
    para uso no frontend e exportação de relatórios.
    """
    comp1 = {
        "empresa": "Fornecedora Alpha LTDA",
        "cnpj": "12.345.678/0001-90",
        "capital_social_num": 5_000_000.0,
        "risco_subsidiario": 5.0,
        "probabilidade_condenacao": 15.0,
        "search_id": 42,
        "logo": {"logo_url": "https://img.logo.dev/alpha.com", "is_verified_domain": True},
        "idoneidade_economica": {
            "situacao_cadastral": "ATIVA",
            "cenario_tipo": "CENARIO_A",
        },
    }
    comp2 = {
        "empresa": "Fornecedora Beta S.A.",
        "cnpj": "98.765.432/0001-10",
        "capital_social_num": 1_000_000.0,
        "risco_subsidiario": 8.0,
        "probabilidade_condenacao": 25.0,
        "search_id": 43,
        "idoneidade_economica": {
            "situacao_cadastral": "SUSPENSA",
            "cenario_tipo": "CENARIO_A",
        },
    }

    res = build_sector_benchmark([comp1, comp2])
    ranked = res["empresas_ranqueadas"]
    assert len(ranked) == 2

    by_name = {c["empresa"]: c for c in ranked}
    alpha = by_name["Fornecedora Alpha LTDA"]
    beta = by_name["Fornecedora Beta S.A."]

    assert alpha["logo"]["logo_url"] == "https://img.logo.dev/alpha.com"
    assert alpha["situacao_cadastral"] == "ATIVA"
    assert alpha["search_id"] == 42

    assert beta["situacao_cadastral"] == "SUSPENSA"
    assert beta["search_id"] == 43



