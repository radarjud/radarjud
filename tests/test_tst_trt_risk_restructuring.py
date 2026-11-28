import pytest
from app.services.prediction_engine import (
    BayesianRiskPredictor,
    calculate_weighted_subsidiary_risk_and_instances,
    build_condenacao_por_motivo,
    is_tst_process,
    is_process_finalized_tst,
    is_process_execution_phase,
)
from app.services.ai_analyzer import LegalAIAnalyzer


def test_subsidiary_risk_weights_and_definitiveness():
    predictor = BayesianRiskPredictor()

    # Cenário 1: 10 condenações definitivas (execução no TRT) e 10 improcedentes
    # Tefetiva = (1.00 * 10) / (10 + 10) * 100 = 50.0%
    procs_def = [
        {"numero_processo": f"000100{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "condenacoes", "fase_processual": "EXECUCAO", "primary_subject": "Horas Extras"}
        for i in range(10)
    ] + [
        {"numero_processo": f"000200{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "improcedentes", "fase_processual": "CONHECIMENTO", "primary_subject": "Horas Extras"}
        for i in range(10)
    ]
    res_def = calculate_weighted_subsidiary_risk_and_instances(procs_def)
    assert res_def["risco_subsidiario"]["percentual"] == 50.0
    assert res_def["risco_subsidiario"]["faixa_exposicao"] == "MODERADA"
    assert res_def["risco_subsidiario"]["rotulo_exposicao"] == "Exposição Moderada — Blindagem Contratual Recomendada"

    # Cenário 2: 10 condenações ainda recorríveis no TRT e 10 improcedentes
    # Tefetiva = (0.85 * 10) / (10 + 10) * 100 = 42.5%
    procs_rec = [
        {"numero_processo": f"000300{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "condenacoes", "fase_processual": "CONHECIMENTO", "primary_subject": "Horas Extras"}
        for i in range(10)
    ] + [
        {"numero_processo": f"000400{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "improcedentes", "fase_processual": "CONHECIMENTO", "primary_subject": "Horas Extras"}
        for i in range(10)
    ]
    res_rec = calculate_weighted_subsidiary_risk_and_instances(procs_rec)
    assert res_rec["risco_subsidiario"]["percentual"] == 42.5
    # Condenação definitiva pesa mais que recorrível (50.0% > 42.5%)
    assert res_def["risco_subsidiario"]["percentual"] > res_rec["risco_subsidiario"]["percentual"]

    # Cenário 3: 10 acordos e 10 improcedentes
    # Tefetiva = (0.55 * 10) / (10 + 10) * 100 = 27.5%
    procs_acordo = [
        {"numero_processo": f"000500{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "acordos", "fase_processual": "CONHECIMENTO", "primary_subject": "Horas Extras"}
        for i in range(10)
    ] + [
        {"numero_processo": f"000600{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "improcedentes", "fase_processual": "CONHECIMENTO", "primary_subject": "Horas Extras"}
        for i in range(10)
    ]
    res_acordo = calculate_weighted_subsidiary_risk_and_instances(procs_acordo)
    assert res_acordo["risco_subsidiario"]["percentual"] == 27.5
    assert res_acordo["risco_subsidiario"]["faixa_exposicao"] == "BAIXA"
    assert res_acordo["risco_subsidiario"]["rotulo_exposicao"] == "Baixa Exposição Trabalhista"


def test_capital_social_independence():
    """Valida que o capital social ou solvência tem peso ZERO e não altera o Risco Subsidiário em 0.01%."""
    predictor = BayesianRiskPredictor()
    procs = [
        {"numero_processo": f"000100{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "condenacoes", "primary_subject": "FGTS / Multa 40%"}
        for i in range(5)
    ] + [
        {"numero_processo": f"000200{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "improcedentes", "primary_subject": "FGTS / Multa 40%"}
        for i in range(5)
    ]

    m_low = predictor.build_dashboard_metrics("EMPRESA TESTE", procs, capital_info={"capital_ativo_formatado": "R$ 10.000,00"})
    m_high = predictor.build_dashboard_metrics("EMPRESA TESTE", procs, capital_info={"capital_ativo_formatado": "R$ 10.000.000.000,00"})

    assert m_low["risco_subsidiario"]["percentual"] == m_high["risco_subsidiario"]["percentual"]
    assert m_low["score_risco_terceirizacao"] == m_high["score_risco_terceirizacao"]
    assert m_low["overall_risk_score"] == m_high["overall_risk_score"]


def test_tst_aggravating_factor_sample_protection():
    """Valida que agravante do TST exige N_TST >= 10 processos julgados no TST."""
    # Menos de 10 no TST (amostra reduzida): agravante NÃO pode ser aplicado
    procs_small_tst = [
        {"numero_processo": f"000100{i}-11.2023.5.00.0001", "tribunal": "TST", "sentence_outcome": "condenacoes", "primary_subject": "Horas Extras"}
        for i in range(5)
    ] + [
        {"numero_processo": f"000200{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "improcedentes", "primary_subject": "Horas Extras"}
        for i in range(10)
    ]
    res_small = calculate_weighted_subsidiary_risk_and_instances(procs_small_tst)
    assert res_small["comparativo_instancias"]["tst"]["amostra_reduzida"] is True
    assert res_small["risco_subsidiario"]["agravante_tst_aplicado"] is False

    # 10 ou mais no TST com taxa de condenação no TST superior aos TRTs: agravante aplicado (+10%)
    # TST: 10 condenações (100%), TRT: 10 improcedentes (0%)
    procs_large_tst = [
        {"numero_processo": f"000100{i}-11.2023.5.00.0001", "tribunal": "TST", "sentence_outcome": "condenacoes", "primary_subject": "Horas Extras"}
        for i in range(10)
    ] + [
        {"numero_processo": f"000200{i}-11.2023.5.02.0001", "tribunal": "TRT2", "sentence_outcome": "improcedentes", "primary_subject": "Horas Extras"}
        for i in range(10)
    ]
    res_large = calculate_weighted_subsidiary_risk_and_instances(procs_large_tst)
    assert res_large["comparativo_instancias"]["tst"]["amostra_reduzida"] is False
    assert res_large["risco_subsidiario"]["agravante_tst_aplicado"] is True
    # Base Tefetiva = (1.0 * 10) / 20 * 100 = 50.0%. Com agravante +10% relativo -> 55.0%
    assert res_large["risco_subsidiario"]["percentual"] == 55.0


def test_execution_phase_separation_from_motives():
    """Valida que 'Execução / Cumprimento de Sentença' é excluída da tabela de motivos."""
    procs = [
        # Processo puramente de execução
        {"numero_processo": "0001001-11.2023.5.02.0001", "tribunal": "TRT2", "primary_subject": "Execução Trabalhista", "sentence_outcome": "condenacoes"},
        # Processo de execução mas com matéria substantiva
        {"numero_processo": "0001002-11.2023.5.02.0001", "tribunal": "TRT2", "primary_subject": "Horas Extras", "subjects": ["Horas Extras", "Cumprimento de Sentença"], "sentence_outcome": "condenacoes"},
        # Processo de conhecimento
        {"numero_processo": "0001003-11.2023.5.02.0001", "tribunal": "TRT2", "primary_subject": "Verbas Rescisórias", "sentence_outcome": "improcedentes"},
    ]

    motivos = build_condenacao_por_motivo(procs)
    motivos_names = [m["motivo"] for m in motivos]

    assert "Execução / Cumprimento de Sentença" not in motivos_names
    assert "Horas Extras / Jornada" in motivos_names

    res = calculate_weighted_subsidiary_risk_and_instances(procs)
    assert res["fase_execucao"]["total_processos"] == 2
    assert res["fase_execucao"]["rotulo"] == "Processos em Fase de Execução / Sentença Aplicada"


def test_tst_finalized_tag_and_classification():
    """Valida que processos finalizados no TST (mesmo improcedentes) recebem a tag de instância máxima."""
    p_tst_imp = {
        "numero_processo": "0009999-11.2022.5.00.0001",
        "tribunal": "TST",
        "sentence_outcome": "improcedentes",
        "primary_subject": "Danos Morais",
    }
    assert is_tst_process(p_tst_imp) is True
    assert is_process_finalized_tst(p_tst_imp) is True

    res = calculate_weighted_subsidiary_risk_and_instances([p_tst_imp])
    sample = res["processos_amostra"][0]
    assert sample["instancia_maxima_finalizado"] is True
    assert sample["tag_instancia"] == "Não cabem mais recursos (Instância Máxima — TST)"
    assert sample["fase_processual"] == "INSTANCIA_MAXIMA"


def test_non_imperative_recommendations_and_shielding():
    """Valida que nenhuma recomendação contém 'Aprovar', 'Reprovar' ou 'Criar comitê de assédio'."""
    analyzer = LegalAIAnalyzer()

    # Caso com risco moderado/alto
    summary_data = {
        "empresa": "EMPRESA DE TESTE LTDA",
        "total_processos": 50,
        "desfechos_sentencas": {"condenacoes": 30, "acordos": 10, "improcedentes": 10, "em_aberto": 0},
        "ranking_motivos": [{"motivo": "Horas Extras / Jornada", "quantidade": 40, "porcentagem": 80.0}],
        "risco_subsidiario": {"percentual": 65.0, "faixa_exposicao": "ALTA", "rotulo_exposicao": "Alta Exposição — Provisionamento e Diligência Rigorosa"},
        "score_risco_terceirizacao": 65.0,
    }

    report = analyzer.analyze_company(summary_data)
    rec_title = report.get("recomendacao_homologacao", "")
    recs_list = " ".join(report.get("recomendacoes_compliance", []))
    resumo = report.get("resumo_executivo", "")

    # Critério de aceite estrito
    assert "APROVAR" not in rec_title.upper()
    assert "REPROVAR" not in rec_title.upper()
    assert "HOMOLOGAR SEM RESTRIÇÕES" not in rec_title.upper()
    assert "comitê de assédio" not in recs_list.lower()
    assert "ouvidoria" not in recs_list.lower()

    # Prescrições de blindagem
    assert any(k in recs_list.lower() for k in ("provisão", "caixa reserva", "conta vinculada"))
    assert any(k in recs_list.lower() for k in ("folhas de ponto", "horas extras", "fgts"))
    assert any(k in recs_list.lower() for k in ("retenção de faturas", "seguro-garantia", "direito de regresso"))
