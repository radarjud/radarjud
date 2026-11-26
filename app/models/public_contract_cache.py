from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from app.extensions import db


def get_utc_now():
    """Retorna timestamp UTC naive compatível com colunas DateTime do SQLAlchemy."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PublicContractCache(db.Model):
    """
    Armazena em cache persistente de Nível 1 (L1) os metadados consolidados de contratos
    com o poder público (PNCP / Portal da Transparência) por CNPJ.
    Evita requisições HTTP redundantes para bases governamentais e mitiga rate limits.
    """
    __tablename__ = "public_contracts_cache"

    id = db.Column(db.Integer, primary_key=True)
    cnpj = db.Column(db.String(14), unique=True, nullable=False, index=True)
    data = db.Column(db.JSON, nullable=False)
    cached_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    ttl_seconds = db.Column(db.Integer, default=86400, nullable=False)  # Padrão 24 horas

    def is_valid(self, ttl_hours: Optional[int] = None) -> bool:
        """Verifica se o registro de cache ainda está dentro da janela de TTL."""
        if not self.cached_at:
            return False
        max_age = timedelta(hours=ttl_hours) if ttl_hours is not None else timedelta(seconds=self.ttl_seconds)
        return (get_utc_now() - self.cached_at) <= max_age

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "cnpj": self.cnpj,
            "data": self.data,
            "cached_at": self.cached_at.isoformat() if self.cached_at else None,
            "ttl_seconds": self.ttl_seconds,
            "is_valid": self.is_valid(),
        }
