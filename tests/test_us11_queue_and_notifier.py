# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import yaml

import app.config as config_module
from app.extensions import db
from app.models.company_search import CompanySearch
from app.models.monitored_supplier import MonitoredSupplier
from app.models.tenant import Tenant
from app.services.notifier import NotificationService
from app.services.task_dispatcher import (
    InProcessFallbackDispatcher,
    UpstashRedisDispatcher,
    get_task_dispatcher,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


class _FakeRedisClient:
    """Mock leve em memória da interface Redis (LPUSH / RPOP / LLEN / PING)."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.closed = False
        self.connection_pool = MagicMock()

    def ping(self) -> bool:
        return True

    def lpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        return len(bucket)

    def rpop(self, key: str) -> str | None:
        bucket = self.lists.get(key) or []
        if not bucket:
            return None
        return bucket.pop()

    def llen(self, key: str) -> int:
        return len(self.lists.get(key) or [])

    def close(self) -> None:
        self.closed = True


def _seed_tenant_and_supplier(app, cnpj: str = "11222333000181") -> tuple[int, int]:
    """Cria Tenant, CompanySearch com delta e MonitoredSupplier em banco de teste."""
    with app.app_context():
        tenant = Tenant(name=f"Tenant Monitor US-11 {cnpj}")
        db.session.add(tenant)
        db.session.flush()

        search = CompanySearch(
            tenant_id=tenant.id,
            company_name="Fornecedor Monitorado US11 Ltda",
            normalized_name=f"FORNECEDOR MONITORADO {cnpj}",
            total_processes=4,
            movements_hash="hash_novo_delta",
            outcomes_summary={"condenacoes": 3, "acordos": 1},
            overall_risk_score=40.0,
            economic_standing={"cenario": "CENARIO_A", "situacao": "ATIVA"},
        )
        db.session.add(search)

        supplier = MonitoredSupplier(
            tenant_id=tenant.id,
            cnpj=cnpj,
            company_name="Fornecedor Monitorado US11 Ltda",
            last_movements_hash="hash_anterior",
            last_condenacoes_count=1,
            last_scenario="CENARIO_A",
            last_rfb_status="ATIVA",
            last_tst_count=0,
            webhook_url="https://hooks.empresa.com.br/radarjus",
            alert_email="compliance@empresa.com.br",
            active=True,
        )
        db.session.add(supplier)
        db.session.commit()
        return tenant.id, supplier.id


def test_internal_run_monitoring_requires_valid_cron_secret(client, monkeypatch):
    # 1. Fail-closed quando CRON_SECRET não estiver configurado no servidor
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setitem(client.application.config, "CRON_SECRET", "")
    resp_unconfigured = client.post(
        "/api/v1/internal/run-monitoring",
        headers={"X-Cron-Secret": "qualquer-coisa"},
    )
    assert resp_unconfigured.status_code == 403

    # 2. Sem header X-Cron-Secret -> HTTP 403 Forbidden
    monkeypatch.setenv("CRON_SECRET", "cron-secret-seguro-us11")
    resp_missing = client.post("/api/v1/internal/run-monitoring")
    assert resp_missing.status_code == 403

    # 3. Header X-Cron-Secret inválido -> HTTP 403 Forbidden
    resp_invalid = client.post(
        "/api/v1/internal/run-monitoring",
        headers={"X-Cron-Secret": "segredo-incorreto"},
    )
    assert resp_invalid.status_code == 403


def test_internal_run_monitoring_in_process_fallback_dispatches_webhook_and_email(
    client, app, monkeypatch
):
    _seed_tenant_and_supplier(app, cnpj="11222333000181")
    monkeypatch.setenv("CRON_SECRET", "cron-secret-seguro-us11")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_123")
    monkeypatch.setenv("ALERT_FROM_EMAIL", "alertas@radarjus.com.br")

    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with (
        patch("app.services.monitoring_job.fetch_accurate_cnpj_data", return_value={"situacao": "ATIVA"}),
        patch("requests.post", side_effect=fake_post),
    ):
        resp = client.post(
            "/api/v1/internal/run-monitoring",
            headers={"X-Cron-Secret": "cron-secret-seguro-us11"},
            json={"batch_size": 10, "max_items": 25},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["dispatcher_backend"] == "in_process_fallback"
    assert data["enqueued"] == 1
    assert data["processed"] == 1
    assert data["succeeded"] == 1
    assert data["failed"] == 0
    assert data["remaining"] == 0
    assert data["duration_ms"] >= 0

    webhook_calls = [c for c in calls if c["url"] == "https://hooks.empresa.com.br/radarjus"]
    resend_calls = [c for c in calls if c["url"] == "https://api.resend.com/emails"]
    assert len(webhook_calls) == 1
    assert webhook_calls[0]["timeout"] == 5.0
    assert "NEW_CONVICTIONS" in webhook_calls[0]["json"]["triggers"]
    assert len(resend_calls) == 1
    assert resend_calls[0]["json"]["to"] == ["compliance@empresa.com.br"]
    assert resend_calls[0]["json"]["from"] == "alertas@radarjus.com.br"


def test_internal_run_monitoring_upstash_redis_mocked(client, app, monkeypatch):
    _seed_tenant_and_supplier(app, cnpj="22333444000192")
    monkeypatch.setenv("CRON_SECRET", "cron-secret-upstash")
    monkeypatch.setenv("REDIS_URL", "rediss://default:token@sa-east-1.upstash.io:6379")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_upstash")

    fake_redis_client = _FakeRedisClient()
    mock_redis_module = types.ModuleType("redis")
    from_url_mock = MagicMock(return_value=fake_redis_client)
    mock_redis_module.Redis = MagicMock(from_url=from_url_mock)

    calls: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with (
        patch.dict(sys.modules, {"redis": mock_redis_module}),
        patch("app.services.monitoring_job.fetch_accurate_cnpj_data", return_value={"situacao": "ATIVA"}),
        patch("requests.post", side_effect=fake_post),
    ):
        resp = client.post(
            "/api/v1/internal/run-monitoring",
            headers={"X-Cron-Secret": "cron-secret-upstash"},
            json={"batch_size": 10, "max_items": 25},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["dispatcher_backend"] == "upstash_redis"
    assert data["enqueued"] == 1
    assert data["processed"] == 1
    assert data["succeeded"] == 1
    assert data["failed"] == 0
    assert data["remaining"] == 0

    from_url_mock.assert_called_once_with(
        "rediss://default:token@sa-east-1.upstash.io:6379",
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        max_connections=5,
    )
    assert fake_redis_client.closed is True
    assert fake_redis_client.connection_pool.disconnect.called
    assert "https://hooks.empresa.com.br/radarjus" in calls
    assert "https://api.resend.com/emails" in calls


def test_task_dispatcher_falls_back_when_redis_unreachable():
    mock_redis_module = types.ModuleType("redis")
    bad_client = MagicMock()
    bad_client.ping.side_effect = ConnectionError("Upstash unreachable")
    mock_redis_module.Redis = MagicMock(from_url=MagicMock(return_value=bad_client))

    with patch.dict(sys.modules, {"redis": mock_redis_module}):
        dispatcher = get_task_dispatcher(redis_url="rediss://default:x@bad-host:6379")

    assert isinstance(dispatcher, InProcessFallbackDispatcher)

    dispatcher.enqueue("CUSTOM_TASK", {"id": 1})
    dispatcher.enqueue("CUSTOM_TASK", {"id": 2})
    dispatcher.enqueue("CUSTOM_TASK", {"id": 3})

    seen: list[int] = []
    summary = dispatcher.process_batch(
        max_items=2,
        handler=lambda _t, p: seen.append(p["id"]) or True,
    )
    assert seen == [1, 2]
    assert summary == {"processed": 2, "succeeded": 2, "failed": 0, "remaining": 1}


def test_notifier_webhook_exponential_retry_timeout_and_504():
    notifier = NotificationService(max_retries=3, backoff_base=0.5, timeout=5.0)

    # 1. Timeout nas 3 tentativas -> exatamente 3 tentativas e backoff 0.5s, 1.0s
    with (
        patch("requests.post", side_effect=requests.Timeout("Read timed out")) as mock_post_timeout,
        patch("app.services.notifier.time.sleep") as mock_sleep_timeout,
    ):
        res_timeout = notifier.send_webhook("https://hooks.slack.com/services/T0/B0/XX", {"company_name": "ACME"})

    assert res_timeout["ok"] is False
    assert res_timeout["attempts"] == 3
    assert mock_post_timeout.call_count == 3
    assert [c.args[0] for c in mock_sleep_timeout.call_args_list] == [0.5, 1.0]

    # 2. HTTP 504 nas 3 tentativas -> exatamente 3 tentativas e falha graciosa sem exceção
    resp_504 = MagicMock(status_code=504)
    with (
        patch("requests.post", return_value=resp_504) as mock_post_504,
        patch("app.services.notifier.time.sleep") as mock_sleep_504,
    ):
        res_504 = notifier.send_webhook("https://hooks.slack.com/services/T0/B0/XX", {"company_name": "ACME"})

    assert res_504["ok"] is False
    assert res_504["status_code"] == 504
    assert res_504["attempts"] == 3
    assert mock_post_504.call_count == 3
    assert [c.args[0] for c in mock_sleep_504.call_args_list] == [0.5, 1.0]

    # 3. 504 -> 504 -> 200 recupera na 3ª tentativa
    resp_200 = MagicMock(status_code=200)
    with (
        patch("requests.post", side_effect=[resp_504, resp_504, resp_200]) as mock_post_recover,
        patch("app.services.notifier.time.sleep") as mock_sleep_recover,
    ):
        res_recover = notifier.send_webhook("https://hooks.slack.com/services/T0/B0/XX", {"company_name": "ACME"})

    assert res_recover == {"ok": True, "status_code": 200, "attempts": 3, "error": None}
    assert mock_post_recover.call_count == 3
    assert [c.args[0] for c in mock_sleep_recover.call_args_list] == [0.5, 1.0]


def test_notifier_email_resend_and_smtp_fallback():
    notifier = NotificationService(
        resend_api_key="re_fail_key",
        from_email="alertas@radarjus.com.br",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="mailer",
        smtp_password="app-password",
        max_retries=3,
        backoff_base=0.5,
    )

    resp_502 = MagicMock(status_code=502)
    smtp_instance = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__.return_value = smtp_instance

    with (
        patch("requests.post", return_value=resp_502) as mock_resend,
        patch("smtplib.SMTP", return_value=smtp_cm) as mock_smtp_cls,
        patch("app.services.notifier.time.sleep"),
    ):
        res = notifier.send_email("compliance@cliente.com.br", "Alerta Teste", "<p>Delta detectado</p>")

    assert mock_resend.call_count == 3
    assert res["ok"] is True
    assert res["provider"] == "smtp"
    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=5.0)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("mailer", "app-password")
    smtp_instance.sendmail.assert_called_once()


def test_daily_monitoring_workflow_and_us11_config_specs():
    wf = yaml.safe_load((ROOT_DIR / ".github" / "workflows" / "daily_monitoring.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert "schedule" in triggers
    assert triggers["schedule"][0]["cron"] == "0 11 * * *"
    assert "workflow_dispatch" in triggers

    steps = wf["jobs"]["run-monitoring"]["steps"]
    cmd = "\n".join(step.get("run", "") for step in steps)
    assert "curl -f -s -S -X POST" in cmd
    assert "secrets.RENDER_API_URL" in cmd
    assert "/api/v1/internal/run-monitoring" in cmd
    assert "X-Cron-Secret" in cmd
    assert "secrets.CRON_SECRET" in cmd

    reqs = (ROOT_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert "redis>=5.0.0" in reqs

    env_ex = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    for var_name in (
        "REDIS_URL",
        "CRON_SECRET",
        "RESEND_API_KEY",
        "ALERT_FROM_EMAIL",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
    ):
        assert var_name in env_ex
        assert hasattr(config_module.Config, var_name)
