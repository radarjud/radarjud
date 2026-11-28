# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
import os
import logging
from typing import Dict, Any, List
from app.extensions import db
from app.models.tenant import Tenant
from app.models.user import User

logger = logging.getLogger(__name__)

DEFAULT_ORGANIZATION = {
    "name": "RadarJus Matriz",
    "cnpj": "00000000000191",
    "plan": Tenant.PLAN_ENTERPRISE,
    "limite_mensal_consultas": None,
}

DEFAULT_USERS: List[Dict[str, Any]] = [
    {
        "name": "Administrador RadarJus",
        "email": "admin@radarjus.com.br",
        "password": os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123"),
        "role": User.ROLE_ADMIN,
    },
    {
        "name": "Analista de Compliance",
        "email": "analista@radarjus.com.br",
        "password": os.getenv("DEFAULT_ANALISTA_PASSWORD", "analista123"),
        "role": User.ROLE_ANALISTA,
    },
]


def seed_default_users(commit: bool = True) -> Dict[str, Any]:
    """
    Semeia os usuários padrão de sistema (Admin e Analista) com idempotência estrita.
    Caso já existam no banco de dados (busca case-insensitive por e-mail),
    não duplica registros e preserva a integridade do banco.
    """
    created_users: List[str] = []
    existing_users: List[str] = []

    try:
        # 1. Obtém ou cria a Organização (Tenant) padrão
        tenant = Tenant.query.filter_by(name=DEFAULT_ORGANIZATION["name"]).first()
        if not tenant:
            # Reutiliza o primeiro tenant caso já exista outro no sistema
            tenant = Tenant.query.order_by(Tenant.id.asc()).first()
            if not tenant:
                tenant = Tenant(
                    name=DEFAULT_ORGANIZATION["name"],
                    cnpj=DEFAULT_ORGANIZATION["cnpj"],
                    plan=DEFAULT_ORGANIZATION["plan"],
                    limite_mensal_consultas=DEFAULT_ORGANIZATION["limite_mensal_consultas"],
                    is_active=True,
                )
                db.session.add(tenant)
                db.session.flush()
                logger.info("Tenant padrão criado: %s (id=%d)", tenant.name, tenant.id)

        # 2. Semeia os usuários de forma estritamente idempotente
        for spec in DEFAULT_USERS:
            clean_email = str(spec["email"]).strip().lower()
            existing = User.query.filter_by(email=clean_email).first()
            if existing:
                existing_users.append(existing.email)
                continue

            user = User(
                tenant_id=tenant.id,
                name=spec["name"],
                email=clean_email,
                role=spec["role"],
                is_active=True,
            )
            user.set_password(spec["password"])
            db.session.add(user)
            created_users.append(clean_email)
            logger.info("Usuário padrão criado: %s com perfil %s (tenant_id=%d)", clean_email, spec["role"], tenant.id)

        if commit and created_users:
            db.session.commit()
            logger.info("Seed de usuários padrão finalizado com sucesso. Novos criados: %s", created_users)
        elif commit:
            db.session.commit()

        return {
            "status": "success",
            "created": created_users,
            "existing": existing_users,
            "tenant_id": tenant.id if tenant else None,
        }

    except Exception as exc:
        if commit:
            db.session.rollback()
        logger.warning("Falha defensiva ao semear usuários padrão: %s", exc)
        return {
            "status": "error",
            "created": created_users,
            "existing": existing_users,
            "error": str(exc),
        }
