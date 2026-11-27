// Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados.
// Módulo de Benchmarking Setorial e Comparação de Fornecedores (US-05)

import React, { useState, useEffect } from 'react';
import {
  ArrowLeftRight, Plus, X, AlertCircle, AlertTriangle, CheckCircle2,
  Download, RefreshCw, Building2, TrendingUp, ShieldAlert,
  ArrowRight, Sparkles, FileText, BarChart3, HelpCircle
} from 'lucide-react';
import { CompanyLogo } from './CompanyLogo.jsx';
import {
  separateExecutionPhaseData,
  EXECUTION_PHASE_DESCRIPTION,
} from './businessLabels.js';
import { apiFetch } from './apiClient.js';

/**
 * Utilitários de formatação para apresentação comparativa.
 */
export function cleanDigits(val) {
  return String(val ?? '').replace(/\D/g, '');
}

export function formatCnpj(val) {
  const d = cleanDigits(val);
  if (d.length !== 14) return val || '—';
  return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`;
}

export function formatPercent(val) {
  if (val == null || val === '') return '0,0%';
  const num = Number(val);
  if (Number.isNaN(num)) return '0,0%';
  return `${num.toLocaleString('pt-BR', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

export function formatCurrency(val) {
  if (val == null || val === '') return 'R$ 0,00';
  if (typeof val === 'string' && val.includes('R$')) return val;
  const num = Number(val);
  if (Number.isNaN(num)) return String(val);
  return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(num);
}

export function getCompanyDisplayName(item) {
  if (!item) return 'Empresa sem nome';
  const econ = item.idoneidade_economica || item.economic_standing;
  const raw = item.company_name || item.empresa || item.name || '';
  if ((raw.startsWith('CNPJ:') || /^\d+$/.test(raw.replace(/\D/g, ''))) && econ?.razao_social) {
    return econ.razao_social;
  }
  return raw || econ?.razao_social || 'Empresa sem nome';
}

/**
 * Gráfico SVG Agrupado de Barras (Zero Dependências Externas).
 * Compara Probabilidade de Condenação (Bayes) vs Risco Subsidiário (Súmula 331).
 */
function GroupedBarChart({ grafico }) {
  if (!grafico || !Array.isArray(grafico.labels) || grafico.labels.length === 0) {
    return null;
  }

  const labels = grafico.labels;
  const bayes = grafico.series?.probabilidade_condenacao_bayes || [];
  const sumula = grafico.series?.risco_subsidiario_sumula_331 || [];

  const svgWidth = 720;
  const svgHeight = 260;
  const marginLeft = 60;
  const marginRight = 20;
  const marginTop = 30;
  const marginBottom = 65;

  const chartWidth = svgWidth - marginLeft - marginRight;
  const chartHeight = svgHeight - marginTop - marginBottom;

  const groupWidth = chartWidth / labels.length;
  const barWidth = Math.min(32, Math.max(16, (groupWidth - 28) / 2));

  // Eixo Y fixado em 100% para percentuais probabilísticos
  const maxY = 100;
  const yTicks = [0, 25, 50, 75, 100];

  return (
    <div className="compare-chart-card card">
      <div className="panel-heading">
        <div>
          <h3>Comparativo Gráfico: Probabilidade Bayesiana vs. Risco Subsidiário</h3>
          <p>Visualização lado a lado da chance processual de condenação e do impacto financeiro (Súmula 331 TST).</p>
        </div>
        <div className="compare-legend">
          <span className="legend-item">
            <span className="legend-color" style={{ background: '#245a81' }} />
            Probabilidade Condenação (Bayes)
          </span>
          <span className="legend-item">
            <span className="legend-color" style={{ background: '#b67b23' }} />
            Risco Subsidiário (Súmula 331)
          </span>
        </div>
      </div>

      <div className="chart-svg-container">
        <svg
          viewBox={`0 0 ${svgWidth} ${svgHeight}`}
          className="compare-svg"
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="Gráfico comparativo de barras"
        >
          {/* Linhas de Grade Horizontais */}
          {yTicks.map((tick) => {
            const y = marginTop + chartHeight - (tick / maxY) * chartHeight;
            return (
              <g key={tick} className="grid-line-group">
                <line
                  x1={marginLeft}
                  y1={y}
                  x2={svgWidth - marginRight}
                  y2={y}
                  stroke="#e7ebef"
                  strokeDasharray={tick === 0 ? 'none' : '4,4'}
                  strokeWidth={1}
                />
                <text
                  x={marginLeft - 8}
                  y={y + 4}
                  textAnchor="end"
                  fill="#788697"
                  fontSize="11"
                  fontWeight="500"
                >
                  {tick}%
                </text>
              </g>
            );
          })}

          {/* Barras Agrupadas por Empresa */}
          {labels.map((label, idx) => {
            const valBayes = Number(bayes[idx] || 0);
            const valSumula = Number(sumula[idx] || 0);

            const hBayes = Math.max(2, (valBayes / maxY) * chartHeight);
            const hSumula = Math.max(2, (valSumula / maxY) * chartHeight);

            const groupCenterX = marginLeft + idx * groupWidth + groupWidth / 2;
            const xBayes = groupCenterX - barWidth - 3;
            const xSumula = groupCenterX + 3;

            const yBayes = marginTop + chartHeight - hBayes;
            const ySumula = marginTop + chartHeight - hSumula;

            // Rótulo da empresa encurtado para até 16 caracteres
            const displayLabel = label.length > 16 ? `${label.slice(0, 15)}…` : label;

            return (
              <g key={idx} className="bar-group">
                {/* Barra Bayes */}
                <rect
                  x={xBayes}
                  y={yBayes}
                  width={barWidth}
                  height={hBayes}
                  rx={4}
                  fill="#245a81"
                  className="bar-rect"
                />
                <text
                  x={xBayes + barWidth / 2}
                  y={yBayes - 6}
                  textAnchor="middle"
                  fill="#142335"
                  fontSize="10"
                  fontWeight="700"
                >
                  {valBayes.toFixed(1)}%
                </text>

                {/* Barra Súmula 331 */}
                <rect
                  x={xSumula}
                  y={ySumula}
                  width={barWidth}
                  height={hSumula}
                  rx={4}
                  fill="#b67b23"
                  className="bar-rect"
                />
                <text
                  x={xSumula + barWidth / 2}
                  y={ySumula - 6}
                  textAnchor="middle"
                  fill="#8c6114"
                  fontSize="10"
                  fontWeight="700"
                >
                  {valSumula.toFixed(1)}%
                </text>

                {/* Rótulo inferior da Empresa */}
                <text
                  x={groupCenterX}
                  y={marginTop + chartHeight + 18}
                  textAnchor="middle"
                  fill="#25374a"
                  fontSize="11"
                  fontWeight="600"
                >
                  {displayLabel}
                </text>
                <text
                  x={groupCenterX}
                  y={marginTop + chartHeight + 32}
                  textAnchor="middle"
                  fill="#788697"
                  fontSize="10"
                >
                  {idx + 1}º Lugar
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}

/**
 * Painel de Parecer Comparativo de IA (US-12 / Google ADK).
 */
function AiOpinionPanel({ parecer }) {
  if (!parecer) return null;

  return (
    <div className="card ai-opinion-panel">
      <div className="ai-opinion-header">
        <div className="ai-opinion-title">
          <div className="ai-icon-mark">
            <Sparkles size={20} />
          </div>
          <div>
            <h3>Parecer Comparativo Executivo (IA RadarJus / Google ADK)</h3>
            <p>Síntese preditiva consolidada pelo agente autônomo com guardrail anti-alucinação.</p>
          </div>
        </div>
        <span className="tag green">Recomendação IA</span>
      </div>

      <div className="ai-opinion-grid">
        <div className="ai-metric-box primary">
          <span className="ai-box-label">Fornecedor Recomendado (1º Lugar)</span>
          <strong className="ai-box-value">{parecer.primeiro_lugar || 'Não informado'}</strong>
          <p className="ai-box-desc">{parecer.recomendacao_compra || 'Recomendação técnica prioritária.'}</p>
        </div>

        <div className="ai-metric-box secondary">
          <span className="ai-box-label">Racional & Economia Potencial</span>
          <strong className="ai-box-value text-accent">{parecer.economia_potencial_texto || 'Mitigação de passivo'}</strong>
          <p className="ai-box-desc">Diferencial protetivo de capital e solvência perante os demais concorrentes.</p>
        </div>
      </div>

      {parecer.justificativa_executiva && (
        <div className="ai-justification">
          <b>Justificativa Técnica da Decisão:</b>
          <p>{parecer.justificativa_executiva}</p>
        </div>
      )}

      {parecer.alerta_critico && (
        <div className="ai-critical-alert">
          <AlertTriangle size={18} />
          <div>
            <strong>Alerta de Conformidade Contratual:</strong>
            <p>{parecer.alerta_critico}</p>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Cartão Individual de Fornecedor Ranqueado.
 */
function RankedCompanyCard({ company }) {
  const isAprovar = company.faixa_decisao === 'APROVAR';
  const isCautela = company.faixa_decisao === 'CAUTELA';
  const isReprovar = company.faixa_decisao === 'REPROVAR';

  let borderToneClass = 'border-approve';
  let badgeType = 'green';
  if (isCautela) {
    borderToneClass = 'border-caution';
    badgeType = 'amber';
  } else if (isReprovar) {
    borderToneClass = 'border-reject';
    badgeType = 'red';
  }

  const isAtiva = !company.situacao_cadastral || company.situacao_cadastral === 'ATIVA';
  const isPrevisaoBloqueada = Boolean(company.previsao_bloqueada);
  const { executionPhaseSummary: execPhase } = separateExecutionPhaseData({
    rankingMotivos: company.ranking_motivos,
    rankingCondenacoes: company.ranking_motivos_condenacao,
    condenacaoPorMotivo: company.condenacao_por_motivo,
    executionPhaseSummary: company.execution_phase_summary,
    totalProcessos: company.total_processos,
  });

  return (
    <div className={`card ranked-card ${borderToneClass}`}>
      <div className="ranked-card-header">
        <div className="rank-position-badge">
          {company.posicao_ranking === 1 ? '🥇 1º LUGAR' : `${company.posicao_ranking}º LUGAR`}
        </div>
        <span className={`tag ${badgeType}`}>{company.faixa_decisao || 'AVALIAR'}</span>
      </div>

      <div className="ranked-company-info">
        <div className="ranked-company-header-row">
          <div className="company-logo-wrapper" style={{ width: 44, height: 44 }}>
            <CompanyLogo logo={company.logo} companyName={company.empresa} />
          </div>
          <div>
            <h3>{company.empresa || 'Empresa sem nome'}</h3>
            <div className="company-identifiers">
              <span className="cnpj-badge">{formatCnpj(company.cnpj)}</span>
              <span className={`rfb-status ${isAtiva ? 'status-active' : 'status-inactive'}`}>
                <span className="status-dot" />
                {company.situacao_cadastral || 'ATIVA'}
              </span>
            </div>
          </div>
        </div>
      </div>

      {isCautela && (
        <div className="caution-retention-notice">
          <AlertCircle size={15} />
          <span>Retenção técnica sugerida: <b>10% das faturas mensais</b></span>
        </div>
      )}

      <div className="ranked-stats-grid">
        <div className="stat-subcard">
          <span className="stat-subcard-label">Risco Subsidiário</span>
          <strong className={`stat-value ${isReprovar ? 'red' : isCautela ? 'amber' : 'green'}`}>
            {isPrevisaoBloqueada ? '0,0%' : formatPercent(company.risco_subsidiario)}
          </strong>
          <small>{isPrevisaoBloqueada ? 'Presunção de conformidade' : 'Súmula 331 TST'}</small>
        </div>

        <div className="stat-subcard">
          <span className="stat-subcard-label">Probab. Condenação</span>
          <strong className="stat-value">{formatPercent(company.probabilidade_condenacao)}</strong>
          <small>Modelo Bayesiano puro</small>
        </div>

        <div className="stat-subcard">
          <span className="stat-subcard-label">Capital Social</span>
          <strong className="stat-value">{formatCurrency(company.capital_social || company.capital_social_num)}</strong>
          <small>Lastro patrimonial</small>
        </div>

        <div className="stat-subcard">
          <span className="stat-subcard-label">Intervalo Confiança (90%)</span>
          <strong className="stat-value">{company.ic_90?.formatado || '—'}</strong>
          <small>Margem estatística</small>
        </div>
      </div>

      {company.recomendacao && (
        <div className="ranked-card-recommendation">
          <p>{company.recomendacao}</p>
        </div>
      )}

      {execPhase && execPhase.total_processos > 0 && (
        <div className="compare-execution-phase" aria-label="Processos em Fase de Execução / Sentença Aplicada">
          <div className="compare-execution-top">
            <strong>Processos em Fase de Execução / Sentença Aplicada</strong>
            <span className="compare-execution-count">
              {execPhase.total_processos} ({formatPercent(execPhase.percentual_total)})
            </span>
          </div>
          <p>{EXECUTION_PHASE_DESCRIPTION}</p>
          {Array.isArray(execPhase.rotulos) && execPhase.rotulos.length > 0 && (
            <div className="execution-phase-badges">
              {execPhase.rotulos.map((rotulo, idx) => (
                <span key={`${rotulo}-${idx}`} className="tag gray execution-phase-badge">
                  {rotulo}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Componente Principal ComparePage.
 */
export default function ComparePage({ request, history = [], currentCompany, navigate }) {
  // Lista de empresas selecionadas (mínimo 2, máximo 5)
  const [selectedCompanies, setSelectedCompanies] = useState(() => {
    if (currentCompany && (currentCompany.searchId || currentCompany.name)) {
      return [{
        id: currentCompany.searchId,
        search_id: currentCompany.searchId,
        name: currentCompany.name,
        cnpj: currentCompany.cnpj || currentCompany.idoneidadeEconomica?.cnpj_utilizado || ''
      }];
    }
    return [];
  });

  const [inputTerm, setInputTerm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportMessage, setExportMessage] = useState('');

  // Adiciona empresa digitada pelo usuário
  function handleAddCompany(term) {
    const clean = String(term || inputTerm).trim();
    if (!clean) return;

    if (selectedCompanies.length >= 5) {
      setError('Limite máximo de 5 empresas atingido para comparação simultânea.');
      return;
    }

    const cleanNum = cleanDigits(clean);
    const isAlreadySelected = selectedCompanies.some((c) => {
      if (cleanNum.length === 14 && cleanDigits(c.cnpj) === cleanNum) return true;
      return c.name.toLowerCase() === clean.toLowerCase();
    });

    if (isAlreadySelected) {
      setError('Esta empresa já foi adicionada à lista de comparação.');
      return;
    }

    setSelectedCompanies((prev) => [
      ...prev,
      {
        name: clean,
        cnpj: cleanNum.length === 14 ? cleanNum : ''
      }
    ]);
    setInputTerm('');
    setError('');
  }

  // Adiciona a partir de item do histórico
  function handleAddFromHistory(item) {
    if (selectedCompanies.length >= 5) {
      setError('Limite máximo de 5 empresas atingido.');
      return;
    }

    const companyName = getCompanyDisplayName(item);
    const companyCnpj = item.cnpj || item.idoneidade_economica?.cnpj_utilizado || '';
    const sid = item.id || item.search_id;

    const alreadyAdded = selectedCompanies.some(
      (c) => (sid && c.search_id === sid) || (companyName && c.name.toLowerCase() === companyName.toLowerCase())
    );

    if (alreadyAdded) {
      setError(`A empresa "${companyName}" já está na lista.`);
      return;
    }

    setSelectedCompanies((prev) => [
      ...prev,
      {
        id: sid,
        search_id: sid,
        name: companyName,
        cnpj: companyCnpj,
        logo: item.logo || item.idoneidade_economica?.logo
      }
    ]);
    setError('');
  }

  // Remove empresa da lista
  function handleRemoveCompany(index) {
    setSelectedCompanies((prev) => prev.filter((_, i) => i !== index));
    setError('');
  }

  // Dispara o benchmarking setorial no backend
  async function handleCompare() {
    if (selectedCompanies.length < 2) {
      setError('Selecione pelo menos 2 empresas para realizar o comparativo setorial.');
      return;
    }
    if (selectedCompanies.length > 5) {
      setError('Selecione no máximo 5 empresas para comparar.');
      return;
    }

    setBusy(true);
    setError('');
    setExportMessage('');

    try {
      // Monta os itens para POST /api/v1/compare-companies
      const items = selectedCompanies.map((c) => {
        if (c.search_id) return { search_id: c.search_id };
        if (c.cnpj && cleanDigits(c.cnpj).length === 14) return { cnpj: cleanDigits(c.cnpj) };
        return { company_name: c.name };
      });

      const response = await request('/compare-companies', {
        method: 'POST',
        body: JSON.stringify({ items })
      });

      if (!response || !Array.isArray(response.empresas_ranqueadas)) {
        throw new Error('A API não retornou uma estrutura válida de benchmarking.');
      }

      setResult(response);
    } catch (err) {
      setError(err.message || 'Falha ao processar comparativo setorial.');
    } finally {
      setBusy(false);
    }
  }

  // Exportação em PDF v3 (US-03) com tratamento defensivo
  async function handleExportPdf() {
    if (!result || !result.empresas_ranqueadas) return;

    setExportBusy(true);
    setExportMessage('');

    try {
      // 1. Tenta POST com as empresas já ranqueadas e consolidadas
      let res = await apiFetch('/export-compare-pdf', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/pdf, application/json'
        },
        body: JSON.stringify({ companies: result.empresas_ranqueadas })
      });

      // 2. Fallback para GET via query string caso POST falhe
      if (!res.ok) {
        const searchIds = result.empresas_ranqueadas
          .map((c) => c.search_id || c.id)
          .filter(Boolean);

        const queryParams = searchIds.length === result.empresas_ranqueadas.length
          ? `search_ids=${searchIds.join(',')}`
          : `empresas=${encodeURIComponent(result.empresas_ranqueadas.map((c) => c.empresa).join(','))}`;

        res = await apiFetch(`/export-compare-pdf?${queryParams}`, {
          method: 'GET',
          headers: { Accept: 'application/pdf, application/json' }
        });
      }

      if (!res.ok) {
        // Tenta ler erro JSON retornado ou mensagem padrão
        const errJson = await res.json().catch(() => null);
        throw new Error(errJson?.error || 'Exportação de PDF indisponível no momento.');
      }

      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', 'relatorio-comparativo-radarjus.pdf');
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      setExportMessage('Relatório PDF exportado com sucesso!');
    } catch (err) {
      // Fallback seguro exigido pela regra SDD
      console.warn('Fallback seguro na exportação do PDF comparativo:', err);
      setExportMessage('Exportação de PDF comparativo temporariamente indisponível. Visualize as métricas na tela.');
    } finally {
      setExportBusy(false);
    }
  }

  const hasResult = Boolean(result && Array.isArray(result.empresas_ranqueadas));
  const cnaeComum = result?.resumo_setorial?.cnae_comum;
  const alertaSetorial = result?.resumo_setorial?.alerta_setorial;
  const mesmoSegmento = result?.resumo_setorial?.mesmo_segmento;

  return (
    <div className="compare-page stack">
      {/* Cabeçalho da Página */}
      <div className="section-intro">
        <div>
          <div className="eyebrow">BENCHMARKING SETORIAL & COMPARAÇÃO DE FORNECEDORES</div>
          <h1>Comparar Empresas</h1>
          <p>
            Análise simultânea de risco de transferência de passivo (Súmula 331 TST),
            solvência econômico-patrimonial e predição bayesiana entre 2 e 5 empresas.
          </p>
        </div>

        {hasResult && (
          <div className="compare-header-actions">
            <button
              className="secondary"
              onClick={() => {
                setResult(null);
                setError('');
              }}
              title="Ajustar seleção de empresas"
            >
              <RefreshCw size={15} />
              Ajustar Seleção
            </button>
            <button
              className="primary"
              onClick={handleExportPdf}
              disabled={exportBusy}
              title="Baixar relatório executivo em PDF v3"
            >
              {exportBusy ? (
                <>
                  <RefreshCw size={16} className="spin" />
                  Gerando PDF...
                </>
              ) : (
                <>
                  <Download size={16} />
                  Exportar PDF v3
                </>
              )}
            </button>
          </div>
        )}
      </div>

      {exportMessage && (
        <div className="alert info" role="status">
          <CheckCircle2 size={18} />
          <span>{exportMessage}</span>
          <button className="text-button" onClick={() => setExportMessage('')}>Fechar</button>
        </div>
      )}

      {/* Painel do Seletor Multi-Empresa */}
      <section className="card compare-selector-panel">
        <div className="panel-heading">
          <div>
            <h3>Seleção de Empresas para Benchmarking</h3>
            <p>Selecione de 2 a 5 concorrentes ou fornecedores do histórico ou adicione por Razão Social/CNPJ.</p>
          </div>
          <span className="counter-badge">
            {selectedCompanies.length} de 5 selecionadas
          </span>
        </div>

        {/* Input de Adição Manual */}
        <form
          className="compare-input-row"
          onSubmit={(e) => {
            e.preventDefault();
            handleAddCompany(inputTerm);
          }}
        >
          <div className="search-box compare-input-box">
            <Building2 size={20} />
            <input
              type="text"
              value={inputTerm}
              onChange={(e) => setInputTerm(e.target.value)}
              placeholder="Digite a Razão Social ou CNPJ completo para adicionar..."
              aria-label="Empresa para comparar"
              disabled={selectedCompanies.length >= 5 || busy}
            />
          </div>
          <button
            type="submit"
            className="secondary"
            disabled={!inputTerm.trim() || selectedCompanies.length >= 5 || busy}
          >
            <Plus size={16} />
            Adicionar
          </button>
        </form>

        {/* Chips de Empresas Selecionadas */}
        <div className="selected-chips-container">
          {selectedCompanies.length === 0 ? (
            <div className="empty-selection-note">
              <ArrowLeftRight size={20} />
              <span>Nenhuma empresa selecionada ainda. Adicione acima ou escolha do histórico abaixo.</span>
            </div>
          ) : (
            selectedCompanies.map((comp, idx) => (
              <div key={idx} className="company-selection-chip">
                <span className="chip-index">{idx + 1}</span>
                <div className="chip-details">
                  <strong>{comp.name}</strong>
                  {comp.cnpj && <small>{formatCnpj(comp.cnpj)}</small>}
                </div>
                <button
                  type="button"
                  className="chip-remove"
                  onClick={() => handleRemoveCompany(idx)}
                  aria-label={`Remover ${comp.name}`}
                  disabled={busy}
                >
                  <X size={14} />
                </button>
              </div>
            ))
          )}
        </div>

        {/* Sugestões Rápidas a Partir do Histórico */}
        {Array.isArray(history) && history.length > 0 && selectedCompanies.length < 5 && (
          <div className="history-quick-add">
            <span className="quick-add-label">Adicionar do Histórico Recente:</span>
            <div className="quick-add-chips">
              {history.slice(0, 10).map((item, idx) => {
                const name = getCompanyDisplayName(item);
                const isAdded = selectedCompanies.some(
                  (c) => c.name.toLowerCase() === name.toLowerCase()
                );
                if (isAdded) return null;

                return (
                  <button
                    key={idx}
                    type="button"
                    className="quick-chip-button"
                    onClick={() => handleAddFromHistory(item)}
                    disabled={busy || selectedCompanies.length >= 5}
                  >
                    <Plus size={12} />
                    <span>{name}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Botão de Disparo da Comparação */}
        <div className="compare-action-footer">
          <div className="validation-message">
            {selectedCompanies.length < 2 ? (
              <span className="hint-warning">
                <AlertCircle size={15} />
                Adicione pelo menos 2 empresas para habilitar a comparação.
              </span>
            ) : selectedCompanies.length === 5 ? (
              <span className="hint-info">
                Capacidade máxima atingida (5 empresas).
              </span>
            ) : (
              <span className="hint-success">
                <CheckCircle2 size={15} />
                Pronto para comparar {selectedCompanies.length} empresas.
              </span>
            )}
          </div>

          <button
            type="button"
            className="primary compare-submit-btn"
            onClick={handleCompare}
            disabled={selectedCompanies.length < 2 || selectedCompanies.length > 5 || busy}
          >
            {busy ? (
              <>
                <RefreshCw size={17} className="spin" />
                Processando Benchmarking...
              </>
            ) : (
              <>
                <ArrowLeftRight size={18} />
                Comparar {selectedCompanies.length > 1 ? `${selectedCompanies.length} Fornecedores` : 'Empresas'}
              </>
            )}
          </button>
        </div>

        {error && (
          <div className="alert error" role="alert">
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        )}
      </section>

      {/* Exibição de Loading */}
      {busy && (
        <div className="loading compare-loading" role="status">
          <RefreshCw size={24} className="spin" />
          <div>
            <strong>Calculando benchmarking setorial e solvência comparada...</strong>
            <p>Cruzando dados com o modelo bayesiano puro e gerando parecer executivo.</p>
          </div>
        </div>
      )}

      {/* Resultados do Benchmarking */}
      {hasResult && !busy && (
        <>
          {/* Barra de Contexto Setorial (CNAE / Alerta de Divergência) */}
          <div className="cnae-context-bar card">
            <div className="cnae-bar-content">
              <div className="cnae-badges">
                {cnaeComum && (
                  <span className="tag blue">
                    CNAE Comum: <b>{cnaeComum}</b>
                  </span>
                )}
                {mesmoSegmento ? (
                  <span className="tag green">Mesmo Segmento Econômico</span>
                ) : (
                  <span className="tag amber">Segmentos Distintos</span>
                )}
              </div>
              {result.resumo_setorial?.fator_risco_relativo_texto && (
                <span className="relative-risk-text">
                  <b>Diferencial Competitivo:</b> {result.resumo_setorial.fator_risco_relativo_texto}
                </span>
              )}
            </div>

            {alertaSetorial && (
              <div className="info-banner warning" style={{ marginTop: '12px' }}>
                <AlertTriangle size={18} />
                <div>
                  <b>Atenção para Comparação Intersetorial</b>
                  <p>{alertaSetorial}</p>
                </div>
              </div>
            )}
          </div>

          {/* Painel do Parecer Comparativo IA (US-12) */}
          {result.parecer_comparativo && (
            <AiOpinionPanel parecer={result.parecer_comparativo} />
          )}

          {/* Gráfico Agrupado de Barras */}
          {result.grafico_comparativo && (
            <GroupedBarChart grafico={result.grafico_comparativo} />
          )}

          {/* Grade de Cartões Lado a Lado (Ordenados pelo Ranking) */}
          <div className="ranked-cards-section">
            <div className="section-subtitle-row">
              <h2>Classificação de Fornecedores (Ranking de Menor Risco)</h2>
              <span className="text-muted">Ordenado por menor probabilidade de transferência de passivo</span>
            </div>

            <div className="ranked-cards-grid">
              {result.empresas_ranqueadas.map((company, idx) => (
                <RankedCompanyCard key={idx} company={company} />
              ))}
            </div>
          </div>
        </>
      )}

      {/* Estado Vazio Inicial */}
      {!hasResult && !busy && (
        <div className="compare-empty-state card">
          <div className="empty-state-icon">
            <ArrowLeftRight size={42} />
          </div>
          <h3>Como funciona o Benchmarking Setorial?</h3>
          <p>
            A RadarJus compara até 5 fornecedores simultaneamente, aplicando o mesmo modelo de
            inteligência preditiva, verificação cadastral na Receita Federal e solvência financeira.
          </p>
          <div className="empty-features-list">
            <div className="empty-feature-item">
              <span className="feature-bullet">1</span>
              <span>Ordene fornecedores pelo menor risco subsidiário (Súmula 331 TST).</span>
            </div>
            <div className="empty-feature-item">
              <span className="feature-bullet">2</span>
              <span>Identifique a necessidade de retenção técnica cautelar de faturas (10%).</span>
            </div>
            <div className="empty-feature-item">
              <span className="feature-bullet">3</span>
              <span>Obtenha parecer de IA com indicação objetiva de melhor contratação.</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}