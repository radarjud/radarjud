from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func

from app.extensions import db
from app.models.company_search import CompanySearch
from app.models.tenant import Tenant


class QuotaService:
    """
    Controla e audita o consumo de cotas mensais de consultas por Tenant.
    
    Regras:
    1. Apenas novas buscas (não servidas por cache/histórico) consomem cota.
    2. A contagem é baseada no mês corrente em UTC.
    3. Planos:
       - ESSENCIAL: 30 consultas/mês
       - PRO: 150 consultas/mês
       - ENTERPRISE: ilimitado (None)
    4. Ao esgotar 100% da cota, bloqueia requisições novas com HTTP 402/429.
    """

    @staticmethod
    def get_month_range(now_utc: datetime | None = None) -> tuple[datetime, datetime]:
        """Retorna o início do mês corrente e o início do próximo mês em UTC."""
        if now_utc is None:
            now_utc = datetime.now(UTC)
        start_month = datetime(now_utc.year, now_utc.month, 1, 0, 0, 0)
        if now_utc.month == 12:
            start_next_month = datetime(now_utc.year + 1, 1, 1, 0, 0, 0)
        else:
            start_next_month = datetime(now_utc.year, now_utc.month + 1, 1, 0, 0, 0)
        return start_month, start_next_month

    @classmethod
    def get_monthly_usage(cls, tenant_id: int, now_utc: datetime | None = None) -> int:
        """
        Retorna a contagem de novas pesquisas persistidas para o tenant no mês corrente.
        Usa COUNT agregada no banco para não alocar objetos em memória (Render Free 512 MB).
        """
        if not tenant_id:
            return 0
        start_month, start_next_month = cls.get_month_range(now_utc)
        count = (
            db.session.query(func.count(CompanySearch.id))
            .filter(
                CompanySearch.tenant_id == tenant_id,
                CompanySearch.created_at >= start_month,
                CompanySearch.created_at < start_next_month,
            )
            .scalar()
        )
        return int(count or 0)

    @classmethod
    def check_quota(cls, tenant: Tenant, now_utc: datetime | None = None, user: Any = None) -> dict[str, Any]:
        """
        Verifica a cota do tenant ou do usuário específico (se o usuário possuir plano individual).
        Retorna um dicionário com:
        - allowed: bool (se pode realizar nova busca)
        - is_unlimited: bool
        - limit: Optional[int]
        - used: int
        - remaining: Optional[int]
        - header_value: str (para o header X-Quota-Remaining)
        """
        if user and getattr(user, "plan", None) is not None:
            limit = user.limite_mensal_consultas
        else:
            limit = tenant.limite_mensal_consultas if tenant else 30

        # Se limite for None, consideramos ilimitado (ex.: Enterprise)
        if limit is None:
            used = cls.get_monthly_usage(tenant.id if tenant else 0, now_utc)
            return {
                "allowed": True,
                "is_unlimited": True,
                "limit": None,
                "used": used,
                "remaining": None,
                "header_value": "unlimited",
            }

        used = cls.get_monthly_usage(tenant.id if tenant else 0, now_utc)
        remaining = max(0, limit - used)
        allowed = used < limit

        return {
            "allowed": allowed,
            "is_unlimited": False,
            "limit": limit,
            "used": used,
            "remaining": remaining,
            "header_value": str(remaining),
        }
