import json
from typing import Any
from datetime import datetime, timezone, timedelta
from app.extensions import db


def _safe_dict(val: Any) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _safe_list(val: Any) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def get_utc_now():
    """Retorna timestamp UTC naive compatível com colunas DateTime do SQLAlchemy."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CompanySearch(db.Model):
    """
    Registra histórico de consultas e diagnóstico de risco de terceiros (empresas).
    Atua como cache persistente de curto-circuito de alta performance para
    mitigar limites das APIs do CNJ e eliminar chamadas redundantes a modelos de IA.
    """
    __tablename__ = "company_searches"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True)
    company_name = db.Column(db.String(255), nullable=False, index=True)
    normalized_name = db.Column(db.String(255), nullable=False, index=True)
    total_processes = db.Column(db.Integer, default=0, nullable=False)
    secrecy_count = db.Column(db.Integer, default=0, nullable=False)
    secrecy_rate = db.Column(db.Float, default=0.0, nullable=False)

    # Invalidação Inteligente por Hash SHA-256 e Controle de Varredura
    movements_hash = db.Column(db.String(64), index=True, nullable=True)
    last_checked_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    scan_status = db.Column(db.String(20), default="COMPLETED", nullable=False, index=True)
    # Estados: COMPLETED, PARTIAL_FAST_PATH, SCANNING_BACKGROUND,
    # BROWSER_PENDING (cliente coletando), BROWSER_PARTIAL (encerrada incompleta).

    # Sumários estruturados em JSON
    outcomes_summary = db.Column(db.JSON, nullable=True)
    ranking_motivos = db.Column(db.JSON, nullable=True)
    ranking_motivos_condenacao = db.Column(db.JSON, nullable=True)
    condenacao_por_motivo = db.Column(db.JSON, nullable=True)
    predictions_summary = db.Column(db.JSON, nullable=True)
    economic_standing = db.Column(db.JSON, nullable=True)
    overall_risk_score = db.Column(db.Float, default=0.0, nullable=False)
    ai_analysis = db.Column(db.JSON, nullable=True)

    # Metadados Cadastrais RFB (CNAE Principal)
    cnae_codigo = db.Column(db.String(20), nullable=True, index=True)
    cnae_descricao = db.Column(db.String(255), nullable=True)

    # Responsabilidade Subsidiária de Tomadoras PJ (Súmula 331 TST - US-19)
    condenacoes_subsidiarias_tomadoras_count = db.Column(db.Integer, default=0, nullable=True)
    tomadoras_subsidiarias_pj = db.Column(db.JSON, nullable=True)

    # Contratos com o Poder Público (PNCP / Portal da Transparência - US-20)
    contratos_poder_publico = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=get_utc_now, onupdate=get_utc_now, nullable=False)

    # Relacionamento com os processos
    processes = db.relationship("ProcessRecord", backref="search", cascade="all, delete-orphan", lazy="select")

    def is_recent(self, hours: int = 24) -> bool:
        """
        Retorna True se a última verificação junto ao CNJ ocorreu dentro da janela de validade (padrão 24h).
        Permite retorno instantâneo em milissegundos sem novas requisições externas.
        """
        if not self.last_checked_at:
            return False
        delta = get_utc_now() - self.last_checked_at
        return delta <= timedelta(hours=hours)

    def to_dict(self):
        outcomes = _safe_dict(self.outcomes_summary)
        julgados = outcomes.get("condenacoes", 0) + outcomes.get("acordos", 0) + outcomes.get("improcedentes", 0)
        condenacoes = outcomes.get("condenacoes", 0)
        
        previsao_bloqueada = (condenacoes == 0)
        motivo_bloqueio = (
            "Empresa sem histórico de condenações prévias nos tribunais consultados. "
            "Previsão de condenação desativada por ausência de passivo condenatório anterior."
            if previsao_bloqueada else None
        )

        if previsao_bloqueada:
            taxa_cond_empirica = 0.0
            prob_cond_shrunk = 0.0
            plus_previsoes = []
        else:
            taxa_cond_empirica = round((condenacoes / julgados) * 100, 1) if julgados > 0 else None
            prob_cond_shrunk = (
                round(((condenacoes + 3.0 * 0.50) / (julgados + 3.0)) * 100.0, 1)
                if julgados > 0
                else (self.overall_risk_score if (self.total_processes or 0) > 0 else None)
            )
            plus_previsoes = _safe_list(self.predictions_summary)

        public_count = max(0, (self.total_processes or 0) - (self.secrecy_count or 0))
        niveis_sigilo = {"0": public_count}
        if self.secrecy_count and self.secrecy_count > 0:
            niveis_sigilo["1"] = self.secrecy_count

        # Idoneidade Econômica & Logomarca (NUNCA dispara chamadas de rede no to_dict)
        econ_data = _safe_dict(self.economic_standing)
        if not econ_data:
            from app.services.economic_service import EconomicStandingService
            try:
                econ_service = EconomicStandingService()
                logo_info = econ_service.get_company_logo_info(self.company_name or "")
            except Exception:
                logo_info = {}
            econ_data = {
                "cenario": "CENARIO_A",
                "cenario_badge": "Cenário A · Regular",
                "cenario_titulo": "Cenário A: Idoneidade Econômica Observada",
                "cenario_class": "seguro",
                "classificacao_porte": "Porte Médio / Regular",
                "capital_social_declarado": 0.0,
                "capital_social_formatado": "R$ 0,00",
                "valor_formatado": "R$ 0,00",
                "probabilidade_insolvencia": 20.0,
                "probabilidade_insolvencia_pct": 20.0,
                "risco_calote_subsidiario": 10.0,
                "risco_calote_subsidiario_pct": 10.0,
                "logo": logo_info,
                "sinais_solvencia_publica": None,
            }

        cond_por_motivo = _safe_list(self.condenacao_por_motivo)
        if not cond_por_motivo and self.processes:
            try:
                from app.services.prediction_engine import build_condenacao_por_motivo
                cond_por_motivo = build_condenacao_por_motivo([p.to_dict() for p in self.processes])
            except Exception:
                cond_por_motivo = []

        sub_count = int(self.condenacoes_subsidiarias_tomadoras_count or 0)
        sub_tomadoras = _safe_list(self.tomadoras_subsidiarias_pj)
        msg_sub = (
            f"Histórico de Responsabilidade Subsidiária: {sub_count} processos com condenação subsidiária atribuída a contratantes anteriores"
            if sub_count > 0
            else "Nenhuma condenação subsidiária de contratante identificada na amostra"
        )

        contratos_pub = _safe_dict(self.contratos_poder_publico)
        if not contratos_pub:
            contratos_pub = {
                "status": "DISPONIVEL",
                "total_contratos": 0,
                "esferas": [],
                "orgaos_contratantes": [],
                "valor_global_total": None,
                "valor_global_formatado": None,
                "mensagem": "Nenhum contrato público localizado nas bases consultadas",
            }

        tst_count = self._count_tst_processes()

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "company_name": self.company_name,
            "normalized_name": self.normalized_name,
            "total_processes": self.total_processes,
            "tst_count": tst_count,
            "secrecy_count": self.secrecy_count,
            "secrecy_rate": self.secrecy_rate,
            "niveis_sigilo": niveis_sigilo,
            "taxa_sigilo_pct": round((self.secrecy_rate or 0.0) * 100.0, 1),
            "movements_hash": self.movements_hash,
            "scan_status": self.scan_status,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "outcomes_summary": self.outcomes_summary or {},
            "ranking_motivos": self.ranking_motivos or [],
            "ranking_motivos_condenacao": self.ranking_motivos_condenacao or [],
            "condenacao_por_motivo": cond_por_motivo or [],
            "previsao_bloqueada": previsao_bloqueada,
            "motivo_bloqueio_previsao": motivo_bloqueio,
            "plus_previsao_condenacao": plus_previsoes,
            "overall_risk_score": self.overall_risk_score,
            "probabilidade_condenacao_geral_pct": prob_cond_shrunk,
            "taxa_historica_condenacao_pct": taxa_cond_empirica,
            "score_risco_terceirizacao": self.overall_risk_score,
            "cnae_codigo": self.cnae_codigo,
            "cnae_descricao": self.cnae_descricao,
            "cnae": {
                "codigo": self.cnae_codigo,
                "descricao": self.cnae_descricao,
            },
            "idoneidade_economica": econ_data,
            "sinais_solvencia_publica": econ_data.get("sinais_solvencia_publica") if isinstance(econ_data, dict) else None,
            "logo": self._sanitize_logo_for_export(econ_data.get("logo", {}) if isinstance(econ_data, dict) else {}),
            "ai_analysis": self.ai_analysis or {},
            "condenacoes_subsidiarias_tomadoras_count": sub_count,
            "tomadoras_subsidiarias_pj": sub_tomadoras,
            "mensagem_responsabilidade_subsidiaria": msg_sub,
            "contratos_poder_publico": contratos_pub,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def _count_tst_processes(self) -> int:
        """Conta processos no TST diretamente sobre 100% dos registros persistidos no banco."""
        from app.services.cnj_searcher import is_tst_process

        rows = None
        if self.id is not None:
            try:
                from app.models.process_record import ProcessRecord
                rows = (
                    db.session.query(
                        ProcessRecord.numero_processo,
                        ProcessRecord.tribunal,
                        ProcessRecord.grau,
                    )
                    .filter(ProcessRecord.company_search_id == self.id)
                    .all()
                )
            except Exception:
                rows = None

        if rows is not None:
            return sum(
                1 for num, trib, grau in rows
                if is_tst_process(numero_processo=num, tribunal=trib, grau=grau)
            )

        return sum(
            1 for p in (self.processes or [])
            if is_tst_process(
                numero_processo=getattr(p, "numero_processo", None),
                tribunal=getattr(p, "tribunal", None),
                grau=getattr(p, "grau", None),
            )
        )

    @staticmethod
    def _sanitize_logo_for_export(logo_obj):
        if not isinstance(logo_obj, dict) or not logo_obj:
            return {}
        if logo_obj.get("is_verified_domain") is False or logo_obj.get("is_synthetic_domain") is True:
            return {
                **logo_obj,
                "logo_url": None,
                "google_favicon_url": None,
                "is_verified_domain": False,
                "is_synthetic_domain": True,
            }
        from app.services.economic_service import SPECIAL_DOMAINS
        dom = str(logo_obj.get("domain", "")).lower().replace("www.", "")
        if dom and dom not in SPECIAL_DOMAINS.values() and not logo_obj.get("is_verified_domain"):
            return {
                **logo_obj,
                "logo_url": None,
                "google_favicon_url": None,
                "is_verified_domain": False,
                "is_synthetic_domain": True,
            }
        return logo_obj
