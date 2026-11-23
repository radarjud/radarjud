from datetime import datetime, timezone
from app.extensions import db


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(db.Model):
    """
    Representa uma organização cliente (empresa assinante / tenant) na plataforma RadarJus.
    Garante o isolamento lógico e a segurança multitenancy dos históricos e diagnósticos.
    Suporta planos Essencial (30), Pro (150) e Enterprise (ilimitado/None).
    """
    __tablename__ = "tenants"

    PLAN_ESSENCIAL = "ESSENCIAL"
    PLAN_PRO = "PRO"
    PLAN_ENTERPRISE = "ENTERPRISE"

    DEFAULT_QUOTAS = {
        PLAN_ESSENCIAL: 30,
        PLAN_PRO: 150,
        PLAN_ENTERPRISE: None,  # Ilimitado
    }

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    cnpj = db.Column(db.String(20), nullable=True, index=True)
    plan = db.Column(db.String(50), default=PLAN_ESSENCIAL, nullable=False)
    limite_mensal_consultas = db.Column(db.Integer, nullable=True, default=30)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    # Relacionamentos
    users = db.relationship("User", backref="tenant", cascade="all, delete-orphan", lazy="select")
    searches = db.relationship("CompanySearch", backref="tenant", lazy="select")
    audit_logs = db.relationship("AuditLog", backref="tenant", lazy="select")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "cnpj": self.cnpj,
            "plan": self.plan,
            "limite_mensal_consultas": self.limite_mensal_consultas,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
