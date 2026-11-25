import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import yaml

import app.config as config_module
from app.services.cnj_client import CNJClient
from app.services.cnj_searcher import CNJProcessSearcher
from app.services.economic_service import EconomicStandingService
from app.services.orchestrator import CNJMetadataOrchestrator


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_database_url_converts_postgres_scheme_and_falls_back_to_sqlite(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@aws-0-sa-east-1.pooler.supabase.com:6543/postgres?sslmode=require")
    uri = config_module._database_url()
    assert uri.startswith("postgresql://")
    assert uri == "postgresql://user:pass@aws-0-sa-east-1.pooler.supabase.com:6543/postgres?sslmode=require"

    opts = config_module._build_engine_options(uri)
    assert opts == {
        "pool_pre_ping": True,
        "pool_size": 3,
        "max_overflow": 2,
        "pool_recycle": 300,
    }
    assert "connect_args" not in opts

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    fallback_uri = config_module._database_url()
    assert fallback_uri.startswith("sqlite:///")
    assert config_module._build_engine_options(fallback_uri) == {"pool_pre_ping": True}


def test_render_yaml_and_vercel_json_us10_specs():
    render_data = yaml.safe_load((ROOT_DIR / "render.yaml").read_text(encoding="utf-8"))
    svc = render_data["services"][0]
    assert svc["healthCheckPath"] == "/api/v1/health"
    assert "--workers 1" in svc["startCommand"]
    assert "--threads 4" in svc["startCommand"]
    assert "--max-requests 300" in svc["startCommand"]
    assert "--timeout 120" in svc["startCommand"]
    assert "--bind 0.0.0.0:$PORT" in svc["startCommand"]
    assert "run:app" in svc["startCommand"]

    env_map = {item["key"]: item for item in svc["envVars"]}
    for required_secret in (
        "DATABASE_URL",
        "CNJ_PROXY_URL",
        "PROXY_SHARED_SECRET",
        "GEMINI_API_KEY",
        "DATAJUD_API_KEY",
        "PORTAL_TRANSPARENCIA_API_KEY",
    ):
        assert required_secret in env_map
        assert env_map[required_secret].get("sync") is False

    vercel_data = json.loads((ROOT_DIR / "frontend" / "vercel.json").read_text(encoding="utf-8"))
    assert vercel_data["regions"] == ["gru1"]


def test_cnj_proxy_headers_sent_across_all_backend_clients(monkeypatch):
    monkeypatch.setenv("CNJ_PROXY_URL", "https://radarjus.vercel.app/api/cnj-proxy")
    monkeypatch.setenv("CNJ_FORCE_PROXY", "true")
    monkeypatch.setenv("PROXY_SHARED_SECRET", "super-secret-token")
    monkeypatch.setenv("DATAJUD_API_KEY", "test-datajud-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.ok = True
    mock_resp.json.return_value = {"items": [], "hits": {"hits": []}}

    # 1. CNJProcessSearcher (Comunica PJe GET + DataJud POST)
    searcher = CNJProcessSearcher()
    with patch.object(searcher.session, "get", return_value=mock_resp) as m_get:
        searcher.fetch_single_comunica_page("EMPRESA TESTE", page_num=1)
        m_get.assert_called_once()
        assert m_get.call_args[0][0] == "https://radarjus.vercel.app/api/cnj-proxy"
        hdrs = m_get.call_args[1]["headers"]
        assert hdrs["x-proxy-secret"] == "super-secret-token"
        assert "comunicaapi.pje.jus.br" in hdrs["X-Target-Url"]

    with patch.object(searcher.session, "post", return_value=mock_resp) as m_post:
        searcher.enrich_top_tribunals_datajud({"00000012320245020001": {"siglaTribunal": "TRT2"}})
        m_post.assert_called_once()
        assert m_post.call_args[0][0] == "https://radarjus.vercel.app/api/cnj-proxy"
        hdrs = m_post.call_args[1]["headers"]
        assert hdrs["x-proxy-secret"] == "super-secret-token"
        assert "api-publica.datajud.cnj.jus.br/api_publica_trt2/_search" in hdrs["X-Target-Url"]

    # 2. CNJMetadataOrchestrator (_query_datajud_tribunal POST)
    orchestrator = CNJMetadataOrchestrator()
    with patch.object(orchestrator.session, "post", return_value=mock_resp) as m_orch_post:
        orchestrator._query_datajud_tribunal("trt2", ["00000012320245020001"], {"Content-Type": "application/json"})
        m_orch_post.assert_called_once()
        assert m_orch_post.call_args[0][0] == "https://radarjus.vercel.app/api/cnj-proxy"
        assert m_orch_post.call_args[1]["headers"]["x-proxy-secret"] == "super-secret-token"

    # 3. CNJClient (GET + POST) e fallback automatico em 403
    monkeypatch.setenv("CNJ_FORCE_PROXY", "false")
    blocked_resp = MagicMock(status_code=403, ok=False)
    client = CNJClient()
    with patch.object(client.session, "get", side_effect=[blocked_resp, mock_resp]) as m_cli_get:
        client.fetch_comunica_pje_by_name("EMPRESA TESTE")
        assert m_cli_get.call_count == 2
        assert m_cli_get.call_args_list[1][0][0] == "https://radarjus.vercel.app/api/cnj-proxy"
        assert m_cli_get.call_args_list[1][1]["headers"]["x-proxy-secret"] == "super-secret-token"

    with patch.object(client.session, "post", side_effect=[blocked_resp, mock_resp]) as m_cli_post:
        client.fetch_datajud_process_metadata("00000012320245020001")
        assert m_cli_post.call_count == 2
        assert m_cli_post.call_args_list[1][0][0] == "https://radarjus.vercel.app/api/cnj-proxy"
        assert m_cli_post.call_args_list[1][1]["headers"]["x-proxy-secret"] == "super-secret-token"


def test_flask_cnj_proxy_requires_secret_and_enforces_whitelist(client, monkeypatch):
    monkeypatch.setenv("PROXY_SHARED_SECRET", "flask-proxy-secret")

    r_missing = client.get("/api/cnj-proxy?target=https://comunicaapi.pje.jus.br/api/v1/comunicacao")
    assert r_missing.status_code == 401

    r_bad_host = client.get(
        "/api/cnj-proxy?target=https://evil.example.com/data",
        headers={"x-proxy-secret": "flask-proxy-secret"},
    )
    assert r_bad_host.status_code == 400


def test_portal_transparencia_api_key_loaded_from_env(monkeypatch):
    monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "cgu-env-key-123")
    svc = EconomicStandingService()
    assert svc.cgu_api_key == "cgu-env-key-123"


def test_no_hardcoded_datajud_or_secret_literals_in_source():
    forbidden_prefix = "cDZHYz" + "lZa0JadVREZDJCendQbXY6"
    forbidden_fallback = "dev-insecure-" + "fallback-key"
    for rel in (
        "app/config.py",
        "app/services/cnj_searcher.py",
        "app/services/orchestrator.py",
        "app/services/cnj_client.py",
        "app/utils/http_client.py",
        "frontend/api/cnj-proxy.js",
        "render.yaml",
    ):
        txt = (ROOT_DIR / rel).read_text(encoding="utf-8")
        assert forbidden_prefix not in txt
        assert forbidden_fallback not in txt
