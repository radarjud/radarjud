# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import json
import logging
import os
import threading
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.services.notifier import NotificationService

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_KEY = "radarjus:tasks:queue"


def _execute_default_task(
    task_type: str,
    payload: dict[str, Any],
    notifier: NotificationService | None = None,
) -> bool:
    """Executa tarefas assíncronas padrão usando NotificationService."""
    service = notifier or NotificationService()
    normalized_type = str(task_type or "").strip().upper()

    if normalized_type in ("SUPPLIER_ALERT", "SEND_SUPPLIER_ALERT"):
        res = service.notify_alert(payload)
        return bool(res.get("ok"))

    if normalized_type == "SEND_WEBHOOK":
        res = service.send_webhook(
            webhook_url=str(payload.get("webhook_url") or ""),
            payload=payload.get("payload") or payload,
        )
        return bool(res.get("ok"))

    if normalized_type == "SEND_EMAIL":
        res = service.send_email(
            to_email=str(payload.get("to_email") or payload.get("alert_email") or ""),
            subject=str(payload.get("subject") or "[RadarJus] Alerta de Monitoramento"),
            html_body=str(payload.get("html_body") or ""),
            text_body=payload.get("text_body"),
        )
        return bool(res.get("ok"))

    logger.warning("Tipo de tarefa desconhecido ignorado na fila: %s", task_type)
    return False


