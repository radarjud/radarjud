# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import sqlite3
from unittest.mock import MagicMock, patch

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.CompanyRegistry import CompanyRegistry
from app.seeds.catalog_seed import get_full_catalog_entries, seed_company_registry
from app.services.cnj_searcher import (
    SYNC_SCAN_MAX_PAGES,
    SYNC_SCAN_MAX_RECORDS,
    SYNC_SCAN_TIME_BUDGET_SECONDS,
    CNJProcessSearcher,
)
from scripts.import_company_catalog import load_records_from_sqlite


def test_catalog_seed_includes_soserv_and_is_idempotent(app):
    with app.app_context():
        CompanyRegistry.query.delete()
        db.session.commit()

        # Simula os 49 registros iniciais existentes sem duplicação
        first_49 = list(get_full_catalog_entries().values())[:49]
        for meta in first_49:
            db.session.add(
                CompanyRegistry(
                    cnpj=meta["cnpj"],
                    razao_social=meta["razao_social"],
                    capital_social=0.0,
                )
            )
        db.session.commit()
        assert CompanyRegistry.query.count() == 49

        summary1 = seed_company_registry(enrich_via_brasilapi=False, commit=True)
        assert summary1["inserted"] > 0
        assert summary1["total"] == len(get_full_catalog_entries())

        # Garante inclusão das duas empresas SOSERVI (matriz e R$ 690.000,00) + SOSERV ativa
        soserv_matriz = CompanyRegistry.query.filter_by(cnpj="09863853000121").first()
        soserv_vigilancia = CompanyRegistry.query.filter_by(cnpj="11572781000105").first()
        assert soserv_matriz is not None
        assert float(soserv_matriz.capital_social) == 14510000.0
        assert soserv_vigilancia is not None
        assert float(soserv_vigilancia.capital_social) == 690000.0

        # Reexecução estritamente idempotente: 0 inserções, 0 duplicatas
        summary2 = seed_company_registry(enrich_via_brasilapi=False, commit=True)
        assert summary2["inserted"] == 0
        assert summary2["updated"] == 0
        assert summary2["total"] == summary1["total"]


