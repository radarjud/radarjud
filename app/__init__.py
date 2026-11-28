import os
import logging
from flask import Flask, jsonify, render_template
from app.config import config_by_name
from app.extensions import db, cors
from app.routes import api_bp
from app.routes.auth import auth_bp


logger = logging.getLogger(__name__)


def ensure_database_schema(db_instance):
    """
    Garante aplicação idempotente de migrações e novas colunas tanto em PostgreSQL quanto em SQLite.
    Evita falhas do tipo 'no such column' em bancos locais SQLite ou ambientes de desenvolvimento.
    """
    dialect = db_instance.engine.dialect.name
    try:
        with db_instance.engine.connect() as conn:
            if dialect == "postgresql":
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS tenant_id INTEGER;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS movements_hash VARCHAR(64);"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS scan_status VARCHAR(20) DEFAULT 'COMPLETED';"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS ai_analysis JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS economic_standing JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS ranking_motivos JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS ranking_motivos_condenacao JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS condenacao_por_motivo JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS predictions_summary JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS cnae_codigo VARCHAR(20);"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS cnae_descricao VARCHAR(255);"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS condenacoes_subsidiarias_tomadoras_count INTEGER DEFAULT 0;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS tomadoras_subsidiarias_pj JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_searches ADD COLUMN IF NOT EXISTS contratos_poder_publico JSON;"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_company_searches_cnae_codigo ON company_searches (cnae_codigo);"))
                conn.execute(db_instance.text("ALTER TABLE company_registry ADD COLUMN IF NOT EXISTS cnae_codigo VARCHAR(20);"))
                conn.execute(db_instance.text("ALTER TABLE company_registry ADD COLUMN IF NOT EXISTS cnae_descricao VARCHAR(255);"))
                conn.execute(db_instance.text("ALTER TABLE company_registry ADD COLUMN IF NOT EXISTS public_contracts JSON;"))
                conn.execute(db_instance.text("ALTER TABLE company_registry ADD COLUMN IF NOT EXISTS public_contracts_cached_at TIMESTAMP;"))
                conn.execute(db_instance.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(50);"))
                conn.execute(db_instance.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS limite_mensal_consultas INTEGER;"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_company_registry_cnae_codigo ON company_registry (cnae_codigo);"))
                conn.execute(db_instance.text("CREATE TABLE IF NOT EXISTS public_contracts_cache ("
                                              "id SERIAL PRIMARY KEY, "
                                              "cnpj VARCHAR(14) NOT NULL UNIQUE, "
                                              "data JSON NOT NULL, "
                                              "cached_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                                              "ttl_seconds INTEGER NOT NULL DEFAULT 86400"
                                              ");"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_public_contracts_cache_cnpj ON public_contracts_cache (cnpj);"))
                conn.execute(db_instance.text("ALTER TABLE process_records ADD COLUMN IF NOT EXISTS observation VARCHAR(500);"))
                conn.execute(db_instance.text("ALTER TABLE process_records ADD COLUMN IF NOT EXISTS has_subsidiary_liability BOOLEAN DEFAULT FALSE;"))
                conn.execute(db_instance.text("ALTER TABLE process_records ADD COLUMN IF NOT EXISTS subsidiary_contractors_json JSON;"))
                conn.execute(db_instance.text("ALTER TABLE process_records ALTER COLUMN grau TYPE VARCHAR(50);"))
                conn.execute(db_instance.text("ALTER TABLE process_records ALTER COLUMN tribunal TYPE VARCHAR(50);"))
                conn.execute(db_instance.text("ALTER TABLE process_records ADD COLUMN IF NOT EXISTS data_distribuicao DATE;"))
                conn.execute(db_instance.text("ALTER TABLE process_records ADD COLUMN IF NOT EXISTS valor_causa NUMERIC(15, 2);"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_process_records_data_distribuicao ON process_records (data_distribuicao);"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_process_records_tribunal ON process_records (tribunal);"))
                conn.execute(db_instance.text("CREATE UNIQUE INDEX IF NOT EXISTS uq_process_records_search_numero ON process_records (company_search_id, numero_processo);"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_process_records_search_tribunal_data ON process_records (company_search_id, tribunal, data_distribuicao);"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_process_records_search_valor ON process_records (company_search_id, valor_causa);"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_process_records_search_outcome ON process_records (company_search_id, sentence_outcome);"))
                conn.execute(db_instance.text("CREATE TABLE IF NOT EXISTS monitored_suppliers ("
                                              "id SERIAL PRIMARY KEY, "
                                              "tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, "
                                              "cnpj VARCHAR(20) NOT NULL, "
                                              "company_name VARCHAR(255) NOT NULL, "
                                              "last_movements_hash VARCHAR(64), "
                                              "last_condenacoes_count INTEGER DEFAULT 0, "
                                              "last_scenario VARCHAR(20) DEFAULT 'CENARIO_A', "
                                              "last_rfb_status VARCHAR(50) DEFAULT 'ATIVA', "
                                              "last_tst_count INTEGER DEFAULT 0, "
                                              "webhook_url VARCHAR(500), "
                                              "alert_email VARCHAR(255), "
                                              "active BOOLEAN DEFAULT TRUE, "
                                              "last_checked_at TIMESTAMP, "
                                              "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                                              "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"))
                conn.execute(db_instance.text("CREATE UNIQUE INDEX IF NOT EXISTS uq_monitored_suppliers_tenant_cnpj ON monitored_suppliers (tenant_id, cnpj);"))
                conn.execute(db_instance.text("CREATE INDEX IF NOT EXISTS ix_monitored_suppliers_tenant_active ON monitored_suppliers (tenant_id, active);"))
                conn.commit()
            elif dialect == "sqlite":
                def _sqlite_add_missing(table_name, columns_spec):
                    try:
                        res = conn.execute(db_instance.text(f"PRAGMA table_info({table_name});")).fetchall()
                        existing = {r[1].lower() for r in res}
                        for col_name, col_type in columns_spec:
                            if col_name.lower() not in existing:
                                conn.execute(db_instance.text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type};"))
                    except Exception as e:
                        logger.debug("Migração SQLite para tabela %s ignorada: %s", table_name, e)

                _sqlite_add_missing("company_searches", [
                    ("tenant_id", "INTEGER"),
                    ("movements_hash", "VARCHAR(64)"),
                    ("last_checked_at", "TIMESTAMP"),
                    ("scan_status", "VARCHAR(20) DEFAULT 'COMPLETED'"),
                    ("ai_analysis", "JSON"),
                    ("economic_standing", "JSON"),
                    ("ranking_motivos", "JSON"),
                    ("ranking_motivos_condenacao", "JSON"),
                    ("condenacao_por_motivo", "JSON"),
                    ("predictions_summary", "JSON"),
                    ("cnae_codigo", "VARCHAR(20)"),
                    ("cnae_descricao", "VARCHAR(255)"),
                    ("condenacoes_subsidiarias_tomadoras_count", "INTEGER DEFAULT 0"),
                    ("tomadoras_subsidiarias_pj", "JSON"),
                    ("contratos_poder_publico", "JSON"),
                ])
                _sqlite_add_missing("process_records", [
                    ("observation", "VARCHAR(500)"),
                    ("has_subsidiary_liability", "BOOLEAN DEFAULT 0"),
                    ("subsidiary_contractors_json", "JSON"),
                    ("data_distribuicao", "DATE"),
                    ("valor_causa", "NUMERIC(15, 2)"),
                    ("raw_metadata", "JSON"),
                ])
                _sqlite_add_missing("company_registry", [
                    ("cnae_codigo", "VARCHAR(20)"),
                    ("cnae_descricao", "VARCHAR(255)"),
                    ("public_contracts", "JSON"),
                    ("public_contracts_cached_at", "TIMESTAMP"),
                ])
                _sqlite_add_missing("users", [
                    ("plan", "VARCHAR(50)"),
                    ("limite_mensal_consultas", "INTEGER"),
                ])
                conn.commit()
    except Exception as exc:
        logger.warning("Aviso durante ensure_database_schema: %s", exc)


def create_app(config_name: str = None, seed_catalog: bool = True) -> Flask:
    """
    Application Factory para o LexGuard AI.
    Configura banco de dados PostgreSQL, CORS, logs e rotas da API.
    """
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development").lower()

    app = Flask(__name__)
    config_obj = config_by_name.get(config_name, config_by_name["default"])
    app.config.from_object(config_obj)

    # Configuração de Logs
    logging.basicConfig(
        level=logging.DEBUG if app.config.get("DEBUG") else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Inicialização de Extensões
    db.init_app(app)
    cors.init_app(app, resources={r"/api/.*": {"origins": "*"}, r"/api/*": {"origins": "*"}})

    # Registro de Blueprints
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)

    # Aliases de exportação PDF sob /api/... (além de /api/v1/...)
    from app.routes.api import handle_pdf_export_request, export_compare_pdf

    @app.route("/api/export-pdf", methods=["GET", "POST"])
    @app.route("/api/report/pdf", methods=["GET", "POST"])
    @app.route("/api/export/pdf", methods=["GET", "POST"])
    def _api_pdf_query_or_post():
        return handle_pdf_export_request()

    @app.route("/api/export-pdf/<path:company_name>", methods=["GET"])
    @app.route("/api/report/pdf/<path:company_name>", methods=["GET"])
    @app.route("/api/export/pdf/<path:company_name>", methods=["GET"])
    def _api_pdf_by_path(company_name: str):
        return handle_pdf_export_request(company_name=company_name)

    @app.route("/api/export-compare-pdf", methods=["GET", "POST"])
    @app.route("/api/compare-pdf", methods=["GET", "POST"])
    @app.route("/api/export/compare-pdf", methods=["GET", "POST"])
    def _api_compare_pdf():
        return export_compare_pdf()

    @app.route("/api/history/<int:search_id>/summary.pdf", methods=["GET"])
    def _api_pdf_by_history_id(search_id: int):
        return handle_pdf_export_request(search_id=search_id)

    @app.route("/api/cnj-proxy", methods=["GET", "POST", "OPTIONS"])
    def _api_cnj_proxy():
        from app.routes.api import cnj_proxy
        return cnj_proxy()

    @app.route("/api/searches/<int:company_search_id>/processes", methods=["GET"])
    def _api_searches_processes(company_search_id: int):
        from app.routes.api import get_search_processes
        return get_search_processes(company_search_id=company_search_id)

    @app.route("/api/auth/logout", methods=["POST", "GET"])
    def _api_auth_logout():
        from app.routes.auth import logout
        return logout()

    # Rota raiz informativa
    @app.route("/", methods=["GET"])
    def index():
        try:
            return render_template("index.html")
        except Exception:
            return jsonify({
                "service": "LexGuard AI - Orquestrador de Risco de Terceiros",
                "version": "1.0.0",
                "description": "Diagnóstico de Passivo Trabalhista/Cível via Metadados DataJud e Comunica PJe",
                "endpoints": {
                    "health": "/api/v1/health",
                    "search_company": "/api/v1/search-company",
                    "company_dashboard": "/api/v1/company-dashboard",
                    "history": "/api/v1/history"
                }
            }), 200

    # Cria tabelas no banco de dados automaticamente no startup (PostgreSQL/SQLite)
    from app import models as _registered_models  # noqa: F401 - garante registro dos modelos antes de create_all()
    with app.app_context():
        try:
            from app.models.tenant import Tenant  # noqa: F401
            from app.models.user import User  # noqa: F401
            from app.models.company_search import CompanySearch  # noqa: F401
            from app.models.process_record import ProcessRecord  # noqa: F401
            from app.models.CompanyRegistry import CompanyRegistry  # noqa: F401
            from app.models.monitored_supplier import MonitoredSupplier  # noqa: F401
            from app.models.public_contract_cache import PublicContractCache  # noqa: F401
            db.create_all()
            ensure_database_schema(db)

            # Semeia usuários padrão de sistema (Admin e Analista) com idempotência estrita
            try:
                from app.seeds.user_seed import seed_default_users
                seed_default_users(commit=True)
            except Exception as e_user_seed:
                db.session.rollback()
                app.logger.debug("Seed de usuários padrão ignorado/já presente: %s", e_user_seed)

            # Pré-semeia e sincroniza CompanyRegistry com o catálogo completo quando vazio ou incompleto
            if seed_catalog:
                try:
                    from app.seeds.catalog_seed import seed_company_registry
                    seed_company_registry(enrich_via_brasilapi=False, commit=True)
                except Exception as e_seed:
                    db.session.rollback()
                    app.logger.debug("Sincronização de CompanyRegistry ignorada/já presente: %s", e_seed)
        except Exception as exc:
            app.logger.warning("Não foi possível conectar imediatamente ao PostgreSQL para db.create_all(): %s", exc)

    return app

