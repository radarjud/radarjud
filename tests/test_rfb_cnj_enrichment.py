# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import pytest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app import create_app
from app.extensions import db
from app.models.CompanyRegistry import CompanyRegistry
from app.models.company_search import CompanySearch
from app.models.process_record import ProcessRecord
from app.services.economic_service import (
    normalize_cnae_code,
    _fetch_from_brasilapi,
    _fetch_from_receitaws,
    _fetch_from_cnpjws,
    _fetch_from_minhareceita,
    EconomicStandingService,
)
from app.services.cnj_searcher import (
    parse_iso_date,
    parse_currency_value,
    normalize_court_code,
)
from app.seeds.cnae_seed import backfill_cnae_metadata


@pytest.fixture
def test_app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        yield app
        db.session.rollback()
        db.drop_all()


# =============================================================================
# 1. TESTES UNITÁRIOS DE NORMALIZAÇÃO DE CNAE (XXXX-X/XX)
# =============================================================================
class TestCnaeNormalization:
    def test_normalize_cnae_integer_7_digits(self):
        # 6422100 -> 6422-1/00 (Bancos múltiplos)
        assert normalize_cnae_code(6422100) == "6422-1/00"

    def test_normalize_cnae_string_unmasked(self):
        # 4120400 -> 4120-4/00 (Engenharia / Construção)
        assert normalize_cnae_code("4120400") == "4120-4/00"

    def test_normalize_cnae_string_standard_mask(self):
        # 7911-2/00 -> 7911-2/00 (Turismo / Agências)
        assert normalize_cnae_code("7911-2/00") == "7911-2/00"

    def test_normalize_cnae_string_dots_and_hyphen(self):
        # 64.22-1-00 -> 6422-1/00
        assert normalize_cnae_code("64.22-1-00") == "6422-1/00"

    def test_normalize_cnae_string_dots_and_slash(self):
        # 64.22-1/00 -> 6422-1/00
        assert normalize_cnae_code("64.22-1/00") == "6422-1/00"

    def test_normalize_cnae_invalid_lengths(self):
        assert normalize_cnae_code("123") is None
        assert normalize_cnae_code("123456") is None
        assert normalize_cnae_code("12345678") is None

    def test_normalize_cnae_empty_or_none(self):
        assert normalize_cnae_code(None) is None
        assert normalize_cnae_code("") is None
        assert normalize_cnae_code("   ") is None

    def test_normalize_cnae_non_numeric(self):
        assert normalize_cnae_code("ABCDEFG") is None
        assert normalize_cnae_code("abc1234") is None


# =============================================================================
# 2. TESTES UNITÁRIOS DE PARSER DE DATAS JUDICIAIS (data_distribuicao)
# =============================================================================
class TestJudicialDateParser:
    def test_parse_iso_with_timestamp(self):
        assert parse_iso_date("2024-03-15T14:30:00.000Z") == date(2024, 3, 15)
        assert parse_iso_date("2023-11-20T08:00:00Z") == date(2023, 11, 20)
        assert parse_iso_date("2022-01-05 10:15:30") == date(2022, 1, 5)

    def test_parse_iso_standard(self):
        assert parse_iso_date("2024-03-15") == date(2024, 3, 15)

    def test_parse_compact_yyyymmdd(self):
        assert parse_iso_date("20240315") == date(2024, 3, 15)

    def test_parse_brazilian_format(self):
        assert parse_iso_date("15/03/2024") == date(2024, 3, 15)
        assert parse_iso_date("01/01/2020") == date(2020, 1, 1)

    def test_parse_strict_calendar_invalids_return_none(self):
        # Mês 13 ou dia 32 inválidos devem retornar None sem disparar exceção
        assert parse_iso_date("32/13/2024") is None
        assert parse_iso_date("2024-02-31") is None
        assert parse_iso_date("2024-99-99") is None
        assert parse_iso_date("00/00/0000") is None
        assert parse_iso_date("invalid-text") is None
        assert parse_iso_date(None) is None
        assert parse_iso_date("") is None

    def test_parse_fallback_from_publication_text(self):
        text1 = "Ação trabalhista distribuída em 10/05/2023 perante a 2ª Vara do Trabalho de Campinas."
        assert parse_iso_date(None, text_fallback=text1) == date(2023, 5, 10)

        text2 = "Notificação de ajuizamento em 22/11/2022 do processo pelo rito sumaríssimo."
        assert parse_iso_date("", text_fallback=text2) == date(2022, 11, 22)

        text3 = "Termo de distribuição no dia 05/08/2021 às partes."
        assert parse_iso_date(None, text_fallback=text3) == date(2021, 8, 5)


