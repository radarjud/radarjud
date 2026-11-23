from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


def get_utc_now():
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
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    def set_password(self, password: str):
        if not password or len(password) < 6:
            raise ValueError("A senha deve conter no mínimo 6 caracteres.")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash or not password:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
