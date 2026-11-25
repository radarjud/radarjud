const rules = [["Modelo Conjugado Dirichlet[-– ]Multinomial", "Estimativa baseada no histórico"], ["Dirichlet[-– ]Multinomial", "Estimativa baseada no histórico"], ["Intervalo de Credibilidade(?: Beta)?(?:\\s*\\(IC 90%\\)|\\s*\\(90%\\)|\\s*(?:de )?90%)?", "Faixa provável de variação (90% de confiança)"], ["IC 90% Beta", "Faixa provável de variação (90% de confiança)"], ["Empirical Bayes", "estimativa baseada no histórico"], ["Bayesian[oa]s?", "baseada no histórico"], ["prior beta", "histórico de referência"], ["shrinkage", "ajuste pela amostra"]];
export function businessText(value) { return typeof value === "string" ? rules.reduce((s,[pattern,replacement])=>s.replace(new RegExp(pattern,"gi"),replacement),value) : value; }
export function subsidiaryRisk(econ) { const v=econ?.risco_calote_subsidiario ?? econ?.risco_calote_subsidiario_pct; return v == null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v); }
export function translateScanStatus(status) {
  const labels = {
    COMPLETED: 'Concluída',
    PARTIAL_FAST_PATH: 'Em andamento',
    SCANNING_BACKGROUND: 'Varredura em segundo plano',
    BROWSER_PARTIAL: 'Parcialmente concluída',
    PROCESSING: 'Processando',
    PENDING: 'Pendente',
    FAILED: 'Falha na varredura',
  };

  return labels[status] || 'Não informado';
}
