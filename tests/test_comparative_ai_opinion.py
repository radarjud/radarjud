# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import time
from unittest.mock import MagicMock
import pytest

from app.services.ai_analyzer import LegalAIAnalyzer
from app.services.prediction_engine import build_sector_benchmark


@pytest.fixture(autouse=True)
def clear_comparative_cache():
    LegalAIAnalyzer.reset_comparative_cache()
    yield
    LegalAIAnalyzer.reset_comparative_cache()


def _make_sample_companies():
    c1 = {
        "empresa": "Fornecedora Verde Limpa LTDA",
        "cnpj": "11.111.111/0001-11",
        "capital_social": "R$ 50.000.000,00",
        "capital_social_num": 50000000.0,
        "risco_subsidiario": 1.4,
        "probabilidade_condenacao": 12.0,
        "cenario": "CENARIO_A",
        "faixa_decisao": "APROVAR",
    }
    c2 = {
        "empresa": "Fornecedora Amarela Cautela S/A",
        "cnpj": "22.222.222/0001-22",
        "capital_social": "R$ 5.000.000,00",
        "capital_social_num": 5000000.0,
        "risco_subsidiario": 18.6,
        "probabilidade_condenacao": 32.5,
        "cenario": "CENARIO_A",
        "faixa_decisao": "CAUTELA",
    }
    c3 = {
        "empresa": "Fornecedora Vermelha Crítica ME",
        "cnpj": "33.333.333/0001-33",
        "capital_social": "R$ 100.000,00",
        "capital_social_num": 100000.0,
        "risco_subsidiario": 61.8,
        "probabilidade_condenacao": 72.0,
        "cenario": "CENARIO_B",
        "faixa_decisao": "REPROVAR",
    }
    return [c1, c2, c3]


