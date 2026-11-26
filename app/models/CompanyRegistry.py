from app.extensions import db
from app.models.company_search import get_utc_now


class CompanyRegistry(db.Model):
    __tablename__ = "company_registry"

    id = db.Column(db.Integer, primary_key=True)

    cnpj = db.Column(db.String(14), unique=True, nullable=False, index=True)
    razao_social = db.Column(db.String(255), nullable=False, index=True)
    nome_fantasia = db.Column(db.String(255), nullable=True, index=True)

    situacao_cadastral = db.Column(db.String(50), nullable=True)
    matriz_filial = db.Column(db.String(1), nullable=True)

    municipio = db.Column(db.String(150), nullable=True)
    uf = db.Column(db.String(2), nullable=True, index=True)

    capital_social = db.Column(db.Numeric(15, 2), nullable=True)

    cnae_codigo = db.Column(db.String(20), nullable=True, index=True)
    cnae_descricao = db.Column(db.String(255), nullable=True)

    # Contratos com o Poder Público (PNCP / Portal da Transparência - US-20)
    public_contracts = db.Column(db.JSON, nullable=True)
    public_contracts_cached_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=get_utc_now, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=get_utc_now,
        onupdate=get_utc_now,
        nullable=False
    )

    def to_dict(self):
        return {
            "id": self.id,
            "cnpj": self.cnpj,
            "razao_social": self.razao_social,
            "nome_fantasia": self.nome_fantasia,
            "situacao_cadastral": self.situacao_cadastral,
            "matriz_filial": self.matriz_filial,
            "municipio": self.municipio,
            "uf": self.uf,
            "capital_social": float(self.capital_social) if self.capital_social is not None else None,
            "cnae_codigo": self.cnae_codigo,
            "cnae_descricao": self.cnae_descricao,
            "public_contracts": self.public_contracts,
            "public_contracts_cached_at": self.public_contracts_cached_at.isoformat() if self.public_contracts_cached_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }