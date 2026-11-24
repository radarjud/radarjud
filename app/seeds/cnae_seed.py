# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import re
import logging
from typing import Dict, Any, Optional
from app.extensions import db
from app.models.CompanyRegistry import CompanyRegistry
from app.models.company_search import CompanySearch, get_utc_now
from app.services.economic_service import OFFICIAL_CORPORATE_METADATA, normalize_cnae_code
from app.utils.sanitizer import sanitize_company_name

logger = logging.getLogger(__name__)


def backfill_cnae_metadata(app: Optional[Any] = None) -> Dict[str, int]:
    """
    Rotina utilitária e idempotente para preenchimento de metadados CNAE (código e descrição)
    nas tabelas CompanyRegistry e CompanySearch com base no catálogo corporativo oficial.
    Não sobrescreve dados válidos já preenchidos.
    """
    def _execute():
        now = get_utc_now()
        reg_updated = 0
        searches_updated = 0

        for cnpj_raw, meta in OFFICIAL_CORPORATE_METADATA.items():
            clean_cnpj = re.sub(r"\D", "", str(cnpj_raw or ""))
            cnae_code = normalize_cnae_code(meta.get("cnae_codigo"))
            cnae_desc = meta.get("cnae_descricao") or ""

            if not cnae_code:
                continue

            # 1. Backfill CompanyRegistry
            reg = CompanyRegistry.query.filter_by(cnpj=clean_cnpj).first()
            if reg:
                if not reg.cnae_codigo:
                    reg.cnae_codigo = cnae_code
                    reg.cnae_descricao = cnae_desc
                    reg.updated_at = now
                    reg_updated += 1
            else:
                # Cria registro base no catálogo caso ainda não exista
                new_reg = CompanyRegistry(
                    cnpj=clean_cnpj,
                    razao_social=meta.get("razao_social", "")[:255],
                    nome_fantasia=(meta.get("nome_fantasia") or "")[:255] or None,
                    situacao_cadastral=meta.get("descricao_situacao_cadastral", "ATIVA")[:50],
                    matriz_filial="1" if meta.get("identificador_matriz_filial") == 1 else "2",
                    municipio=(meta.get("municipio") or "")[:150] or None,
                    uf=(meta.get("uf") or "")[:2] or None,
                    capital_social=float(meta.get("capital_social") or 0.0),
                    cnae_codigo=cnae_code,
                    cnae_descricao=cnae_desc,
                    created_at=now,
                    updated_at=now,
                )
                db.session.add(new_reg)
                reg_updated += 1

            # 2. Backfill CompanySearch associadas por CNPJ ou nomes oficiais
            targets_to_match = set()
            targets_to_match.add(clean_cnpj)
            if meta.get("razao_social"):
                targets_to_match.add(sanitize_company_name(meta["razao_social"]))
            if meta.get("nome_fantasia"):
                targets_to_match.add(sanitize_company_name(meta["nome_fantasia"]))

            for target in targets_to_match:
                searches = CompanySearch.query.filter(
                    (CompanySearch.company_name.ilike(f"%{target}%"))
                    | (CompanySearch.normalized_name == target)
                ).all()

                for s in searches:
                    changed = False
                    if not s.cnae_codigo:
                        s.cnae_codigo = cnae_code
                        s.cnae_descricao = cnae_desc
                        changed = True

                    # Se possuir economic_standing em JSON, atualiza também a estrutura aninhada
                    if isinstance(s.economic_standing, dict):
                        econ = dict(s.economic_standing)
                        if not econ.get("cnae_codigo"):
                            econ["cnae_codigo"] = cnae_code
                            econ["cnae_descricao"] = cnae_desc
                            econ["cnae"] = {"codigo": cnae_code, "descricao": cnae_desc}
                            s.economic_standing = econ
                            changed = True

                    if changed:
                        searches_updated += 1

        db.session.commit()
        return {
            "registries_updated": reg_updated,
            "searches_updated": searches_updated,
        }

    if app:
        with app.app_context():
            return _execute()
    return _execute()


if __name__ == "__main__":
    from app import create_app
    application = create_app()
    results = backfill_cnae_metadata(application)
    print(f"CNAE Backfill concluído: {results}")
