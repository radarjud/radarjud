# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import re
import math
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Any, Optional, Tuple, Union, TypedDict

from app.services.tpu_constants import (
    TPU_SUBJECT_BASELINES,
    SUBJECT_KEYWORD_BASELINES,
    TRT_REGIONAL_LOGIT_MODIFIERS,
    DEFAULT_TRT_BASE_PROBABILITY,
    DEFAULT_TRT_ACORDO_PROBABILITY,
    DEFAULT_TRT_IMP_PROBABILITY,
    BAYESIAN_ALPHA_PRIOR,
    EXECUTION_PHASE_DESCRIPTION,
    normalize_subject_text,
    is_execution_phase_label,
    extract_class_names,
    classify_execution_vs_material_labels,
)

logger = logging.getLogger(__name__)

try:
    from scipy.stats import beta as _scipy_beta  # type: ignore
except ImportError:
    _scipy_beta = None


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Converte valor para int com segurança, tratando None, booleanos e strings não numéricas."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def empty_execution_phase_summary() -> Dict[str, Any]:
    """Retorna estrutura padrão vazia para processos em fase de execução/cumprimento."""
    return {
        "total_processos": 0,
        "percentual_total": 0.0,
        "rotulos": [],
        "descricao": EXECUTION_PHASE_DESCRIPTION,
    }


