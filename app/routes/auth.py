import logging
from flask import Blueprint, request, jsonify, g
from app.extensions import db
from app.models.tenant import Tenant
from app.models.user import User
from app.utils.auth import require_auth, require_role, get_token_from_header
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

    user = User.query.filter_by(email=email).first()
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
    user = db.session.get(User, user_id)
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
