import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

try:
    import jwt as _pyjwt
except ImportError:
    _pyjwt = None


def _get_secret_key() -> str:
    from flask import current_app
    try:
        if current_app:
            return current_app.config.get("SECRET_KEY", "radarjud-default-secret-key-change-in-prod")
    except Exception:
        pass
    return os.getenv("SECRET_KEY", "radarjud-default-secret-key-change-in-prod")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _native_hs256_encode(payload: Dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


def _native_hs256_decode(token: str, secret: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Token JWT malformado.")

    signing_input = f"{parts[0]}.{parts[1]}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(parts[2])

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Assinatura do token inválida.")

    payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    exp = payload.get("exp")
    if exp and time.time() > float(exp):
        raise ValueError("Token expirado.")

    return payload


def create_access_token(user, expires_in_minutes: int = 30) -> str:
    """Gera um JWT de curta duração (30 min) para acesso autenticado às rotas."""
    now = int(time.time())
    payload = {
        "sub": user.id,
        "tenant_id": user.tenant_id,
        "email": user.email,
        "role": user.role,
        "type": "access",
        "iat": now,
        "exp": now + (expires_in_minutes * 60),
    }
    secret = _get_secret_key()
    if _pyjwt:
        return _pyjwt.encode(payload, secret, algorithm="HS256")
    return _native_hs256_encode(payload, secret)


def create_refresh_token(user, expires_in_days: int = 7) -> str:
    """Gera um refresh token de longa duração (7 dias) para renovação do access token."""
    now = int(time.time())
    payload = {
        "sub": user.id,
        "tenant_id": user.tenant_id,
        "type": "refresh",
        "iat": now,
        "exp": now + (expires_in_days * 86400),
    }
    secret = _get_secret_key()
    if _pyjwt:
        return _pyjwt.encode(payload, secret, algorithm="HS256")
    return _native_hs256_encode(payload, secret)


def decode_token(token: str) -> Dict[str, Any]:
    """Decodifica e valida assinatura e expiração do JWT."""
    secret = _get_secret_key()
    if _pyjwt:
        return _pyjwt.decode(token, secret, algorithms=["HS256"])
    return _native_hs256_decode(token, secret)
