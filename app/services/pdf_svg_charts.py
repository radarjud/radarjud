"""
Gerador de gráficos puramente vetoriais em formato SVG inline para relatórios PDF v3.0.
Desenvolvido sem dependências externas pesadas (sem Matplotlib, sem Pillow, sem Headless Browsers).
Gera strings XML SVG limpas, determinísticas e compatíveis com WeasyPrint e navegadores modernos.
"""

import html
from typing import Any


def _clamp(val: float, min_val: float = 0.0, max_val: float = 100.0) -> float:
    return max(min_val, min(max_val, val))


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        # Se veio na escala 0.0 a 1.0 (ex: 0.88), normaliza para escala percentual (88.0)
        # exceto se explicitamente 0.0
        if 0.0 < f <= 1.0:
            return f * 100.0
        return f
    except (ValueError, TypeError):
        return default


def generate_outcomes_simplex_svg(
    outcomes: dict[str, Any] | None = None,
    width: int = 520,
    height: int = 68,
) -> str:
    """
    Gera a barra simplex de distribuição dos desfechos processuais (soma garantida de 100%).
    Categorias: Condenações (Vermelho), Acordos (Laranja), Improcedentes (Verde), Em Aberto (Cinza).
    """
    data = outcomes or {}
    cond = max(0, int(data.get("condenacoes") or data.get("condenacao") or 0))
    acord = max(0, int(data.get("acordos") or data.get("acordo") or 0))
    improc = max(0, int(data.get("improcedentes") or data.get("improcedente") or 0))
    aberto = max(0, int(data.get("em_aberto") or data.get("aberto") or data.get("outros") or 0))

    total = cond + acord + improc + aberto

    svg_id = f"simplex_{width}x{height}"

    if total == 0:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" class="simplex-chart empty">\n'
            f'  <rect x="0" y="8" width="{width}" height="20" rx="6" fill="#f1f5f9" stroke="#cbd5e1" stroke-width="1"/>\n'
            f'  <text x="{width / 2:.1f}" y="22" text-anchor="middle" fill="#64748b" '
            f'font-family="Inter, -apple-system, sans-serif" font-size="11" font-weight="500">'
            f'Nenhum processo com desfecho mapeado</text>\n'
            f'</svg>'
        )

    # Cálculo do simplex 100%
    p_cond = (cond / total) * 100.0
    p_acord = (acord / total) * 100.0
    p_improc = (improc / total) * 100.0
    p_aberto = (aberto / total) * 100.0

    bar_y = 6
    bar_h = 22
    bar_w = float(width)

    # Larguras proporcionais
    w_cond = (p_cond / 100.0) * bar_w
    w_acord = (p_acord / 100.0) * bar_w
    w_improc = (p_improc / 100.0) * bar_w
    w_aberto = bar_w - (w_cond + w_acord + w_improc)  # Fecha exatamente 100%

    x_acord = w_cond
    x_improc = x_acord + w_acord
    x_aberto = x_improc + w_improc

    # Monta os segmentos da barra empilhada com clip-path para bordas arredondadas perfeitas
    segments: list[str] = []
    if w_cond > 0:
        segments.append(
            f'    <rect x="0" y="{bar_y}" width="{w_cond:.2f}" height="{bar_h}" fill="#dc2626"/>'
        )
    if w_acord > 0:
        segments.append(
            f'    <rect x="{x_acord:.2f}" y="{bar_y}" width="{w_acord:.2f}" height="{bar_h}" fill="#d97706"/>'
        )
    if w_improc > 0:
        segments.append(
            f'    <rect x="{x_improc:.2f}" y="{bar_y}" width="{w_improc:.2f}" height="{bar_h}" fill="#16a34a"/>'
        )
    if w_aberto > 0:
        segments.append(
            f'    <rect x="{x_aberto:.2f}" y="{bar_y}" width="{w_aberto:.2f}" height="{bar_h}" fill="#64748b"/>'
        )

    # Legendas com bolinhas coloridas e percentuais na segunda linha
    legend_y = bar_y + bar_h + 20
    # Distribuição equilibrada em 4 colunas
    col_w = width / 4.0

    items = [
        ("#dc2626", f"Condenações: {cond} ({p_cond:.1f}%)", 0),
        ("#d97706", f"Acordos: {acord} ({p_acord:.1f}%)", 1),
        ("#16a34a", f"Improcedentes: {improc} ({p_improc:.1f}%)", 2),
        ("#64748b", f"Em Aberto: {aberto} ({p_aberto:.1f}%)", 3),
    ]

    legends_svg: list[str] = []
    for color, text, idx in items:
        lx = idx * col_w
        escaped_txt = html.escape(text)
        legends_svg.append(
            f'  <g transform="translate({lx:.1f}, {legend_y:.1f})">\n'
            f'    <circle cx="5" cy="0" r="4.5" fill="{color}"/>\n'
            f'    <text x="14" y="3.5" fill="#334155" font-family="Inter, -apple-system, sans-serif" '
            f'font-size="9.5" font-weight="500">{escaped_txt}</text>\n'
            f'  </g>'
        )

    all_segments = "\n".join(segments)
    all_legends = "\n".join(legends_svg)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" class="simplex-chart">\n'
        f'  <defs>\n'
        f'    <clipPath id="{svg_id}_clip">\n'
        f'      <rect x="0" y="{bar_y}" width="{width}" height="{bar_h}" rx="6" ry="6"/>\n'
        f'    </clipPath>\n'
        f'  </defs>\n'
        f'  <rect x="0" y="{bar_y}" width="{width}" height="{bar_h}" rx="6" fill="#f8fafc"/>\n'
        f'  <g clip-path="url(#{svg_id}_clip)">\n'
        f'{all_segments}\n'
        f'  </g>\n'
        f'{all_legends}\n'
        f'</svg>'
    )


def generate_bayes_vs_sumula331_svg(
    score_bayes: float,
    risco_sumula: float,
    is_blocked: bool = False,
    width: int = 520,
    height: int = 135,
) -> str:
    """
    Gera o gráfico comparativo de Risco Bayesiano Estimado vs. Risco Subsidiário (Súmula 331 TST).
    Suporta a Regra de Bloqueio de Predição do Cenário B (0 condenações => score travado em 0.0%).
    """
    raw_bayes = _to_float(score_bayes)
    val_sumula = _clamp(_to_float(risco_sumula))

    if is_blocked:
        val_bayes = 0.0
    else:
        val_bayes = _clamp(raw_bayes)

    svg_lines: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" class="bayes-comparison-chart">'
        )
    ]

    track_x = 180.0
    track_w = float(width) - track_x - 65.0
    bar_h = 14.0

    # Linha 1: Risco Bayesiano Estimado
    y1 = 20.0
    b_fill = "#16a34a" if val_bayes < 30.0 else ("#d97706" if val_bayes < 60.0 else "#dc2626")
    w_b = (val_bayes / 100.0) * track_w

    svg_lines.append(
        f'  <!-- Linha 1: Risco Bayesiano -->\n'
        f'  <text x="0" y="{y1 + 11}" fill="#1e293b" font-family="Inter, -apple-system, sans-serif" '
        f'font-size="11" font-weight="600">Risco Bayesiano (Histórico):</text>\n'
        f'  <rect x="{track_x}" y="{y1}" width="{track_w}" height="{bar_h}" rx="4" fill="#e2e8f0"/>\n'
    )
    if is_blocked:
        # Padrão ou indicador de bloqueio
        svg_lines.append(
            f'  <rect x="{track_x}" y="{y1}" width="4" height="{bar_h}" rx="2" fill="#94a3b8"/>\n'
            f'  <text x="{track_x + track_w + 10}" y="{y1 + 11}" fill="#94a3b8" '
            f'font-family="Inter, -apple-system, sans-serif" font-size="11" font-weight="700">0.0% (Bloqueado)</text>'
        )
    else:
        if w_b > 0:
            svg_lines.append(
                f'  <rect x="{track_x}" y="{y1}" width="{w_b:.2f}" height="{bar_h}" rx="4" fill="{b_fill}"/>\n'
            )
        svg_lines.append(
            f'  <text x="{track_x + track_w + 10}" y="{y1 + 11}" fill="{b_fill}" '
            f'font-family="Inter, -apple-system, sans-serif" font-size="11" font-weight="700">{val_bayes:.1f}%</text>'
        )

    # Linha 2: Risco Subsidiário (Súmula 331 TST)
    y2 = 50.0
    w_s = (val_sumula / 100.0) * track_w
    s_fill = "#245a81" if val_sumula < 40.0 else ("#d97706" if val_sumula < 70.0 else "#dc2626")

    svg_lines.append(
        f'  <!-- Linha 2: Risco Súmula 331 -->\n'
        f'  <text x="0" y="{y2 + 11}" fill="#1e293b" font-family="Inter, -apple-system, sans-serif" '
        f'font-size="11" font-weight="600">Risco Subsidiário (Súmula 331):</text>\n'
        f'  <rect x="{track_x}" y="{y2}" width="{track_w}" height="{bar_h}" rx="4" fill="#e2e8f0"/>\n'
    )
    if w_s > 0:
        svg_lines.append(
            f'  <rect x="{track_x}" y="{y2}" width="{w_s:.2f}" height="{bar_h}" rx="4" fill="{s_fill}"/>\n'
        )
    svg_lines.append(
        f'  <text x="{track_x + track_w + 10}" y="{y2 + 11}" fill="{s_fill}" '
        f'font-family="Inter, -apple-system, sans-serif" font-size="11" font-weight="700">{val_sumula:.1f}%</text>'
    )

    # Se bloqueado, adiciona alerta do Cenário B (zero condenações)
    if is_blocked:
        box_y = 78.0
        box_h = 44.0
        svg_lines.append(
            f'  <!-- Banner de Alerta Cenário B -->\n'
            f'  <rect x="0" y="{box_y}" width="{width}" height="{box_h}" rx="6" fill="#fffbeb" stroke="#fef3c7" stroke-width="1"/>\n'
            f'  <path d="M12 {box_y + 14} L16 {box_y + 22} L8 {box_y + 22} Z" fill="#d97706"/>\n'
            f'  <text x="24" y="{box_y + 17}" fill="#92400e" font-family="Inter, -apple-system, sans-serif" font-size="10.5" font-weight="700">'
            f'REGRA DE BLOQUEIO DE PREVISÃO ATIVA (CENÁRIO B)'
            f'</text>\n'
            f'  <text x="24" y="{box_y + 32}" fill="#b45309" font-family="Inter, -apple-system, sans-serif" font-size="9.5" font-weight="400">'
            f'0 condenações no histórico. Predição bayesiana travada em 0,0%. Alerta preventivo da Súmula 331 do TST.'
            f'</text>'
        )
    else:
        # Escala de referência (0%, 25%, 50%, 75%, 100%)
        scale_y = 78.0
        ticks = [0, 25, 50, 75, 100]
        svg_lines.append('  <!-- Escala Numérica -->\n  <g opacity="0.6">')
        for t in ticks:
            tx = track_x + (t / 100.0) * track_w
            svg_lines.append(
                f'    <line x1="{tx:.1f}" y1="{scale_y}" x2="{tx:.1f}" y2="{scale_y + 4}" stroke="#94a3b8" stroke-width="1"/>\n'
                f'    <text x="{tx:.1f}" y="{scale_y + 14}" text-anchor="middle" fill="#64748b" '
                f'font-family="Inter, -apple-system, sans-serif" font-size="8.5">{t}%</text>'
            )
        svg_lines.append('  </g>')

    svg_lines.append("</svg>")
    return "\n".join(svg_lines)