# =============================================================================
# 3. TESTES UNITÁRIOS DE PARSER DE VALOR DA CAUSA (valor_causa)
# =============================================================================
class TestClaimValueParser:
    def test_parse_currency_brazilian_string(self):
        assert parse_currency_value("R$ 45.000,00") == Decimal("45000.00")
        assert parse_currency_value("R$ 1.250.300,75") == Decimal("1250300.75")
        assert parse_currency_value("45.000,00") == Decimal("45000.00")
        assert parse_currency_value("1200,50") == Decimal("1200.50")

    def test_parse_currency_international_string(self):
        assert parse_currency_value("45000.00") == Decimal("45000.00")
        assert parse_currency_value("1250300.75") == Decimal("1250300.75")
        assert parse_currency_value("45000") == Decimal("45000.00")

    def test_parse_currency_numeric(self):
        assert parse_currency_value(45000) == Decimal("45000.00")
        assert parse_currency_value(1250300.75) == Decimal("1250300.75")
        assert parse_currency_value(Decimal("78900.50")) == Decimal("78900.50")

    def test_parse_currency_from_publication_text_fallback(self):
        text1 = "Dá-se à causa o valor de R$ 50.000,00 para fins de alçada recursal."
        assert parse_currency_value(None, text_fallback=text1) == Decimal("50000.00")

        text2 = "Valor da causa: R$ 1.350.200,45 atribuído pela reclamante."
        assert parse_currency_value("", text_fallback=text2) == Decimal("1350200.45")

        text3 = "Arbitrada condenação no valor de R$ 25.000,00 com custas processuais."
        assert parse_currency_value(None, text_fallback=text3) == Decimal("25000.00")

    def test_parse_currency_invalid_or_zero(self):
        assert parse_currency_value(0) is None
        assert parse_currency_value("R$ 0,00") is None
        assert parse_currency_value("-500.00") is None
        assert parse_currency_value("sem valor atribuido") is None
        assert parse_currency_value(None) is None
        assert parse_currency_value("") is None


# =============================================================================
# 4. TESTES UNITÁRIOS DE NORMALIZAÇÃO DE TRIBUNAIS (tribunal)
# =============================================================================
class TestCourtCodeNormalization:
    def test_normalize_trt_variations(self):
        assert normalize_court_code("TRT-2") == "TRT2"
        assert normalize_court_code("TRT 02") == "TRT2"
        assert normalize_court_code("trt2") == "TRT2"
        assert normalize_court_code("TRT02") == "TRT2"
        assert normalize_court_code("Tribunal Regional do Trabalho da 2ª Região") == "TRT2"
        assert normalize_court_code("Tribunal Regional do Trabalho da 15ª Região") == "TRT15"
        assert normalize_court_code("TRT-15") == "TRT15"
        assert normalize_court_code("TRT-01") == "TRT1"
        assert normalize_court_code("TRT 24") == "TRT24"

    def test_normalize_tst(self):
        assert normalize_court_code("TST") == "TST"
        assert normalize_court_code("Tribunal Superior do Trabalho") == "TST"

    def test_normalize_state_and_federal(self):
        assert normalize_court_code("TJSP") == "TJSP"
        assert normalize_court_code("TJ-SP") == "TJSP"
        assert normalize_court_code("TJRJ") == "TJRJ"
        assert normalize_court_code("TRF1") == "TRF1"
        assert normalize_court_code("TRF-03") == "TRF3"
        assert normalize_court_code("STJ") == "STJ"

    def test_normalize_from_cnj_number_inference(self):
        # 0001234-56.2024.5.02.0001 -> Ramo 5, Tribunal 02 -> TRT2
        assert normalize_court_code("", cnj="0001234-56.2024.5.02.0001") == "TRT2"
        # 0001234-56.2024.5.15.0001 -> Ramo 5, Tribunal 15 -> TRT15
        assert normalize_court_code(None, cnj="0001234-56.2024.5.15.0001") == "TRT15"
        # 0001234-56.2024.5.00.0001 -> Ramo 5, Tribunal 00 -> TST
        assert normalize_court_code("N/A", cnj="0001234-56.2024.5.00.0001") == "TST"
        # 0001234-56.2024.8.26.0001 -> Ramo 8, Tribunal 26 -> TJSP
        assert normalize_court_code(None, cnj="0001234-56.2024.8.26.0001") == "TJSP"
        # 0001234-56.2024.4.01.0001 -> Ramo 4, Tribunal 01 -> TRF1
        assert normalize_court_code("", cnj="0001234-56.2024.4.01.0001") == "TRF1"


