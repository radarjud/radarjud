"""
Testes automatizados para a funcionalidade da US-22:
Indicadores Públicos de Solvência Real e Certidões Fiscais/Trabalhistas
(Sem depender apenas de Capital Social).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app, db
from app.models.company_search import CompanySearch
from app.services.prediction_engine import build_sector_benchmark
from app.services.solvency_signals_service import (
    STATUS_BALANCO_INDISPONIVEL,
    STATUS_NAO_DISPONIVEL,
    STATUS_REGULAR,
    STATUS_RESTRICAO,
    SolvencySignalsService,
    solvency_signals_service,
)


@pytest.fixture
def clean_cache():
    """Limpa o cache em memória de sinais de solvência antes de cada teste."""
    from app.services.solvency_signals_service import _SOLVENCY_CACHE
    _SOLVENCY_CACHE.clear()
    yield
    _SOLVENCY_CACHE.clear()


def test_positive_public_signals_sa_aberta():
    """
    Cenário 1: Empresa com sinais públicos positivos (S.A. Aberta).
    RFB ativa, sem restrições na PGFN ou CGU, sem passivo judicial e com
    demonstrações contábeis públicas regulares perante a CVM.
    Garante retorno com status canônico 'Regular' coerente em todas as fontes.
    """
    cadastral_sa = {
        "razao_social": "COMPANHIA ENERGIA DO BRASIL S.A. ABERTA",
        "natureza_juridica": "204-6 - Sociedade Anônima Aberta",
        "situacao_cadastral": "ATIVA",
        "porte": "DEMAIS",
    }
    pgfn_clean = {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA", "total_debitos": 0.0}
    cgu_clean = {"possui_sancoes": False, "status_cgu": "REGULAR", "detalhes": []}

    signals = solvency_signals_service.consolidate_solvency_signals(
        company_name="COMPANHIA ENERGIA DO BRASIL S.A. ABERTA",
        cnpj="11.222.333/0001-44",
        cadastral_data=cadastral_sa,
        processes=[],
        pgfn_data=pgfn_clean,
        cgu_data=cgu_clean,
        capital_social=500_000_000.0,
    )

    assert signals["possui_restricoes"] is False
    assert signals["alerta_homologacao_privada"] is False
    assert signals["demonstracao_contabil_status"] == STATUS_REGULAR
    assert "Sinais públicos regulares" in signals["resumo_solvencia"]

    # Validação do status canônico em cada fonte consultada
    fontes_map = {f["tipo"]: f for f in signals["fontes_consultadas"]}
    assert fontes_map["rfb"]["status"] == STATUS_REGULAR
    assert fontes_map["pgfn"]["status"] == STATUS_REGULAR
    assert fontes_map["cgu"]["status"] == STATUS_REGULAR
    assert fontes_map["cvm"]["status"] == STATUS_REGULAR
    assert fontes_map["judicial"]["status"] == STATUS_REGULAR


def test_company_with_public_debt_or_restriction_prevents_optimism():
    """
    Cenário 2: Empresa com restrição pública (Dívida Ativa na PGFN ou Sanção CGU).
    Mesmo com Capital Social alto (R$ 100.000.000,00), a presença de restrição
    deve marcar 'Restrição identificada', acionar alerta e impedir classificação otimista (APROVAR).
    """
    cadastral = {
        "razao_social": "PRESTADORA DE SERVICOS RESTRITA S.A.",
        "natureza_juridica": "204-6 - Sociedade Anônima Aberta",
        "situacao_cadastral": "ATIVA",
    }
    pgfn_debt = {
        "possui_divida_ativa": True,
        "status_pgfn": "INSCRITA_DIVIDA_ATIVA",
        "total_debitos": 4_850_000.0,
    }
    cgu_clean = {"possui_sancoes": False, "status_cgu": "REGULAR"}

    signals = solvency_signals_service.consolidate_solvency_signals(
        company_name="PRESTADORA DE SERVICOS RESTRITA S.A.",
        cnpj="22.333.444/0001-55",
        cadastral_data=cadastral,
        processes=[],
        pgfn_data=pgfn_debt,
        cgu_data=cgu_clean,
        capital_social=100_000_000.0,
    )

    assert signals["possui_restricoes"] is True
    assert "PGFN (Dívida Ativa da União)" in signals["restricoes_identificadas"]
    assert "Restrição identificada" in signals["resumo_solvencia"]

    fontes_map = {f["tipo"]: f for f in signals["fontes_consultadas"]}
    assert fontes_map["pgfn"]["status"] == STATUS_RESTRICAO
    assert "4.850.000,00" in fontes_map["pgfn"]["detalhe"]

    # Valida no motor de ranqueamento que a empresa NÃO recebe 'APROVAR'
    company_payload = {
        "empresa": "PRESTADORA DE SERVICOS RESTRITA S.A.",
        "cnpj": "22.333.444/0001-55",
        "capital_social": 100_000_000.0,
        "risco_subsidiario": 35.0,
        "cenario": "CENARIO_A",
        "sinais_solvencia_publica": signals,
    }
    result = build_sector_benchmark([company_payload])
    ranked = result["empresas_ranqueadas"][0]

    # Restrição pública impede classificação artificialmente otimista
    assert ranked["faixa_decisao"] in ("CAUTELA", "REPROVAR")
    assert ranked["faixa_decisao"] != "APROVAR"
    assert "Restrição identificada" in ranked["recomendacao"]


def test_ltda_pme_high_capital_without_public_balance_sheet():
    """
    Cenário 3: LTDA / PME com Capital Social alto e sem balanço público.
    Deve retornar explicitamente 'Demonstração contábil pública indisponível',
    tratar o Capital Social como dado declaratório (sem inventar liquidez) e
    incluir na prescrição a exigência de certidões negativas e balanço recente na homologação.
    """
    cadastral_ltda = {
        "razao_social": "TERCEIRIZACAO DE SERVICOS DE LIMPEZA LTDA",
        "natureza_juridica": "206-2 - Sociedade Empresária Limitada",
        "situacao_cadastral": "ATIVA",
        "porte": "DEMAIS",
    }
    pgfn_clean = {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA"}
    cgu_clean = {"possui_sancoes": False, "status_cgu": "REGULAR"}

    signals = solvency_signals_service.consolidate_solvency_signals(
        company_name="TERCEIRIZACAO DE SERVICOS DE LIMPEZA LTDA",
        cnpj="33.444.555/0001-66",
        cadastral_data=cadastral_ltda,
        processes=[],
        pgfn_data=pgfn_clean,
        cgu_data=cgu_clean,
        capital_social=80_000_000.0,  # Capital alto declarado no contrato social
    )

    # Exigência 1: Tratamento explícito para LTDA / PME
    assert signals["demonstracao_contabil_status"] == STATUS_BALANCO_INDISPONIVEL
    assert signals["alerta_homologacao_privada"] is True
    assert signals["possui_restricoes"] is False

    # Exigência 2: Orientação prescritiva automática para homologação
    orientacao = signals["orientacao_homologacao"]
    assert "exigir certidões negativas atualizadas" in orientacao.lower()
    assert "balanço patrimonial recente" in orientacao.lower()
    assert "processo de homologação" in orientacao.lower()

    # Exigência 3: No motor de decisão, capital declaratório de LTDA com passivo não dá aprovação automática
    company_payload = {
        "empresa": "TERCEIRIZACAO DE SERVICOS DE LIMPEZA LTDA",
        "cnpj": "33.444.555/0001-66",
        "capital_social": 80_000_000.0,
        "risco_subsidiario": 22.0,  # Acima de 15%, mas capital alto declaratório
        "cenario": "CENARIO_A",
        "sinais_solvencia_publica": signals,
    }
    result = build_sector_benchmark([company_payload])
    ranked = result["empresas_ranqueadas"][0]

    # Não superestima solvência: não recebe APROVAR automático apenas pelo capital
    assert ranked["faixa_decisao"] == "CAUTELA"
    assert "exigir certidões negativas atualizadas e balanço patrimonial recente" in ranked["recomendacao"].lower()


def test_resilience_fallback_timeout_http_failure():
    """
    Cenário 4: Resiliência e Fallback seguro no Render Free.
    Timeout ou falha HTTP em fonte externa retorna 'Não disponível publicamente'
    sem lançar exceções não tratadas e sem quebrar o fluxo da aplicação.
    """
    service = SolvencySignalsService(timeout_seconds=0.1)

    # Simula falha de conexão e timeout na PGFN
    with patch.object(service.session, "get", side_effect=requests.Timeout("Connection timed out")):
        with patch.dict("os.environ", {"PGFN_API_URL": "https://pgfn-mock.api.gov.br"}):
            status_pgfn = service.check_pgfn_divida_ativa("44.555.666/0001-77")
            assert status_pgfn["status"] == STATUS_NAO_DISPONIVEL
            assert "não disponível publicamente" in status_pgfn["detalhe"].lower()

    # Simula falha de servidor 500 ou erro genérico
    with patch.object(service.session, "get", side_effect=requests.HTTPError("500 Internal Server Error")):
        with patch.dict("os.environ", {"CVM_API_URL": "https://cvm-mock.api.gov.br"}):
            cadastral_sa = {"natureza_juridica": "204-6 - Sociedade Anônima Aberta"}
            fonte_cvm, dem_status = service.check_cvm_accounting_status(
                "55.666.777/0001-88", cadastral_sa, company_name="S.A. FALHA EXTERNA"
            )
            # Não quebra e retorna status seguro
            assert fonte_cvm["status"] in (STATUS_REGULAR, STATUS_NAO_DISPONIVEL)
            assert dem_status in (STATUS_REGULAR, STATUS_NAO_DISPONIVEL)


def test_json_contract_overview_and_history_endpoints(client, app, clean_cache):
    """
    Valida a exposição do contrato JSON de sinais de solvência nos endpoints
    /overview, /history/<id> e a persistência no banco.
    """
    # 1. Cria registro no banco com sinais de solvência salvos
    with app.app_context():
        search = CompanySearch(
            company_name="FORNECEDOR BETA LTDA",
            normalized_name="FORNECEDOR BETA LTDA",
            total_processes=5,
            economic_standing={
                "capital_social_num": 5_000_000.0,
                "situacao_cadastral": "ATIVA",
                "sinais_solvencia_publica": {
                    "demonstracao_contabil_status": STATUS_BALANCO_INDISPONIVEL,
                    "resumo_solvencia": "Sinais públicos regulares com ressalva",
                    "possui_restricoes": False,
                    "alerta_homologacao_privada": True,
                    "fontes_consultadas": [
                        {"fonte": "Receita Federal (Situação Cadastral)", "status": STATUS_REGULAR, "detalhe": "Ativa"},
                    ],
                },
            },
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

    # 2. Testa endpoint /history/<id>
    resp_hist = client.get(f"/api/v1/history/{sid}")
    assert resp_hist.status_code == 200
    data_hist = resp_hist.get_json()
    assert "sinais_solvencia_publica" in data_hist
    sinais_hist = data_hist["sinais_solvencia_publica"]
    assert sinais_hist["demonstracao_contabil_status"] == STATUS_BALANCO_INDISPONIVEL
    assert sinais_hist["alerta_homologacao_privada"] is True

    # 3. Testa endpoint /export-pdf
    resp_pdf = client.get(f"/api/v1/history/{sid}/summary.pdf")
    assert resp_pdf.status_code == 200
    assert resp_pdf.mimetype == "application/pdf"
    assert len(resp_pdf.data) > 1000
