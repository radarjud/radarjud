"""US-16: contrato de propriedade e encerramento da varredura do navegador."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.routes import api


def _number(n):
    return f"{n:07d}0020245020001"


@pytest.fixture
def scan_mocks(app, monkeypatch):
    monkeypatch.setattr(api, "SEARCH_CACHE", {})
    first = [{"numeroProcesso": _number(n)} for n in range(1, 101)]
    metrics = {
        "total_processos": 100,
        "scan_status": "PARTIAL_FAST_PATH",
        "processos": [{"numero_processo": _number(n), "tribunal": "TRT2", "sentence_outcome": "em_aberto"} for n in range(1, 101)],
        "desfechos_sentencas": {"em_aberto": 100},
        "niveis_sigilo": {},
    }
    analyze = Mock(side_effect=lambda **kwargs: dict(metrics))
    submit = Mock()
    monkeypatch.setattr(api.orchestrator, "analyze_company_by_name", analyze)
    monkeypatch.setattr(api.orchestrator.ai_analyzer, "analyze_company", lambda *args, **kwargs: {})
    monkeypatch.setattr(api.economic_service, "evaluate_economic_standing", lambda *args, **kwargs: {})
    monkeypatch.setattr(api.background_executor, "submit", submit)
    # Permite verificar o disparo do worker sem executar thread ou acessar APIs externas.
    monkeypatch.setitem(app.config, "TESTING", False)
    return first, analyze, submit


def _start(client, first, term="EMPRESA TESTE LTDA"):
    return client.post("/api/v1/search-company", json={
        "company_name": "EMPRESA TESTE LTDA", "force_refresh": True,
        "browser_deep_scan": True, "browser_query_term": term,
        "browser_page1_count": 903, "client_prefetched_items": first,
    })


def test_browser_owns_pagination_without_server_worker(client, scan_mocks):
    first, analyze, submit = scan_mocks
    response = _start(client, first)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scan_status"] == "BROWSER_PENDING"
    assert payload["search_id"]
    assert analyze.call_args.kwargs["client_prefetched_items"] == first
    submit.assert_not_called()
    status = client.get(f'/api/v1/search_status/{payload["search_id"]}').get_json()
    assert status["is_terminal"] is False


def test_mismatched_term_rejects_prefetch_and_keeps_server_fallback(client, scan_mocks):
    first, analyze, _submit = scan_mocks
    response = _start(client, first, "OUTRA EMPRESA LTDA")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scan_status"] == "PARTIAL_FAST_PATH"
    assert analyze.call_args.kwargs["client_prefetched_items"] == []
    assert payload.get("idoneidade_economica", {}).get("labor_scan_cursor", {}).get("layer") == 1


@pytest.mark.parametrize("complete,expected", [(True, "COMPLETED"), (False, "BROWSER_PARTIAL")])
def test_browser_ingestion_terminal_even_if_no_new_items(client, scan_mocks, complete, expected):
    first, _, submit = scan_mocks
    sid = _start(client, first).get_json()["search_id"]
    response = client.post("/api/v1/ingest-deep-scan", json={
        "search_id": sid, "items": [], "browser_scan_finished": True,
        "is_final_batch": complete,
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["scan_status"] == expected
    assert body["ingested_count"] == 0
    assert body["total_processes"] == 100
    assert sum(body["desfechos_sentencas"].values()) == 100
    status = client.get(f"/api/v1/search_status/{sid}").get_json()
    assert status["is_terminal"] is True
    assert status["is_completed"] is complete
    submit.assert_not_called()


def test_abandoned_browser_scan_expires(client, app, scan_mocks):
    first, _, submit = scan_mocks
    sid = _start(client, first).get_json()["search_id"]
    with app.app_context():
        search = db.session.get(CompanySearch, sid)
        search.last_checked_at = get_utc_now() - timedelta(seconds=121)
        db.session.commit()
    status = client.get(f"/api/v1/search_status/{sid}").get_json()
    assert status["is_terminal"] is True
    assert status["scan_status"] == "BROWSER_PARTIAL"
    submit.assert_not_called()
