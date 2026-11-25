# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

from datetime import UTC, datetime

from app.extensions import db


def get_utc_now():
    """Retorna timestamp UTC naive compatível com SQLAlchemy."""
    return datetime.now(UTC).replace(tzinfo=None)


class MonitoredSupplier(db.Model):
    """
    Representa um fornecedor homologado submetido ao monitoramento contínuo de passivo trabalhista
    e conformidade cadastral (US-09). Isolado estritamente por tenant.
    """
    __tablename__ = "monitored_suppliers"

    __table_args__ = (
        db.UniqueConstraint("tenant_id", "cnpj", name="uq_monitored_suppliers_tenant_cnpj"),
        db.Index("ix_monitored_suppliers_tenant_active", "tenant_id", "active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    cnpj = db.Column(db.String(20), nullable=False, index=True)
    company_name = db.Column(db.String(255), nullable=False)

    # Baseline Snapshot para Detecção Determinística de Delta
    last_movements_hash = db.Column(db.String(64), nullable=True)
    last_condenacoes_count = db.Column(db.Integer, default=0, nullable=False)
    last_scenario = db.Column(db.String(20), default="CENARIO_A", nullable=False)
    last_rfb_status = db.Column(db.String(50), default="ATIVA", nullable=True)
    last_tst_count = db.Column(db.Integer, default=0, nullable=False)

    # Canais de Notificação / Webhooks
    webhook_url = db.Column(db.String(500), nullable=True)
    alert_email = db.Column(db.String(255), nullable=True)

    active = db.Column(db.Boolean, default=True, nullable=False, index=True)

    last_checked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    # Relacionamento com Tenant
    tenant = db.relationship("Tenant", backref=db.backref("monitored_suppliers", cascade="all, delete-orphan", lazy="select"))

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "cnpj": self.cnpj,
            "company_name": self.company_name,
            "last_movements_hash": self.last_movements_hash,
            "last_condenacoes_count": self.last_condenacoes_count,
            "last_scenario": self.last_scenario,
            "last_rfb_status": self.last_rfb_status,
            "last_tst_count": self.last_tst_count,
            "webhook_url": self.webhook_url,
            "alert_email": self.alert_email,
            "active": self.active,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
