import time
import random
import logging
import threading
import math
from typing import Callable, Any, Dict, List, Tuple
from functools import wraps
import requests

logger = logging.getLogger(__name__)


class TokenBucketRateLimiter:
    """
    Controlador de taxa de requisições baseado no algoritmo Token Bucket,
    com suporte a sincronização thread-safe para chamadas paralelas em background.
    """

    def __init__(self, rate_per_second: float, capacity: float):
        """
        :param rate_per_second: Taxa contínua de reposição de tokens por segundo.
        :param capacity: Capacidade máxima do balde de tokens para acomodar pequenos bursts.
        """
        self.rate = rate_per_second
        self.capacity = capacity
        self.tokens = capacity
        self.last_check = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, block: bool = True) -> bool:
        """Adquire tokens antes de executar a chamada HTTP, aguardando caso o balde esteja vazio."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_check
                self.last_check = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

                if not block:
                    return False

                missing = tokens - self.tokens
                sleep_time = missing / self.rate

            # Dorme fora do lock para não prender outras threads
            time.sleep(max(0.01, sleep_time))


class RateLimiter:
    """
    Controladores de Rate Limit pré-configurados para as APIs Oficiais do CNJ:
    - DataJud: máx 2 a 5 requisições por segundo, com balde generoso para consultas interativas rápidas.
    - Comunica PJe: capacidade de burst de 20 tokens para garantir resposta imediata nas buscas do usuário,
      com reposição contínua sem pausas forçadas.
    """

    # DataJud: capacidade de burst de 30 tokens para execução paralela em alta velocidade
    datajud = TokenBucketRateLimiter(rate_per_second=5.0, capacity=30.0)

    # Comunica PJe: burst de 20 tokens para permitir paginação instantânea na busca interativa
    comunica_pje = TokenBucketRateLimiter(rate_per_second=2.0, capacity=20.0)


def with_exponential_backoff(
    func: Callable[..., requests.Response],
    *args: Any,
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    jitter: float = 0.5,
    **kwargs: Any,
) -> requests.Response:
    """
    Executa chamadas HTTP com política de Retry com Exponential Backoff e Jitter
    em caso de HTTP 429 (Too Many Requests) ou erros temporários 5xx.
    Respeita explicitamente o cabeçalho 'Retry-After' quando enviado pelo servidor do CNJ.
    """
    attempt = 0
    while True:
        try:
            resp = func(*args, **kwargs)

            # Se status for 429 ou erro 5xx de servidor, aplica política de backoff
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                attempt += 1

                # Verifica se o CNJ retornou header Retry-After
                retry_after_hdr = resp.headers.get("Retry-After")
                if retry_after_hdr and retry_after_hdr.isdigit():
                    wait_time = float(retry_after_hdr) + random.uniform(0.1, jitter)
                else:
                    wait_time = (initial_backoff * (2 ** (attempt - 1))) + random.uniform(0.05, jitter)

                logger.warning(
                    "Resposta HTTP %d recebida da API do CNJ. Tentativa %d/%d. Aguardando %.2fs antes de reexecutar...",
                    resp.status_code,
                    attempt,
                    max_retries,
                    wait_time,
                )
                time.sleep(wait_time)
                continue

            return resp

        except (requests.ConnectionError, requests.Timeout) as exc:
            attempt += 1
            if attempt > max_retries:
                raise exc

            wait_time = (initial_backoff * (2 ** (attempt - 1))) + random.uniform(0.05, jitter)
            logger.warning(
                "Falha de conexão com CNJ (%s). Tentativa %d/%d. Aguardando %.2fs...",
                exc,
                attempt,
                max_retries,
                wait_time,
            )
            time.sleep(wait_time)


class SlidingWindowRateLimiter:
    """
    Controlador de taxa de requisições inbound baseado em Janela Deslizante (Sliding Window).
    Leve, thread-safe e seguro para o limite de 512 MB RAM do Render Free.
    Segmenta chamadas por IP e por tenant_id (quando autenticado).
    """

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._history: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, client_key: str) -> Tuple[bool, int]:
        """
        Avalia se a requisição é permitida na janela atual.
        Retorna (permitido: bool, retry_after: int em segundos).
        """
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            # Purga periódica para prevenir memory leaks quando o mapa acumula muitas chaves
            if len(self._history) > 500:
                self._prune_stale(cutoff)

            timestamps = self._history.get(client_key, [])
            valid_timestamps = [ts for ts in timestamps if ts > cutoff]

            if len(valid_timestamps) >= self.max_requests:
                earliest = valid_timestamps[0]
                retry_after = max(1, int(math.ceil(earliest + self.window_seconds - now)))
                self._history[client_key] = valid_timestamps
                return False, retry_after

            valid_timestamps.append(now)
            self._history[client_key] = valid_timestamps
            return True, 0

    def _prune_stale(self, cutoff: float) -> None:
        stale_keys = [k for k, v in self._history.items() if not v or v[-1] <= cutoff]
        for k in stale_keys:
            self._history.pop(k, None)

    def reset(self) -> None:
        """Limpa todo o histórico em memória (útil para testes unitários)."""
        with self._lock:
            self._history.clear()


def get_client_rate_limit_key() -> str:
    """Gera chave única do cliente para rate limiting baseada em tenant_id ou IP de origem."""
    try:
        from flask import request as flask_req, g
        tenant_id = getattr(g, "tenant_id", None) or flask_req.headers.get("X-Tenant-ID")
        if tenant_id:
            return f"tenant:{tenant_id}"
        forwarded = flask_req.headers.get("X-Forwarded-For")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
            return f"ip:{ip}"
        return f"ip:{flask_req.remote_addr or '127.0.0.1'}"
    except Exception:
        return "ip:127.0.0.1"


def limit_rate(max_requests: int = 60, window_seconds: int = 60):
    """
    Decorator para proteção contra abuso/bots em rotas críticas da API.
    Retorna HTTP 429 com cabeçalho 'Retry-After' em caso de excedente.
    """
    limiter = SlidingWindowRateLimiter(max_requests=max_requests, window_seconds=window_seconds)

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            from flask import jsonify
            client_key = f"{f.__name__}:{get_client_rate_limit_key()}"
            allowed, retry_after = limiter.check(client_key)
            if not allowed:
                response = jsonify({
                    "error": "Limite de requisições excedido. Por favor, aguarde antes de tentar novamente.",
                    "retry_after": retry_after
                })
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response
            return f(*args, **kwargs)

        wrapper.limiter = limiter
        return wrapper

    return decorator
