# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import html
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

RESEND_EMAILS_URL = "https://api.resend.com/emails"
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _build_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normaliza o payload JSON para compatibilidade simultânea com Slack (`text`),
    Microsoft Teams (`title`/`text`) e ERPs/webhooks corporativos (campos estruturados).
    Mantém estrita conformidade LGPD (sem nomes ou CPFs de pessoas físicas).
    """
    enriched = dict(payload or {})
    company = str(enriched.get("company_name") or "Fornecedor Monitorado")
    cnpj = str(enriched.get("cnpj") or "-")
    triggers = enriched.get("triggers") or []
    triggers_label = ", ".join(str(t) for t in triggers) if triggers else "MONITORING_UPDATE"

    enriched.setdefault("event", "SUPPLIER_ALERT")
    enriched.setdefault("title", f"RadarJus Alerta: {company} ({cnpj})")
    enriched.setdefault(
        "text",
        f"[RadarJus] Alerta de Monitoramento — {company} (CNPJ: {cnpj}) | Gatilhos: {triggers_label}",
    )
    return enriched


def build_alert_email_html(payload: dict[str, Any]) -> str:
    """Gera corpo HTML enxuto e seguro (escape XSS e sem PII de reclamantes) para alertas US-11."""
    company = html.escape(str(payload.get("company_name") or "Fornecedor Monitorado"))
    cnpj = html.escape(str(payload.get("cnpj") or "-"))
    timestamp = html.escape(str(payload.get("timestamp") or ""))
    triggers = payload.get("triggers") or []
    deltas = payload.get("deltas") or {}

    triggers_items = "".join(
        f"<li><strong>{html.escape(str(t))}</strong></li>" for t in triggers
    ) or "<li>Atualização de baseline detectada</li>"

    deltas_rows = ""
    for key, val in deltas.items():
        deltas_rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;border:1px solid #e2e8f0;'>{html.escape(str(key))}</td>"
            f"<td style='padding:6px 10px;border:1px solid #e2e8f0;'>{html.escape(str(val))}</td>"
            f"</tr>"
        )

    deltas_section = (
        "<table style='border-collapse:collapse;width:100%;margin-top:12px;'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:6px 10px;background:#f8fafc;border:1px solid #e2e8f0;'>Indicador</th>"
        "<th style='text-align:left;padding:6px 10px;background:#f8fafc;border:1px solid #e2e8f0;'>Variação</th>"
        "</tr></thead>"
        f"<tbody>{deltas_rows}</tbody></table>"
        if deltas_rows
        else ""
    )

    return (
        "<div style='font-family:Arial,sans-serif;color:#0f172a;max-width:640px;margin:0 auto;'>"
        "<h2 style='color:#0f4c81;margin-bottom:8px;'>RadarJus — Alerta Contínuo de Fornecedor</h2>"
        f"<p><strong>Empresa:</strong> {company}<br>"
        f"<strong>CNPJ:</strong> {cnpj}<br>"
        f"<strong>Detectado em:</strong> {timestamp}</p>"
        "<h3>Gatilhos Identificados</h3>"
        f"<ul>{triggers_items}</ul>"
        f"{deltas_section}"
        "<p style='font-size:12px;color:#64748b;margin-top:20px;'>"
        "Notificação automatizada pelo monitoramento contínuo RadarJus (LGPD by Design)."
        "</p></div>"
    )


class NotificationService:
    """
    Serviço resiliente de notificações (Webhook JSON + E-mail transacional via Resend/SMTP)
    com retry exponencial curto adequado ao orçamento de execução do Render Free.
    """

    def __init__(
        self,
        resend_api_key: str | None = None,
        from_email: str | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        smtp_use_tls: bool | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        timeout: float = 5.0,
    ) -> None:
        self.resend_api_key = (
            resend_api_key if resend_api_key is not None else os.getenv("RESEND_API_KEY", "")
        ).strip()
        self.from_email = (
            from_email if from_email is not None else os.getenv("ALERT_FROM_EMAIL", "alertas@radarjus.com.br")
        ).strip() or "alertas@radarjus.com.br"
        self.smtp_host = (smtp_host if smtp_host is not None else os.getenv("SMTP_HOST", "")).strip()
        raw_port = smtp_port if smtp_port is not None else (os.getenv("SMTP_PORT", "587") or "587")
        try:
            self.smtp_port = int(raw_port)
        except (TypeError, ValueError):
            self.smtp_port = 587
        self.smtp_user = (smtp_user if smtp_user is not None else os.getenv("SMTP_USER", "")).strip()
        self.smtp_password = (smtp_password if smtp_password is not None else os.getenv("SMTP_PASSWORD", "")).strip()
        if smtp_use_tls is None:
            self.smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("1", "true", "yes")
        else:
            self.smtp_use_tls = bool(smtp_use_tls)

        self.max_retries = max(1, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.timeout = max(0.5, float(timeout))

    def _sleep_backoff(self, attempt: int) -> None:
        """Aplica backoff exponencial (ex.: 0.5s, 1.0s, 2.0s) entre tentativas."""
        if self.backoff_base > 0:
            delay = self.backoff_base * (2 ** attempt)
            time.sleep(delay)

    def send_webhook(
        self,
        webhook_url: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        Envia payload JSON estruturado via HTTP POST com até 3 tentativas e retry exponencial.
        Nunca propaga exceções não tratadas para proteger o worker.
        """
        url = (webhook_url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {
                "ok": False,
                "status_code": None,
                "attempts": 0,
                "error": "Invalid webhook URL",
            }

        body = _build_webhook_payload(payload)
        req_timeout = float(timeout if timeout is not None else self.timeout)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RadarJus-Monitoring/1.1",
        }

        last_status: int | None = None
        last_error: str | None = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=req_timeout,
                )
                last_status = getattr(resp, "status_code", None)
                if last_status is not None and 200 <= int(last_status) < 300:
                    return {
                        "ok": True,
                        "status_code": int(last_status),
                        "attempts": attempt + 1,
                        "error": None,
                    }

                last_error = f"HTTP {last_status}"
                retryable = last_status in RETRYABLE_HTTP_STATUSES or (
                    last_status is not None and int(last_status) >= 500
                )
                if not retryable:
                    logger.warning(
                        "Webhook retornou status não-retentável %s após tentativa %d/%d",
                        last_status,
                        attempt + 1,
                        self.max_retries,
                    )
                    return {
                        "ok": False,
                        "status_code": int(last_status) if last_status is not None else None,
                        "attempts": attempt + 1,
                        "error": last_error,
                    }
            except requests.RequestException as exc:
                last_error = str(exc)
                logger.warning(
                    "Falha transitória no webhook (tentativa %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.exception(
                    "Erro inesperado ao disparar webhook (tentativa %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )

            if attempt < self.max_retries - 1:
                self._sleep_backoff(attempt)

        return {
            "ok": False,
            "status_code": last_status,
            "attempts": self.max_retries,
            "error": last_error or "Webhook delivery failed",
        }

    def _send_via_resend(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> dict[str, Any]:
        """Envia e-mail transacional via Resend API com retry exponencial."""
        headers = {
            "Authorization": f"Bearer {self.resend_api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "from": self.from_email,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        }
        if text_body:
            body["text"] = text_body

        last_status: int | None = None
        last_error: str | None = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    RESEND_EMAILS_URL,
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )
                last_status = getattr(resp, "status_code", None)
                if last_status is not None and 200 <= int(last_status) < 300:
                    return {
                        "ok": True,
                        "provider": "resend",
                        "status_code": int(last_status),
                        "attempts": attempt + 1,
                        "error": None,
                    }

                last_error = f"Resend HTTP {last_status}"
                retryable = last_status in RETRYABLE_HTTP_STATUSES or (
                    last_status is not None and int(last_status) >= 500
                )
                if not retryable:
                    return {
                        "ok": False,
                        "provider": "resend",
                        "status_code": int(last_status) if last_status is not None else None,
                        "attempts": attempt + 1,
                        "error": last_error,
                    }
            except requests.RequestException as exc:
                last_error = str(exc)
                logger.warning(
                    "Erro Resend API (tentativa %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.exception("Erro inesperado no envio Resend: %s", exc)

            if attempt < self.max_retries - 1:
                self._sleep_backoff(attempt)

        return {
            "ok": False,
            "provider": "resend",
            "status_code": last_status,
            "attempts": self.max_retries,
            "error": last_error or "Resend delivery failed",
        }

    def _send_via_smtp(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> dict[str, Any]:
        """Envia e-mail transacional via SMTP como fallback automático ao Resend."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_email
        msg["To"] = to_email
        if text_body:
            msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        last_error: str | None = None

        for attempt in range(self.max_retries):
            try:
                if self.smtp_port == 465:
                    with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout) as server:
                        if self.smtp_user and self.smtp_password:
                            server.login(self.smtp_user, self.smtp_password)
                        server.sendmail(self.from_email, [to_email], msg.as_string())
                else:
                    with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as server:
                        if self.smtp_use_tls:
                            server.starttls()
                        if self.smtp_user and self.smtp_password:
                            server.login(self.smtp_user, self.smtp_password)
                        server.sendmail(self.from_email, [to_email], msg.as_string())

                return {
                    "ok": True,
                    "provider": "smtp",
                    "status_code": 250,
                    "attempts": attempt + 1,
                    "error": None,
                }
            except (smtplib.SMTPException, OSError, TimeoutError) as exc:
                last_error = str(exc)
                logger.warning(
                    "Erro SMTP (tentativa %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.exception("Erro inesperado no envio SMTP: %s", exc)

            if attempt < self.max_retries - 1:
                self._sleep_backoff(attempt)

        return {
            "ok": False,
            "provider": "smtp",
            "status_code": None,
            "attempts": self.max_retries,
            "error": last_error or "SMTP delivery failed",
        }

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> dict[str, Any]:
        """
        Envia e-mail HTML transacional priorizando Resend API com fallback automático para SMTP.
        Nunca lança exceções não tratadas.
        """
        recipient = (to_email or "").strip()
        if not recipient or "@" not in recipient:
            return {
                "ok": False,
                "provider": None,
                "attempts": 0,
                "error": "Invalid recipient email",
            }

        resend_result: dict[str, Any] | None = None
        if self.resend_api_key:
            resend_result = self._send_via_resend(recipient, subject, html_body, text_body=text_body)
            if resend_result["ok"]:
                return resend_result

        if self.smtp_host:
            smtp_result = self._send_via_smtp(recipient, subject, html_body, text_body=text_body)
            if smtp_result["ok"] or resend_result is None:
                return smtp_result
            return {
                "ok": False,
                "provider": "smtp",
                "attempts": int(resend_result.get("attempts", 0)) + int(smtp_result.get("attempts", 0)),
                "error": f"Resend: {resend_result.get('error')} | SMTP: {smtp_result.get('error')}",
            }

        if resend_result is not None:
            return resend_result

        return {
            "ok": False,
            "provider": None,
            "attempts": 0,
            "error": "No email provider configured (RESEND_API_KEY or SMTP_HOST required)",
        }

    def notify_alert(self, alert_payload: dict[str, Any]) -> dict[str, Any]:
        """
        Despacha um alerta de monitoramento para os canais configurados (webhook_url e alert_email).
        """
        webhook_url = (alert_payload.get("webhook_url") or "").strip()
        alert_email = (alert_payload.get("alert_email") or "").strip()

        webhook_result = None
        email_result = None

        if webhook_url:
            webhook_result = self.send_webhook(webhook_url, alert_payload)

        if alert_email:
            company = str(alert_payload.get("company_name") or "Fornecedor Monitorado")
            cnpj = str(alert_payload.get("cnpj") or "")
            subject = f"[RadarJus] Alerta de Risco — {company} ({cnpj})"
            html_body = build_alert_email_html(alert_payload)
            email_result = self.send_email(alert_email, subject, html_body)

        webhook_ok = True if not webhook_url else bool(webhook_result and webhook_result.get("ok"))
        email_ok = True if not alert_email else bool(email_result and email_result.get("ok"))

        return {
            "ok": webhook_ok and email_ok,
            "webhook": webhook_result,
            "email": email_result,
        }
