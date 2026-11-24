import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import requests


def prepare_cnj_request(
    target_url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str], Optional[Dict[str, Any]], bool]:
    """
    Prepara uma requisição HTTP ao CNJ/DataJud:
    - Se CNJ_PROXY_URL estiver configurada, roteia pelo proxy Edge (Vercel gru1)
      anexando os query params na URL de destino via header `x-target-url` e enviando
      obrigatoriamente o header `x-proxy-secret` com `PROXY_SHARED_SECRET`.
    - Caso contrário, mantém a chamada direta com `target_url` e `params`.
    """
    proxy_url = (os.getenv("CNJ_PROXY_URL") or "").strip()
    if not proxy_url:
        return target_url, dict(headers or {}), params, False

    full_target_url = target_url
    if params:
        parsed = urlparse(target_url)
        query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key, value in params.items():
            if value is not None:
                query_items[str(key)] = str(value)
        full_target_url = urlunparse(parsed._replace(query=urlencode(query_items)))

    out_headers = dict(headers or {})
    out_headers["x-target-url"] = full_target_url
    proxy_secret = (os.getenv("PROXY_SHARED_SECRET") or "").strip()
    if proxy_secret:
        out_headers["x-proxy-secret"] = proxy_secret

    return proxy_url, out_headers, None, True


def cnj_get(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Any = 15,
) -> requests.Response:
    """Executa GET para APIs do CNJ/Comunica PJe, roteando por CNJ_PROXY_URL quando configurada."""
    req_url, req_headers, req_params, using_proxy = prepare_cnj_request(url, params=params, headers=headers)
    if using_proxy:
        return session.get(req_url, headers=req_headers, timeout=timeout)
    if headers is not None:
        return session.get(req_url, params=req_params, headers=req_headers, timeout=timeout)
    return session.get(req_url, params=req_params, timeout=timeout)


def cnj_post(
    session: requests.Session,
    url: str,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Any = 15,
) -> requests.Response:
    """Executa POST para APIs do DataJud/CNJ, roteando por CNJ_PROXY_URL quando configurada."""
    req_url, req_headers, _, using_proxy = prepare_cnj_request(url, headers=headers)
    if using_proxy:
        return session.post(req_url, json=json, headers=req_headers, timeout=timeout)
    return session.post(req_url, json=json, headers=req_headers, timeout=timeout)
