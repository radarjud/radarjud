# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import logging
import re
from typing import Any

from app.extensions import db
from app.models.company_search import get_utc_now
from app.models.CompanyRegistry import CompanyRegistry
from app.services.economic_service import (
    OFFICIAL_CORPORATE_CNPJ_DIRECTORY,
    OFFICIAL_CORPORATE_METADATA,
    normalize_cnae_code,
)

logger = logging.getLogger(__name__)

# Catálogo estendido com empresas corporativas do diretório oficial e unidades ativas SOSERV/SOSERVI
EXTENDED_CATALOG_COMPANIES: dict[str, dict[str, Any]] = {
    "00776574000156": {
        "cnpj": "00776574000156",
        "razao_social": "AMERICANAS S.A.",
        "nome_fantasia": "AMERICANAS",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "RJ",
        "municipio": "RIO DE JANEIRO",
        "identificador_matriz_filial": 1,
        "capital_social": 39918282260.30,
        "cnae_codigo": "4789-0/99",
        "cnae_descricao": "Comércio varejista de outros produtos não especificados anteriormente",
    },
    "06057223000171": {
        "cnpj": "06057223000171",
        "razao_social": "SENDAS DISTRIBUIDORA S/A",
        "nome_fantasia": "ASSAI ATACADISTA",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "RJ",
        "municipio": "RIO DE JANEIRO",
        "identificador_matriz_filial": 1,
        "capital_social": 1581499597.51,
        "cnae_codigo": "4711-3/02",
        "cnae_descricao": "Comércio varejista de mercadorias em geral, com predominância de produtos alimentícios - supermercados",
    },
    "08561701000101": {
        "cnpj": "08561701000101",
        "razao_social": "PAGSEGURO INTERNET INSTITUICAO DE PAGAMENTO S.A.",
        "nome_fantasia": "PAGSEGURO",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "SP",
        "municipio": "SAO PAULO",
        "identificador_matriz_filial": 1,
        "capital_social": 5473662500.00,
        "cnae_codigo": "6619-3/99",
        "cnae_descricao": "Outras atividades auxiliares dos serviços financeiros não especificadas anteriormente",
    },
    "16501555000157": {
        "cnpj": "16501555000157",
        "razao_social": "STONE INSTITUICAO DE PAGAMENTO S.A",
        "nome_fantasia": "STONE",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "SP",
        "municipio": "SAO PAULO",
        "identificador_matriz_filial": 1,
        "capital_social": 1389445297.20,
        "cnae_codigo": "6619-3/99",
        "cnae_descricao": "Outras atividades auxiliares dos serviços financeiros não especificadas anteriormente",
    },
    "45543915000181": {
        "cnpj": "45543915000181",
        "razao_social": "CARREFOUR COMERCIO E INDUSTRIA LTDA",
        "nome_fantasia": "CARREFOUR",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "SP",
        "municipio": "SAO PAULO",
        "identificador_matriz_filial": 1,
        "capital_social": 10805230000.00,
        "cnae_codigo": "4711-3/01",
        "cnae_descricao": "Comércio varejista de mercadorias em geral, com predominância de produtos alimentícios - hipermercados",
    },
    "46124624000111": {
        "cnpj": "46124624000111",
        "razao_social": "UNIMED CAMPINAS COOPERATIVA DE TRABALHO MEDICO",
        "nome_fantasia": "UNIMED CAMPINAS",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "SP",
        "municipio": "CAMPINAS",
        "identificador_matriz_filial": 1,
        "capital_social": 0.0,
        "cnae_codigo": "6550-2/00",
        "cnae_descricao": "Planos de saúde",
    },
    "63554067000198": {
        "cnpj": "63554067000198",
        "razao_social": "HAPVIDA ASSISTENCIA MEDICA S.A.",
        "nome_fantasia": "HAPVIDA",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "CE",
        "municipio": "FORTALEZA",
        "identificador_matriz_filial": 1,
        "capital_social": 9829087699.48,
        "cnae_codigo": "6550-2/00",
        "cnae_descricao": "Planos de saúde",
    },
    # Grupo SOSERVI / SOSERV (empresas ativas e filiais homologadas)
    "09863853000121": {
        "cnpj": "09863853000121",
        "razao_social": "SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA",
        "nome_fantasia": "SOSERVI",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "PE",
        "municipio": "OLINDA",
        "identificador_matriz_filial": 1,
        "capital_social": 14510000.0,
        "cnae_codigo": "8121-4/00",
        "cnae_descricao": "Limpeza em prédios e em domicílios",
    },
    "09863853000474": {
        "cnpj": "09863853000474",
        "razao_social": "SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA",
        "nome_fantasia": "SOSERVI",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "RN",
        "municipio": "NATAL",
        "identificador_matriz_filial": 2,
        "capital_social": 14510000.0,
        "cnae_codigo": "8121-4/00",
        "cnae_descricao": "Limpeza em prédios e em domicílios",
    },
    "09863853000717": {
        "cnpj": "09863853000717",
        "razao_social": "SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA",
        "nome_fantasia": "SOSERVI",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "BA",
        "municipio": "SALVADOR",
        "identificador_matriz_filial": 2,
        "capital_social": 14510000.0,
        "cnae_codigo": "8121-4/00",
        "cnae_descricao": "Limpeza em prédios e em domicílios",
    },
    "09863853000989": {
        "cnpj": "09863853000989",
        "razao_social": "SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA",
        "nome_fantasia": "SOSERVI",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "MA",
        "municipio": "SAO LUIS",
        "identificador_matriz_filial": 2,
        "capital_social": 14510000.0,
        "cnae_codigo": "8121-4/00",
        "cnae_descricao": "Limpeza em prédios e em domicílios",
    },
    "11572781000105": {
        "cnpj": "11572781000105",
        "razao_social": "SOSERVI VIGILANCIA LTDA",
        "nome_fantasia": "SOSERVI VIGILANCIA",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "PE",
        "municipio": "OLINDA",
        "identificador_matriz_filial": 1,
        "capital_social": 690000.0,
        "cnae_codigo": "8011-1/01",
        "cnae_descricao": "Atividades de vigilância e segurança privada",
    },
    "86985595000105": {
        "cnpj": "86985595000105",
        "razao_social": "SOSERV COMERCIO REPRESENTACOES E SERVICOS LTDA",
        "nome_fantasia": "SOSERV",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "BA",
        "municipio": "LAURO DE FREITAS",
        "identificador_matriz_filial": 1,
        "capital_social": 200000.0,
        "cnae_codigo": "4633-8/01",
        "cnae_descricao": "Comércio atacadista de frutas, verduras, raízes, tubérculos, hortaliças e legumes frescos",
    },
}


