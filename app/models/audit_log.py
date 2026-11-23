from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import event

from app.extensions import db


def get_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AuditLog(db.Model):
    """
    Registro imutável (append-only) de decisões de compliance tomadas por analistas.
    Garante trilha de auditoria completa com snapshot do score de risco e cenário econômico.
    """
    __tablename__ = "audit_logs"

    DECISION_APROVADO = "APROVADO"
    DECISION_REPROVADO = "REPROVADO"
    DECISION_RETENCAO = "RETENCAO"
    VALID_DECISIONS: ClassVar[set[str]] = {DECISION_APROVADO, DECISION_REPROVADO, DECISION_RETENCAO}

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    company_search_id = db.Column(db.Integer, db.ForeignKey("company_searches.id", ondelete="CASCADE"), nullable=False, index=True)

    decision = db.Column(db.String(20), nullable=False, index=True)
    justification = db.Column(db.Text, nullable=False)
    risk_score_snapshot = db.Column(db.JSON, nullable=False)

    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False, index=True)

    # Relacionamentos
    user = db.relationship("User", backref="audit_logs", lazy="joined")
    company_search = db.relationship("CompanySearch", backref="audit_logs", lazy="joined")

    def to_dict(self):
        user_name = self.user.name if self.user else None
        user_email = self.user.email if self.user else None
        company_name = self.company_search.company_name if self.company_search else None

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "analyst_name": user_name,
            "analyst_email": user_email,
            "company_search_id": self.company_search_id,
            "company_name": company_name,
            "decision": self.decision,
            "justification": self.justification,
            "risk_score_snapshot": self.risk_score_snapshot,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Imutabilidade estrita na camada ORM (Append-Only Enforcement)
@event.listens_for(AuditLog, "before_update")
def block_audit_log_update(mapper, connection, target):
    raise PermissionError("AuditLog é imutável: atualizações não são permitidas.")


@event.listens_for(AuditLog, "before_delete")
def block_audit_log_delete(mapper, connection, target):
    raise PermissionError("AuditLog é imutável: exclusões não são permitidas.")