def build_execution_phase_summary(processes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Computa execution_phase_summary diretamente de uma lista de processos."""
    total = len(processes or [])
    if total == 0:
        return empty_execution_phase_summary()

    execution_only_count = 0
    labels_counter: Counter = Counter()
    display_names: Dict[str, str] = {}

    for p in processes:
        raw_subjects = p.get("subjects")
        if isinstance(raw_subjects, list) and raw_subjects:
            subj_list = [str(s).strip() for s in raw_subjects if str(s).strip()]
        else:
            primary = str(p.get("primary_subject") or "").strip()
            subj_list = [primary] if primary else []

        _mat, exec_labels, is_exec_only = classify_execution_vs_material_labels(
            subj_list, classes_pje=p.get("classes_pje")
        )
        if not is_exec_only:
            continue

        execution_only_count += 1
        seen_norm = set()
        for lbl in exec_labels:
            norm = normalize_subject_text(lbl)
            if norm and norm not in seen_norm:
                seen_norm.add(norm)
                if norm not in display_names:
                    display_names[norm] = lbl.strip()
                labels_counter[norm] += 1

    return {
        "total_processos": execution_only_count,
        "percentual_total": round((execution_only_count / total) * 100.0, 1),
        "rotulos": [display_names[n] for n, _ in labels_counter.most_common()],
        "descricao": EXECUTION_PHASE_DESCRIPTION,
    }


def normalize_execution_phase_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Normaliza payload (inclusive registros legados em cache L1/BD) separando
    eventuais rótulos estritamente de execução/cumprimento das tabelas de motivos
    e garantindo a presença de 'execution_phase_summary'.
    """
    if not isinstance(payload, dict):
        return payload

    total_processos = safe_int(payload.get("total_processos"), default=0) or 0
    existing_exec = payload.get("execution_phase_summary")
    existing_fase_exec = payload.get("fase_execucao")
    ranking = payload.get("ranking_motivos") or []
    ranking_cond = payload.get("ranking_motivos_condenacao") or []
    cpm = payload.get("condenacao_por_motivo") or payload.get("motivos_processo") or []

    def _item_label(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("motivo") or item.get("nome") or "").strip()
        return ""

    filtered_ranking = [
        item for item in ranking if not is_execution_phase_label(_item_label(item))
    ]
    filtered_ranking_cond = [
        item for item in ranking_cond if not is_execution_phase_label(_item_label(item))
    ]
    filtered_cpm = [
        item for item in cpm if not is_execution_phase_label(_item_label(item))
    ]

    if isinstance(existing_exec, dict) and "total_processos" in existing_exec:
        exec_total = safe_int(existing_exec.get("total_processos"), default=0) or 0
        pct = existing_exec.get("percentual_total")
        if pct is None:
            pct = round((exec_total / total_processos) * 100.0, 1) if total_processos > 0 else 0.0
        rotulos = [
            str(r).strip()
            for r in (existing_exec.get("rotulos") or [])
            if str(r).strip()
        ]
        summary = {
            "total_processos": exec_total,
            "percentual_total": round(float(pct or 0.0), 1),
            "rotulos": rotulos,
            "descricao": existing_exec.get("descricao") or EXECUTION_PHASE_DESCRIPTION,
        }
    else:
        # Fallback determinístico para payloads antigos em cache/banco
        exec_rotulos: List[str] = []
        seen_norm = set()
        exec_total = 0

        # Preferir condenacao_por_motivo (partição 1:1 de processos) se houver itens de execução nela
        cpm_exec_items = [item for item in cpm if is_execution_phase_label(_item_label(item))]
        ranking_exec_items = [item for item in ranking if is_execution_phase_label(_item_label(item))]
        cond_exec_items = [item for item in ranking_cond if is_execution_phase_label(_item_label(item))]

        source_for_count = cpm_exec_items or ranking_exec_items or cond_exec_items
        for item in source_for_count:
            cnt = safe_int(
                item.get("total_processos")
                if item.get("total_processos") is not None
                else (item.get("quantidade") if item.get("quantidade") is not None else item.get("total")),
                default=0,
            ) or 0
            exec_total += cnt

        for item in cpm_exec_items + ranking_exec_items + cond_exec_items:
            lbl = _item_label(item)
            norm = normalize_subject_text(lbl)
            if lbl and norm not in seen_norm:
                seen_norm.add(norm)
                exec_rotulos.append(lbl)

        if total_processos > 0 and exec_total > total_processos:
            exec_total = total_processos

        summary = {
            "total_processos": exec_total,
            "percentual_total": round((exec_total / total_processos) * 100.0, 1) if total_processos > 0 else 0.0,
            "rotulos": exec_rotulos,
            "descricao": EXECUTION_PHASE_DESCRIPTION,
        }

    # Normalização de fase_execucao padronizada pelo contrato novo
    if isinstance(existing_fase_exec, dict) and "total_processos" in existing_fase_exec:
        fase_exec_dict = {
            "total_processos": safe_int(existing_fase_exec.get("total_processos"), default=0) or 0,
            "percentual_base": round(float(existing_fase_exec.get("percentual_base", 0.0) or 0.0), 1),
            "rotulo": existing_fase_exec.get("rotulo") or "Processos em Fase de Execução / Sentença Aplicada",
            "descricao": existing_fase_exec.get("descricao") or "Processos já concluídos na fase de conhecimento e em etapa de cumprimento de sentença.",
        }
    else:
        fase_exec_dict = {
            "total_processos": summary["total_processos"],
            "percentual_base": summary["percentual_total"],
            "rotulo": "Processos em Fase de Execução / Sentença Aplicada",
            "descricao": "Processos já concluídos na fase de conhecimento e em etapa de cumprimento de sentença.",
        }

    # Normalização do Risco Subsidiário estruturado (sem peso de solvência)
    raw_risco = payload.get("risco_subsidiario")
    if isinstance(raw_risco, dict) and "percentual" in raw_risco:
        pct_risco = round(float(raw_risco.get("percentual", 0.0) or 0.0), 1)
        faixa = raw_risco.get("faixa_exposicao") or ("BAIXA" if pct_risco <= 30.0 else ("MODERADA" if pct_risco < 60.0 else "ALTA"))
        rotulo = raw_risco.get("rotulo_exposicao") or (
            "Baixa Exposição Trabalhista" if faixa == "BAIXA"
            else ("Exposição Moderada — Blindagem Contratual Recomendada" if faixa == "MODERADA"
                  else "Alta Exposição — Provisionamento e Diligência Rigorosa")
        )
        risco_dict = {
            "percentual": pct_risco,
            "faixa_exposicao": faixa,
            "rotulo_exposicao": rotulo,
            "metodologia_resumo": raw_risco.get("metodologia_resumo") or "Ponderado por definitividade: TST/Execução peso 1.0, TRT recorrível 0.85, Acordo 0.55",
            "agravante_tst_aplicado": bool(raw_risco.get("agravante_tst_aplicado", False)),
        }
    else:
        try:
            pct_risco = round(float(raw_risco if raw_risco is not None else (payload.get("score_risco_terceirizacao") or payload.get("overall_risk_score") or 0.0)), 1)
        except (ValueError, TypeError):
            pct_risco = 0.0
        faixa = "BAIXA" if pct_risco <= 30.0 else ("MODERADA" if pct_risco < 60.0 else "ALTA")
        rotulo = (
            "Baixa Exposição Trabalhista" if faixa == "BAIXA"
            else ("Exposição Moderada — Blindagem Contratual Recomendada" if faixa == "MODERADA"
                  else "Alta Exposição — Provisionamento e Diligência Rigorosa")
        )
        risco_dict = {
            "percentual": pct_risco,
            "faixa_exposicao": faixa,
            "rotulo_exposicao": rotulo,
            "metodologia_resumo": "Ponderado por definitividade: TST/Execução peso 1.0, TRT recorrível 0.85, Acordo 0.55",
            "agravante_tst_aplicado": False,
        }

    # Normalização de comparativo_instancias
    comp_inst = payload.get("comparativo_instancias")
    if not isinstance(comp_inst, dict) or "trt" not in comp_inst or "tst" not in comp_inst:
        comp_inst = {
            "trt": {
                "total_processos": total_processos,
                "julgados": safe_int(payload.get("total_julgados"), default=0) or 0,
                "condenacoes": safe_int((payload.get("desfechos_sentencas") or {}).get("condenacoes"), default=0) or 0,
                "acordos": safe_int((payload.get("desfechos_sentencas") or {}).get("acordos"), default=0) or 0,
                "favoraveis": safe_int((payload.get("desfechos_sentencas") or {}).get("improcedentes"), default=0) or 0,
                "em_aberto": safe_int((payload.get("desfechos_sentencas") or {}).get("em_aberto"), default=0) or 0,
                "taxa_condenacao": float(payload.get("taxa_historica_condenacao_pct") or 0.0),
            },
            "tst": {
                "total_processos": 0,
                "finalizados": 0,
                "condenacoes_mantidas": 0,
                "favoraveis_ou_revertidos": 0,
                "taxa_condenacao": 0.0,
                "taxa_reversao_recurso": 0.0,
                "amostra_reduzida": True,
            },
        }

    payload["ranking_motivos"] = filtered_ranking
    payload["ranking_motivos_condenacao"] = filtered_ranking_cond
    payload["condenacao_por_motivo"] = filtered_cpm
    payload["motivos_processo"] = filtered_cpm
    payload["execution_phase_summary"] = summary
    payload["fase_execucao"] = fase_exec_dict
    payload["risco_subsidiario"] = risco_dict
    payload["comparativo_instancias"] = comp_inst
    payload["score_risco_terceirizacao"] = risco_dict["percentual"]
    payload["overall_risk_score"] = risco_dict["percentual"]
    return payload


def _tilt_simplex(
    p_cond: float,
    p_acordo: float,
    p_imp: float,
    delta_logit: float,
) -> Tuple[float, float, float]:
    """
    Aplica deslocamento em escala logit (delta_logit) sobre p_cond na simplex 3-dimensional (softmax tilting).
    Preserva estritamente:
    1) p'_cond + p'_acordo + p'_imp == 1.0
    2) logit(p'_cond) == logit(p_cond) + delta_logit
    3) p'_acordo / p'_imp == p_acordo / p_imp (independência de alternativas irrelevantes)
    """
    s = max(1e-9, p_cond + p_acordo + p_imp)
    pc, pa, pi = p_cond / s, p_acordo / s, p_imp / s
    if delta_logit == 0.0:
        return pc, pa, pi
    mult = math.exp(max(-10.0, min(10.0, delta_logit)))
    c_tilted = pc * mult
    z = c_tilted + pa + pi
    return c_tilted / z, pa / z, pi / z


def _betacf(a: float, b: float, x: float) -> float:
    """Fração continuada de Lentz para a função Beta Incompleta Regularizada I_x(a, b)."""
    max_iter = 200
    eps = 3.0e-9
    fpmin = 1.0e-30

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d

    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c

        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break

    return h


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """Calcula a CDF da distribuição Beta(a, b) no ponto x in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    bt = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """
    Calcula o quantil q in (0, 1) da distribuição Beta(a, b).
    Usa scipy.stats.beta.ppf se disponível, ou bissecção sobre I_x(a, b) com precisão 1e-9.
    """
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    if _scipy_beta is not None:
        return float(_scipy_beta.ppf(q, a, b))

    lo, hi = 0.0, 1.0
    for _ in range(45):
        mid = 0.5 * (lo + hi)
        if regularized_incomplete_beta(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _round_simplex_pct(theta_cond: float, theta_acordo: float, theta_imp: float) -> Tuple[float, float, float]:
    """
    Arredonda as 3 probabilidades da simplex para 1 casa decimal garantindo soma exata de 100.0%,
    calculando cada componente diretamente de sua própria massa posterior e ajustando apenas o
    resíduo de arredondamento (±0.1%) no maior componente.
    """
    c_pct = round(theta_cond * 100.0, 1)
    a_pct = round(theta_acordo * 100.0, 1)
    i_pct = round(theta_imp * 100.0, 1)
    diff = round(100.0 - (c_pct + a_pct + i_pct), 1)
    if diff != 0.0:
        if c_pct >= a_pct and c_pct >= i_pct:
            c_pct = round(c_pct + diff, 1)
        elif a_pct >= c_pct and a_pct >= i_pct:
            a_pct = round(a_pct + diff, 1)
        else:
            i_pct = round(i_pct + diff, 1)
    return c_pct, a_pct, i_pct


def _extract_process_date(proc: Dict[str, Any]) -> str:
    """Extrai a data de atualização ou publicação de um processo para ordenação cronológica."""
    return str(proc.get("dataHoraUltimaAtualizacao") or proc.get("data_publicacao") or "")


def _build_default_reason_stat() -> Dict[str, Any]:
    """Inicializa contadores estatísticos agregados por motivo de ação trabalhista."""
    return {
        "cond": 0.0,
        "acordo": 0.0,
        "imp": 0.0,
        "laudo_count": 0,
        "total_count": 0,
        "open_count": 0,
        "code": None,
        "tribunals": Counter(),
    }


def _sort_predictions_priority(pred: Dict[str, Any]) -> Tuple[int, float]:
    """Critério de prioridade de exibição: maior volume de processos abertos e maior probabilidade de condenação."""
    return (int(pred.get("processos_abertos", 0)), float(pred.get("probabilidade_condenacao_pct", 0.0)))


def _sort_condenacoes_quantity(item: Dict[str, Any]) -> float:
    """Ordena ranking de motivos pela quantidade absoluta de condenações pretéritas."""
    return float(item.get("condenacoes") or item.get("quantidade_condenacoes") or item.get("quantidade") or 0.0)


def build_conviction_ranking_and_vectors(processes: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Gera:
    1. ranking_condenacoes: Motivos onde a empresa teve CONDENAÇÃO confirmada (condenacoes >= 1),
       deixando explícito o número de condenações e a taxa de perda naquele motivo.
    2. vetores_recorrentes: Todos os motivos mais frequentes no geral (sem duplicar nomes!).
    """
    stats_by_motive = {}
    total_convictions_all = sum(
        1 for p in processes
        if (p.get("statusDesfecho") == "CONDENACAO" or p.get("sentence_outcome") == "condenacoes")
    )

    for p in processes:
        is_condenacao = (p.get("statusDesfecho") == "CONDENACAO" or p.get("sentence_outcome") == "condenacoes")
        is_acordo = (p.get("statusDesfecho") == "ACORDO" or p.get("sentence_outcome") == "acordos")

        raw_assuntos = p.get("assuntos") or []
        if not raw_assuntos and p.get("subjects"):
            raw_assuntos = [{"codigo": 0, "nome": s} for s in p.get("subjects") if s]
        if not raw_assuntos and p.get("primary_subject"):
            raw_assuntos = [{"codigo": 0, "nome": p.get("primary_subject")}]

        raw_names = [
            ((ass.get("nome") if isinstance(ass, dict) else str(ass)) or "").strip()
            for ass in raw_assuntos
        ]
        raw_names = [n for n in raw_names if n]
        material_labels, _exec_labels, is_exec_only = classify_execution_vs_material_labels(
            raw_names, classes_pje=p.get("classes_pje")
        )
        if is_exec_only:
            continue

        effective_names = material_labels if material_labels else (raw_names or ["Reclamação em Apuração"])

        # Evita contar o mesmo motivo duas vezes no mesmo processo
        seen_names_in_proc = set()
        for nome in effective_names:
            if not nome or nome in seen_names_in_proc:
                continue
            seen_names_in_proc.add(nome)

            entry = stats_by_motive.setdefault(nome, {
                "motivo": nome,
                "nome": nome,
                "total": 0,
                "count": 0,
                "condenacoes": 0,
                "impactos_confirmados": 0,
                "quantidade": 0,
                "acordos": 0,
            })
            entry["total"] += 1
            entry["count"] += 1
            if is_condenacao:
                entry["condenacoes"] += 1
                entry["impactos_confirmados"] += 1
                entry["quantidade"] += 1
            elif is_acordo:
                entry["acordos"] += 1

    # 1. Lista de Vetores mais recorrentes (ordenada por total de processos, sem duplicatas)
    vetores_recorrentes = sorted(stats_by_motive.values(), key=lambda x: x["total"], reverse=True)

    # 2. Ranking de Condenações (APENAS motivos onde condenacoes >= 1, ordenado por condenações!)
    ranking_condenacoes = []
    for item in stats_by_motive.values():
        if item["condenacoes"] > 0:
            taxa_no_motivo = round((item["condenacoes"] / max(1, item["total"])) * 100, 1)
            share_condenacoes = round((item["condenacoes"] / max(1, total_convictions_all)) * 100, 1)
            ranking_condenacoes.append({
                **item,
                "quantidade_condenacoes": item["condenacoes"],
                "porcentagem_sobre_condenacoes": share_condenacoes,
                "taxa_no_motivo": taxa_no_motivo,
                "taxa_perda_no_motivo": taxa_no_motivo,
                "percentual": share_condenacoes,
                "label_direita": f"{item['condenacoes']} condenações ({taxa_no_motivo}% de perda no motivo)"
            })

    ranking_condenacoes.sort(key=lambda x: (x["condenacoes"], x["total"]), reverse=True)
    return ranking_condenacoes[:8], vetores_recorrentes[:8]


def classify_process_canonical_motive(process: Dict[str, Any]) -> str:
    """
    Classifica determinísticamente o processo em uma das categorias canônicas de negócio.
    Avalia os metadados do DataJud/PJe e texto em memória com baixo consumo de RAM.
    Garante fallback seguro para 'Assunto não especificado' caso nenhum tema seja informado.
    """
    candidates: List[str] = []

    raw_assuntos = process.get("assuntos")
    if isinstance(raw_assuntos, list):
        for item in raw_assuntos:
            if isinstance(item, dict):
                nome = item.get("nome") or item.get("descricao")
                if nome:
                    candidates.append(str(nome))
            elif isinstance(item, str) and item.strip():
                candidates.append(item.strip())

    raw_subjects = process.get("subjects")
    if isinstance(raw_subjects, list):
        for s in raw_subjects:
            if s and str(s).strip():
                candidates.append(str(s).strip())

    primary = process.get("primary_subject")
    if primary and str(primary).strip():
        candidates.append(str(primary).strip())

    if not candidates:
        classes = process.get("classes_pje") or []
        if isinstance(classes, list):
            for c in classes:
                if isinstance(c, dict):
                    nome = c.get("nome") or c.get("descricao")
                    if nome:
                        candidates.append(str(nome))
                elif isinstance(c, str) and c.strip():
                    candidates.append(c.strip())

        texts = process.get("publicacoes_texto") or ([process["resumoPublicacao"]] if process.get("resumoPublicacao") else [])
        if isinstance(texts, list):
            for t in texts[:3]:
                if t and isinstance(t, str):
                    candidates.append(t[:300])

    if not candidates:
        return "Assunto não especificado"

    material_candidates, _exec_candidates, is_exec_only = classify_execution_vs_material_labels(
        candidates, classes_pje=process.get("classes_pje")
    )
    if is_exec_only:
        return "Execução / Cumprimento de Sentença"
    if material_candidates:
        candidates = material_candidates

    # Se houver candidatos materiais, filtra rótulos de execução antes de normalizar
    non_exec_candidates = [c for c in candidates if not is_execution_phase_label(c)]
    if non_exec_candidates:
        candidates = non_exec_candidates

    full_normalized = " ".join(normalize_subject_text(c) for c in candidates)
    if not full_normalized.strip() or full_normalized.strip() in ("assunto nao especificado", "nao informado"):
        return "Assunto não especificado"

    # 1. Execução / Cumprimento de Sentença (APENAS se não houver temas substantivos identificados)
    if not non_exec_candidates and any(k in full_normalized for k in (
        "execuc", "cumprimento de senten", "liquidac", "penhora", "sisbajud",
        "bloqueio judicial", "expropriac", "desconsideracao da personalidade",
    )):
        return "Execução / Cumprimento de Sentença"

    # 2. FGTS / Multa 40% (prioritário sobre rescisórias gerais quando FGTS é o foco)
    if any(k in full_normalized for k in (
        "fgts", "fundo de garantia", "multa de 40%", "multa 40%",
        "levantamento de fgts", "deposito de fgts", "deposito do fgts",
    )):
        return "FGTS / Multa 40%"

    # 3. Verbas Rescisórias
    if any(k in full_normalized for k in (
        "verbas rescisori", "rescisao", "rescisor", "aviso previo", "decimo terceiro",
        "13o salario", "13º salario", "saldo de salario", "477", "467", "atraso rescis", "guias rescis",
    )):
        return "Verbas Rescisórias"

    # 4. Horas Extras / Jornada
    if any(k in full_normalized for k in (
        "hora extra", "horas extras", "sobrejornada", "jornada", "intrajornada",
        "interjornada", "banco de horas", "cartao de ponto", "controle de ponto",
        "registro de ponto", "folha de ponto", "trabalho noturno",
        "adicional noturno", "repouso semanal", "dsr", "sobreaviso", "prontidao",
    )):
        return "Horas Extras / Jornada"

    # 5. Adicionais (Insalubridade / Periculosidade)
    if any(k in full_normalized for k in (
        "insalubridade", "periculosidade", "adicional de insalubridade",
        "adicional de periculosidade", "grau maximo", "grau medio", "grau minimo",
    )):
        return "Adicionais (Insalubridade / Periculosidade)"

    # 6. Danos Morais / Assédio
    if any(k in full_normalized for k in (
        "dano moral", "danos morais", "assedio moral", "assedio sexual",
        "indenizacao por dano", "ofensa", "acidente de trabalho", "doenca ocupacional",
    )):
        return "Danos Morais / Assédio"

    # 7. Vínculo Empregatício
    if any(k in full_normalized for k in (
        "vinculo empregaticio", "reconhecimento de vinculo", "relacao de emprego",
        "pejotizacao", "anotacao na ctps", "anotacao de ctps", "registro em carteira",
        "fraude trabalhista", "carteira de trabalho",
    )):
        return "Vínculo Empregatício"

    # Qualquer outro assunto formal reconhecido cai em Outros
    return "Outros"


def is_process_conviction(p: Dict[str, Any], canonical_motive: Optional[str] = None) -> bool:
    """
    Verifica se o processo resultou em condenação judicial confirmada.
    Considera também execuções e liquidações onde o passivo condenatório já é definitivo.
    """
    status_desfecho = str(p.get("statusDesfecho") or "").strip().upper()
    raw_status = str(p.get("sentence_outcome") or "em_aberto").strip().lower()
    if status_desfecho == "CONDENACAO" or raw_status == "condenacoes":
        return True
    if canonical_motive == "Execução / Cumprimento de Sentença" and raw_status in ("", "em_aberto"):
        return True
    return False


def build_condenacao_por_motivo(processes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Agrupa 100% dos processos mapeados em categorias canônicas de motivo, calculando
    a taxa individual de condenação e a faixa de variação amigável (sem slice arbitrário).
    Invariantes:
    - sum(item['total_processos'] for item in condenacao_por_motivo) == len(substantivos)
    - Regra de bloqueio: condenacoes == 0 -> taxa_condenacao == 0.0 e faixa_variacao == '0,0% a 0,0%'
    - Ordenação determinística por maior incidência de condenações (desempate por total_processos).
    - Exclusão estrita de 'Execução / Cumprimento de Sentença'.
    """
    if not processes:
        return []

    # Agrupa todos os processos substantivos por motivo canônico (exclui estritamente execução/cumprimento)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in processes:
        motive = classify_process_canonical_motive(p)
        if motive == "Execução / Cumprimento de Sentença" or is_execution_phase_label(motive):
            continue
        grouped[motive].append(p)

    results: List[Dict[str, Any]] = []

    for motivo, procs in grouped.items():
        total_proc = len(procs)
        condenacoes = sum(1 for p in procs if is_process_conviction(p, motivo))

        if condenacoes == 0:
            taxa_cond = 0.0
            faixa_var = "0,0% a 0,0%"
            lo_val = 0.0
            hi_val = 0.0
        else:
            taxa_cond = round((condenacoes / total_proc) * 100.0, 1)
            # Prior Dirichlet não-informativo (1/3) na simplex tripartida
            a0 = 1.0 / 3.0
            b0 = 1.0 / 3.0
            lo = beta_ppf(0.05, condenacoes + a0, (total_proc - condenacoes) + b0) * 100.0
            hi = beta_ppf(0.95, condenacoes + a0, (total_proc - condenacoes) + b0) * 100.0
            lo_val = round(lo, 1)
            hi_val = round(hi, 1)
            lo_str = f"{lo:.1f}".replace(".", ",")
            hi_str = f"{hi:.1f}".replace(".", ",")
            faixa_var = f"{lo_str}% a {hi_str}%"

        results.append({
            "motivo": motivo,
            "processos_mapeados": total_proc,
            "total_processos": total_proc,
            "condenacoes": condenacoes,
            "taxa_condenacao": taxa_cond,
            "faixa_variacao_min": lo_val,
            "faixa_variacao_max": hi_val,
            "faixa_variacao": faixa_var,
        })

    results.sort(key=lambda x: (x["condenacoes"], x["total_processos"]), reverse=True)
    return results


def is_tst_process(p: Dict[str, Any]) -> bool:
    """Verifica se o processo pertence à esfera do TST (Instância Máxima)."""
    if not isinstance(p, dict):
        return False
    if p.get("is_tst_recurso") is True:
        return True
    trib = str(p.get("tribunal") or p.get("siglaTribunal") or p.get("sigla_tribunal") or "").strip().upper()
    if trib == "TST" or "TST" in trib:
        return True
    grau = str(p.get("grau") or "").strip().upper()
    if "TST" in grau:
        return True
    num = str(p.get("numero_processo") or p.get("numeroProcesso") or p.get("numero_cnj") or "").strip()
    if ".5.00." in num:
        return True
    return False


def is_process_finalized_tst(p: Dict[str, Any]) -> bool:
    """
    Verifica se um processo no TST já possui decisão/acórdão finalizado (não cabem mais recursos).
    Aplica-se independentemente de ter resultado em condenação, acordo ou improcedência.
    """
    if not is_tst_process(p):
        return False
    if p.get("instancia_maxima_finalizado") is True:
        return True
    status_desfecho = str(p.get("statusDesfecho") or "").strip().upper()
    raw_status = str(p.get("sentence_outcome") or "").strip().lower()
    fase = str(p.get("fase_processual") or "").strip().upper()

    if fase in ("INSTANCIA_MAXIMA", "FINALIZADO", "TRANSITADO"):
        return True
    if status_desfecho in ("CONDENACAO", "ACORDO", "IMPROCEDENTE", "EXTINTO"):
        return True
    if raw_status in (
        "condenacoes", "condenacao", "acordos", "acordo",
        "improcedentes", "improcedente", "extintos_sem_merito",
        "baixa_arquivado", "arquivado", "desistencia"
    ):
        return True
    if raw_status == "em_aberto" or status_desfecho == "EM_ABERTO":
        return False
    return bool(status_desfecho or raw_status)


def is_process_execution_phase(p: Dict[str, Any]) -> bool:
    """
    Verifica se o processo se encontra em fase de Execução / Cumprimento de Sentença.
    """
    if not isinstance(p, dict):
        return False
    fase = str(p.get("fase_processual") or "").strip().upper()
    if any(k in fase for k in ("EXECU", "CUMPRIMENTO", "LIQUIDA")):
        return True

    classes_pje = p.get("classes_pje") or []
    if isinstance(classes_pje, list):
        for c in classes_pje:
            c_name = ""
            if isinstance(c, dict):
                c_name = str(c.get("nome") or c.get("descricao") or "")
            elif isinstance(c, str):
                c_name = c
            if is_execution_phase_label(c_name):
                return True

    subjects = p.get("subjects") or []
    if isinstance(subjects, list):
        for s in subjects:
            if is_execution_phase_label(str(s)):
                return True

    primary = str(p.get("primary_subject") or "")
    if is_execution_phase_label(primary):
        return True

    obs = str(p.get("observation") or "").lower()
    if any(k in obs for k in (
        "execucao", "execução", "cumprimento de sentenca", "cumprimento de sentença",
        "liquidacao", "liquidação", "sisbajud", "penhora", "bloqueio judicial"
    )):
        return True

    return False


def calculate_weighted_subsidiary_risk_and_instances(
    processes: List[Dict[str, Any]],
    previsao_bloqueada: bool = False,
    sub_count: int = 0,
) -> Dict[str, Any]:
    """
    Implementa a Nova Fórmula de Risco Subsidiário Ponderado por Definitividade:
    Tefetiva = [(1,00 * Cdef) + (0.85 * Crec) + (0.55 * A)] / [Cdef + Crec + A + I] * 100

    Estados Processuais:
    - Instância Máxima (TST): Processos finalizados no TST. Peso 1,00 se condenado.
    - Definitivo / Execução (TRTs): Processos nos TRTs em fase de execução. Peso 1,00.
    - Condenação Recorrível (TRTs): Condenações nos TRTs fora de execução. Peso 0,85.
    - Acordo Homologado: Processos encerrados por acordo. Peso 0,55.
    - Improcedente / Favorável: Processos julgados sem condenação. Peso 0,00.

    Agravante por Reincidência no TST (Proteção Amostral):
    - Volume mínimo confiável: NTST >= 10 processos julgados no TST.
    - Padrão desfavorável: Taxa de condenação mantida no TST > taxa dos TRTs OU condenação subsidiária mantida no TST.
    - Quando ambas são verdadeiras: agravante de +10% relativo (teto de 100%).
    """
    total_procs = len(processes or [])
    if total_procs == 0:
        return {
            "risco_subsidiario": {
                "percentual": 0.0,
                "faixa_exposicao": "BAIXA",
                "rotulo_exposicao": "Baixa Exposição Trabalhista",
                "metodologia_resumo": "Ponderado por definitividade: TST/Execução peso 1.0, TRT recorrível 0.85, Acordo 0.55",
                "agravante_tst_aplicado": False,
            },
            "fase_execucao": {
                "total_processos": 0,
                "percentual_base": 0.0,
                "rotulo": "Processos em Fase de Execução / Sentença Aplicada",
                "descricao": "Processos já concluídos na fase de conhecimento e em etapa de cumprimento de sentença.",
            },
            "comparativo_instancias": {
                "trt": {
                    "total_processos": 0,
                    "julgados": 0,
                    "condenacoes": 0,
                    "acordos": 0,
                    "favoraveis": 0,
                    "em_aberto": 0,
                    "taxa_condenacao": 0.0,
                },
                "tst": {
                    "total_processos": 0,
                    "finalizados": 0,
                    "condenacoes_mantidas": 0,
                    "favoraveis_ou_revertidos": 0,
                    "taxa_condenacao": 0.0,
                    "taxa_reversao_recurso": 0.0,
                    "amostra_reduzida": True,
                },
            },
            "processos_amostra": [],
        }

    trt_procs: List[Dict[str, Any]] = []
    tst_procs: List[Dict[str, Any]] = []

    for p in processes:
        if is_tst_process(p):
            tst_procs.append(p)
        else:
            trt_procs.append(p)

    # Contabilidade TRT
    cond_trt = 0
    acordos_trt = 0
    fav_trt = 0
    em_aberto_trt = 0

    # Contabilidade TST
    cond_tst = 0
    fav_tst = 0
    acordos_tst = 0
    em_aberto_tst = 0

    # Categorias da Fórmula Ponderada
    c_def = 0.0
    c_rec = 0.0
    total_a = 0.0
    total_i = 0.0
    total_execucao = 0

    for p in processes:
        in_tst = is_tst_process(p)
        in_exec = is_process_execution_phase(p)
        if in_exec:
            total_execucao += 1

        raw_status = str(p.get("sentence_outcome") or "em_aberto").strip().lower()
        status_desfecho = str(p.get("statusDesfecho") or "").strip().upper()

        is_cond = (
            status_desfecho == "CONDENACAO"
            or raw_status in ("condenacoes", "condenacao")
            or is_process_conviction(p)
        )
        is_acordo = status_desfecho == "ACORDO" or raw_status in ("acordos", "acordo")
        is_imp = status_desfecho == "IMPROCEDENTE" or raw_status in (
            "improcedentes", "improcedente", "extintos_sem_merito", "extinto_sem_merito"
        )
        is_open = not (is_cond or is_acordo or is_imp)

        # Atualiza métricas por esfera
        if in_tst:
            if is_cond:
                cond_tst += 1
            elif is_acordo:
                acordos_tst += 1
                fav_tst += 1
            elif is_imp:
                fav_tst += 1
            else:
                em_aberto_tst += 1
        else:
            if is_cond:
                cond_trt += 1
            elif is_acordo:
                acordos_trt += 1
            elif is_imp:
                fav_trt += 1
            else:
                em_aberto_trt += 1

        # Classificação nos pesos da fórmula de Tefetiva
        if in_tst:
            if is_cond:
                c_def += 1.0  # TST condenado: peso 1.00
            elif is_acordo:
                total_a += 1.0  # Acordo: peso 0.55
            elif is_imp:
                total_i += 1.0  # Favorável/revertido: peso 0.00
        else:
            if in_exec and is_cond:
                c_def += 1.0  # Execução TRT com condenação: peso 1.00
            elif in_exec and is_open:
                # Execução sem status explicitado: sentença já aplicada (peso 1.00)
                c_def += 1.0
            elif is_cond:
                c_rec += 1.0  # TRT recorrível: peso 0.85
            elif is_acordo:
                total_a += 1.0  # Acordo: peso 0.55
            elif is_imp:
                total_i += 1.0  # Improcedente: peso 0.00

    # Totais consolidados TRT
    julgados_trt = cond_trt + acordos_trt + fav_trt
    taxa_cond_trt = round((cond_trt / julgados_trt) * 100.0, 1) if julgados_trt > 0 else 0.0

    # Totais consolidados TST
    finalizados_tst = cond_tst + fav_tst
    taxa_cond_tst = round((cond_tst / finalizados_tst) * 100.0, 1) if finalizados_tst > 0 else 0.0
    taxa_rev_tst = round((fav_tst / finalizados_tst) * 100.0, 1) if finalizados_tst > 0 else 0.0
    amostra_reduzida_tst = finalizados_tst < 10

    # Cálculo da Tefetiva
    denominador = c_def + c_rec + total_a + total_i
    if denominador > 0 and not previsao_bloqueada:
        t_efetiva = (
            (1.00 * c_def) + (0.85 * c_rec) + (0.55 * total_a)
        ) / denominador * 100.0
    else:
        t_efetiva = 0.0

    # Aplicação do Agravante por Reincidência no TST (Proteção Amostral)
    agravante_tst_aplicado = False
    if not previsao_bloqueada and finalizados_tst >= 10:
        padrao_desfavoravel = (taxa_cond_tst > taxa_cond_trt) or (sub_count > 0)
        if padrao_desfavoravel:
            t_final = min(100.0, t_efetiva * 1.10)
            agravante_tst_aplicado = True
        else:
            t_final = t_efetiva
    else:
        t_final = t_efetiva

    percentual_risco = round(t_final, 1)

    if percentual_risco <= 30.0:
        faixa_exposicao = "BAIXA"
        rotulo_exposicao = "Baixa Exposição Trabalhista"
    elif percentual_risco < 60.0:
        faixa_exposicao = "MODERADA"
        rotulo_exposicao = "Exposição Moderada — Blindagem Contratual Recomendada"
    else:
        faixa_exposicao = "ALTA"
        rotulo_exposicao = "Alta Exposição — Provisionamento e Diligência Rigorosa"

    pct_base_exec = round((total_execucao / total_procs) * 100.0, 1) if total_procs > 0 else 0.0

    # Amostra formatada de processos
    processos_amostra = []
    for p in processes:
        in_tst = is_tst_process(p)
        fin_tst = in_tst and is_process_finalized_tst(p)
        in_exec = is_process_execution_phase(p)

        if in_tst:
            fase_proc = "INSTANCIA_MAXIMA"
        elif in_exec:
            fase_proc = "EXECUCAO"
        elif p.get("is_recurso") or "RECURS" in str(p.get("fase_processual") or "").upper():
            fase_proc = "RECURSAL"
        else:
            fase_proc = "CONHECIMENTO"

        tag_inst = (
            "Não cabem mais recursos (Instância Máxima — TST)"
            if fin_tst
            else None
        )

        num_cnj = str(
            p.get("numero_cnj") or p.get("numero_processo") or p.get("numeroProcesso") or ""
        ).strip()
        trib_str = str(
            p.get("tribunal") or p.get("siglaTribunal") or ("TST" if in_tst else "TRT")
        ).strip()

        # Enriquecimento in-place
        p["instancia_maxima_finalizado"] = bool(fin_tst)
        p["tag_instancia"] = tag_inst
        p["fase_processual"] = fase_proc
        p["numero_cnj"] = num_cnj

        processos_amostra.append({
            "numero_cnj": num_cnj,
            "tribunal": trib_str,
            "fase_processual": fase_proc,
            "instancia_maxima_finalizado": bool(fin_tst),
            "tag_instancia": tag_inst,
        })

    return {
        "risco_subsidiario": {
            "percentual": percentual_risco,
            "faixa_exposicao": faixa_exposicao,
            "rotulo_exposicao": rotulo_exposicao,
            "metodologia_resumo": "Ponderado por definitividade: TST/Execução peso 1.0, TRT recorrível 0.85, Acordo 0.55",
            "agravante_tst_aplicado": agravante_tst_aplicado,
        },
        "fase_execucao": {
            "total_processos": total_execucao,
            "percentual_base": pct_base_exec,
            "rotulo": "Processos em Fase de Execução / Sentença Aplicada",
            "descricao": "Processos já concluídos na fase de conhecimento e em etapa de cumprimento de sentença.",
        },
        "comparativo_instancias": {
            "trt": {
                "total_processos": len(trt_procs),
                "julgados": julgados_trt,
                "condenacoes": cond_trt,
                "acordos": acordos_trt,
                "favoraveis": fav_trt,
                "em_aberto": em_aberto_trt,
                "taxa_condenacao": taxa_cond_trt,
            },
            "tst": {
                "total_processos": len(tst_procs),
                "finalizados": finalizados_tst,
                "condenacoes_mantidas": cond_tst,
                "favoraveis_ou_revertidos": fav_tst,
                "taxa_condenacao": taxa_cond_tst,
                "taxa_reversao_recurso": taxa_rev_tst,
                "amostra_reduzida": amostra_reduzida_tst,
            },
        },
        "processos_amostra": processos_amostra,
    }


class BayesianRiskPredictor:
    """
    Motor Bayesiano Empírico (Dirichlet-Multinomial / Beta-Binomial) de Previsão de Risco:
    1. Separa os 3 desfechos reais (Condenação, Acordo, Improcedência) via conjugação Dirichlet-Multinomial.
    2. Conserva estritamente a massa amostral: cada processo julgado soma exatamente 1.0 nos contadores posteriores.
    3. Calcula o Intervalo de Credibilidade de 90% (Beta PPF 5% e 95%) com desconto monotônico de censura à direita.
    4. Aplica o efeito da proporção de laudos periciais e do tribunal via softmax tilting em escala logit.
    5. Estima o prior hierárquico Leave-One-Subject-Out (Empirical Bayes shrinkage) sem dupla contagem dos dados.
    6. Calcula o Score de Risco Súmula 331 TST em fórmula única convexa (65% adverso + 20% rescisórias + 15% Súmula 331/Execução).
    """

    def __init__(self, alpha_prior: float = BAYESIAN_ALPHA_PRIOR):
        self.alpha_prior = float(alpha_prior)

    def get_subject_baseline_prior(
        self,
        subject_name: str,
        subject_code: Any = None,
        tribunal: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retorna os parâmetros basais (pbase_cond, pbase_acordo, pbase_imp, logit_laudo_beta)
        para o assunto TPU e ajusta pelo modificador regional do Tribunal (TRT) via softmax tilting.
        """
        code_int = safe_int(subject_code)
        base_entry = None
        if code_int is not None and code_int in TPU_SUBJECT_BASELINES:
            base_entry = dict(TPU_SUBJECT_BASELINES[code_int])
        else:
            norm_name = normalize_subject_text(subject_name)
            for keywords, cfg in SUBJECT_KEYWORD_BASELINES:
                if any(k in norm_name for k in keywords):
                    base_entry = dict(cfg)
                    break

        if base_entry is None:
            base_entry = {
                "pbase": DEFAULT_TRT_BASE_PROBABILITY,
                "pbase_acordo": DEFAULT_TRT_ACORDO_PROBABILITY,
                "pbase_imp": DEFAULT_TRT_IMP_PROBABILITY,
                "logit_laudo_beta": 0.10,
                "sumula_331_core": False,
            }

        p_cond = float(base_entry["pbase"])
        p_acordo = float(base_entry.get("pbase_acordo", DEFAULT_TRT_ACORDO_PROBABILITY))
        p_imp = float(base_entry.get("pbase_imp", max(0.05, 1.0 - p_cond - p_acordo)))

        mod = 0.0
        if tribunal:
            norm_trib = normalize_subject_text(tribunal).replace(" ", "")
            mod = TRT_REGIONAL_LOGIT_MODIFIERS.get(norm_trib, 0.0)

        p_cond, p_acordo, p_imp = _tilt_simplex(p_cond, p_acordo, p_imp, mod)

        return {
            "pbase_cond": p_cond,
            "pbase_acordo": p_acordo,
            "pbase_imp": p_imp,
            "logit_laudo_beta": float(base_entry.get("logit_laudo_beta", 0.10)),
            "sumula_331_core": bool(base_entry.get("sumula_331_core", False)),
        }

    def get_subject_baseline_probability(
        self,
        subject_name: str,
        subject_code: Any = None,
        tribunal: Optional[str] = None,
    ) -> float:
        """Compatibilidade direta com chamadas que esperam apenas pbase_cond."""
        prior = self.get_subject_baseline_prior(subject_name, subject_code, tribunal)
        return round(prior["pbase_cond"], 4)

    def calculate_prediction_for_subject(
        self,
        subject_name: str,
        subject_code: Any,
        cond: float,
        acordo: float,
        imp: float,
        open_count: int,
        laudo_ratio: float = 0.0,
        has_expert_report: bool = False,
        tribunal: Optional[str] = None,
        empirical_company_prior: Optional[Dict[str, float]] = None,
        secrecy_rate: float = 0.0,
        meta_completeness: float = 1.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Modelo Conjugado Dirichlet-Multinomial com Shrinkage Empirical Bayes e Intervalo de Credibilidade Beta (90%):
        - Separa Condenação (theta_cond), Acordo (theta_acordo) e Vitória/Improcedência (theta_imp).
        - Aplica ajuste de proporção de laudos periciais via softmax tilting em escala logit.
        - Calcula o IC 90% [lo, hi] via distribuição Beta marginal com desconto monotônico de censura.
        """
        prior_info = self.get_subject_baseline_prior(subject_name, subject_code, tribunal)
        p0_cond = prior_info["pbase_cond"]
        p0_acordo = prior_info["pbase_acordo"]
        p0_imp = prior_info["pbase_imp"]

        # Empirical Bayes (Leave-One-Subject-Out): combina o prior do assunto/TRT com a distribuição
        # empírica dos DEMAIS assuntos da empresa (evitando dupla contagem dos dados deste assunto)
        if empirical_company_prior and empirical_company_prior.get("weight", 0.0) > 0.0:
            w = empirical_company_prior["weight"]
            p0_cond = (1.0 - w) * p0_cond + w * empirical_company_prior["p_cond"]
            p0_acordo = (1.0 - w) * p0_acordo + w * empirical_company_prior["p_acordo"]
            p0_imp = (1.0 - w) * p0_imp + w * empirical_company_prior["p_imp"]

        # Efeito da proporção de laudos periciais em escala logit (softmax tilting)
        effective_laudo_ratio = float(laudo_ratio) if laudo_ratio > 0 else (1.0 if has_expert_report else 0.0)
        beta_laudo = prior_info["logit_laudo_beta"]
        if effective_laudo_ratio > 0.0 and beta_laudo != 0.0:
            p0_cond, p0_acordo, p0_imp = _tilt_simplex(
                p0_cond, p0_acordo, p0_imp, beta_laudo * effective_laudo_ratio
            )

        julgados = float(cond) + float(acordo) + float(imp)
        total_motivo = julgados + float(open_count)
        censoring_rate = (float(open_count) / total_motivo) if total_motivo > 0 else 0.0

        # Parâmetros posteriores da Dirichlet(alpha_cond, alpha_acordo, alpha_imp)
        alpha_cond = self.alpha_prior * p0_cond + float(cond)
        alpha_acordo = self.alpha_prior * p0_acordo + float(acordo)
        alpha_imp = self.alpha_prior * p0_imp + float(imp)
        alpha_sum = alpha_cond + alpha_acordo + alpha_imp

        theta_cond = alpha_cond / alpha_sum
        theta_acordo = alpha_acordo / alpha_sum
        theta_imp = alpha_imp / alpha_sum

        prob_cond_pct, prob_acordo_pct, prob_vencer_pct = _round_simplex_pct(
            theta_cond, theta_acordo, theta_imp
        )

        # Desconto monotônico de censura à direita:
        # Apenas o tamanho amostral observado (julgados) sofre desconto de até 25% conforme a fração de abertos,
        # garantindo que (a_eff + b_eff) = alpha_prior + n_eff >= alpha_prior seja estritamente crescente em julgados.
        n_eff = julgados * (1.0 - 0.25 * censoring_rate)
        concentration_eff = self.alpha_prior + n_eff
        a_eff = max(0.25, theta_cond * concentration_eff)
        b_eff = max(0.25, (1.0 - theta_cond) * concentration_eff)

        lo_90 = beta_ppf(0.05, a_eff, b_eff)
        hi_90 = beta_ppf(0.95, a_eff, b_eff)
        ic_width_pp = round((hi_90 - lo_90) * 100.0, 1)

        # Cálculo da confiança estatística calibrada conforme a especificação técnica:
        # Se a empresa tem 18 processos já julgados com sigilo 0 e metadados íntegros, a confiança atinge ~85% a 94%.
        # Se for um motivo novo com apenas 1 processo anterior julgado, o sistema alerta confiança baixa (~48% a 54%).
        n = julgados
        term_volume = 1.0 - (1.0 / math.sqrt(1.0 + 1.45 * n)) if n > 0 else 0.0
        term_meta = max(0.0, min(1.0, float(meta_completeness)))
        term_visibility = 1.0 - min(1.0, max(0.0, float(secrecy_rate)))

        conf_factor = (term_volume * 0.72) + (term_meta * 0.16) + (term_visibility * 0.12)
        confidence_pct = round(min(98.0, max(15.0, conf_factor * 100.0)), 1)

        if confidence_pct >= 85.0:
            confidence_level = "Alta"
        elif confidence_pct >= 60.0:
            confidence_level = "Média"
        else:
            confidence_level = "Baixa (Amostra Inicial)"

        # Diagnóstico baseado na distribuição tripartida (Vitória vs Acordo vs Condenação)
        if julgados == 0:
            diagnostico_vitoria = "Prior Basal do TRT (Sem julgados anteriores na empresa)"
        elif prob_vencer_pct >= 50.0:
            diagnostico_vitoria = "Alta probabilidade de improcedência (êxito)"
        elif prob_cond_pct >= 55.0:
            diagnostico_vitoria = "Risco elevado de condenação"
        elif prob_acordo_pct >= 40.0:
            diagnostico_vitoria = "Forte tendência conciliatória (acordo)"
        else:
            diagnostico_vitoria = "Equilíbrio / Risco moderado"

        expected_open_cond = round(open_count * theta_cond, 2)
        expected_open_acordos = round(open_count * theta_acordo, 2)

        return {
            "motivo": subject_name,
            "codigo_tpu": safe_int(subject_code),
            "processos_abertos": int(open_count),
            "processos_julgados": int(round(julgados)),
            "probabilidade_condenacao_pct": prob_cond_pct,
            "probabilidade_acordo_pct": prob_acordo_pct,
            "probabilidade_vencer_pct": prob_vencer_pct,
            "ic_90_inferior_pct": round(lo_90 * 100.0, 1),
            "ic_90_superior_pct": round(hi_90 * 100.0, 1),
            "largura_ic_90_pp": ic_width_pp,
            "condenacoes_esperadas_abertos": expected_open_cond,
            "acordos_esperados_abertos": expected_open_acordos,
            "taxa_censura_abertos_pct": round(censoring_rate * 100.0, 1),
            "proporcao_laudo_pericial_pct": round(effective_laudo_ratio * 100.0, 1),
            "diagnostico_chance_vitoria": diagnostico_vitoria,
            "confianca_previsao_pct": confidence_pct,
            "nivel_confianca": confidence_level,
            "possui_laudo_pericial": effective_laudo_ratio > 0.0,
            "pbase_tribunal_pct": round(prior_info["pbase_cond"] * 100.0, 1),
        }

    def evaluate_temporal_backtest(
        self,
        processes: List[Dict[str, Any]],
        split_ratio: float = 0.7,
    ) -> Optional[Dict[str, Any]]:
        """
        Executa backtest temporal (out-of-time validation) sobre os processos julgados:
        - Ordena processos julgados por data (dataHoraUltimaAtualizacao / data_publicacao).
        - Estima as probabilidades posteriores no conjunto de treino (até t_split).
        - Avalia o Brier Score multiclasse (Condenação, Acordo, Improcedência) e binário no conjunto de teste.
        """
        judged = [
            p for p in processes
            if p.get("sentence_outcome") in ("condenacoes", "acordos", "improcedentes")
        ]
        if len(judged) < 6:
            return None

        judged_sorted = sorted(
            judged,
            key=_extract_process_date,
        )
        split_idx = max(3, int(len(judged_sorted) * split_ratio))
        if split_idx >= len(judged_sorted):
            return None

        train_procs = judged_sorted[:split_idx]
        test_procs = judged_sorted[split_idx:]

        train_metrics = self.generate_dashboard_metrics("Backtest_Train", train_procs, _run_backtest=False)
        preds_by_subject = {
            normalize_subject_text(item["motivo"]): item
            for item in train_metrics.get("plus_previsao_condenacao", [])
        }

        brier_binary_sum = 0.0
        brier_multiclass_sum = 0.0
        for p in test_procs:
            sub = p.get("primary_subject") or (p.get("subjects") or ["Outros"])[0]
            norm_sub = normalize_subject_text(sub)
            pred = preds_by_subject.get(norm_sub)
            if pred:
                p_cond = pred["probabilidade_condenacao_pct"] / 100.0
                p_acordo = pred["probabilidade_acordo_pct"] / 100.0
                p_imp = pred["probabilidade_vencer_pct"] / 100.0
            else:
                prior = self.get_subject_baseline_prior(sub, p.get("primary_subject_code"), p.get("tribunal"))
                p_cond, p_acordo, p_imp = prior["pbase_cond"], prior["pbase_acordo"], prior["pbase_imp"]

            outcome = p.get("sentence_outcome")
            y_cond = 1.0 if outcome == "condenacoes" else 0.0
            y_acordo = 1.0 if outcome == "acordos" else 0.0
            y_imp = 1.0 if outcome == "improcedentes" else 0.0

            brier_binary_sum += (p_cond - y_cond) ** 2
            brier_multiclass_sum += ((p_cond - y_cond) ** 2 + (p_acordo - y_acordo) ** 2 + (p_imp - y_imp) ** 2) / 3.0

        n_test = len(test_procs)
        brier_score = round(brier_binary_sum / n_test, 4)
        brier_multi = round(brier_multiclass_sum / n_test, 4)
        return {
            "amostra_treino": len(train_procs),
            "amostra_teste": n_test,
            "brier_score_condenacao": brier_score,
            "brier_score_multiclasse": brier_multi,
            "calibracao": "Adequada" if brier_score < 0.25 else "Requer mais dados",
        }

    def generate_dashboard_metrics(
        self,
        company_name: str,
        processes: List[Dict[str, Any]],
        _run_backtest: bool = True,
    ) -> Dict[str, Any]:
        """
        Gera o conjunto completo de métricas executivas com consistência matemática:
        - Conservação estrita de massa amostral por motivo (julgados = cond + acordo + imp = inteiro exato).
        - Em processos de procedência parcial com múltiplos assuntos (k > 1), reparte (0.85 cond, 0.15 imp)
          para preservar a soma 1.0 por processo julgado sem perder massa amostral.
        - Shrinkage Empirical Bayes Leave-One-Subject-Out (sem dupla contagem).
        - Score de Risco de Terceirização (Súmula 331 TST) em fórmula única convexa (65% adverso + 20% rescisórias + 15% Súmula 331/Execução).
        """
        total = len(processes)
        outcomes = {
            "condenacoes": 0,
            "acordos": 0,
            "improcedentes": 0,
            "em_aberto": 0,
        }
        detailed_outcomes = {
            "procedencia_total": 0,
            "procedencia_parcial": 0,
            "acordos": 0,
            "improcedentes": 0,
            "extintos_sem_merito": 0,
            "em_aberto": 0,
        }

        if total == 0:
            return {
                "empresa": company_name,
                "total_processos": 0,
                "ranking_motivos": [],
                "ranking_motivos_condenacao": [],
                "condenacao_por_motivo": [],
                "execution_phase_summary": empty_execution_phase_summary(),
                "desfechos_sentencas": outcomes,
                "desfechos_detalhados": detailed_outcomes,
                "niveis_sigilo": {},
                "taxa_sigilo_pct": 0.0,
                "score_risco_terceirizacao": 0.0,
                "probabilidade_condenacao_geral_pct": None,
                "taxa_historica_condenacao_pct": None,
                "plus_previsao_condenacao": [],
                "alerta_sumula_331_tst": False,
            }

        reasons_counter: Counter = Counter()
        canonical_display_name: Dict[str, str] = {}
        execution_only_count = 0
        execution_labels_counter: Counter = Counter()
        exec_display_names: Dict[str, str] = {}
        secrecy_levels: Counter = Counter()
        tribunal_counter: Counter = Counter()

        history_by_reason: Dict[str, Dict[str, Any]] = defaultdict(_build_default_reason_stat)

        sumula_331_direct_count = 0
        rescisorias_fgts_count = 0

        for p in processes:
            raw_sigilo = p.get("nivel_sigilo") if p.get("nivel_sigilo") is not None else p.get("nivelSigilo")
            sigilo = safe_int(raw_sigilo, default=0) or 0
            secrecy_levels[sigilo] += 1

            trib = str(p.get("tribunal") or "").strip().lower()
            if trib:
                tribunal_counter[trib] += 1

            raw_status = str(p.get("sentence_outcome") or "em_aberto").strip().lower()
            status_desfecho = str(p.get("statusDesfecho") or "").strip().upper()
            sub_outcome = str(p.get("sentence_sub_outcome") or "").strip().lower()

            if raw_status == "condenacoes" or status_desfecho == "CONDENACAO":
                outcomes["condenacoes"] += 1
                if sub_outcome == "procedencia_parcial":
                    detailed_outcomes["procedencia_parcial"] += 1
                else:
                    detailed_outcomes["procedencia_total"] += 1
                eff_status = "condenacoes"
            elif raw_status == "acordos" or status_desfecho == "ACORDO":
                outcomes["acordos"] += 1
                detailed_outcomes["acordos"] += 1
                eff_status = "acordos"
            elif raw_status == "improcedentes" or status_desfecho == "IMPROCEDENTE":
                outcomes["improcedentes"] += 1
                detailed_outcomes["improcedentes"] += 1
                eff_status = "improcedentes"
            elif raw_status in ("extintos_sem_merito", "baixa_arquivado", "arquivado", "desistencia"):
                outcomes["em_aberto"] += 1
                detailed_outcomes["extintos_sem_merito"] += 1
                eff_status = "extintos_sem_merito"
            else:
                outcomes["em_aberto"] += 1
                detailed_outcomes["em_aberto"] += 1
                eff_status = "em_aberto"

            from app.services.cnj_searcher import GENERIC_SUBJECT_TERMS, extract_real_cause_fast

            raw_subjects = p.get("subjects")
            if isinstance(raw_subjects, list) and raw_subjects:
                subj_list = [str(s).strip() for s in raw_subjects if str(s).strip()]
            else:
                primary = str(p.get("primary_subject") or "").strip()
                subj_list = [primary] if primary else []

            # Se todos os assuntos forem genéricos ou 'Outros Motivos Trabalhistas', extrai causas reais do texto
            is_all_generic = not subj_list or all(
                normalize_subject_text(s) in GENERIC_SUBJECT_TERMS or "outros motivos" in normalize_subject_text(s)
                for s in subj_list
            )
            if is_all_generic:
                raw_texts = p.get("publicacoes_texto") or ([p["resumoPublicacao"]] if p.get("resumoPublicacao") else [])
                classes_pje = p.get("classes_pje") or []
                if raw_texts or classes_pje:
                    extracted = extract_real_cause_fast([], raw_texts, classes_pje=classes_pje)
                    if extracted and extracted[0].get("codigo") != 7644:
                        subj_list = [e["nome"] for e in extracted if e.get("nome")]

            if not subj_list:
                subj_list = ["Outros Motivos Trabalhistas"]

            material_subjects, exec_labels_in_proc, is_exec_only = classify_execution_vs_material_labels(
                subj_list, classes_pje=p.get("classes_pje")
            )

            reason_code = safe_int(p.get("primary_subject_code"))
            # Ignora códigos TPU genéricos de raiz (864 = Direito do Trabalho, 899 = Processo do Trabalho, 10000 = Outros)
            # para evitar que múltiplos motivos específicos distintos colapsem sob a chave genérica 'tpu:864'
            if reason_code in (864, 899, 10000):
                reason_code = None

            # BUG 2: Processos em Execução / Cumprimento de Sentença / Liquidação JÁ POSSUEM CONDENAÇÃO!
            if eff_status == "em_aberto":
                if exec_labels_in_proc or any(is_execution_phase_label(s) for s in subj_list):
                    outcomes["em_aberto"] -= 1
                    outcomes["condenacoes"] += 1
                    detailed_outcomes["procedencia_total"] += 1
                    detailed_outcomes["em_aberto"] = max(0, detailed_outcomes["em_aberto"] - 1)
                    eff_status = "condenacoes"

            has_sumula_331_in_proc = bool(exec_labels_in_proc)
            has_rescisoria_in_proc = False

            if is_exec_only:
                execution_only_count += 1
                seen_exec_norm = set()
                for exec_label in exec_labels_in_proc:
                    norm_exec = normalize_subject_text(exec_label)
                    if norm_exec and norm_exec not in seen_exec_norm:
                        seen_exec_norm.add(norm_exec)
                        if norm_exec not in exec_display_names:
                            exec_display_names[norm_exec] = exec_label.strip()
                        execution_labels_counter[norm_exec] += 1
                if has_sumula_331_in_proc:
                    sumula_331_direct_count += 1
                continue

            # Remove duplicatas no mesmo processo preservando a ordem (somente assuntos materiais)
            effective_subjects = material_subjects if material_subjects else subj_list
            unique_subjects = list(dict.fromkeys(effective_subjects))
            is_multi_subject = len(unique_subjects) > 1

            for idx, reason_label in enumerate(unique_subjects):
                code_for_sub = reason_code if idx == 0 else None
                # BUG 4: Agrupa estritamente pelo nome normalizado do assunto para NUNCA duplicar barras na UI
                norm_key = normalize_subject_text(reason_label)
                if norm_key not in canonical_display_name:
                    canonical_display_name[norm_key] = reason_label.strip()

                reasons_counter[norm_key] += 1
                r_stat = history_by_reason[norm_key]
                if code_for_sub is not None and r_stat["code"] is None:
                    r_stat["code"] = code_for_sub
                r_stat["total_count"] += 1
                if trib:
                    r_stat["tribunals"][trib] += 1

                # Cada processo julgado contribui com massa total exatamente 1.0 no assunto:
                # Se for procedência parcial em processo com múltiplos pedidos (k > 1),
                # reparte (0.85 cond, 0.15 imp) para refletir que nem todos os pedidos foram acolhidos,
                # mantendo cond + acordo + imp == 1.0 por processo julgado.
                if eff_status == "condenacoes":
                    if sub_outcome == "procedencia_parcial" and is_multi_subject:
                        r_stat["cond"] += 0.85
                        r_stat["imp"] += 0.15
                    else:
                        r_stat["cond"] += 1.0
                elif eff_status == "acordos":
                    r_stat["acordo"] += 1.0
                elif eff_status == "improcedentes":
                    r_stat["imp"] += 1.0
                elif eff_status == "em_aberto":
                    r_stat["open_count"] += 1

                if p.get("has_expert_report"):
                    r_stat["laudo_count"] += 1

                norm_text = normalize_subject_text(reason_label)
                if any(k in norm_text for k in ("terceiriz", "subsidiar", "331", "execuc", "sisbajud", "penhora", "bloqueio")):
                    has_sumula_331_in_proc = True
                if any(k in norm_text for k in ("rescis", "fgts", "477", "467", "salari", "aviso")):
                    has_rescisoria_in_proc = True

            if has_sumula_331_in_proc:
                sumula_331_direct_count += 1
            if has_rescisoria_in_proc:
                rescisorias_fgts_count += 1

        ranking = [
            {
                "motivo": canonical_display_name[norm_key],
                "codigo_tpu": history_by_reason[norm_key]["code"],
                "quantidade": qtd,
                "porcentagem": round((qtd / total) * 100.0, 1),
            }
            for norm_key, qtd in reasons_counter.most_common()
        ]

        # Ranking específico de motivos onde houve condenação pretérita efetiva
        ranking_condenacoes, vetores_rec = build_conviction_ranking_and_vectors(processes)
        if not ranking:
            ranking = [
                {
                    "motivo": v["motivo"],
                    "quantidade": v["total"],
                    "porcentagem": round((v["total"] / max(total, 1)) * 100.0, 1),
                }
                for v in vetores_rec
            ]

        secret_processes = sum(qtd for lvl, qtd in secrecy_levels.items() if int(lvl) > 0)
        secrecy_rate = round(secret_processes / total, 4)

        julgados_totais = outcomes["condenacoes"] + outcomes["acordos"] + outcomes["improcedentes"]
        total_condenacoes = outcomes["condenacoes"]

        # REGRA CRÍTICA DE PREVISÃO ZERO:
        # Empresas sem histórico de condenação prévia (total_condenacoes == 0) não recebem
        # previsão de condenação, pois não há amparo estatístico nem jurídico para projetar risco
        # condenatório para quem nunca sofreu condenação.
        previsao_bloqueada = (total_condenacoes == 0)
        motivo_bloqueio_previsao = (
            "Empresa sem histórico de condenações prévias nos tribunais consultados. "
            "Previsão de condenação desativada por ausência de passivo condenatório anterior."
            if previsao_bloqueada else None
        )

        predictions = []
        if not previsao_bloqueada:
            # Previsões Bayesianas por motivo com Empirical Bayes Leave-One-Subject-Out
            for norm_key, stats in history_by_reason.items():
                dominant_trib = stats["tribunals"].most_common(1)[0][0] if stats["tribunals"] else None
                laudo_ratio = stats["laudo_count"] / max(stats["total_count"], 1)

                subj_julgados = stats["cond"] + stats["acordo"] + stats["imp"]
                other_julgados = max(0.0, float(julgados_totais) - subj_julgados)
                eb_prior = None
                if other_julgados >= 1.0:
                    other_cond = max(0.0, float(outcomes["condenacoes"]) - stats["cond"])
                    other_acordo = max(0.0, float(outcomes["acordos"]) - stats["acordo"])
                    other_imp = max(0.0, float(outcomes["improcedentes"]) - stats["imp"])
                    other_sum = other_cond + other_acordo + other_imp
                    if other_sum > 0:
                        eb_prior = {
                            "weight": other_sum / (other_sum + 10.0),
                            "p_cond": other_cond / other_sum,
                            "p_acordo": other_acordo / other_sum,
                            "p_imp": other_imp / other_sum,
                        }

                pred = self.calculate_prediction_for_subject(
                    subject_name=canonical_display_name[norm_key],
                    subject_code=stats["code"],
                    cond=stats["cond"],
                    acordo=stats["acordo"],
                    imp=stats["imp"],
                    open_count=stats["open_count"],
                    laudo_ratio=laudo_ratio,
                    has_expert_report=stats["laudo_count"] > 0,
                    tribunal=dominant_trib,
                    empirical_company_prior=eb_prior,
                    secrecy_rate=secrecy_rate,
                )
                predictions.append(pred)

            predictions.sort(key=_sort_predictions_priority, reverse=True)

        # Taxa histórica empírica de condenação (para exibição do histórico factual)
        taxa_condenacao_empirica_pct = (
            round((outcomes["condenacoes"] / julgados_totais) * 100.0, 1)
            if julgados_totais > 0 and not previsao_bloqueada
            else (0.0 if previsao_bloqueada else None)
        )

        # Posterior global de condenação e de desfecho adverso da empresa (com shrinkage Bayesiano)
        if previsao_bloqueada:
            probabilidade_condenacao_geral_pct = 0.0
            posterior_adverse_rate = (0.50 * outcomes["acordos"]) / max(julgados_totais + self.alpha_prior, 1.0)
        else:
            posterior_cond_rate = (
                outcomes["condenacoes"] + self.alpha_prior * DEFAULT_TRT_BASE_PROBABILITY
            ) / (julgados_totais + self.alpha_prior)

            posterior_adverse_rate = (
                outcomes["condenacoes"]
                + 0.50 * outcomes["acordos"]
                + self.alpha_prior * (DEFAULT_TRT_BASE_PROBABILITY + 0.50 * DEFAULT_TRT_ACORDO_PROBABILITY)
            ) / (julgados_totais + self.alpha_prior)

            probabilidade_condenacao_geral_pct = round(posterior_cond_rate * 100.0, 1)

        # Nova Fórmula de Risco Subsidiário e Comparativo por Instância (TST vs TRTs)
        risk_inst_data = calculate_weighted_subsidiary_risk_and_instances(
            processes=processes,
            previsao_bloqueada=previsao_bloqueada,
            sub_count=sumula_331_direct_count,
        )
        risco_subsidiario = risk_inst_data["risco_subsidiario"]
        fase_execucao = risk_inst_data["fase_execucao"]
        comparativo_instancias = risk_inst_data["comparativo_instancias"]
        processos_amostra = risk_inst_data["processos_amostra"]

        # O Risco Subsidiário (Súmula 331 TST) passa a ser estritamente Tefetiva ponderada sem variáveis financeiras
        overall_risk_score = risco_subsidiario["percentual"]
        taxa_rescisorias = rescisorias_fgts_count / total if total > 0 else 0.0

        backtest = self.evaluate_temporal_backtest(processes) if (_run_backtest and not previsao_bloqueada) else None

        # BUG 1: A soma dos Desfechos DEVE ser 100% igual ao Total de Registros Identificados (total == sum(outcomes))
        sum_outcomes = outcomes["condenacoes"] + outcomes["acordos"] + outcomes["improcedentes"] + outcomes["em_aberto"]
        if sum_outcomes != total:
            diff = total - sum_outcomes
            outcomes["em_aberto"] = max(0, outcomes["em_aberto"] + diff)
            detailed_outcomes["em_aberto"] = max(0, detailed_outcomes["em_aberto"] + diff)

        execution_phase_summary = {
            "total_processos": execution_only_count,
            "percentual_total": round((execution_only_count / total) * 100.0, 1) if total > 0 else 0.0,
            "rotulos": [
                exec_display_names[norm_lbl]
                for norm_lbl, _ in execution_labels_counter.most_common()
            ],
            "descricao": EXECUTION_PHASE_DESCRIPTION,
        }

        cpm = build_condenacao_por_motivo(processes)

        return {
            "empresa": company_name,
            "total_processos": total,
            "ranking_motivos": ranking,
            "ranking_motivos_condenacao": ranking_condenacoes,
            "condenacao_por_motivo": cpm,
            "motivos_processo": cpm,
            "execution_phase_summary": execution_phase_summary,
            "fase_execucao": fase_execucao,
            "comparativo_instancias": comparativo_instancias,
            "risco_subsidiario": risco_subsidiario,
            "processos_amostra": processos_amostra,
            "previsao_bloqueada": previsao_bloqueada,
            "motivo_bloqueio_previsao": motivo_bloqueio_previsao,
            "desfechos_sentencas": outcomes,
            "desfechos_detalhados": detailed_outcomes,
            "niveis_sigilo": dict(secrecy_levels),
            "taxa_sigilo_pct": round(secrecy_rate * 100.0, 1),
            "score_risco_terceirizacao": overall_risk_score,
            "overall_risk_score": overall_risk_score,
            "probabilidade_condenacao_geral_pct": probabilidade_condenacao_geral_pct,
            "taxa_historica_condenacao_pct": taxa_condenacao_empirica_pct,
            "plus_previsao_condenacao": predictions,
            "alerta_sumula_331_tst": overall_risk_score >= 60.0 or taxa_rescisorias >= 0.35,
            "backtest_temporal": backtest,
            "modelo_matematico": "Ponderação por Definitividade / Dirichlet-Multinomial Conjugado / Beta-Binomial com IC 90%",
        }

    def build_dashboard_metrics(
        self,
        company_name: str,
        processes: List[Dict[str, Any]],
        capital_info: Optional[Dict[str, Any]] = None,
        scan_status: str = "COMPLETED",
    ) -> Dict[str, Any]:
        """
        Gera métricas completas do dashboard incluindo informações consolidadas de capital social.
        Garante rigor matemático estrito: sum(desfechos) == total_processos.
        """
        metrics = self.generate_dashboard_metrics(company_name, processes)
        metrics["scan_status"] = scan_status
        if capital_info:
            metrics["capital_social_consolidado"] = capital_info.get("capital_ativo_formatado")
            metrics["unidades_consolidadas"] = capital_info.get("unidades_consolidadas", 1)
            metrics["observacao_porte"] = capital_info.get("observacao_porte")
            metrics["capital_social_display"] = capital_info.get("capital_social_display")
            if capital_info.get("sinais_solvencia_publica"):
                metrics["sinais_solvencia_publica"] = capital_info.get("sinais_solvencia_publica")

        sum_outcomes = sum(metrics.get("desfechos_sentencas", {}).values())
        if sum_outcomes < metrics.get("total_processos", 0):
            diff = metrics["total_processos"] - sum_outcomes
            metrics["desfechos_sentencas"]["em_aberto"] = metrics["desfechos_sentencas"].get("em_aberto", 0) + diff

        return metrics


def _parse_capital_social_num(val: Optional[Any]) -> float:
    """Extrai valor numérico de capital social a partir de float, int ou string formatada em BRL."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _format_capital_brl(num: Union[int, float]) -> str:
    """Formata valor numérico no padrão monetário brasileiro R$ 0.000,00."""
    return f"R$ {float(num):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class RankedCompanySummary(TypedDict, total=False):
    posicao_ranking: int
    empresa: str
    cnpj: str
    capital_social: str
    capital_social_num: float
    risco_subsidiario: float
    probabilidade_condenacao: float
    faixa_decisao: str
    recomendacao: str
    ic_90: Dict[str, float]
    ic_90_inferior: float
    ic_90_superior: float
    cenario: str
    cenario_tipo: str
    total_processos: int
    previsao_bloqueada: bool
    cnae_codigo: Optional[str]
    cnae_descricao: Optional[str]


class SectorBenchmarkResponse(TypedDict):
    empresas_ranqueadas: List[RankedCompanySummary]
    resumo_setorial: Dict[str, Any]
    grafico_comparativo: Dict[str, Any]
    parecer_comparativo: Dict[str, Any]


def build_sector_benchmark(
    company_payloads: List[Dict[str, Any]],
    ai_analyzer: Optional[Any] = None,
) -> SectorBenchmarkResponse:
    """
    Motor de Benchmarking Setorial com Ranqueamento Bayesiano, Classificação em 3 Faixas e IC 90%.

    Regras e Invariantes:
    1. Não recalcula nem diverge dos scores individuais de /overview.
    2. Preserva regra de bloqueio para zero condenações (0.0%).
    3. Três faixas de decisão: APROVAR (R_sub <= 10.0%), CAUTELA (10.0% < R_sub < 40.0%), REPROVAR (R_sub >= 40.0%).
    4. Ordenação determinística: menor risco subsidiário, maior capital social, menor probabilidade condenação.
    5. Fator de risco relativo: R_sub_max / max(R_sub_min, 0.01), com tratamento para 0.0%.
    6. Validação de CNAE resiliente com detecção de mesmo segmento econômico.
    7. Contrato com series de gráficos e parecer comparativo executivo (compatível com US-12).
    """
    if not company_payloads:
        return {
            "empresas_ranqueadas": [],
            "resumo_setorial": {
                "total_empresas_comparadas": 0,
                "mesmo_segmento": None,
                "cnae_comum": None,
                "alerta_setorial": None,
                "fator_risco_relativo": 1.0,
                "fator_risco_relativo_texto": "Nenhuma empresa avaliada.",
                "melhor_opcao": None,
                "pior_opcao": None,
            },
            "grafico_comparativo": {
                "labels": [],
                "series": {
                    "probabilidade_condenacao_bayes": [],
                    "risco_subsidiario_sumula_331": [],
                },
            },
            "parecer_comparativo": {
                "recomendacao_compra": "",
                "primeiro_lugar": None,
                "alerta_critico": None,
                "economia_potencial_texto": "",
                "justificativa_executiva": "",
            },
        }

    processed_companies = []

    for raw in company_payloads:
        p = normalize_execution_phase_payload(dict(raw)) if isinstance(raw, dict) else {}

        empresa_nome = str(
            p.get("empresa")
            or p.get("company_name")
            or p.get("display_title")
            or p.get("name")
            or "Empresa Avaliada"
        ).strip()

        econ = p.get("idoneidade_economica") if isinstance(p.get("idoneidade_economica"), dict) else {}
        strat = p.get("search_strategy") if isinstance(p.get("search_strategy"), dict) else {}

        # CNPJ
        cnpj_val = str(
            p.get("cnpj")
            or econ.get("cnpj_utilizado")
            or econ.get("cnpj_triangulado")
            or strat.get("target_cnpj")
            or ""
        ).strip()

        # Capital Social
        cap_num = _parse_capital_social_num(
            econ.get("capital_social_num")
            or p.get("capital_social_num")
            or econ.get("capital_social")
            or p.get("capital_social")
        )
        cap_formatted = (
            econ.get("capital_social_formatado")
            or econ.get("capital_social_display")
            or p.get("capital_social_consolidado")
            or p.get("capital_social_display")
            or _format_capital_brl(cap_num)
        )

        # Cenário de Idoneidade Econômica
        cenario_val = str(
            p.get("cenario")
            or p.get("cenario_tipo")
            or econ.get("cenario_tipo")
            or econ.get("cenario")
            or ""
        ).upper()
        if "CENARIO_A" in cenario_val or "CENÁRIO A" in cenario_val:
            cenario_clean = "CENARIO_A"
        elif "CENARIO_B" in cenario_val or "CENÁRIO B" in cenario_val:
            cenario_clean = "CENARIO_B"
        else:
            cenario_clean = None

        # Risco Subsidiário (Súmula 331)
        r_sub_candidate = p.get("risco_subsidiario")
        if isinstance(r_sub_candidate, dict):
            r_sub_candidate = r_sub_candidate.get("percentual")
        r_sub_raw = (
            r_sub_candidate
            if r_sub_candidate is not None
            else (
                p.get("score_risco_terceirizacao")
                if p.get("score_risco_terceirizacao") is not None
                else (
                    econ.get("risco_calote_subsidiario")
                    if econ.get("risco_calote_subsidiario") is not None
                    else (
                        p.get("indice_exposicao_financeira")
                        if p.get("indice_exposicao_financeira") is not None
                        else (
                            econ.get("indice_exposicao_financeira")
                            if econ.get("indice_exposicao_financeira") is not None
                            else p.get("overall_risk_score", 0.0)
                        )
                    )
                )
            )
        )
        try:
            r_sub = round(float(r_sub_raw), 1)
        except (ValueError, TypeError):
            r_sub = 0.0

        if cenario_clean is None:
            cenario_clean = "CENARIO_A" if r_sub < 40.0 else "CENARIO_B"

        # Probabilidade de Condenação (Bayes)
        p_cond_raw = (
            p.get("probabilidade_condenacao")
            if p.get("probabilidade_condenacao") is not None
            else (
                p.get("probabilidade_condenacao_geral_pct")
                if p.get("probabilidade_condenacao_geral_pct") is not None
                else (
                    p.get("taxa_historica_condenacao_pct")
                    if p.get("taxa_historica_condenacao_pct") is not None
                    else 0.0
                )
            )
        )
        try:
            p_cond = round(float(p_cond_raw), 1)
        except (ValueError, TypeError):
            p_cond = 0.0

        # Regra de Bloqueio de 0 condenações
        outcomes = p.get("desfechos_sentencas") or p.get("outcomes_summary") or {}
        condenacoes_count = outcomes.get("condenacoes", 0)
        previsao_bloqueada = bool(
            p.get("previsao_bloqueada")
            or (condenacoes_count == 0 and p.get("total_processos", 0) > 0 and r_sub == 0.0)
            or (r_sub == 0.0 and p_cond == 0.0 and p.get("total_processos", 0) > 0)
        )
        if previsao_bloqueada and r_sub == 0.0:
            p_cond = 0.0

        # IC 90%
        ic_90_data = p.get("ic_90")
        if isinstance(ic_90_data, dict):
            lo = float(ic_90_data.get("inferior", ic_90_data.get("lo", 0.0)))
            hi = float(ic_90_data.get("superior", ic_90_data.get("hi", 0.0)))
        elif isinstance(ic_90_data, (list, tuple)) and len(ic_90_data) >= 2:
            lo = float(ic_90_data[0])
            hi = float(ic_90_data[1])
        elif p.get("ic_90_inferior_pct") is not None and p.get("ic_90_superior_pct") is not None:
            lo = float(p["ic_90_inferior_pct"])
            hi = float(p["ic_90_superior_pct"])
        elif p.get("plus_previsao_condenacao"):
            first_pred = p["plus_previsao_condenacao"][0]
            lo = float(first_pred.get("ic_90_inferior_pct", 0.0))
            hi = float(first_pred.get("ic_90_superior_pct", 0.0))
        elif previsao_bloqueada or r_sub == 0.0:
            lo, hi = 0.0, 0.0
        else:
            lo = max(0.0, round(p_cond - 4.5, 1))
            hi = min(100.0, round(p_cond + 4.5, 1))

        ic_dict = {
            "inferior": round(lo, 1),
            "superior": round(hi, 1),
            "largura_pp": round(hi - lo, 1),
            "formatado": f"{round(lo, 1)}% a {round(hi, 1)}%",
        }

        # Sinais Públicos de Solvência Real e Certidões (US-22)
        sinais = (
            p.get("sinais_solvencia_publica")
            or econ.get("sinais_solvencia_publica")
            or {}
        )
        if not sinais:
            from app.services.solvency_signals_service import solvency_signals_service
            sinais = solvency_signals_service.consolidate_solvency_signals(
                company_name=empresa_nome,
                cnpj=cnpj_val,
                cadastral_data={"situacao_cadastral": p.get("situacao_cadastral") or econ.get("situacao_cadastral")},
                processes=p.get("processos") or p.get("processes") or [],
                capital_social=cap_num,
            )

        possui_restricoes = bool(sinais.get("possui_restricoes"))
        dem_status = sinais.get("demonstracao_contabil_status")
        alerta_homologacao_privada = bool(sinais.get("alerta_homologacao_privada"))

        # Se houver restrições públicas identificadas (PGFN, CGU, Falência ou RFB irregular),
        # impede classificação artificialmente otimista
        if possui_restricoes:
            if cenario_clean is None or cenario_clean == "CENARIO_A":
                cenario_clean = "CENARIO_B"

        # Classificação em 3 faixas de decisão (Súmula 331 TST + Sinais Públicos de Solvência):
        # APROVAR: Cenário A com risco subsidiário baixo a moderado (<= 15.0%) E sem restrições públicas.
        #          Capital Social alto (> 50M) NÃO concede aprovação automática se houver restrições ou se for declaratório sem balanço.
        # CAUTELA: Faixa intermediária OU Cenário A com passivo relevante OU ressalva de balanço privado declaratório.
        # REPROVAR: Cenário B ou risco de insolvência / calote crítico (>= 40%) OU restrições fiscais/judiciais em certidões.
        if possui_restricoes:
            if r_sub >= 25.0 or cenario_clean == "CENARIO_B":
                faixa = "REPROVAR"
                rec = "Reprovar / Restrição identificada em certidões públicas ou passivo crítico associado a risco de calote (Súmula 331 TST)."
            else:
                faixa = "CAUTELA"
                rec = "Contratar com cautela. Restrição identificada em bases públicas: exigir regularização fiscal e retenção cautelar preventiva."
        elif cenario_clean == "CENARIO_A":
            if r_sub <= 15.0:
                faixa = "APROVAR"
                if alerta_homologacao_privada:
                    rec = (
                        "Aprovar fornecedor com baixo risco subsidiário. Capital social declaratório: "
                        "exigir certidões negativas atualizadas e balanço patrimonial recente diretamente do fornecedor no processo de homologação."
                    )
                else:
                    rec = "Aprovar fornecedor. Baixo risco subsidiário e solvência econômico-trabalhista preservada."
            else:
                # Com r_sub > 15.0, apenas S.A. com balanço público auditado na CVM pode mitigar risco via capital social
                if cap_num >= 50_000_000.0 and not alerta_homologacao_privada and dem_status == "Regular":
                    faixa = "APROVAR"
                    rec = "Aprovar fornecedor. Solvência pública comprovada perante a CVM mitigando o risco subsidiário."
                else:
                    faixa = "CAUTELA"
                    rec = "Contratar com cautela. Recomendação de retenção técnica de 10% do valor contratual para contingências."
                    if alerta_homologacao_privada:
                        rec += " Exigir certidões negativas atualizadas e balanço patrimonial recente diretamente do fornecedor no processo de homologação."
        elif r_sub <= 10.0:
            faixa = "APROVAR"
            rec = "Aprovar fornecedor. Baixo risco subsidiário."
            if alerta_homologacao_privada:
                rec += " Exigir certidões negativas atualizadas e balanço patrimonial recente diretamente do fornecedor no processo de homologação."
        elif r_sub < 40.0:
            faixa = "CAUTELA"
            rec = "Contratar com cautela. Recomendação de retenção técnica de 10% do valor contratual para contingências."
            if alerta_homologacao_privada:
                rec += " Exigir certidões negativas atualizadas e balanço patrimonial recente diretamente do fornecedor no processo de homologação."
        else:
            faixa = "REPROVAR"
            rec = "Reprovar / Alto risco subsidiário de calote trabalhista (Súmula 331 TST)."

        # CNAE
        cnae_cod = (
            p.get("cnae_codigo")
            or p.get("cnae_fiscal")
            or econ.get("cnae_codigo")
            or econ.get("cnae_fiscal")
            or econ.get("cnae")
        )
        cnae_desc = (
            p.get("cnae_descricao")
            or p.get("cnae_fiscal_descricao")
            or econ.get("cnae_descricao")
            or econ.get("cnae_fiscal_descricao")
        )

        processed_companies.append({
            "empresa": empresa_nome,
            "cnpj": cnpj_val,
            "capital_social": cap_formatted,
            "capital_social_num": cap_num,
            "risco_subsidiario": r_sub,
            "probabilidade_condenacao": p_cond,
            "ic_90": ic_dict,
            "ic_90_inferior": ic_dict["inferior"],
            "ic_90_superior": ic_dict["superior"],
            "cenario": cenario_clean,
            "cenario_tipo": cenario_clean,
            "faixa_decisao": faixa,
            "recomendacao": rec,
            "sinais_solvencia_publica": sinais,
            "demonstracao_contabil_status": dem_status,
            "possui_restricoes": possui_restricoes,
            "alerta_homologacao_privada": alerta_homologacao_privada,
            "cnae_codigo": str(cnae_cod).strip() if cnae_cod else None,
            "cnae_descricao": str(cnae_desc).strip() if cnae_desc else None,
            "previsao_bloqueada": previsao_bloqueada,
            "total_processos": p.get("total_processos", 0),
            "execution_phase_summary": p.get("execution_phase_summary") or empty_execution_phase_summary(),
            "ranking_motivos": p.get("ranking_motivos", []),
            "condenacao_por_motivo": p.get("condenacao_por_motivo", []),
            "situacao_cadastral": str(econ.get("situacao_cadastral") or p.get("situacao_cadastral") or "ATIVA").upper(),
            "logo": p.get("logo") or econ.get("logo") or {},
            "search_id": p.get("search_id") or p.get("id"),
        })

    # Ordenação determinística (do melhor para o pior fornecedor):
    # 1º critério: menor risco_subsidiario (crescente)
    # 2º critério (desempate): menor presença de restrições (sem restrições primeiro)
    # 3º critério (desempate): maior capital_social (decrescente, portanto -capital_social_num)
    # 4º critério (desempate): menor probabilidade_condenacao (crescente)
    processed_companies.sort(
        key=lambda x: (
            x["risco_subsidiario"],
            1 if x.get("possui_restricoes") else 0,
            -x["capital_social_num"],
            x["probabilidade_condenacao"],
        )
    )

    # Atribuição da posição no ranking
    for idx, comp in enumerate(processed_companies):
        comp["posicao_ranking"] = idx + 1

    # Fator de Risco Relativo
    r_sub_min = processed_companies[0]["risco_subsidiario"]
    r_sub_max = max(c["risco_subsidiario"] for c in processed_companies)

    if r_sub_min == 0.0 and r_sub_max == 0.0:
        fator_risco_relativo = 1.0
        fator_texto = "Todas as empresas avaliadas possuem 0.0% de risco subsidiário (sem passivo condenatório)."
    else:
        denom = max(r_sub_min, 0.01)
        fator_risco_relativo = round(r_sub_max / denom, 2)
        fator_texto = (
            f"A fornecedora de maior risco ({processed_companies[-1]['empresa']}) apresenta exposição "
            f"subsidiária {fator_risco_relativo}x superior à melhor colocada ({processed_companies[0]['empresa']})."
        )

    # Validação Setorial (CNAE)
    cnaes_present = [
        (c["cnae_codigo"], c["cnae_descricao"])
        for c in processed_companies
        if c.get("cnae_codigo")
    ]

    if not cnaes_present:
        mesmo_segmento = None
        cnae_comum = None
        alerta_setorial = None
    else:
        unique_clean_codes = {
            re.sub(r"\D", "", cod) for cod, _desc in cnaes_present if cod
        }
        if len(unique_clean_codes) == 1:
            mesmo_segmento = True
            cnae_comum = {
                "codigo": cnaes_present[0][0],
                "descricao": cnaes_present[0][1],
            }
            alerta_setorial = None
        else:
            mesmo_segmento = False
            cnae_comum = None
            alerta_setorial = (
                "Atenção: As empresas comparadas pertencem a segmentos econômicos distintos (CNAEs diferentes). "
                "Recomenda-se ponderar particularidades setoriais ao avaliar o risco trabalhista e subsidiário."
            )

    melhor_opcao = {
        "empresa": processed_companies[0]["empresa"],
        "cnpj": processed_companies[0]["cnpj"],
        "risco_subsidiario": processed_companies[0]["risco_subsidiario"],
        "posicao_ranking": 1,
        "faixa_decisao": processed_companies[0]["faixa_decisao"],
    }
    pior_opcao = {
        "empresa": processed_companies[-1]["empresa"],
        "cnpj": processed_companies[-1]["cnpj"],
        "risco_subsidiario": processed_companies[-1]["risco_subsidiario"],
        "posicao_ranking": len(processed_companies),
        "faixa_decisao": processed_companies[-1]["faixa_decisao"],
    }

    resumo_setorial = {
        "total_empresas_comparadas": len(processed_companies),
        "mesmo_segmento": mesmo_segmento,
        "cnae_comum": cnae_comum,
        "alerta_setorial": alerta_setorial,
        "fator_risco_relativo": fator_risco_relativo,
        "fator_risco_relativo_texto": fator_texto,
        "melhor_opcao": melhor_opcao,
        "pior_opcao": pior_opcao,
    }

    grafico_comparativo = {
        "labels": [c["empresa"] for c in processed_companies],
        "series": {
            "probabilidade_condenacao_bayes": [c["probabilidade_condenacao"] for c in processed_companies],
            "risco_subsidiario_sumula_331": [c["risco_subsidiario"] for c in processed_companies],
        },
    }

    # Parecer Comparativo com IA (US-12) via LegalAIAnalyzer (ADK -> Gemini -> Fallback Heurístico Local)
    parecer_comparativo = None
    if ai_analyzer is None:
        try:
            from app.services.ai_analyzer import LegalAIAnalyzer
            ai_analyzer = LegalAIAnalyzer()
        except Exception:
            ai_analyzer = None

    if ai_analyzer is not None and hasattr(ai_analyzer, "generate_comparative_analysis"):
        try:
            parecer_comparativo = ai_analyzer.generate_comparative_analysis(
                processed_companies,
                cnae_info=cnae_comum,
            )
        except Exception as ai_err:
            logger.warning("Falha ao invocar LegalAIAnalyzer no benchmark setorial: %s", ai_err)
            parecer_comparativo = None

    if not parecer_comparativo:
        diff_pts = round(pior_opcao["risco_subsidiario"] - melhor_opcao["risco_subsidiario"], 1)
        alerta_critico = (
            f"Alerta crítico: {pior_opcao['empresa']} apresenta risco subsidiário de {pior_opcao['risco_subsidiario']}%, "
            f"classificada na faixa de REPROVAÇÃO com alta probabilidade de calote e redirecionamento (Súmula 331 TST)."
            if pior_opcao["risco_subsidiario"] >= 40.0
            else None
        )
        economia_texto = (
            f"A contratação prioritária de {melhor_opcao['empresa']} em relação à última colocada "
            f"mitiga a exposição ao passivo trabalhista em até {diff_pts} pontos percentuais de risco subsidiário."
            if diff_pts > 0
            else "Todas as empresas avaliadas possuem risco subsidiário equivalente."
        )
        justificativa = (
            f"Comparativo técnico entre {len(processed_companies)} fornecedores com base em inferência bayesiana e solvência patrimonial. "
            f"A empresa {melhor_opcao['empresa']} obteve a melhor qualificação ({melhor_opcao['faixa_decisao']}) "
            f"com risco subsidiário de {melhor_opcao['risco_subsidiario']}%. "
        )
        if pior_opcao["faixa_decisao"] == "REPROVAR":
            justificativa += (
                f"Recomenda-se reprovação ou exigência de garantia fidejussória para {pior_opcao['empresa']} "
                f"devido ao elevado risco subsidiário ({pior_opcao['risco_subsidiario']}%)."
            )

        parecer_comparativo = {
            "recomendacao_compra": f"Recomenda-se a contratação prioritária de {melhor_opcao['empresa']}, classificada em 1º lugar.",
            "primeiro_lugar": melhor_opcao["empresa"],
            "alerta_critico": alerta_critico,
            "economia_potencial_texto": economia_texto,
            "justificativa_executiva": justificativa,
        }

    return {
        "empresas_ranqueadas": processed_companies,
        "resumo_setorial": resumo_setorial,
        "grafico_comparativo": grafico_comparativo,
        "parecer_comparativo": parecer_comparativo,
    }