def generate_comparative_bars_svg(
    companies: list[dict[str, Any]],
    width: int = 540,
    height: int | None = None,
) -> str:
    """
    Gera gráfico de barras horizontais comparando de 2 a 5 empresas.
    Exibe nome da empresa, barra de risco percentual, volume de processos e passivo estimado.
    """
    if not companies:
        calc_h = height or 80
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {calc_h}" '
            f'width="{width}" height="{calc_h}">\n'
            f'  <text x="{width / 2:.1f}" y="{calc_h / 2:.1f}" text-anchor="middle" fill="#64748b" '
            f'font-family="Inter, -apple-system, sans-serif" font-size="12">Nenhuma empresa para comparação</text>\n'
            f'</svg>'
        )

    # Limita de 2 a 5 empresas
    selected = companies[:5]
    row_height = 42
    header_height = 20
    calc_h = height or (header_height + len(selected) * row_height + 15)

    svg_lines: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {calc_h}" '
            f'width="{width}" height="{calc_h}" class="comparative-bars-chart">'
        )
    ]

    label_w = 160.0
    track_x = label_w + 10.0
    track_w = float(width) - track_x - 90.0
    bar_h = 14.0

    current_y = header_height

    for comp in selected:
        name = str(
            comp.get("nome")
            or comp.get("company_name")
            or comp.get("razao_social")
            or comp.get("empresa")
            or "Empresa"
        )[:22]
        raw_score = comp.get("score") if comp.get("score") is not None else comp.get("riskScore")
        if raw_score is None:
            raw_score = comp.get("score_risco_terceirizacao") or comp.get("exposure_score") or 0.0

        score = _clamp(_to_float(raw_score))
        procs = int(comp.get("processos") or comp.get("total_processos") or comp.get("total") or 0)

        color = "#16a34a" if score < 30.0 else ("#d97706" if score < 60.0 else "#dc2626")
        w_bar = (score / 100.0) * track_w

        escaped_name = html.escape(name)
        bar_y = current_y + 5

        svg_lines.append(
            f'  <!-- Linha: {escaped_name} -->\n'
            f'  <text x="0" y="{bar_y + 11}" fill="#1e293b" font-family="Inter, -apple-system, sans-serif" '
            f'font-size="10.5" font-weight="600">{escaped_name}</text>\n'
            f'  <rect x="{track_x}" y="{bar_y}" width="{track_w}" height="{bar_h}" rx="4" fill="#e2e8f0"/>\n'
        )
        if w_bar > 0:
            svg_lines.append(
                f'  <rect x="{track_x}" y="{bar_y}" width="{w_bar:.2f}" height="{bar_h}" rx="4" fill="{color}"/>\n'
            )

        svg_lines.append(
            f'  <text x="{track_x + track_w + 8}" y="{bar_y + 11}" fill="{color}" '
            f'font-family="Inter, -apple-system, sans-serif" font-size="11" font-weight="700">{score:.1f}%</text>\n'
            f'  <text x="{track_x + track_w + 50}" y="{bar_y + 11}" fill="#64748b" '
            f'font-family="Inter, -apple-system, sans-serif" font-size="9">({procs} proc.)</text>'
        )

        current_y += row_height

    svg_lines.append("</svg>")
    return "\n".join(svg_lines)
