import os
import secrets
import socket
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Carrega variáveis do .env se existir
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _database_url() -> str:
    """
    Resolve a URI do banco de dados:
    - Normaliza postgres://, postgresql:// e postgresql+psycopg:// para postgresql+psycopg2://
      garantindo o uso explícito do psycopg2-binary no SQLAlchemy.
    - Preserva postgresql+psycopg2:// quando já informado.
    - Faz fallback para SQLite local se DATABASE_URL não estiver definida ou apontar para 'db' fora do Docker.
    """
    configured_url = os.getenv("DATABASE_URL", "").strip()
    if configured_url:
        for prefix in ("postgres://", "postgresql+psycopg://", "postgresql://"):
            if configured_url.startswith(prefix):
                configured_url = "postgresql+psycopg2://" + configured_url[len(prefix):]
                break

        # Se a URL aponta para o host 'db' (Docker) e estamos rodando fora do Docker (sem resolver 'db')
        if "@db:" in configured_url or "@db/" in configured_url:
            try:
                socket.gethostbyname("db")
            except Exception:
                db_file = BASE_DIR / "radarjud.db"
                return f"sqlite:///{db_file}"

        # Se a URL aponta para localhost ou 127.0.0.1 (portas 5432 ou 55432)
        if any(h in configured_url for h in ("@localhost:", "@127.0.0.1:")):
            try:
                import psycopg2
                test_uri = (
                    configured_url.replace("postgresql+psycopg2://", "postgresql://")
                    .replace("@localhost:", "@127.0.0.1:")
                )
                conn = psycopg2.connect(test_uri, connect_timeout=1)
                conn.close()
                return configured_url
            except Exception:
                # Tenta alternar entre as portas 5432 e 55432 (Docker x Local)
                if ":5432" in configured_url:
                    alt_url = configured_url.replace(":5432", ":55432")
                elif ":55432" in configured_url:
                    alt_url = configured_url.replace(":55432", ":5432")
                else:
                    alt_url = None

                if alt_url:
                    try:
                        alt_test = (
                            alt_url.replace("postgresql+psycopg2://", "postgresql://")
                            .replace("@localhost:", "@127.0.0.1:")
                        )
                        conn = psycopg2.connect(alt_test, connect_timeout=1)
                        conn.close()
                        return alt_url
                    except Exception:
                        pass

                db_file = BASE_DIR / "radarjud.db"
                return f"sqlite:///{db_file.as_posix()}"

        return configured_url

    # Fallback para Postgres apenas se credenciais/host foram explicitamente definidos sem DATABASE_URL
    if os.getenv("POSTGRES_HOST") or os.getenv("POSTGRES_PASSWORD"):
        user = os.getenv("POSTGRES_USER", "radarjud")
        password = os.getenv("POSTGRES_PASSWORD", "")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        db_name = os.getenv("POSTGRES_DB", "radarjud")
        uri = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"
        if host in ("localhost", "127.0.0.1") and port == "5432":
            try:
                import psycopg2
                conn = psycopg2.connect(uri.replace("postgresql+psycopg2://", "postgresql://"), connect_timeout=1)
                conn.close()
            except Exception:
                alt_uri = f"postgresql+psycopg2://{user}:{password}@{host}:55432/{db_name}"
                try:
                    conn = psycopg2.connect(alt_uri.replace("postgresql+psycopg2://", "postgresql://"), connect_timeout=1)
                    conn.close()
                    return alt_uri
                except Exception:
                    db_file = BASE_DIR / "radarjud.db"
                    return f"sqlite:///{db_file.as_posix()}"
        return uri

    db_file = BASE_DIR / "radarjud.db"
    return f"sqlite:///{db_file.as_posix()}"


def _build_engine_options(db_uri: str) -> dict[str, Any]:
    """
    Configura o pool do SQLAlchemy para compatibilidade com PgBouncer (Supabase Transaction Mode - porta 6543).
    O projeto usa psycopg2-binary (não passa prepare_threshold; preserva SSL da URI).
    """
    if str(db_uri or "").startswith("postgresql"):
        return {
            "pool_pre_ping": True,
            "pool_size": 3,
            "max_overflow": 2,
            "pool_recycle": 300,
        }
    return {"pool_pre_ping": True}


class Config:
    """Configuração base da aplicação LexGuard AI."""
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
    DEBUG = False
    TESTING = False

    # Banco de Dados PostgreSQL
    POSTGRES_USER = os.getenv("POSTGRES_USER", "radarjud")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
    POSTGRES_DB = os.getenv("POSTGRES_DB", "radarjud")
    POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = _build_engine_options(SQLALCHEMY_DATABASE_URI)

    # APIs Públicas Oficiais do Judiciário (sem chaves hardcoded)
    DATAJUD_API_KEY = os.getenv("DATAJUD_API_KEY", "").strip()
    DATAJUD_BASE_URL = os.getenv("DATAJUD_BASE_URL", "https://api-publica.datajud.cnj.jus.br")
    COMUNICA_PJE_URL = os.getenv("COMUNICA_PJE_URL", "https://comunicaapi.pje.jus.br/api/v1/comunicacao")

    # Proxy Edge Vercel / Transporte CNJ
    CNJ_PROXY_URL = os.getenv("CNJ_PROXY_URL", "").strip()
    PROXY_SHARED_SECRET = os.getenv("PROXY_SHARED_SECRET", "").strip()

    # Portal da Transparência / CGU
    PORTAL_TRANSPARENCIA_API_KEY = os.getenv("PORTAL_TRANSPARENCIA_API_KEY", "").strip()

    # Fila Assíncrona (Upstash Redis Serverless + Cron US-11)
    REDIS_URL = os.getenv("REDIS_URL", "").strip()
    CRON_SECRET = os.getenv("CRON_SECRET", "").strip()

    # Notificações Transacionais (Resend API + Fallback SMTP)
    RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
    ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", "alertas@radarjus.com.br").strip()
    SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
    SMTP_USER = os.getenv("SMTP_USER", "").strip()
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("1", "true", "yes")

    # Configurações de Conectividade e Resiliência
    REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    MAX_PROCESSES_PER_QUERY = int(os.getenv("MAX_PROCESSES_PER_QUERY", "100"))
    CACHE_EXPIRATION_HOURS = int(os.getenv("CACHE_EXPIRATION_HOURS", "24"))


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    # Mantém exatamente pool_size=3 e max_overflow=2 para PgBouncer em Transaction Mode (Supabase 6543)
    SQLALCHEMY_ENGINE_OPTIONS = _build_engine_options(Config.SQLALCHEMY_DATABASE_URI)


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    # Em testes, usamos sqlite em memória para rapidez e isolamento caso Postgres não esteja rodando
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
