import hashlib
import json
from typing import Any


def _sort_by_process_number(entry: dict[str, Any]) -> str:
    """Extrai o número do processo para ordenação canônica determinística do hash."""
    return str(entry.get("processo", ""))


def generate_movements_hash(processes_data: list[dict[str, Any]], rfb_status: str | None = None) -> str:
    """
    Gera um hash SHA-256 representativo do estado consolidado de movimentações,
    desfechos, tribunais e metadados dos processos e situação RFB de uma empresa.

    O hash é calculado de forma determinística ordenando:
    1. Número do processo.
    2. Data da última atualização processual (dataHoraUltimaAtualizacao).
    3. Lista ordenada de códigos de movimentação e suas respectivas datas/horas.
    4. Desfecho processual e tribunal.
    5. Situação cadastral RFB (se informada).
    """
    if not processes_data and not rfb_status:
        return hashlib.sha256(b"empty_movements").hexdigest()

    normalized_entries = []

    for item in (processes_data or []):
        proc_num = str(item.get("numero_processo") or item.get("numeroProcesso") or "").strip()
        last_update = str(item.get("dataHoraUltimaAtualizacao") or item.get("dataHora") or "").strip()
        court = str(item.get("tribunal") or item.get("court") or item.get("siglaTribunal") or "").strip().upper()
        desfecho = str(item.get("sentence_outcome") or item.get("desfecho") or "")

        # Extrai movimentos de raw_metadata ou do dicionário direto
        raw_meta = item.get("raw_metadata") or {}
        raw_movs = item.get("movimentos") or raw_meta.get("movimentos") or []

        movement_tuples = []
        if isinstance(raw_movs, list):
            for m in raw_movs:
                if isinstance(m, dict):
                    code = str(m.get("codigo") or "")
                    dt = str(m.get("dataHora") or "")
                    movement_tuples.append((code, dt))

        movement_tuples.sort()

        normalized_entries.append({
            "processo": proc_num,
            "ultima_atualizacao": last_update,
            "movimentos": movement_tuples,
            "desfecho": desfecho,
            "tribunal": court,
        })

    # Ordena pelo número do processo para garantir determinismo
    normalized_entries.sort(key=_sort_by_process_number)

    payload = {
        "processes": normalized_entries,
    }
    if rfb_status is not None:
        payload["rfb_status"] = str(rfb_status).strip().upper()

    serialized = json.dumps(payload if rfb_status is not None else normalized_entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

