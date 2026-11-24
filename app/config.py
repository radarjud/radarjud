import os
import secrets
import socket
from pathlib import Path
from dotenv import load_dotenv

# Carrega variáveis do .env se existir
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _database_url() -> str:
    """
    Resolve a URI do banco de dados:
    - Converte postgres:// (e postgresql+psycopg:// / postgresql+psycopg2://) para postgresql:// (psycopg2-binary).
    - Faz fallback para SQLite local se DATABASE_URL não estiver definida ou apontar para 'db' fora do Docker.
    """
    configured_url = os.getenv("DATABASE_URL", "").strip()
    if configured_url:
        for driver in ("postgres://", "postgresql+psycopg://", "postgresql+psycopg2://"):
            if configured_url.startswith(driver):
                configured_url = "postgresql://" + configured_url[len(driver):]
                break
        # Se a URL aponta para o host 'db' (Docker) e estamos rodando fora do Docker (sem resolver 'db')
        if "@db:" in configured_url or "@db/" in configured_url:
            try:
                socket.gethostbyname("db")
            except Exception:
                db_file = BASE_DIR / "radarjud.db"
                return f"sqlite:///{db_file}"
        return configured_url

    # Fallback para Postgres apenas se credenciais/host foram explicitamente definidos sem DATABASE_URL
    if os.getenv("POSTGRES_HOST") or os.getenv("POSTGRES_PASSWORD"):
        user = os.getenv("POSTGRES_USER", "radarjud")
        password = os.getenv("POSTGRES_PASSWORD", "")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        db_name = os.getenv("POSTGRES_DB", "radarjud")
        return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"

    db_file = BASE_DIR / "radarjud.db"
    return f"sqlite:///{db_file}"


def _build_engine_options(db_uri: str) -> dict:
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