# =============================================================================
# 5. TESTES DE PROVEDORES RFB (BrasilAPI, ReceitaWS, CNPJ.ws) E 3 RAMOS DE CNAE
# =============================================================================
class TestRfbProvidersCnaeExtraction:
    @patch("requests.get")
    def test_fetch_brasilapi_banco(self, mock_get):
        # Ramo 1: Banco (6422-1/00)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "razao_social": "BANCO EXEMPLO S.A.",
            "nome_fantasia": "BANCO EXEMPLO",
            "capital_social": 50000000.0,
            "porte": "DEMAIS",
            "descricao_situacao_cadastral": "ATIVA",
            "cnae_fiscal": 6422100,
            "cnae_fiscal_descricao": "Bancos múltiplos, com carteira comercial",
            "email": "contato@bancoexemplo.com.br",
        }
        mock_get.return_value = mock_resp

        res = _fetch_from_brasilapi("12345678000199", {}, 4.0)
        assert res["cnae_codigo"] == "6422-1/00"
        assert res["cnae_descricao"] == "Bancos múltiplos, com carteira comercial"

    @patch("requests.get")
    def test_fetch_receitaws_engenharia(self, mock_get):
        # Ramo 2: Engenharia / Construção (4120-4/00)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "OK",
            "nome": "ENGENHARIA E CONSTRUCOES LTDA",
            "fantasia": "ENG CONSTRUTORA",
            "capital_social": "15000000.00",
            "porte": "DEMAIS",
            "situacao": "ATIVA",
            "atividade_principal": [
                {
                    "code": "41.20-4-00",
                    "text": "Construção de edifícios",
                }
            ],
            "email": "financeiro@engconstrutora.com.br",
        }
        mock_get.return_value = mock_resp

        res = _fetch_from_receitaws("98765432000188", {}, 4.0)
        assert res["cnae_codigo"] == "4120-4/00"
        assert res["cnae_descricao"] == "Construção de edifícios"

    @patch("requests.get")
    def test_fetch_cnpjws_turismo(self, mock_get):
        # Ramo 3: Agência de Viagens / Turismo (7911-2/00)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "razao_social": "TURISMO E VIAGENS BRASIL LTDA",
            "capital_social": 2000000.0,
            "porte": {"descricao": "DEMAIS"},
            "estabelecimento": {
                "nome_fantasia": "VIAGENS BRASIL",
                "situacao_cadastral": "ATIVA",
                "email": "operacoes@viagensbrasil.com.br",
                "atividade_principal": {
                    "id": 7911200,
                    "subclasse": "7911-2/00",
                    "descricao": "Agências de viagens",
                },
            },
        }
        mock_get.return_value = mock_resp

        res = _fetch_from_cnpjws("11222333000144", {}, 4.0)
        assert res["cnae_codigo"] == "7911-2/00"
        assert res["cnae_descricao"] == "Agências de viagens"

    @patch("requests.get")
    def test_fetch_minhareceita_tecnologia(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "razao_social": "SOFTWARE TECH S.A.",
            "nome_fantasia": "SOFTWARE TECH",
            "capital_social": 8000000.0,
            "descricao_porte": "DEMAIS",
            "descricao_situacao_cadastral": "ATIVA",
            "cnae_fiscal": 6202300,
            "cnae_fiscal_descricao": "Desenvolvimento e licenciamento de software",
            "email": "contato@softwaretech.com.br",
        }
        mock_get.return_value = mock_resp

        res = _fetch_from_minhareceita("55444333000122", {}, 4.0)
        assert res["cnae_codigo"] == "6202-3/00"
        assert res["cnae_descricao"] == "Desenvolvimento e licenciamento de software"

    def test_evaluate_economic_standing_enriches_cnae(self, test_app):
        service = EconomicStandingService()
        with patch.object(service, "fetch_brasilapi_data") as mock_bapi:
            mock_bapi.return_value = {
                "razao_social": "EMPRESA TESTE S.A.",
                "nome_fantasia": "TESTE",
                "capital_social": 20000000.0,
                "capital_social_formatado": "R$ 20.000.000,00",
                "situacao_cadastral": "ATIVA",
                "cnae_codigo": "6422-1/00",
                "cnae_descricao": "Bancos múltiplos, com carteira comercial",
                "cnae_fiscal_descricao": "Bancos múltiplos, com carteira comercial",
                "porte": "DEMAIS",
                "email": "contato@teste.com",
            }
            res = service.evaluate_economic_standing("EMPRESA TESTE", cnpj="60746948000112")
            assert res.get("cnae_codigo") == "6422-1/00"
            assert res.get("cnae_descricao") == "Bancos múltiplos, com carteira comercial"
            assert res.get("cnae") == {
                "codigo": "6422-1/00",
                "descricao": "Bancos múltiplos, com carteira comercial",
            }


