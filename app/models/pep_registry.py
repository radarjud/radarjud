import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.extensions import db


def get_utc_now():
    """Retorna timestamp UTC naive compatível com colunas DateTime do SQLAlchemy."""
    return datetime.now(UTC).replace(tzinfo=None)


def mask_cpf_lgpd(raw_doc: Any) -> str | None:
    """
    Higieniza e mascara fragmento de documento estritamente sob as regras da LGPD.
    Jamais permite a persistência ou exposição de CPFs completos (11 dígitos).
    Padrão oficial da CGU / Portal da Transparência: '***.XXX.XXX-**'.
    """
    if not raw_doc or not isinstance(raw_doc, str):
        return None

    cleaned = str(raw_doc).strip()
    digits = re.sub(r"\D", "", cleaned)

    # Se for um CPF completo de 11 dígitos, mascara imediatamente (primeiros 3 e últimos 2 dígitos ocultos)
    if len(digits) == 11:
        return f"***.{digits[3:6]}.{digits[6:9]}-**"

    # Se já for um fragmento com 6 dígitos visíveis (ex: ***123456** ou 123456)
    if len(digits) == 6:
        return f"***.{digits[0:3]}.{digits[3:6]}-**"

    # Se já contém máscara oficial com asteriscos, valida que não há CPF completo de 11 dígitos exposto
    if "***" in cleaned:
        if len(digits) == 11:
            return f"***.{digits[3:6]}.{digits[6:9]}-**"
        return cleaned.upper()

    return None


class PEPRegistry(db.Model):
    """
    Tabela indexada enxuta de Pessoas Politicamente Expostas (PEP - Base CGU).
    Armazena apenas metadados necessários para cruzamento com o QSA,
    sem jamais reter dados pessoais sensíveis ou CPFs completos (LGPD by Design).
    """
    __tablename__ = "pep_registry"

    id = db.Column(db.Integer, primary_key=True)
    nome_normalizado = db.Column(db.String(255), nullable=False, index=True)
    cpf_parcial_mascarado = db.Column(db.String(32), nullable=True, index=True)
    cargo_funcao = db.Column(db.String(255), nullable=True)
    esfera_orgao = db.Column(db.String(255), nullable=True)
    data_inicio_exercicio = db.Column(db.String(32), nullable=True)
    data_fim_exercicio = db.Column(db.String(32), nullable=True)
    data_fim_carencia = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)

    __table_args__ = (
        db.Index("ix_pep_registry_nome_cpf", "nome_normalizado", "cpf_parcial_mascarado"),
    )

    def __init__(self, **kwargs):
        # Guardrail LGPD ativo no construtor
        if "cpf_parcial_mascarado" in kwargs:
            kwargs["cpf_parcial_mascarado"] = mask_cpf_lgpd(kwargs["cpf_parcial_mascarado"])
        if kwargs.get("nome_normalizado"):
            from app.utils.sanitizer import sanitize_company_name
            # Normalização de nome (sem acentos, uppercase, sem espaços extras)
            kwargs["nome_normalizado"] = sanitize_company_name(kwargs["nome_normalizado"]).upper()
        super().__init__(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "nome_normalizado": self.nome_normalizado,
            "cpf_parcial_mascarado": self.cpf_parcial_mascarado,
            "cargo_funcao": self.cargo_funcao,
            "esfera_orgao": self.esfera_orgao,
            "data_inicio_exercicio": self.data_inicio_exercicio,
            "data_fim_exercicio": self.data_fim_exercicio,
            "data_fim_carencia": self.data_fim_carencia,
        }


class PEPComplianceCache(db.Model):
    """
    Cache persistente de Nível 1 (L1) no PostgreSQL para o resultado de compliance PEP
    do Quadro Societário (QSA) por CNPJ da empresa.
    """
    __tablename__ = "pep_compliance_cache"

    id = db.Column(db.Integer, primary_key=True)
    cnpj = db.Column(db.String(14), unique=True, nullable=False, index=True)
    data = db.Column(db.JSON, nullable=False)
    cached_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    ttl_seconds = db.Column(db.Integer, default=86400, nullable=False)  # 24 horas

    def is_valid(self, ttl_hours: int | None = None) -> bool:
        if not self.cached_at:
            return False
        max_age = timedelta(hours=ttl_hours) if ttl_hours is not None else timedelta(seconds=self.ttl_seconds)
        return (get_utc_now() - self.cached_at) <= max_age

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cnpj": self.cnpj,
            "data": self.data,
            "cached_at": self.cached_at.isoformat() if self.cached_at else None,
            "ttl_seconds": self.ttl_seconds,
            "is_valid": self.is_valid(),
        }
