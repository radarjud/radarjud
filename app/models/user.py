from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    """
    Representa um usuário com acesso ao sistema RadarJus sob um Tenant específico.
    Suporta RBAC com perfis 'ADMIN' e 'ANALISTA'.
    """
    __tablename__ = "users"

    ROLE_ADMIN = "ADMIN"
    ROLE_ANALISTA = "ANALISTA"
    VALID_ROLES = {ROLE_ADMIN, ROLE_ANALISTA}

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default=ROLE_ANALISTA, nullable=False)
    plan = db.Column(db.String(50), nullable=True)  # Plano individual (ESSENCIAL, PRO, ENTERPRISE). Se None, herda do Tenant.
    limite_mensal_consultas = db.Column(db.Integer, nullable=True)  # Limite individual. Se None, herda do Tenant.
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    @property
    def effective_plan(self) -> str:
        """Retorna o plano efetivo do usuário: individual se configurado, caso contrário herda do Tenant."""
        if self.plan:
            return self.plan
        return self.tenant.plan if self.tenant else "ESSENCIAL"

    @property
    def effective_limit(self) -> int | None:
        """Retorna o limite de consultas: individual se configurado, caso contrário herda do Tenant."""
        if self.plan:
            return self.limite_mensal_consultas
        return self.tenant.limite_mensal_consultas if self.tenant else 30

    def set_password(self, password: str):
        if not password or len(password) < 6:
            raise ValueError("A senha deve conter no mínimo 6 caracteres.")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash or not password:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        # Garante estritamente que senha e hash de senha jamais sejam expostos em to_dict()
        eff_plan = self.effective_plan
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "plan": eff_plan,
            "user_plan": self.plan,
            "limite_mensal_consultas": self.effective_limit,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
