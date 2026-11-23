from functools import wraps
from flask import request, jsonify, g
from app.extensions import db
from app.models.user import User
from app.utils.jwt import decode_token


def get_token_from_header() -> str:
    auth_header = request.headers.get("Authorization", "").strip()
    if not auth_header:
        return ""
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return ""


def get_current_user_optional():
    """Identifica o usuário atual pelo JWT se presente no header, sem bloquear se ausente."""
    g.current_user = None
    g.current_tenant_id = None
    token = get_token_from_header()
    if not token:
        return None

    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None
        user = db.session.get(User, user_id)
        if user and user.is_active:
            g.current_user = user
            g.current_tenant_id = user.tenant_id
            return user
    except Exception:
        pass
    return None


def require_auth(f):
    """
    Decorator que exige autenticação via JWT Bearer Token válido.
    Injeta `g.current_user` e `g.current_tenant_id` no contexto da requisição.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_token_from_header()
        if not token:
            return jsonify({"error": "Token de autenticação ausente ou inválido."}), 401

        try:
            payload = decode_token(token)
        except Exception as e:
            return jsonify({"error": f"Token inválido ou expirado: {str(e)}"}), 401

        if payload.get("type") != "access":
            return jsonify({"error": "Tipo de token inválido para esta operação."}), 401

        user_id = payload.get("sub")
        if not user_id:
            return jsonify({"error": "Payload de token incompleto."}), 401

        user = db.session.get(User, user_id)
        if not user or not user.is_active:
            return jsonify({"error": "Usuário inexistente ou inativo."}), 401

        g.current_user = user
        g.current_tenant_id = user.tenant_id
        return f(*args, **kwargs)

    return decorated


def require_role(*allowed_roles):
    """
    Decorator RBAC que valida se o usuário autenticado possui o perfil requerido (ex: 'ADMIN', 'ANALISTA').
    Sempre re-valida o token da requisição atual para garantir isolamento seguro.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = get_token_from_header()
            if not token:
                return jsonify({"error": "Token de autenticação ausente ou inválido."}), 401
            try:
                payload = decode_token(token)
            except Exception as e:
                return jsonify({"error": f"Token inválido ou expirado: {str(e)}"}), 401

            if payload.get("type") != "access":
                return jsonify({"error": "Tipo de token inválido para esta operação."}), 401

            user = db.session.get(User, payload.get("sub"))
            if not user or not user.is_active:
                return jsonify({"error": "Usuário inexistente ou inativo."}), 401

            g.current_user = user
            g.current_tenant_id = user.tenant_id

            if user.role not in allowed_roles:
                return jsonify({
                    "error": "Acesso não autorizado para o perfil atual.",
                    "required_roles": list(allowed_roles),
                    "current_role": user.role,
                }), 403

            return f(*args, **kwargs)

        return decorated
    return decorator
