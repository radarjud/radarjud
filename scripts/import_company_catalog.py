# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

"""
Script idempotente de carga e sincronização do catálogo corporativo (`company_registry`).

Uso:
    DATABASE_URL="postgresql://..." python scripts/import_company_catalog.py
    python scripts/import_company_catalog.py --sqlite-path instance/radarjud.db
    python scripts/import_company_catalog.py --skip-brasilapi
"""

import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import create_app
from app.config import Config
from app.seeds.catalog_seed import seed_company_registry

logger = logging.getLogger(__name__)

# CNPJs de referência do grupo SOSERV/SOSERVI (matriz + unidades ativas conhecidas)
SOSERV_SEED_CNPJS = [
    "09863853000121",  # SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA (Matriz - R$ 14.510.000,00)
    "09863853000474",  # SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA (Filial RN)
    "09863853000717",  # SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA (Filial BA)
    "09863853000989",  # SOSERVI-SOCIEDADE DE SERVICOS GERAIS LTDA (Filial MA)
    "11572781000105",  # SOSERVI VIGILANCIA LTDA (Matriz - R$ 690.000,00)
    "86985595000105",  # SOSERV COMERCIO REPRESENTACOES E SERVICOS LTDA (Matriz - R$ 200.000,00)
]


def load_records_from_sqlite(sqlite_path: Path) -> list[dict[str, Any]]:
    """Lê registros existentes de `company_registry` de um arquivo SQLite legado, se disponível."""
    if not sqlite_path.exists() or not sqlite_path.is_file():
        return []

    records: list[dict[str, Any]] = []
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        tables = {
            row[0]
            for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "company_registry" not in tables:
            return []
        rows = cur.execute("SELECT * FROM company_registry").fetchall()
        for row in rows:
            records.append(dict(row))
    finally:
        conn.close()
    return records


def load_records_from_file(data_path: Path) -> list[dict[str, Any]]:
    """Lê registros de catálogo a partir de arquivo JSON ou CSV."""
    if not data_path.exists() or not data_path.is_file():
        return []
    suffix = data_path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(data_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            return [r for r in payload.values() if isinstance(r, dict)]
        return []
    if suffix == ".csv":
        with data_path.open("r", encoding="utf-8", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    if suffix in (".db", ".sqlite", ".sqlite3"):
        return load_records_from_sqlite(data_path)
    return []


def discover_legacy_catalog_records(explicit_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Procura bases locais legadas (`radarjud.db`, `instance/`, `data/`) e extrai registros."""
    records: list[dict[str, Any]] = []
    candidates: list[Path] = []

    if explicit_paths:
        for p in explicit_paths:
            candidates.append(Path(p))
    else:
        candidates.append(BASE_DIR / "radarjud.db")
        for folder_name in ("instance", "data"):
            folder = BASE_DIR / folder_name
            if folder.exists() and folder.is_dir():
                candidates.extend(sorted(folder.glob("*.db")))
                candidates.extend(sorted(folder.glob("*.sqlite*")))
                candidates.extend(sorted(folder.glob("*.json")))
                candidates.extend(sorted(folder.glob("*.csv")))

    seen_paths: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen_paths or not resolved.exists():
            continue
        seen_paths.add(resolved)
        records.extend(load_records_from_file(resolved))

    return records


def _discover_active_soserv_records(skip_brasilapi: bool = False) -> list[dict[str, Any]]:
    """Garante inclusão das empresas/filiais SOSERV/SOSERVI ativas, consultando BrasilAPI quando ativo."""
    discovered: list[dict[str, Any]] = []
    candidate_cnpjs = set(SOSERV_SEED_CNPJS)

    if not skip_brasilapi:
        try:
            from app.services.cnj_searcher import CNJProcessSearcher
            from app.services.economic_service import fetch_accurate_cnpj_data_cached

            searcher = CNJProcessSearcher()
            procs, _disc = searcher.fetch_comunica_pje_all_branches(
                "SOSERV", max_pages=3, time_budget_seconds=10.0, max_records=150
            )
            for p in procs.values():
                for c in p.get("cnpjs_encontrados") or []:
                    d = re.sub(r"\D", "", str(c))
                    if len(d) == 14:
                        candidate_cnpjs.add(d)

            for cnpj in sorted(candidate_cnpjs):
                live = fetch_accurate_cnpj_data_cached(cnpj, timeout=4.0)
                if isinstance(live, dict) and live.get("razao_social"):
                    sit = str(live.get("situacao") or "ATIVA").upper()
                    if "ATIVA" in sit:
                        discovered.append(live)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Descoberta complementar SOSERV via BrasilAPI ignorada: %s", exc)

    return discovered


def import_company_catalog(
    sqlite_paths: list[str] | None = None,
    skip_brasilapi: bool = False,
) -> dict[str, int]:
    """
    Executa a importação idempotente de catálogo corporativo usando Config.SQLALCHEMY_DATABASE_URI.
    """
    legacy_records = discover_legacy_catalog_records(explicit_paths=sqlite_paths)
    soserv_records = _discover_active_soserv_records(skip_brasilapi=skip_brasilapi)
    extra_records = legacy_records + soserv_records

    app = create_app(
        "production" if Config.SQLALCHEMY_DATABASE_URI.startswith("postgresql") else "development",
        seed_catalog=False,
    )
    with app.app_context():
        summary = seed_company_registry(
            enrich_via_brasilapi=not skip_brasilapi,
            extra_records=extra_records,
            commit=True,
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa/sincroniza catálogo corporativo completo (company_registry) no banco configurado."
    )
    parser.add_argument(
        "--sqlite-path",
        action="append",
        dest="sqlite_paths",
        default=None,
        help="Caminho opcional para SQLite/JSON/CSV legado contendo registros extras.",
    )
    parser.add_argument(
        "--skip-brasilapi",
        action="store_true",
        help="Não consulta BrasilAPI/Comunica PJe online durante a carga.",
    )
    args = parser.parse_args()

    summary = import_company_catalog(
        sqlite_paths=args.sqlite_paths,
        skip_brasilapi=args.skip_brasilapi,
    )
    print(
        f"Catálogo company_registry sincronizado com sucesso | "
        f"Inseridos: {summary['inserted']} | "
        f"Atualizados: {summary['updated']} | "
        f"Sem alteração: {summary['unchanged']} | "
        f"Total geral: {summary['total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