def test_parity_gemini_active_vs_fallback_disabled(monkeypatch):
    """
    Cenário 1: Paridade Gemini Ativo vs. Fallback Heurístico Desligado:
    - Ambos retornam rigorosamente as 5 chaves canônicas obrigatórias.
    - Ambos recomendam exatamente a mesma empresa em primeiro_lugar ('Fornecedora Verde Limpa LTDA').
    - Ambos preservam coerência com Cenário A e Cenário B.
    """
    companies = _make_sample_companies()
    analyzer = LegalAIAnalyzer()

    # 1. Fallback Heurístico Local (sem chaves externas de API)
    analyzer.gemini_api_key = None
    fallback_res = analyzer.generate_comparative_analysis(companies)

    required_keys = {
        "recomendacao_compra",
        "primeiro_lugar",
        "alerta_critico",
        "economia_potencial_texto",
        "justificativa_executiva",
    }
    assert set(fallback_res.keys()) == required_keys
    assert fallback_res["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"
    assert "Fornecedora Verde Limpa LTDA" in fallback_res["recomendacao_compra"]
    assert fallback_res["alerta_critico"] is not None
    assert "Fornecedora Vermelha Crítica ME" in fallback_res["alerta_critico"]
    assert "60.4 pontos percentuais" in fallback_res["economia_potencial_texto"]

    # 2. Modo com Gemini Ativo (Simulado via Mock de REST API)
    LegalAIAnalyzer.reset_comparative_cache()
    mock_llm_payload = {
        "recomendacao_compra": "Recomenda-se a contratação prioritária de Fornecedora Verde Limpa LTDA, classificada em 1º lugar.",
        "primeiro_lugar": "Fornecedora Verde Limpa LTDA",
        "alerta_critico": "Alerta crítico para Fornecedora Vermelha Crítica ME devido a risco de 61.8% na Súmula 331 TST.",
        "economia_potencial_texto": "Economia de até 60.4 pontos percentuais em mitigação de risco trabalhista.",
        "justificativa_executiva": "Parecer executivo emitido por radarjusAgent baseado no ranking consolidado.",
    }
    analyzer.gemini_api_key = "fake-gemini-key"
    monkeypatch.setattr(analyzer, "_call_gemini_api_comparative", MagicMock(return_value=mock_llm_payload))

    gemini_res = analyzer.generate_comparative_analysis(companies)
    assert set(gemini_res.keys()) == required_keys
    assert gemini_res["primeiro_lugar"] == fallback_res["primeiro_lugar"]
    assert "Fornecedora Verde Limpa LTDA" in gemini_res["recomendacao_compra"]


def test_guardrail_anti_hallucination_inversion_and_malformed_json(monkeypatch):
    """
    Cenário 2: Guardrail Anti-Alucinação:
    - Se o LLM alucinar e tentar inverter a 1ª colocada (ex: recomendar a empresa com 61.8% de risco),
      o guardrail intercepta a divergência e descarta a resposta, acionando o fallback heurístico seguro.
    - Se o LLM retornar JSON com chaves ausentes ou formato incorreto, o fallback também é acionado.
    """
    companies = _make_sample_companies()
    analyzer = LegalAIAnalyzer()
    analyzer.gemini_api_key = "fake-gemini-key"

    # Tentativa de alucinação: LLM escolhe a pior colocada em 1º lugar
    hallucinated_payload = {
        "recomendacao_compra": "Recomenda-se a contratação prioritária de Fornecedora Vermelha Crítica ME.",
        "primeiro_lugar": "Fornecedora Vermelha Crítica ME",  # Inversão proposital
        "alerta_critico": None,
        "economia_potencial_texto": "Sem economia.",
        "justificativa_executiva": "Opinião divergente e alucinada.",
    }
    monkeypatch.setattr(analyzer, "_call_gemini_api_comparative", MagicMock(return_value=hallucinated_payload))

    res = analyzer.generate_comparative_analysis(companies)
    # O guardrail deve impedir a inversão e o fallback deve restaurar a verdadeira vencedora
    assert res["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"
    assert "Fornecedora Verde Limpa LTDA" in res["recomendacao_compra"]

    # Tentativa com chaves ausentes
    LegalAIAnalyzer.reset_comparative_cache()
    incomplete_payload = {
        "primeiro_lugar": "Fornecedora Verde Limpa LTDA",
        "recomendacao_compra": "Aprovar Fornecedora Verde Limpa LTDA",
        # faltando alerta_critico, economia_potencial_texto, justificativa_executiva
    }
    monkeypatch.setattr(analyzer, "_call_gemini_api_comparative", MagicMock(return_value=incomplete_payload))

    res_incomplete = analyzer.generate_comparative_analysis(companies)
    assert set(res_incomplete.keys()) == {
        "recomendacao_compra",
        "primeiro_lugar",
        "alerta_critico",
        "economia_potencial_texto",
        "justificativa_executiva",
    }
    assert res_incomplete["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"


def test_zero_conviction_rule_preserves_0_percent_in_ai_opinion():
    """
    Cenário 3: Regra Zero Condenações (0.0%):
    - Se a melhor empresa tiver 0.0% de risco subsidiário (sem condenações prévias),
      o parecer comparativo deve citar explicitamente 0.0% de risco sem alucinar passivo inexistente.
    """
    companies = [
        {
            "empresa": "Fornecedora Impecável S.A.",
            "cnpj": "99.999.999/0001-99",
            "capital_social": "R$ 10.000.000,00",
            "capital_social_num": 10000000.0,
            "risco_subsidiario": 0.0,
            "probabilidade_condenacao": 0.0,
            "previsao_bloqueada": True,
            "cenario": "CENARIO_A",
            "faixa_decisao": "APROVAR",
        },
        {
            "empresa": "Fornecedora Média LTDA",
            "cnpj": "88.888.888/0001-88",
            "capital_social": "R$ 1.000.000,00",
            "capital_social_num": 1000000.0,
            "risco_subsidiario": 25.0,
            "probabilidade_condenacao": 30.0,
            "cenario": "CENARIO_A",
            "faixa_decisao": "CAUTELA",
        },
    ]

    analyzer = LegalAIAnalyzer()
    analyzer.gemini_api_key = None
    res = analyzer.generate_comparative_analysis(companies)

    assert res["primeiro_lugar"] == "Fornecedora Impecável S.A."
    assert "0.0%" in res["justificativa_executiva"]
    assert "sem passivo condenatório" in res["justificativa_executiva"]


def test_performance_and_deterministic_hash_cache(monkeypatch):
    """
    Cenário 4: Performance e Cache por Hash Determinístico:
    - A primeira chamada calcula e armazena em cache.
    - A segunda chamada idêntica consome do cache instantaneamente (< 50ms)
      sem acionar o método de LLM novamente.
    - Alteração nos scores de alguma empresa invalida a chave e dispara novo cálculo.
    """
    companies = _make_sample_companies()
    analyzer = LegalAIAnalyzer()
    analyzer.gemini_api_key = "fake-gemini-key"

    mock_llm_payload = {
        "recomendacao_compra": "Recomenda-se a contratação prioritária de Fornecedora Verde Limpa LTDA, classificada em 1º lugar.",
        "primeiro_lugar": "Fornecedora Verde Limpa LTDA",
        "alerta_critico": "Alerta para risco na empresa de menor solvência.",
        "economia_potencial_texto": "Economia expressiva.",
        "justificativa_executiva": "Parecer executivo de IA gerado com sucesso.",
    }
    mock_call = MagicMock(return_value=mock_llm_payload)
    monkeypatch.setattr(analyzer, "_call_gemini_api_comparative", mock_call)

    # 1ª chamada: Executa o LLM e armazena em cache
    t0 = time.perf_counter()
    res1 = analyzer.generate_comparative_analysis(companies)
    t1 = time.perf_counter()
    assert mock_call.call_count == 1
    assert res1["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"

    # 2ª chamada: Deve retornar do cache em menos de 50ms sem chamar o mock
    t2 = time.perf_counter()
    res2 = analyzer.generate_comparative_analysis(companies)
    latency_ms = (time.perf_counter() - t2) * 1000
    assert mock_call.call_count == 1  # Não foi chamado novamente
    assert latency_ms < 50.0  # < 50ms
    assert res2 == res1

    # Invalidação: altera o score de uma empresa -> deve calcular nova chave e chamar LLM
    modified_companies = _make_sample_companies()
    modified_companies[0]["risco_subsidiario"] = 2.0  # Mudou de 1.4 para 2.0
    res3 = analyzer.generate_comparative_analysis(modified_companies)
    assert mock_call.call_count == 2
    assert res3["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"


def test_build_sector_benchmark_integration_with_ai_analyzer():
    """
    Cenário 5: Integração com build_sector_benchmark:
    - O motor de benchmarking invoca automaticamente o LegalAIAnalyzer
      e anexa o parecer_comparativo com contrato 100% completo e preservado.
    """
    companies = _make_sample_companies()
    benchmark = build_sector_benchmark(companies)

    assert "parecer_comparativo" in benchmark
    parecer = benchmark["parecer_comparativo"]
    assert parecer["primeiro_lugar"] == "Fornecedora Verde Limpa LTDA"
    assert "Fornecedora Verde Limpa LTDA" in parecer["recomendacao_compra"]
    assert parecer["alerta_critico"] is not None
    assert "60.4 pontos percentuais" in parecer["economia_potencial_texto"]
    assert "Comparativo técnico" in parecer["justificativa_executiva"]