def get_full_catalog_entries() -> dict[str, dict[str, Any]]:
    """
    Consolida todas as empresas conhecidas do catálogo (OFFICIAL_CORPORATE_METADATA +
    EXTENDED_CATALOG_COMPANIES + entradas do diretório corporativo) indexadas por CNPJ 14 dígitos.
    """
    merged: dict[str, dict[str, Any]] = {}
    for source in (OFFICIAL_CORPORATE_METADATA, EXTENDED_CATALOG_COMPANIES):
        for cnpj_raw, meta in source.items():
            c_clean = re.sub(r"\D", "", str(cnpj_raw or meta.get("cnpj") or ""))
            if len(c_clean) != 14:
                continue
            entry = dict(meta)
            entry["cnpj"] = c_clean
            merged[c_clean] = entry

    for brand_key, cnpjs in OFFICIAL_CORPORATE_CNPJ_DIRECTORY.items():
        for cnpj_raw in cnpjs:
            c_clean = re.sub(r"\D", "", str(cnpj_raw or ""))
            if len(c_clean) == 14 and c_clean not in merged:
                # Só adiciona fallback mínimo se já possuir dados em outra fonte
                continue

    return merged


def _normalize_catalog_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    c_clean = re.sub(r"\D", "", str(raw.get("cnpj") or ""))
    if len(c_clean) != 14:
        return None
    razao = str(raw.get("razao_social") or "").strip()[:255]
    fantasia = str(raw.get("nome_fantasia") or "").strip()[:255] or None
    situacao = str(
        raw.get("situacao_cadastral")
        or raw.get("descricao_situacao_cadastral")
        or raw.get("situacao")
        or "ATIVA"
    ).strip().upper()[:50]
    raw_mf = raw.get("matriz_filial") or raw.get("identificador_matriz_filial")
    if str(raw_mf) in ("1", "MATRIZ") or (raw_mf is None and c_clean[8:12] == "0001"):
        matriz_filial = "1"
    else:
        matriz_filial = "2"

    municipio = str(raw.get("municipio") or "").strip()[:150] or None
    uf = str(raw.get("uf") or "").strip().upper()[:2] or None

    try:
        capital_social = float(raw.get("capital_social") or 0.0)
    except (TypeError, ValueError):
        capital_social = 0.0

    cnae_raw = raw.get("cnae_codigo") or raw.get("cnae_fiscal")
    cnae_codigo = normalize_cnae_code(str(cnae_raw)) if cnae_raw else None
    cnae_desc = str(raw.get("cnae_descricao") or raw.get("cnae_fiscal_descricao") or "").strip()[:255] or None

    return {
        "cnpj": c_clean,
        "razao_social": razao,
        "nome_fantasia": fantasia,
        "situacao_cadastral": situacao,
        "matriz_filial": matriz_filial,
        "municipio": municipio,
        "uf": uf,
        "capital_social": capital_social,
        "cnae_codigo": cnae_codigo,
        "cnae_descricao": cnae_desc,
    }