class TaskDispatcher(ABC):
    """
    Abstração de fila assíncrona desacoplada das regras de negócio.
    Permite plugar Celery/RabbitMQ futuramente (v3+) mantendo o mesmo contrato.
    """

    backend_name: str = "abstract"

    @abstractmethod
    def enqueue(self, task_type: str, payload: dict[str, Any]) -> str:
        """Serializa e enfileira uma tarefa, retornando o task_id gerado."""

    @abstractmethod
    def process_batch(
        self,
        max_items: int = 25,
        handler: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> dict[str, int]:
        """Processa até `max_items` tarefas da fila e retorna métricas do lote."""

    @abstractmethod
    def pending_count(self) -> int:
        """Retorna a quantidade de itens pendentes na fila."""

    def close(self) -> None:
        """Libera recursos/conexões mantidos pelo dispatcher."""

    @staticmethod
    def _serialize_message(task_type: str, payload: dict[str, Any]) -> tuple[str, str]:
        task_id = uuid.uuid4().hex
        envelope = {
            "task_id": task_id,
            "task_type": str(task_type),
            "payload": payload if isinstance(payload, dict) else {"value": payload},
            "enqueued_at": datetime.now(UTC).isoformat(),
        }
        return task_id, json.dumps(envelope, ensure_ascii=False, default=str)

    @staticmethod
    def _run_item(
        envelope: dict[str, Any],
        handler: Callable[[str, dict[str, Any]], Any] | None = None,
        notifier: NotificationService | None = None,
    ) -> bool:
        task_type = str(envelope.get("task_type") or "")
        payload = envelope.get("payload") or {}
        if handler is not None:
            outcome = handler(task_type, payload)
        else:
            outcome = _execute_default_task(task_type, payload, notifier=notifier)

        if isinstance(outcome, dict) and "ok" in outcome:
            return bool(outcome["ok"])
        if outcome is None:
            return True
        return bool(outcome)


class UpstashRedisDispatcher(TaskDispatcher):
    """
    Implementação leve baseada em lista Redis (LPUSH / RPOP) compatível com
    Upstash Redis Serverless via TLS (`rediss://`) e limite rígido de RAM (< 50 MB) no Render Free.
    """

    backend_name: str = "upstash_redis"

    def __init__(
        self,
        redis_url: str | None = None,
        queue_key: str = DEFAULT_QUEUE_KEY,
        max_connections: int = 5,
        socket_timeout: float = 5.0,
        socket_connect_timeout: float = 5.0,
        redis_client: Any = None,
        notifier: NotificationService | None = None,
    ) -> None:
        self.redis_url = (redis_url if redis_url is not None else os.getenv("REDIS_URL", "")).strip()
        self.queue_key = queue_key
        self.max_connections = int(max_connections)
        self.socket_timeout = float(socket_timeout)
        self.socket_connect_timeout = float(socket_connect_timeout)
        self._notifier = notifier
        self._injected_client = redis_client is not None
        self._client = redis_client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        if not self.redis_url:
            raise ValueError("REDIS_URL não configurada para UpstashRedisDispatcher.")

        import redis

        self._client = redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
            socket_timeout=self.socket_timeout,
            socket_connect_timeout=self.socket_connect_timeout,
            max_connections=self.max_connections,
        )
        return self._client

    def close(self) -> None:
        """Fecha cliente/pool Redis para evitar sockets órfãos e retenção de memória."""
        client = self._client
        if client is None:
            return
        try:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Falha ao fechar cliente Redis: %s", exc)
        try:
            pool = getattr(client, "connection_pool", None)
            disconnect_fn = getattr(pool, "disconnect", None) if pool is not None else None
            if callable(disconnect_fn):
                disconnect_fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Falha ao desconectar pool Redis: %s", exc)
        if not self._injected_client:
            self._client = None

    def enqueue(self, task_type: str, payload: dict[str, Any]) -> str:
        task_id, raw_msg = self._serialize_message(task_type, payload)
        client = self._get_client()
        client.lpush(self.queue_key, raw_msg)
        return task_id

    def pending_count(self) -> int:
        client = self._get_client()
        return int(client.llen(self.queue_key) or 0)

    def process_batch(
        self,
        max_items: int = 25,
        handler: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> dict[str, int]:
        processed = 0
        succeeded = 0
        failed = 0
        limit = max(0, int(max_items))
        client = self._get_client()
        notifier = self._notifier or NotificationService()

        try:
            for _ in range(limit):
                raw_msg = client.rpop(self.queue_key)
                if raw_msg is None:
                    break
                processed += 1
                try:
                    if isinstance(raw_msg, bytes):
                        raw_msg = raw_msg.decode("utf-8")
                    envelope = json.loads(raw_msg)
                    ok = self._run_item(envelope, handler=handler, notifier=notifier)
                    if ok:
                        succeeded += 1
                    else:
                        failed += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.warning("Erro ao processar item da fila Redis: %s", exc)

            remaining = int(client.llen(self.queue_key) or 0)
        finally:
            self.close()

        return {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "remaining": remaining,
        }


class InProcessFallbackDispatcher(TaskDispatcher):
    """
    Fallback thread-safe em memória (`collections.deque` + `threading.Lock`)
    utilizado automaticamente quando `REDIS_URL` não está definida ou está inacessível.
    """

    backend_name: str = "in_process_fallback"

    def __init__(self, notifier: NotificationService | None = None) -> None:
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._notifier = notifier

    def enqueue(self, task_type: str, payload: dict[str, Any]) -> str:
        task_id, raw_msg = self._serialize_message(task_type, payload)
        with self._lock:
            self._queue.append(raw_msg)
        return task_id

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def process_batch(
        self,
        max_items: int = 25,
        handler: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> dict[str, int]:
        processed = 0
        succeeded = 0
        failed = 0
        limit = max(0, int(max_items))
        notifier = self._notifier or NotificationService()

        for _ in range(limit):
            with self._lock:
                if not self._queue:
                    break
                raw_msg = self._queue.popleft()

            processed += 1
            try:
                envelope = json.loads(raw_msg)
                ok = self._run_item(envelope, handler=handler, notifier=notifier)
                if ok:
                    succeeded += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("Erro ao processar item da fila in-process: %s", exc)

        with self._lock:
            remaining = len(self._queue)

        return {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "remaining": remaining,
        }


def get_task_dispatcher(
    redis_url: str | None = None,
    notifier: NotificationService | None = None,
) -> TaskDispatcher:
    """
    Instancia `UpstashRedisDispatcher` quando `REDIS_URL` estiver configurada e acessível;
    caso contrário retorna automaticamente `InProcessFallbackDispatcher`.
    """
    resolved_url = (redis_url if redis_url is not None else os.getenv("REDIS_URL", "")).strip()
    if not resolved_url:
        return InProcessFallbackDispatcher(notifier=notifier)

    dispatcher = UpstashRedisDispatcher(redis_url=resolved_url, notifier=notifier)
    try:
        client = dispatcher._get_client()
        client.ping()
        return dispatcher
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Redis indisponível (%s). Ativando fallback in-process automaticamente.",
            exc,
        )
        dispatcher.close()
        return InProcessFallbackDispatcher(notifier=notifier)
