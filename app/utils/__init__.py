"""
Módulo de utilitários centrais do RadarJus:
- Sanitização de Razão Social e consulta BrasilAPI para CNPJs.
- Geração de hash SHA-256 para invalidação inteligente de cache.
- Rate Limiter resiliente com Token Bucket e Exponential Backoff para APIs do CNJ.
"""

from app.utils.sanitizer import (
    sanitize_company_name,
    is_cnpj,
    fetch_cnpj_brasilapi,
    validate_webhook_url,
    validate_alert_email,
)
from app.utils.crypto import generate_movements_hash
from app.utils.rate_limiter import RateLimiter, with_exponential_backoff

__all__ = [
    "sanitize_company_name",
    "is_cnpj",
    "fetch_cnpj_brasilapi",
    "validate_webhook_url",
    "validate_alert_email",
    "generate_movements_hash",
    "RateLimiter",
    "with_exponential_backoff",
]

