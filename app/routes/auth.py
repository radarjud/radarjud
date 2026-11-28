import logging
from flask import Blueprint, request, jsonify, g
from app.extensions import db
from app.models.tenant import Tenant
from app.models.user import User
from app.utils.auth import require_auth, require_role, get_token_from_header, get_current_user_optional
from app.utils.jwt import create_access_token, create_refresh_token, decode_token

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth_bp", __name__, url_prefix="/api/v1/auth")


@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Cadastra uma nova organização (Tenant) e o usuário inicial (ADMIN).
    Retorna tokens de acesso (30 min) e refresh (7 dias), com isolamento por tenant.
    """
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "").strip()
    name = str(data.get("name") or "").strip()
    tenant_name = str(data.get("tenant_name") or data.get("company_name") or "").strip()
    cnpj = str(data.get("cnpj") or "").strip() or None
    role = str(data.get("role") or User.ROLE_ADMIN).strip().upper()

    if not email or "@" not in email:
        return jsonify({"error": "Informe um endereço de e-mail válido."}), 400

    if not password or len(password) < 6:
        return jsonify({"error": "A senha deve conter no mínimo 6 caracteres."}), 400

    if not name:
        return jsonify({"error": "O campo 'name' é obrigatório."}), 400

    if role not in User.VALID_ROLES:
        role = User.ROLE_ADMIN

    # Verifica se e-mail já existe
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"error": "Email já cadastrado no sistema."}), 409

    try:
        # Se tenant_name não informado, usa o nome do usuário/empresa
        final_tenant_name = tenant_name or f"Organização de {name}"
        tenant = Tenant(name=final_tenant_name, cnpj=cnpj)
        db.session.add(tenant)
        db.session.flush()

        user = User(
            tenant_id=tenant.id,
            name=name,
            email=email,
            role=role,
            is_active=True,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        access_token = create_access_token(user)
        refresh_token = create_refresh_token(user)

        logger.info("Novo Tenant (id=%d, name=%s) e Usuário (id=%d, email=%s) cadastrados com sucesso.",
                    tenant.id, tenant.name, user.id, user.email)

        return jsonify({
            "status": "success",
            "message": "Usuário e organização cadastrados com sucesso.",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": 1800,
            "user": user.to_dict(),
            "tenant": tenant.to_dict(),
        }), 201

    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro ao registrar novo usuário/tenant: %s", exc)
        return jsonify({"error": "Falha interna ao realizar cadastro."}), 500


@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Autentica um usuário existente com e-mail e senha.
    Retorna access_token de curta duração (30 min) e refresh_token (7 dias).
    """
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email e senha são obrigatórios."}), 400

    try:
        user = User.query.filter_by(email=email).first()
    except Exception as db_exc:
        logger.error("Falha ao consultar usuário no banco durante o login: %s", db_exc)
        return jsonify({"error": "Banco de dados temporariamente indisponível. Tente novamente em instantes."}), 503

    if not user or not user.check_password(password):
        return jsonify({"error": "Credenciais inválidas."}), 401

    if not user.is_active:
        return jsonify({"error": "Usuário inativo. Contate o administrador."}), 403

    tenant = user.tenant
    if not tenant or not tenant.is_active:
        return jsonify({"error": "Organização inativa. Contate o suporte."}), 403

    access_token = create_access_token(user)
    refresh_token = create_refresh_token(user)

    logger.info("Login bem-sucedido: user_id=%d, tenant_id=%d, email=%s", user.id, user.tenant_id, user.email)

    return jsonify({
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": 1800,
        "user": user.to_dict(),
        "tenant": tenant.to_dict() if tenant else None,
    }), 200


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    """
    Emite um novo access_token a partir de um refresh_token válido.
    """
    data = request.get_json(silent=True) or {}
    token = data.get("refresh_token") or get_token_from_header()

    if not token:
        return jsonify({"error": "Parâmetro 'refresh_token' é obrigatório."}), 400

    try:
        payload = decode_token(token)
    except Exception as exc:
        return jsonify({"error": f"Refresh token inválido ou expirado: {str(exc)}"}), 401

    if payload.get("type") != "refresh":
        return jsonify({"error": "Tipo de token incorreto para renovação."}), 401

    user_id = payload.get("sub")
    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        uid_int = user_id
    user = db.session.get(User, uid_int)
    if not user or not user.is_active:
        return jsonify({"error": "Usuário inexistente ou inativo."}), 401

    new_access_token = create_access_token(user)
    new_refresh_token = create_refresh_token(user)

    return jsonify({
        "status": "success",
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "Bearer",
        "expires_in": 1800,
    }), 200


@auth_bp.route("/me", methods=["GET"])
@require_auth
def get_me():
    """
    Retorna os dados do usuário autenticado e da sua organização (tenant).
    """
    user = g.current_user
    tenant = user.tenant
    return jsonify({
        "user": user.to_dict(),
        "tenant": tenant.to_dict() if tenant else None,
    }), 200


@auth_bp.route("/logout", methods=["POST", "GET"])
def logout():
    """
    Encerra a sessão do usuário no backend.
    Invalida o contexto atual da sessão e registra o encerramento em log.
    Permite requisições com ou sem token Bearer para garantir saída limpa
    mesmo que o token de acesso já esteja expirado.
    """
    user = get_current_user_optional()
    if user:
        logger.info("Logout realizado com sucesso para o usuário id=%d (email=%s).", user.id, user.email)
    else:
        logger.info("Logout realizado com sucesso (sessão anônima ou token expirado).")

    return jsonify({
        "status": "success",
        "message": "Sessão encerrada com sucesso."
    }), 200


