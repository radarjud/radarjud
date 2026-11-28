const rules = [["Modelo Conjugado Dirichlet[-– ]Multinomial", "Estimativa baseada no histórico"], ["Dirichlet[-– ]Multinomial", "Estimativa baseada no histórico"], ["Intervalo de Credibilidade(?: Beta)?(?:\\s*\\(IC 90%\\)|\\s*\\(90%\\)|\\s*(?:de )?90%)?", "Faixa provável de variação (90% de confiança)"], ["IC 90% Beta", "Faixa provável de variação (90% de confiança)"], ["Empirical Bayes", "estimativa baseada no histórico"], ["Bayesian[oa]s?", "baseada no histórico"], ["prior beta", "histórico de referência"], ["shrinkage", "ajuste pela amostra"]];
export function businessText(value) { return typeof value === "string" ? rules.reduce((s,[pattern,replacement])=>s.replace(new RegExp(pattern,"gi"),replacement),value) : value; }
export function subsidiaryRisk(econ) { const v=econ?.risco_calote_subsidiario ?? econ?.risco_calote_subsidiario_pct; return v == null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v); }
export function translateScanStatus(status) {
  const labels = {
    COMPLETED: 'Concluída',
    PARTIAL_FAST_PATH: 'Em andamento',
    SCANNING_BACKGROUND: 'Varredura em segundo plano',
    BROWSER_PENDING: 'Aguardando varredura',
    BROWSER_PARTIAL: 'Parcialmente concluída',
    PROCESSING: 'Processando',
    PENDING: 'Pendente',
    FAILED: 'Falha na varredura',
  };

  return labels[status] || 'Não informado';
}

export const EXECUTION_PHASE_DESCRIPTION =
  'Ações já concluídas na fase de conhecimento e em etapa de cumprimento, liquidação ou cobrança de sentença — exibidas separadamente para não distorcer as causas reais de litígio.';

const EXECUTION_PHASE_TERMS = [
  'execuc',
  'cumprimento de senten',
  'cumprimento provisorio',
  'cumprimento definitivo',
  'liquidac',
  'carta precatoria executoria',
  'carta de sentenca',
  'penhora',
  'sisbajud',
  'expropriac',
];

export function normalizeExecutionText(value) {
  if (typeof value !== 'string') return '';
  return value
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .trim()
    .replace(/\s+/g, ' ');
}

/**
 * Identifica rótulos estritamente ligados à fase de execução/cumprimento de sentença.
 * Usado como classificação compartilhada e fallback defensivo para caches L1 legados.
 */
export function isExecutionPhaseLabel(label) {
  const normalized = normalizeExecutionText(label);
  if (!normalized) return false;
  return EXECUTION_PHASE_TERMS.some((term) => normalized.includes(term));
}

function extractLabel(item) {
  if (typeof item === 'string') return item.trim();
  if (item && typeof item === 'object') {
    return String(item.motivo || item.nome || item.assunto || '').trim();
  }
  return '';
}

function extractCount(item) {
  if (!item || typeof item !== 'object') return 0;
  const raw = item.total_processos ?? item.quantidade ?? item.total ?? item.count ?? 0;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

/**
 * Separa itens puramente de execução/cumprimento dos rankings de causas materiais.
 * Quando executionPhaseSummary não vier do backend (ex.: cache L1 antigo), calcula via fallback.
 */
export function separateExecutionPhaseData({
  rankingMotivos = [],
  rankingCondenacoes = [],
  condenacaoPorMotivo = [],
  executionPhaseSummary = null,
  totalProcessos = 0,
} = {}) {
  const safeRanking = Array.isArray(rankingMotivos) ? rankingMotivos : [];
  const safeCond = Array.isArray(rankingCondenacoes) ? rankingCondenacoes : [];
  const safeCpm = Array.isArray(condenacaoPorMotivo) ? condenacaoPorMotivo : [];

  const filteredRanking = safeRanking.filter((item) => !isExecutionPhaseLabel(extractLabel(item)));
  const filteredCond = safeCond.filter((item) => !isExecutionPhaseLabel(extractLabel(item)));
  const filteredCpm = safeCpm.filter((item) => !isExecutionPhaseLabel(extractLabel(item)));

  const totalNum = Number(totalProcessos) || 0;
  const hasExplicitSummary =
    executionPhaseSummary &&
    typeof executionPhaseSummary === 'object' &&
    typeof executionPhaseSummary.total_processos === 'number';

  if (hasExplicitSummary && (executionPhaseSummary.total_processos > 0 || (safeRanking.length === filteredRanking.length && safeCpm.length === filteredCpm.length))) {
    const execCount = Math.max(0, Number(executionPhaseSummary.total_processos) || 0);
    const pct =
      executionPhaseSummary.percentual_total != null
        ? Number(executionPhaseSummary.percentual_total) || 0
        : totalNum > 0
          ? Number(((execCount / totalNum) * 100).toFixed(1))
          : 0;
    return {
      rankingMotivos: filteredRanking,
      rankingCondenacoes: filteredCond,
      condenacaoPorMotivo: filteredCpm,
      executionPhaseSummary: {
        total_processos: execCount,
        percentual_total: pct,
        rotulos: Array.isArray(executionPhaseSummary.rotulos)
          ? executionPhaseSummary.rotulos.filter((r) => typeof r === 'string' && r.trim())
          : [],
        descricao: EXECUTION_PHASE_DESCRIPTION,
      },
    };
  }

  const cpmExec = safeCpm.filter((item) => isExecutionPhaseLabel(extractLabel(item)));
  const rankExec = safeRanking.filter((item) => isExecutionPhaseLabel(extractLabel(item)));
  const condExec = safeCond.filter((item) => isExecutionPhaseLabel(extractLabel(item)));

  const countSource = cpmExec.length ? cpmExec : (rankExec.length ? rankExec : condExec);
  let execCount = countSource.reduce((sum, item) => sum + extractCount(item), 0);
  if (totalNum > 0 && execCount > totalNum) {
    execCount = totalNum;
  }

  const rotulos = [];
  const seen = new Set();
  for (const item of [...cpmExec, ...rankExec, ...condExec]) {
    const lbl = extractLabel(item);
    const norm = normalizeExecutionText(lbl);
    if (lbl && norm && !seen.has(norm)) {
      seen.add(norm);
      rotulos.push(lbl);
    }
  }

  const pct = totalNum > 0 ? Number(((execCount / totalNum) * 100).toFixed(1)) : 0;

  return {
    rankingMotivos: filteredRanking,
    rankingCondenacoes: filteredCond,
    condenacaoPorMotivo: filteredCpm,
    executionPhaseSummary: {
      total_processos: execCount,
      percentual_total: pct,
      rotulos,
      descricao: EXECUTION_PHASE_DESCRIPTION,
    },
  };
}