def _enrich_incomplete_via_brasilapi(record: dict[str, Any]) -> dict[str, Any]:
    """Completa via BrasilAPI quando razão social, capital ou CNAE estiverem ausentes."""
    needs_enrich = (
        not record.get("razao_social")
        or float(record.get("capital_social") or 0.0) == 0.0
        or not record.get("cnae_codigo")
    )
    if not needs_enrich:
        return record

    try:
        from app.services.economic_service import fetch_accurate_cnpj_data_cached

        live = fetch_accurate_cnpj_data_cached(record["cnpj"], timeout=4.0)
        if isinstance(live, dict) and live.get("razao_social"):
            record["razao_social"] = record["razao_social"] or str(live.get("razao_social"))[:255]
            record["nome_fantasia"] = record["nome_fantasia"] or (str(live.get("nome_fantasia") or "").strip()[:255] or None)
            live_cap = float(live.get("capital_social") or 0.0)
            if live_cap > 0.0 and float(record.get("capital_social") or 0.0) == 0.0:
                record["capital_social"] = live_cap
            if not record.get("cnae_codigo") and live.get("cnae_codigo"):
                record["cnae_codigo"] = normalize_cnae_code(str(live.get("cnae_codigo")))
                record["cnae_descricao"] = (
                    record.get("cnae_descricao")
                    or (str(live.get("cnae_descricao") or "").strip()[:255] or None)
                )
            if not record.get("situacao_cadastral") and (live.get("situacao") or live.get("descricao_situacao_cadastral")):
                record["situacao_cadastral"] = str(
                    live.get("situacao") or live.get("descricao_situacao_cadastral")
                ).strip().upper()[:50]
            if not record.get("uf") and live.get("uf"):
                record["uf"] = str(live.get("uf")).strip().upper()[:2]
            if not record.get("municipio") and live.get("municipio"):
                record["municipio"] = str(live.get("municipio")).strip()[:150]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Enriquecimento BrasilAPI ignorado para CNPJ %s: %s", record["cnpj"], exc)

    return record


def seed_company_registry(
    enrich_via_brasilapi: bool = False,
    extra_records: list[dict[str, Any]] | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """
    Executa upsert idempotente do catálogo corporativo na tabela `company_registry`
    (usando uma única consulta em lote para manter baixa latência no Supabase).
    """
    merged_entries = get_full_catalog_entries()
    for extra in (extra_records or []):
        if isinstance(extra, dict):
            norm = _normalize_catalog_record(extra)
            if norm:
                existing_entry = merged_entries.get(norm["cnpj"])
                if existing_entry:
                    merged_entries[norm["cnpj"]] = {**existing_entry, **{k: v for k, v in norm.items() if v}}
                else:
                    merged_entries[norm["cnpj"]] = norm

    existing_map = {
        reg.cnpj: reg
        for reg in CompanyRegistry.query.all()
        if reg and reg.cnpj
    }

    now = get_utc_now()
    inserted = 0
    updated = 0
    unchanged = 0

    for c_clean, raw_meta in merged_entries.items():
        norm = _normalize_catalog_record(raw_meta)
        if not norm:
            continue
        if enrich_via_brasilapi:
            norm = _enrich_incomplete_via_brasilapi(norm)
        if not norm.get("razao_social"):
            continue

        existing = existing_map.get(c_clean)
        if not existing:
            reg = CompanyRegistry(
                cnpj=c_clean,
                razao_social=norm["razao_social"],
                nome_fantasia=norm["nome_fantasia"],
                situacao_cadastral=norm["situacao_cadastral"],
                matriz_filial=norm["matriz_filial"],
                municipio=norm["municipio"],
                uf=norm["uf"],
                capital_social=norm["capital_social"],
                cnae_codigo=norm["cnae_codigo"],
                cnae_descricao=norm["cnae_descricao"],
                created_at=now,
                updated_at=now,
            )
            db.session.add(reg)
            existing_map[c_clean] = reg
            inserted += 1
        else:
            changed = False
            if norm["capital_social"] > 0.0 and float(existing.capital_social or 0.0) == 0.0:
                existing.capital_social = norm["capital_social"]
                changed = True
            if not existing.cnae_codigo and norm["cnae_codigo"]:
                existing.cnae_codigo = norm["cnae_codigo"]
                existing.cnae_descricao = norm["cnae_descricao"] or existing.cnae_descricao
                changed = True
            if not existing.nome_fantasia and norm["nome_fantasia"]:
                existing.nome_fantasia = norm["nome_fantasia"]
                changed = True
            if not existing.municipio and norm["municipio"]:
                existing.municipio = norm["municipio"]
                changed = True
            if not existing.uf and norm["uf"]:
                existing.uf = norm["uf"]
                changed = True
            if not existing.situacao_cadastral and norm["situacao_cadastral"]:
                existing.situacao_cadastral = norm["situacao_cadastral"]
                changed = True
            if not existing.matriz_filial and norm["matriz_filial"]:
                existing.matriz_filial = norm["matriz_filial"]
                changed = True

            if changed:
                existing.updated_at = now
                updated += 1
            else:
                unchanged += 1

    if commit:
        db.session.commit()

    total = CompanyRegistry.query.count() if commit else len(existing_map)
    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "total": total,
    }