# =============================================================================
# ADMIN BOARD: Gestão de Usuários e Planos (Restrito a Perfil ADMIN)
# =============================================================================

@auth_bp.route("/admin/users", methods=["GET"])
@require_role(User.ROLE_ADMIN)
def admin_list_users():
    """
    Lista todos os usuários pertencentes ao tenant do administrador logado,
    incluindo detalhes do plano da organização e histórico de cotas.
    Protegido contra IDOR: apenas usuários do mesmo tenant_id são retornados.
    Nenhum campo de senha ou credencial é exposto.
    """
    admin_user = g.current_user
    tenant = admin_user.tenant
    if not tenant:
        return jsonify({"error": "Organização não localizada para o administrador."}), 404

    users = (
        User.query.filter_by(tenant_id=tenant.id)
        .order_by(User.role.asc(), User.name.asc())
        .all()
    )

    from app.services.quota_service import QuotaService
    quota_info = QuotaService.check_quota(tenant)

    user_list = []
    for u in users:
        u_data = u.to_dict()
        # Garantia explícita de sanitização de segurança (segurança em profundidade)
        u_data.pop("password", None)
        u_data.pop("password_hash", None)
        u_data["tenant_name"] = tenant.name
        u_data["tenant_plan"] = tenant.plan
        user_list.append(u_data)

    return jsonify({
        "status": "success",
        "total": len(user_list),
        "users": user_list,
        "tenant": tenant.to_dict(),
        "quota": quota_info,
        "available_plans": [
            {
                "id": Tenant.PLAN_ESSENCIAL,
                "label": "Essencial",
                "quota": Tenant.DEFAULT_QUOTAS[Tenant.PLAN_ESSENCIAL],
                "description": "Até 30 consultas mensais e monitoramento padrão."
            },
            {
                "id": Tenant.PLAN_PRO,
                "label": "Pro",
                "quota": Tenant.DEFAULT_QUOTAS[Tenant.PLAN_PRO],
                "description": "Até 150 consultas mensais com inteligência recursal TST avançada."
            },
            {
                "id": Tenant.PLAN_ENTERPRISE,
                "label": "Enterprise",
                "quota": "Ilimitado",
                "description": "Consultas ilimitadas, relatórios executivos customizados e SLA dedicado."
            },
        ],
    }), 200


@auth_bp.route("/admin/users/<int:user_id>/plan", methods=["PUT", "PATCH"])
@require_role(User.ROLE_ADMIN)
def admin_update_user_plan(user_id: int):
    """
    Atualiza o plano da organização do usuário.
    Permitido apenas para administradores do mesmo tenant (Anti-IDOR).
    Atualiza o plano do Tenant e recalcula os limites de cotas mensais.
    """
    admin_user = g.current_user
    target_user = db.session.get(User, user_id)
    if not target_user:
        return jsonify({"error": "Usuário não encontrado."}), 404

    # Trava Anti-IDOR: O administrador só pode alterar planos do seu próprio Tenant
    if target_user.tenant_id != admin_user.tenant_id:
        logger.warning(
            "Tentativa de violação IDOR bloqueada: Admin id=%d (tenant=%d) tentou alterar plano do user id=%d (tenant=%d)",
            admin_user.id, admin_user.tenant_id, target_user.id, target_user.tenant_id
        )
        return jsonify({"error": "Acesso não autorizado para gerenciar usuários de outra organização."}), 403

    data = request.get_json(silent=True) or {}
    new_plan = str(data.get("plan") or "").strip().upper()
    valid_plans = {Tenant.PLAN_ESSENCIAL, Tenant.PLAN_PRO, Tenant.PLAN_ENTERPRISE}

    if new_plan not in valid_plans:
        return jsonify({
            "error": f"Plano inválido '{new_plan}'. Planos válidos: {list(valid_plans)}",
            "valid_plans": list(valid_plans)
        }), 400

    target_tenant = target_user.tenant
    old_plan = target_tenant.plan
    target_tenant.plan = new_plan
    target_tenant.limite_mensal_consultas = Tenant.DEFAULT_QUOTAS.get(new_plan)

    # Suporte adicional opcional: alteração de role (ex: promover para ADMIN ou ANALISTA)
    new_role = data.get("role")
    if new_role and str(new_role).strip().upper() in User.VALID_ROLES:
        target_user.role = str(new_role).strip().upper()

    # Suporte adicional opcional: ativação/desativação de usuário
    if "is_active" in data:
        target_user.is_active = bool(data["is_active"])

    try:
        db.session.commit()
        logger.info(
            "Plano atualizado com sucesso pelo Admin id=%d: Tenant id=%d alterado de %s para %s (Limite: %s)",
            admin_user.id, target_tenant.id, old_plan, new_plan, target_tenant.limite_mensal_consultas
        )

        from app.services.quota_service import QuotaService
        quota_info = QuotaService.check_quota(target_tenant)

        return jsonify({
            "status": "success",
            "message": f"Plano atualizado com sucesso para '{new_plan}'.",
            "user": target_user.to_dict(),
            "tenant": target_tenant.to_dict(),
            "quota": quota_info,
        }), 200

    except Exception as exc:
        db.session.rollback()
        logger.exception("Erro ao atualizar plano do usuário/tenant: %s", exc)
        return jsonify({"error": "Falha interna ao atualizar plano do usuário."}), 500