def test_import_company_catalog_reads_legacy_sqlite(tmp_path):
    legacy_db = tmp_path / "legacy_radarjud.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE company_registry (
            cnpj TEXT PRIMARY KEY,
            razao_social TEXT,
            nome_fantasia TEXT,
            situacao_cadastral TEXT,
            matriz_filial TEXT,
            municipio TEXT,
            uf TEXT,
            capital_social REAL,
            cnae_codigo TEXT,
            cnae_descricao TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO company_registry VALUES (
            '11572781000105', 'SOSERVI VIGILANCIA LTDA', 'SOSERVI VIGILANCIA',
            'ATIVA', '1', 'OLINDA', 'PE', 690000.0, '8011-1/01',
            'Atividades de vigilância e segurança privada'
        )
        """
    )
    conn.commit()
    conn.close()

    rows = load_records_from_sqlite(legacy_db)
    assert len(rows) == 1
    assert rows[0]["cnpj"] == "11572781000105"
    assert float(rows[0]["capital_social"]) == 690000.0


def test_scan_calibration_strips_html_and_enforces_budget_and_caps():
    assert SYNC_SCAN_TIME_BUDGET_SECONDS == 50.0
    assert SYNC_SCAN_MAX_PAGES == 15
    assert SYNC_SCAN_MAX_RECORDS == 1500

    searcher = CNJProcessSearcher()
    raw_html = (
        "<div class='pub'><p>Condeno a reclamada CNPJ 09.863.853/0001-21 ao pagamento de "
        "horas extras e adicional de insalubridade.</p>" + ("<span>texto longo </span>" * 400) + "</div>"
    )
    processes: dict = {}
    searcher._parse_comunica_items(
        [
            {
                "numeroProcesso": "00001234520245060001",
                "siglaTribunal": "TRT6",
                "texto": raw_html,
                "destinatarios": [{"nome": "SOSERVI LTDA"}],
            }
        ],
        processes,
    )
    parsed = processes["00001234520245060001"]
    assert "09863853000121" in parsed["cnpjs_encontrados"]
    assert "<div" not in parsed["publicacoes_texto"][0]
    assert len(parsed["publicacoes_texto"][0]) <= 1200

    merged = searcher._merge_process_data_fast("00001234520245060001", parsed, None)
    assert merged["publicacoes_texto"] == []
    assert "<div" not in merged["resumoPublicacao"]
    assert len(merged["resumoPublicacao"]) <= 500
    assert "09863853000121" in merged["cnpjs_encontrados"]

    # Valida orçamento de tempo em paginate_comunica_pje_standard
    page_items = [
        {"numeroProcesso": f"{i:07d}0020245060001", "siglaTribunal": "TRT6", "texto": "horas extras"}
        for i in range(1, 51)
    ]
    mock_resp = MagicMock(ok=True, status_code=200)
    mock_resp.json.return_value = {"items": page_items}

    ticks = iter([0.0, 1.0, 51.0])  # página 1 executa, página 2 ultrapassa 50s
    with (
        patch.object(searcher, "_request_gov", return_value=mock_resp),
        patch("app.services.cnj_searcher.time.monotonic", side_effect=lambda: next(ticks, 55.0)),
    ):
        res = searcher.paginate_comunica_pje_standard(
            search_strategy={"comunica_query_term": "SOSERVI", "mode": "BROAD_BRAND", "is_broad_brand": True},
            start_page=1,
            max_pages=15,
            time_budget_seconds=50.0,
            max_records=1500,
        )

    assert res["pages_read"] == 1
    assert res["truncated"] is True
    assert res["stop_reason"] == "TIME_BUDGET"


def test_l1_database_cache_serves_browser_partial_without_external_calls(client, app):
    import time

    with app.app_context():
        search = CompanySearch(
            company_name="SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA",
            normalized_name="SOSERVI SOCIEDADE DE GERAIS",
            total_processes=1500,
            last_checked_at=get_utc_now(),
            scan_status="BROWSER_PARTIAL",
            outcomes_summary={"condenacoes": 400, "acordos": 300, "improcedentes": 200, "em_aberto": 600},
            ranking_motivos=[{"motivo": "Horas Extras", "quantidade": 500, "porcentagem": 33.3}],
            predictions_summary=[{"motivo": "Horas Extras", "probabilidade_condenacao_pct": 62.0}],
            economic_standing={
                "cenario": "CENARIO_A",
                "situacao_cadastral": "ATIVA",
                "search_cache_key": "STRICT_CNPJ::SOSERVI SOCIEDADE DE GERAIS::09863853000121",
                "valor_formatado": "R$ 14.510.000,00",
            },
            ai_analysis={"resumo_executivo": "Parecer persistido em cache L1."},
            overall_risk_score=45.0,
        )
        db.session.add(search)
        db.session.commit()

    with (
        patch("app.routes.api.orchestrator.analyze_company_by_name") as mock_analyze,
        patch("app.routes.api.economic_service.evaluate_economic_standing") as mock_econ,
        patch("app.routes.api.orchestrator.ai_analyzer.analyze_company") as mock_ai,
    ):
        t0 = time.perf_counter()
        resp = client.post(
            "/api/v1/search-company",
            json={
                "company_name": "09.863.853/0001-21",
                "browser_deep_scan": True,
            },
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert resp.status_code == 200
    assert elapsed_ms < 300.0
    body = resp.get_json()
    assert body["cached"] is True
    assert body["cache_level"] == "L1_DATABASE"
    assert body["total_processos"] == 1500
    assert body["ai_analysis"]["resumo_executivo"] == "Parecer persistido em cache L1."
    mock_analyze.assert_not_called()
    mock_econ.assert_not_called()
    mock_ai.assert_not_called()