# =============================================================================
# 6. TESTES DE COMPATIBILIDADE RETROATIVA E VALORES FALTANTES (NULL SAFE)
# =============================================================================
class TestBackwardCompatibilityAndMissingValues:
    def test_process_record_null_metadata_to_dict(self, test_app):
        # Garante que processos legados sem data_distribuicao e valor_causa serializam perfeitamente
        proc = ProcessRecord(
            company_search_id=1,
            numero_processo="0001234-56.2024.5.02.0001",
            tribunal="TRT2",
            grau="G1",
            nivel_sigilo=0,
            sentence_outcome="em_aberto",
            data_distribuicao=None,
            valor_causa=None,
        )
        d = proc.to_dict()
        assert d["data_distribuicao"] is None
        assert d["valor_causa"] is None
        assert d["numero_processo"] == "0001234-56.2024.5.02.0001"

    def test_process_record_with_values_to_dict(self, test_app):
        proc = ProcessRecord(
            company_search_id=1,
            numero_processo="0001234-56.2024.5.02.0001",
            tribunal="TRT2",
            grau="G1",
            nivel_sigilo=0,
            sentence_outcome="condenacoes",
            data_distribuicao=date(2024, 3, 15),
            valor_causa=Decimal("45000.00"),
        )
        d = proc.to_dict()
        assert d["data_distribuicao"] == "2024-03-15"
        assert d["valor_causa"] == 45000.0

    def test_company_search_null_cnae_to_dict(self, test_app):
        search = CompanySearch(
            company_name="EMPRESA SEM CNAE LTDA",
            normalized_name="EMPRESA SEM CNAE LTDA",
            cnae_codigo=None,
            cnae_descricao=None,
        )
        d = search.to_dict()
        assert d["cnae_codigo"] is None
        assert d["cnae_descricao"] is None
        assert d["cnae"] == {"codigo": None, "descricao": None}

    def test_company_search_with_cnae_to_dict(self, test_app):
        search = CompanySearch(
            company_name="BANCO BRADESCO S.A.",
            normalized_name="BANCO BRADESCO S.A.",
            cnae_codigo="6422-1/00",
            cnae_descricao="Bancos múltiplos, com carteira comercial",
        )
        d = search.to_dict()
        assert d["cnae_codigo"] == "6422-1/00"
        assert d["cnae_descricao"] == "Bancos múltiplos, com carteira comercial"
        assert d["cnae"] == {
            "codigo": "6422-1/00",
            "descricao": "Bancos múltiplos, com carteira comercial",
        }

    def test_company_registry_null_and_filled_cnae(self, test_app):
        reg = CompanyRegistry(
            cnpj="99999999000199",
            razao_social="REGISTRO SEM CNAE",
            cnae_codigo=None,
            cnae_descricao=None,
        )
        d = reg.to_dict()
        assert d["cnae_codigo"] is None
        assert d["cnae_descricao"] is None

        reg2 = CompanyRegistry(
            cnpj="88888888000188",
            razao_social="REGISTRO COM CNAE",
            cnae_codigo="4120-4/00",
            cnae_descricao="Construção de edifícios",
        )
        d2 = reg2.to_dict()
        assert d2["cnae_codigo"] == "4120-4/00"
        assert d2["cnae_descricao"] == "Construção de edifícios"


# =============================================================================
# 7. TESTE DA ROTINA DE SEED / BACKFILL DE CNAE
# =============================================================================
class TestCnaeBackfillSeed:
    def test_backfill_cnae_metadata_idempotent(self, test_app):
        with test_app.app_context():
            # Cria busca sem CNAE para testar preenchimento
            search = CompanySearch(
                company_name="BANCO BRADESCO S.A.",
                normalized_name="BANCO BRADESCO S.A.",
                cnae_codigo=None,
                cnae_descricao=None,
                economic_standing={"capital_social": 100000000000.0},
            )
            db.session.add(search)

            # Limpa cnae de um registro do catálogo para verificar backfill
            reg = CompanyRegistry.query.filter_by(cnpj="60746948000112").first()
            if reg:
                reg.cnae_codigo = None
                reg.cnae_descricao = None
            db.session.commit()

            # Executa a rotina
            stats = backfill_cnae_metadata(test_app)
            assert isinstance(stats, dict)
            assert stats["registries_updated"] >= 1
            assert stats["searches_updated"] >= 1

            # Valida que foi preenchido
            db.session.refresh(search)
            assert search.cnae_codigo == "6422-1/00"
            assert search.cnae_descricao == "Bancos múltiplos, com carteira comercial"
            assert search.economic_standing["cnae_codigo"] == "6422-1/00"

            if reg:
                db.session.refresh(reg)
                assert reg.cnae_codigo == "6422-1/00"
                assert reg.cnae_descricao == "Bancos múltiplos, com carteira comercial"

            # Segunda execução deve ser idempotente (0 atualizações)
            stats2 = backfill_cnae_metadata(test_app)
            assert stats2["searches_updated"] == 0
