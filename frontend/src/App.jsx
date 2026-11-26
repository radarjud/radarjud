// Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
// Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import React, { useEffect, useRef, useState } from 'react';
import {
  businessText,
  subsidiaryRisk,
  translateScanStatus
} from './businessLabels.js';
import './prescriptive.css';
import ProcessFiltersBar from './ProcessFiltersBar.jsx';
import { EMPTY_FILTERS, validateFilters, processQuery, advancedFiltersConfirmed } from './processFilters.js';
import {
  Search, LayoutDashboard, FileText, ChartNoAxesCombined, History,
  Menu, X, ArrowRight, RefreshCw, AlertCircle, AlertTriangle, ShieldCheck, ShieldAlert,
  Info, ChevronRight, Building2, TrendingUp, Filter, CheckCircle2,
  Check, Activity, BarChart3, Radar, Share2, Download, Library, ArrowLeftRight
} from 'lucide-react';
import { companyHash, companyShareUrl, isCompanyPage, parseRoute, validSearchId } from './companyNavigation.js';
import { CompanyLogo } from "./CompanyLogo.jsx";
import { fetchPjePage, runBrowserDeepScan } from './services/pjeBrowserScanner.js';
import CatalogPage, { normalizeCatalog, onlyDigits } from './CatalogPage.jsx';
import ComparePage from './ComparePage.jsx';

// Vite encaminha /api ao Flask. Defina VITE_API_BASE_URL se a API estiver em outro host.
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
const PAGE_LABELS = {
  search: 'Pesquisar empresas', catalog: 'Catálogo de empresas', overview: 'Visão geral', processes: 'Processos',
  predictions: 'Estimativas', history: 'Histórico', compare: 'Comparar empresas',
};
const NAV_ITEMS = [
  { id: 'search', icon: Search }, { id: 'catalog', icon: Library }, { id: 'overview', icon: LayoutDashboard },
  { id: 'processes', icon: FileText }, { id: 'predictions', icon: ChartNoAxesCombined },
  { id: 'compare', icon: ArrowLeftRight },
  { id: 'history', icon: History },
];
const OUTCOME_LABELS = {
  condenacoes: 'Condenações (Impacto Confirmado)',
  acordos: 'Acordos',
  improcedentes: 'Risco Mitigado / Favorável',
  em_aberto: 'Em aberto',
};
const CHART_COLORS = ['#245a81', '#674b3c', '#b79b85', '#8998a7'];
const formatNumber = (value) => new Intl.NumberFormat('pt-BR').format(Number(value) || 0);
const formatMoneyBRL = (value) => value == null ? null : new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(Number(value) || 0);
const formatPercent = (value) => `${Number(value || 0).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`;
const formatProcessNumber = (raw) => {
  if (!raw) return 'Não informado';
  const digits = String(raw).replace(/\D/g, '');
  if (digits.length === 20) {
    return `${digits.slice(0, 7)}-${digits.slice(7, 9)}.${digits.slice(9, 13)}.${digits.slice(13, 14)}.${digits.slice(14, 16)}.${digits.slice(16, 20)}`;
  }
  return String(raw);
};
const formatDate = (value) => {
  if (!value) return 'Não disponível';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Não disponível' : parsed.toLocaleDateString('pt-BR');
};
const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

function normalize(raw) {
  if (!isRecord(raw)) return null;
  const payload = isRecord(raw.data) ? raw.data : raw;
  const riskScore = payload.indice_exposicao_financeira != null
    ? Number(payload.indice_exposicao_financeira)
    : payload.idoneidade_economica?.indice_exposicao_financeira != null
      ? Number(payload.idoneidade_economica.indice_exposicao_financeira)
      : payload.score_risco_terceirizacao != null
        ? Number(payload.score_risco_terceirizacao)
        : payload.overall_risk_score != null
          ? Number(payload.overall_risk_score)
          : payload.probabilidade_condenacao_geral_pct != null
            ? Number(payload.probabilidade_condenacao_geral_pct)
            : null;

  const companyName = payload.empresa || payload.company_name || 'Empresa consultada';
  const total = Number(payload.total_processos ?? payload.total_processes ?? 0);

  // Garante que a Idoneidade Econômica e Solvência estejam preenchidas
  const rawEcon = isRecord(payload.idoneidade_economica) ? payload.idoneidade_economica : null;
  const defaultCapitalVal = (rawEcon && (rawEcon.valor_formatado || rawEcon.capital_social_display?.valor_formatado || rawEcon.capital_social_consolidado_formatado || rawEcon.capital_social_formatado))
    || payload.capital_social_consolidado
    || payload.capital_social_display?.valor_formatado
    || 'R$ 0,00';
  const defaultObservacao = (rawEcon && (rawEcon.observacao_porte || rawEcon.capital_social_display?.observacao_porte))
    || payload.observacao_porte
    || 'Compatível com o porte';

  const econ = rawEcon ? {
    ...rawEcon,
    valor_formatado: defaultCapitalVal,
    observacao_porte: defaultObservacao,
  } : {
    cenario: 'CENARIO_A',
    cenario_tipo: 'CENARIO_A',
    cenario_titulo: 'Cenário A: Idoneidade Econômica Robusta & Alta Solvência (Baixo Risco ao Contratante)',
    cenario_class: 'seguro',
    probabilidade_insolvencia_pct: 3.5,
    risco_calote_subsidiario_pct: 1.2,
    capital_social_formatado: defaultCapitalVal,
    valor_formatado: defaultCapitalVal,
    observacao_porte: defaultObservacao,
    situacao_cadastral: 'ATIVA',
    possui_sancoes_cgu: false,
    possui_divida_ativa: false,
    parecer_sumula_331: 'Idoneidade econômica preservada: a empresa apresenta situação cadastral ativa e ausência de restrições impeditivas na Fazenda Nacional ou CGU, com baixo risco de transferência de ônus ao tomador.',
    logo: isRecord(payload.logo) ? payload.logo : {
      fallback_initials: companyName.replace(/[^A-Za-z0-9]/g, '').slice(0, 3).toUpperCase() || 'EMP',
      logo_url: null
    }
  };

  const logo = isRecord(payload.logo) ? payload.logo : (econ ? econ.logo : null);

  const outcomesObj = isRecord(payload.desfechos_sentencas) ? payload.desfechos_sentencas
    : isRecord(payload.outcomes_summary) ? payload.outcomes_summary : {};
  const numCondenacoes = Number(outcomesObj.condenacoes ?? 0);
  const isPrevisaoBloqueada = Boolean(payload.previsao_bloqueada) || (numCondenacoes === 0 && total > 0);
  const effectiveRiskScore = (total === 0 || isPrevisaoBloqueada) ? 0.0 : riskScore;

  const subCount = Number(payload.condenacoes_subsidiarias_tomadoras_count ?? payload.condenacoes_subsidiarias_count ?? 0);
  const subTomadoras = Array.isArray(payload.tomadoras_subsidiarias_pj) ? payload.tomadoras_subsidiarias_pj : [];
  const subMsg = payload.mensagem_responsabilidade_subsidiaria || (
    subCount > 0
      ? `Histórico de Responsabilidade Subsidiária: ${subCount} processos com condenação subsidiária atribuída a contratantes anteriores`
      : 'Nenhuma condenação subsidiária de contratante identificada na amostra'
  );

  const cpObj = isRecord(payload.contratos_poder_publico) ? payload.contratos_poder_publico : {};
  const cpStatus = String(cpObj.status || 'DISPONIVEL').toUpperCase();
  const cpTotal = typeof cpObj.total_contratos === 'number' ? cpObj.total_contratos : 0;
  const cpEsferas = Array.isArray(cpObj.esferas) ? cpObj.esferas : [];
  const cpOrgaos = Array.isArray(cpObj.orgaos_contratantes) ? cpObj.orgaos_contratantes : [];
  const cpValorTotal = typeof cpObj.valor_global_total === 'number' ? cpObj.valor_global_total : null;
  const cpValorFormatado = cpObj.valor_global_formatado || (cpValorTotal != null ? formatMoneyBRL(cpValorTotal) : null);
  const cpMsg = cpObj.mensagem || (
    cpStatus === 'INDISPONIVEL'
      ? 'Base governamental temporariamente indisponível no momento da consulta.'
      : (cpTotal === 0
        ? 'Nenhum contrato público localizado nas bases consultadas'
        : `${cpTotal} contrato(s) público(s) identificado(s) nas bases governamentais`)
  );

  return {
    name: companyName,
    total: total,
    riskScore: effectiveRiskScore,
    overallRiskScore: effectiveRiskScore,
    subsidiaryLiabilityCount: subCount,
    subsidiaryContractorsPJ: subTomadoras,
    subsidiaryMessage: subMsg,
    publicContractsStatus: cpStatus,
    publicContractsTotal: cpTotal,
    publicContractsEsferas: cpEsferas,
    publicContractsOrgaos: cpOrgaos,
    publicContractsValorTotal: cpValorTotal,
    publicContractsValorFormatado: cpValorFormatado,
    publicContractsMensagem: cpMsg,
    reasons: Array.isArray(payload.ranking_motivos) ? payload.ranking_motivos : [],
    rankingCondenacoes: Array.isArray(payload.ranking_motivos_condenacao) ? payload.ranking_motivos_condenacao : [],
    condenacaoPorMotivo: Array.isArray(payload.condenacao_por_motivo) ? payload.condenacao_por_motivo : [],
    outcomes: outcomesObj,
    secrecy: isRecord(payload.niveis_sigilo) ? payload.niveis_sigilo : null,
    predictions: Array.isArray(payload.plus_previsao_condenacao) ? payload.plus_previsao_condenacao : [],
    previsaoBloqueada: isPrevisaoBloqueada,
    motivoBloqueioPrevisao: payload.motivo_bloqueio_previsao || (isPrevisaoBloqueada ? 'Previsão de condenação desativada por ausência de passivo condenatório prévio na amostra (presunção de conformidade e integridade probatória - Súmula 331 TST).' : null),
    idoneidadeEconomica: econ,
    sinaisSolvenciaPublica: payload.sinais_solvencia_publica || econ?.sinais_solvencia_publica || null,
    subsidiaryRisk: subsidiaryRisk(rawEcon),
    hasEconomicData: Boolean(rawEcon),
    logo: logo,
    processes: Array.isArray(payload.processos) ? payload.processos
      : Array.isArray(payload.process_records) ? payload.process_records : [],
    searchId: payload.search_id ?? payload.id ?? null,
    searchedAt: payload.updated_at || payload.created_at || null,
    scanStatus: payload.scan_status || 'COMPLETED',
    aiAnalysis: isRecord(payload.ai_analysis) ? payload.ai_analysis
      : isRecord(payload.parecer_agente_adk) ? payload.parecer_agente_adk : null,
    cached: Boolean(payload.cached),
    cacheLevel: payload.cache_level || null,
    isBroadBrand: Boolean(payload.is_broad_brand || payload.isBroadBrand || (companyName && companyName.includes('GRUPO CONSOLIDADO'))),
  };
}

async function request(path, options = {}) {
  let response;
  const endpoint = `${API_BASE}/api/v1${path}`;
  try {
    response = await fetch(endpoint, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
    });
  } catch {
    if (!API_BASE && typeof window !== 'undefined' && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
      throw new Error('A variável de ambiente VITE_API_BASE_URL não está configurada na Vercel apontando para o Render.');
    }
    throw new Error('Não foi possível conectar à API. Se o serviço no Render for gratuito, aguarde cerca de 1 minuto para inicialização (cold start).');
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const failure = new Error(body?.error || body?.message || `Erro HTTP ${response.status}`);
    failure.status = response.status;
    failure.hasBody = body !== null;
    throw failure;
  }
  if (body === null) throw new Error('A API não retornou JSON válido.');
  return body;
}

function Stat({ label, value, hint, icon: Icon, tone }) {
  return (
    <article className="stat card">
      <div className="stat-label">{label}<Icon size={18} /></div>
      <strong className={tone || ''}>{value}</strong>
      <span>{hint}</span>
    </article>
  );
}

function Empty({ title, description, icon: Icon = Info }) {
  return (
    <div className="empty" role="status">
      <Icon size={28} />
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  );
}

function Panel({ title, subtitle, children, action }) {
  return (
    <section className="card panel">
      <div className="panel-heading">
        <div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div>
        {action}
      </div>
      {children}
    </section>
  );
}

function Tag({ children, type = 'blue' }) {
  return <span className={`tag ${type}`}>{children}</span>;
}

function Sidebar({page,navigate,mobileOpen,setMobileOpen,showCatalog}) {
  useEffect(() => {
    if (typeof document !== 'undefined') {
      document.body.style.overflow = mobileOpen ? 'hidden' : '';
    }
    return () => {
      if (typeof document !== 'undefined') {
        document.body.style.overflow = '';
      }
    };
  }, [mobileOpen]);

  return (
    <>
      <div className={`mobile-overlay ${mobileOpen ? 'visible' : ''}`} onClick={() => setMobileOpen(false)} />
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}>
        <div className="brand">
          <div className="brand-mark"><Activity size={26} /></div>
          <div><strong>Radar<span>Jus</span></strong><small>INTELIGÊNCIA PREDITIVA</small></div>
          <button className="mobile-close" onClick={() => setMobileOpen(false)} aria-label="Fechar menu"><X size={20} /></button>
        </div>
        <div className="nav-label">PLATAFORMA</div>
        <nav aria-label="Navegação principal">
         {NAV_ITEMS
        .filter(({ id }) => id !== 'catalog' || showCatalog)
        .map(({ id, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className={`nav-item ${page === id ? 'active' : ''}`}
              aria-current={page === id ? 'page' : undefined}
              onClick={() => { navigate(id); setMobileOpen(false); }}
            >
              <Icon size={19} /><span>{PAGE_LABELS[id]}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="side-help">
            <ShieldCheck size={19} />
            <div><b>Dados com contexto</b><small>Metadados públicos para auditoria e inteligência preditiva.</small></div>
          </div>
          <div className="account">
            <span className="avatar">RJ</span>
            <div><b>RadarJus</b><small>Inteligência B2B & Previsibilidade</small></div>
          </div>
        </div>
      </aside>
    </>
  );
}

function SearchPage({ onSearch, query, setQuery, busy, error, navigate, result }) {
  const features = [
    { icon: Building2, title: 'Visão do fornecedor', text: 'Indicadores consolidados de eventos identificados.' },
    { icon: FileText, title: 'Vetores e ocorrências', text: 'Temas recorrentes e detalhes quando disponíveis.' },
    { icon: TrendingUp, title: 'Estimativas de Exposição', text: 'Previsibilidade de impacto financeiro por vetor.' },
  ];
  return (
    <div className="search-page">
      <div className="search-hero">
        <div className="hero-icon"><Radar size={31} /></div>
        <div className="eyebrow">INTELIGÊNCIA PREDITIVA DE FORNECEDORES</div>
        <h1>Inteligência preditiva para<br /><em>gestão de risco e fornecedores.</em></h1>
        <p>Consulte registros operacionais, explore vetores recorrentes e projete a exposição financeira do fornecedor.</p>
        <form className="search-box" onSubmit={(event) => { event.preventDefault(); onSearch(); }}>
          <Search size={22} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Digite o nome da empresa ou CNPJ"
            aria-label="Nome da empresa ou CNPJ"
            required
            minLength={3}
          />
          <button className="primary" disabled={busy}>
            {busy ? <><RefreshCw size={17} className="spin" /> Consultando</> : <>Pesquisar <ArrowRight size={17} /></>}
          </button>
        </form>
        <div className="search-hint">
          <Info size={15} /> Digite um nome para escolher a empresa no catálogo, ou um CNPJ completo para analisar direto.
        </div>
        {error && <div className="alert error" role="alert"><AlertCircle size={18} />{error}</div>}
      </div>
      <div className="feature-grid">
        {features.map(({ icon: Icon, title, text }) => (
          <div className="feature card" key={title}>
            <div className="feature-icon"><Icon size={23} /></div>
            <h3>{title}</h3><p>{text}</p>
          </div>
        ))}
      </div>
      {result && (
        <div className="continue">
          <span>Última consulta: <b>{result.name}</b></span>
          <button className="text-button" onClick={() => navigate('overview')}>Ver análise <ArrowRight size={16} /></button>
        </div>
      )}
    </div>
  );
}

function Overview({ data, navigate, catalogTerm }) {
  const secrecyEntries = data?.secrecy ? Object.entries(data.secrecy) : [];
  const observed = secrecyEntries.reduce((sum, [, count]) => sum + Number(count || 0), 0);
  const knownSecret = secrecyEntries.reduce((sum, [level, count]) => sum + (Number(level) > 0 ? Number(count || 0) : 0), 0);
  const reasons = data?.reasons || [];
  const outcomes = data?.outcomes || {};
  const hasData = Boolean(data);
  const classified = Object.values(outcomes).reduce((sum, value) => sum + Number(value || 0), 0);
  const ai = data?.aiAnalysis;

  const isFastPath = ['PARTIAL_FAST_PATH', 'BROWSER_PENDING', 'SCANNING_BACKGROUND'].includes(data?.scanStatus);
  const isCompleted = data?.scanStatus === 'COMPLETED';
  const tstCount = (data?.processes || []).filter(p => p.is_tst_recurso || p.tribunal === 'tst' || String(p.grau || '').includes('TST')).length;

  return (
    <div className="stack">
      <div className="section-intro">
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          {data?.logo && (
            <div className="company-logo-wrapper">
              <CompanyLogo logo={data.logo} companyName={data.name} />
            </div>
          )}
          <div>
            <div className="eyebrow">INTELIGÊNCIA PREDITIVA DE FORNECEDOR</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              <h1 style={{ margin: 0 }}>{data?.name || 'Visão geral'}</h1>
              {data?.isBroadBrand && (
                <Tag type="blue">GRUPO CONSOLIDADO</Tag>
              )}
            </div>
            <p>{hasData ? 'Panorama da modelagem preditiva e exposição operacional nas bases consultadas.' : 'Pesquise um fornecedor para preencher os indicadores abaixo.'}</p>
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          {isFastPath && (
            <Tag type="amber">
              <RefreshCw size={12} className="spin" style={{ display: 'inline', marginRight: '5px' }} />
              Fast-Path ativo (&lt; 3s) · Varrendo em 2º plano
            </Tag>
          )}
          {data?.scanStatus === 'BROWSER_PARTIAL' && <Tag type="amber">Varredura parcial encerrada</Tag>}
          {isCompleted && <Tag type="green"><Check size={13} style={{ display: 'inline', marginRight: '4px' }} /> Varredura Completa</Tag>}
          {tstCount > 0 && (
            <Tag type="purple">
              <TrendingUp size={13} style={{ display: 'inline', marginRight: '4px' }} />
              {tstCount} em Grau de Recurso no TST
            </Tag>
          )}
          {data?.previsaoBloqueada && (
            <Tag type="green"><CheckCircle2 size={13} style={{ display: 'inline', marginRight: '4px' }} /> Previsão Bloqueada (0,0%)</Tag>
          )}
          {data?.cached && <Tag type="blue">⚡ Cache Inteligente ({data.cacheLevel || 'L1'})</Tag>}
        </div>
      </div>

      {hasData && <section className="prescriptive-hero" aria-label="Decisão e prescrição">
        <div className="prescriptive-decision">
          <div><div className="eyebrow">RECOMENDAÇÃO DE CONTRATAÇÃO</div>
          <h2>{businessText(ai?.recomendacao_homologacao || ai?.recomendacao || 'Recomendação não disponível')}</h2>
          <p>{data.hasEconomicData ? businessText(data.idoneidadeEconomica?.cenario_badge || data.idoneidadeEconomica?.cenario_titulo || '') : 'Dados de solvência indisponíveis'}</p></div>
          <div className="prescriptive-risk"><small>RISCO SUBSIDIÁRIO</small><strong>{data.subsidiaryRisk == null ? '—' : formatPercent(data.subsidiaryRisk)}</strong><span>Estimativa de risco ao contratante · Súmula 331 TST</span></div>
        </div>
        <h3>O que fazer</h3>
        {Array.isArray(ai?.recomendacoes_compliance) && ai.recomendacoes_compliance.length ? <ul>{ai.recomendacoes_compliance.map((item,i)=><li key={i}>{businessText(item)}</li>)}</ul> : <p>Prescrições ainda não disponibilizadas. Consulte o parecer antes de decidir.</p>}
      </section>}
      {/* Condenações por Motivo do Processo (US-18) */}
      {((Array.isArray(data?.condenacaoPorMotivo) && data.condenacaoPorMotivo.length > 0) ||
        (Array.isArray(data?.rankingCondenacoes) && data.rankingCondenacoes.length > 0)) && (
        <Panel
          title="Condenações por Motivo do Processo"
          subtitle="Histórico de condenações por assunto e faixa provável de variação"
        >
          <div className="table-scroll">
            <table className="responsive-table">
              <thead>
                <tr>
                  <th>Motivo / Assunto do Processo</th>
                  <th style={{ textAlign: 'right' }}>Processos Mapeados</th>
                  <th style={{ textAlign: 'right' }}>Condenações</th>
                  <th style={{ textAlign: 'right' }}>Taxa de Condenação</th>
                  <th style={{ textAlign: 'right' }}>Faixa de Variação</th>
                </tr>
              </thead>
              <tbody>
                {(data.condenacaoPorMotivo?.length ? data.condenacaoPorMotivo : data.rankingCondenacoes).map((item, index) => {
                  const totalProc = item.total_processos ?? item.total ?? item.count ?? null;
                  const condenacoes = item.condenacoes ?? item.quantidade_condenacoes ?? item.quantidade ?? 0;
                  const taxa = item.taxa_condenacao ?? item.taxa_no_motivo ?? item.taxa_condenacao_no_motivo ?? item.taxa_perda_no_motivo ?? null;
                  const faixa = item.faixa_variacao || "—";
                  const isBlockedZero = condenacoes === 0;

                  return (
                    <tr key={`${item.motivo}-${index}`}>
                      <td data-label="Motivo / Assunto" className="full-width">
                        <b>{item.motivo}</b>
                      </td>
                      <td data-label="Processos Mapeados" style={{ textAlign: 'right' }}>
                        <span>{totalProc == null ? "—" : formatNumber(totalProc)}</span>
                      </td>
                      <td data-label="Condenações" style={{ textAlign: 'right' }}>
                        <span style={{ fontWeight: 600, color: condenacoes > 0 ? '#c34a4a' : '#25865a' }}>
                          {formatNumber(condenacoes)}
                        </span>
                      </td>
                      <td data-label="Taxa de Condenação" style={{ textAlign: 'right' }}>
                        <Tag type={taxa == null ? 'gray' : isBlockedZero ? 'green' : taxa >= 60 ? 'red' : taxa >= 35 ? 'amber' : 'green'}>
                          {taxa == null ? "—" : formatPercent(taxa)}
                        </Tag>
                      </td>
                      <td data-label="Faixa de Variação" style={{ textAlign: 'right' }}>
                        <span style={{ fontSize: '12px', color: '#556677', fontWeight: 500 }}>
                          {faixa}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="muted-note" style={{ marginTop: '14px' }}>
            <BarChart3 size={15} /> Taxas por assunto conforme os dados disponíveis. O símbolo — indica informação não disponibilizada.
          </div>
        </Panel>
      )}


      {hasData && <Panel title="Diligência complementar" subtitle="Informações para verificar antes da contratação">
        {data.sinaisSolvenciaPublica?.resumo_solvencia ? (
          <p><strong>Solvência e Certidões:</strong> {businessText(data.sinaisSolvenciaPublica.resumo_solvencia)}</p>
        ) : (
          <p>Histórico de responsabilidade subsidiária, contratos públicos e alertas de PEP: dados específicos não disponibilizados nesta consulta.</p>
        )}
        {data.sinaisSolvenciaPublica?.orientacao_homologacao && (
          <p style={{ color: '#245a81', fontWeight: 600 }}>
            📌 Orientação de Homologação: {businessText(data.sinaisSolvenciaPublica.orientacao_homologacao)}
          </p>
        )}
        {data.hasEconomicData && <p>Restrições federais: {data.idoneidadeEconomica.possui_sancoes_cgu || data.idoneidadeEconomica.possui_divida_ativa ? 'Apontamentos informados' : 'Consulte as bases oficiais para confirmação'}.</p>}
      </Panel>}
      {/* Parecer do Consultor de Inteligência Artificial (Agente ADK radarjusAgent) */}
      {ai && (
        <Panel
          title="Resumo executivo da IA"
          subtitle="Síntese para compras e compliance"
        >
          <div style={{ display: 'grid', gap: '16px' }}>
            <p style={{ margin: 0, fontSize: '14px', lineHeight: '1.7', color: '#273b4e' }}>
              {businessText(ai.resumo_executivo || ai.parecer_governanca)}
            </p>
            {ai.taxa_historica_vitoria_empresa_pct != null && (
              <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap', padding: '12px 16px', background: '#f5f8fa', borderRadius: '10px' }}>
                <div>
                  <small style={{ color: '#68798a', display: 'block' }}>Taxa Histórica de Risco Mitigado (Favorável):</small>
                  <strong style={{ fontSize: '18px', color: '#25865a' }}>{formatPercent(ai.taxa_historica_vitoria_empresa_pct)}</strong>
                </div>
                {ai.taxa_historica_acordos_pct != null && (
                  <div>
                    <small style={{ color: '#68798a', display: 'block' }}>Adesão a Acordos:</small>
                    <strong style={{ fontSize: '18px', color: '#245a81' }}>{formatPercent(ai.taxa_historica_acordos_pct)}</strong>
                  </div>
                )}
              </div>
            )}
            {Array.isArray(ai.alertas_sumula_331) && ai.alertas_sumula_331.length > 0 && (
              <div>
                <b style={{ display: 'block', fontSize: '13px', marginBottom: '6px', color: '#b67b23' }}>Alertas Súmula 331 do TST (Agente ADK):</b>
                <ul style={{ margin: 0, paddingLeft: '20px', fontSize: '12px', lineHeight: '1.6', color: '#556677' }}>
                  {ai.alertas_sumula_331.map((alerta, i) => (
                    <li key={i}>{businessText(alerta)}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Panel>
      )}

      {hasData && <div className="prescriptive-volume">
        <Stat label="PROCESSOS ANALISADOS" value={formatNumber(data.total)} icon={FileText}/>
        {Object.entries(OUTCOME_LABELS).map(([key,label])=><Stat key={key} label={label} value={formatNumber(outcomes[key])} icon={Activity}/>)}
        <Stat label="VETORES DE RISCO" value={formatNumber(reasons.length)} icon={Filter}/>
        <Stat label="PROCESSOS NO TST" value={formatNumber(tstCount)} icon={TrendingUp}/>
      </div>}
      <div className="two-col">
        <Panel title="Vetores mais recorrentes" subtitle="Frequência entre os registros identificados" action={<button className="text-button" onClick={() => navigate('processes')}>Ver processos <ArrowRight size={15} /></button>}>
          {reasons.length ? (
            <div className="bars">
              {reasons.slice(0, 6).map((reason, index) => {
                const percentage = Number(reason.porcentagem) || Number(reason.quantidade || 0) / Math.max(data.total, 1) * 100;
                return (
                  <div className="bar-item" key={`${reason.motivo}-${index}`}>
                    <div className="bar-top"><span title={reason.motivo}>{reason.motivo}</span><b>{formatNumber(reason.quantidade)}</b></div>
                    <div className="track"><div style={{ width: `${Math.max(0, Math.min(100, percentage))}%`, background: CHART_COLORS[index % CHART_COLORS.length] }} /></div>
                  </div>
                );
              })}
            </div>
          ) : <Empty title="Sem assuntos identificados" description="Os assuntos aparecerão aqui após uma pesquisa que retorne esses dados." />}
        </Panel>
        <Panel title="Desfechos dos registros" subtitle="Classificação produzida pelo backend">
          {Object.keys(outcomes).length ? (
            <div className="outcome-grid">
              {Object.entries(OUTCOME_LABELS).map(([key, label], index) => (
                <div className="outcome" key={key}>
                  <span className="outcome-dot" style={{ background: CHART_COLORS[index % CHART_COLORS.length] }} />
                  <span>{label}</span><strong>{formatNumber(outcomes[key])}</strong>
                </div>
              ))}
            </div>
          ) : <Empty title="Sem desfechos disponíveis" description="Os desfechos aparecerão após uma consulta com classificações." />}
          <div className="muted-note"><Info size={15} /> Classificações automáticas via códigos TPU e NLP de sentenças.</div>
        </Panel>
      </div>

      <div className="two-col">
        <Panel title="Sigilo nos registros consultados" subtitle="Níveis oficiais dos metadados disponíveis">
          {secrecyEntries.length ? (
            <>
              <div className="secrecy-summary">
                <div><strong>{formatNumber(knownSecret)}</strong><span>registros com nível acima de zero</span></div>
                <div><strong>{formatNumber(observed)}</strong><span>registros com nível informado</span></div>
              </div>
              <div className="secrecy-levels">
                {[...secrecyEntries].sort(([a], [b]) => Number(a) - Number(b)).map(([level, count]) => (
                  <div className="secrecy-row" key={level}>
                    <span>Nível {level}</span>
                    <div className="track"><div style={{ width: `${Math.round(Number(count) / Math.max(observed, 1) * 100)}%`, background: Number(level) === 0 ? '#25865a' : Number(level) === 2 ? '#b67b23' : '#c34a4a' }} /></div>
                    <b>{count}</b>
                  </div>
                ))}
              </div>
            </>
          ) : <Empty title="Sigilo não informado" description="Não é possível calcular um indicador de sigilo sem dados da API." />}
          <div className="muted-note"><ShieldAlert size={16} /> Processos sob segredo de justiça (nível &gt; 0) omitem dados de partes.</div>
        </Panel>
        <Panel title="Aprofundamento de análise" subtitle="Próximas ações de compliance">
          <div className="next-steps">
            {[
              ...(catalogTerm ? [{ page: 'catalog', icon: Library, title: 'Voltar ao catálogo', text: `Escolha outra empresa da busca “${catalogTerm}”.` }] : []),
              { page: 'search', icon: Search, title: 'Pesquisar outra empresa', text: 'Inicie uma nova consulta rápida.' },
              { page: 'processes', icon: FileText, title: 'Explorar processos e observações', text: 'Veja números, assuntos inferidos e desfechos.' },
              { page: 'predictions', icon: ChartNoAxesCombined, title: 'Estimativas de Exposição', text: 'Consulte a previsibilidade de impacto financeiro por vetor.' },
            ].map(({ page, icon: Icon, title, text }) => (
              <button key={page} onClick={() => navigate(page)}>
                <Icon size={20} /><span><b>{title}</b><small>{text}</small></span><ChevronRight size={18} />
              </button>
            ))}
          </div>
        </Panel>
      </div>
      {/* Idoneidade Econômica & Risco de Exposição Subsidiária */}
      {data?.hasEconomicData && (() => {
        const econ = data.idoneidadeEconomica;
        const sinais = data.sinaisSolvenciaPublica || econ?.sinais_solvencia_publica || {};
        const isCenarioA = (econ.cenario_tipo === 'CENARIO_A') || (econ.cenario === 'CENARIO_A') || (econ.cenario_class === 'seguro');
        const badgePorte = econ.badge_compatibilidade_porte || econ.observacao_porte || econ.capital_social_display?.observacao_porte;
        const capitalFormatted = econ.capital_social_formatado || econ.valor_formatado || econ.capital_social_display?.valor_formatado || 'R$ 0,00';
        const situacao = econ.situacao_cadastral || 'Não informado';
        const isSituacaoAtiva = situacao === 'ATIVA';
        const probInsolv = econ.probabilidade_insolvencia ?? econ.probabilidade_insolvencia_pct;
        const riscoCalote = Number(econ.risco_calote_subsidiario ?? econ.risco_calote_subsidiario_pct ?? 0);
        const cenarioTitulo = econ.cenario_titulo || (isCenarioA ? 'Cenário A: Idoneidade Econômica Robusta • Risco de Transferência de Passivo Mitigado' : 'Cenário B: Risco Crítico de Transferência de Passivo (Alerta Máximo de Retenção)');
        const cenarioDescricao = econ.cenario_descricao || econ.parecer_sumula_331 || '';
        const cenarioBadge = econ.cenario_badge || (isCenarioA ? 'Cenário A · Solvência Comprovada' : 'Cenário B · Risco Crítico');
        const subtituloUnidades = econ.subtitulo_unidades || econ.detalhe_filiais || 'Soma e lastro patrimonial consolidado cobrindo a matriz e todas as filiais ativas (IN RFB 2119/2022).';

        return (
          <details className="prescriptive-more"><summary>Mais informações</summary>
            <div style={{ display: 'grid', gap: '16px' }}>
              <div
                style={{
                  padding: '16px',
                  borderRadius: '10px',
                  border: isCenarioA ? '1px solid #bbf7d0' : '1px solid #fecaca',
                  background: isCenarioA ? '#f0fdf4' : '#fef2f2',
                  color: isCenarioA ? '#166534' : '#991b1b',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  flexWrap: 'wrap',
                  gap: '10px',
                }}
              >
                <div>
                  <strong style={{ display: 'block', fontSize: '15px', marginBottom: '4px' }}>
                    {businessText(cenarioTitulo)}
                  </strong>
                  <p style={{ margin: 0, fontSize: '13px', lineHeight: 1.5, opacity: 0.9 }}>
                    {businessText(cenarioDescricao)}
                  </p>
                </div>
              </div>

              <div className="econ-grid">
                <div className="econ-badge econ-capital">
                  <small style={{ color: '#245a81', fontWeight: 600 }}>
                    {data?.isBroadBrand || (econ?.unidades_identificadas > 1)
                      ? 'Capital Social Consolidado (Matriz + Filiais):'
                      : 'Capital Social Cadastral (Receita Federal):'}
                  </small>
                  <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '10px', marginTop: '4px', marginBottom: '4px' }}>
                    <strong style={{ fontSize: '20px', color: '#17283a', lineHeight: 1.2 }}>
                      {capitalFormatted}
                    </strong>
                    {badgePorte && (
                      <span style={{
                        fontSize: '11px',
                        fontWeight: 600,
                        padding: '3px 8px',
                        borderRadius: '6px',
                        background: '#e8f0f6',
                        color: '#245a81',
                        border: '1px solid #cdd7df'
                      }}>
                        {badgePorte}
                      </span>
                    )}
                  </div>
                  <small style={{ color: '#68798a', fontSize: '11px' }}>
                    {subtituloUnidades}
                  </small>
                </div>

                <div className="econ-badge">
                  <small style={{ color: '#245a81', fontWeight: 600 }}>Situação Cadastral RFB:</small>
                  <strong style={{ fontSize: '16px', color: isSituacaoAtiva ? '#25865a' : '#c34a4a', marginTop: '4px' }}>
                    {situacao}
                  </strong>
                  <small style={{ color: '#68798a', fontSize: '11px' }}>
                    {isSituacaoAtiva ? 'Sem bloqueios cadastrais' : 'Restrição cadastral na RFB'}
                  </small>
                </div>

                <div className="econ-badge">
                  <small style={{ color: '#245a81', fontWeight: 600 }}>Probabilidade de Insolvência:</small>
                  <strong style={{ fontSize: '16px', color: probInsolv >= 30 ? '#c34a4a' : probInsolv >= 15 ? '#b67b23' : '#25865a', marginTop: '4px' }}>
                    {probInsolv == null ? "Não informado" : formatPercent(probInsolv)}
                  </strong>
                  <small style={{ color: '#68798a', fontSize: '11px' }}>
                    Estimativa de solvência
                  </small>
                </div>

                <div className="econ-badge">
                  <small style={{ color: '#245a81', fontWeight: 600 }}>Sanções e Débitos Federais:</small>
                  <strong style={{ fontSize: '15px', color: (econ.possui_sancoes_cgu || econ.possui_divida_ativa) ? '#c34a4a' : '#25865a', marginTop: '4px' }}>
                    {(econ.possui_sancoes_cgu || econ.possui_divida_ativa) ? 'Constam Apontamentos' : (econ.possui_sancoes_cgu === false && econ.possui_divida_ativa === false ? 'Nada Consta informado' : 'Não informado')}
                  </strong>
                  <small style={{ color: '#68798a', fontSize: '11px' }}>
                    CEIS/CNEP/CEPIM &amp; PGFN
                  </small>
                </div>
              </div>

              {/* Sinais Públicos de Solvência e Certidões (US-22) */}
              {Array.isArray(sinais?.fontes_consultadas) && sinais.fontes_consultadas.length > 0 && (
                <div style={{ marginTop: '12px', background: '#f8fafc', padding: '14px', borderRadius: '8px', border: '1px solid #e2e8f0' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px', flexWrap: 'wrap', gap: '6px' }}>
                    <strong style={{ fontSize: '13px', color: '#1e293b' }}>
                      Fontes e Certidões Públicas Verificadas
                    </strong>
                    <span style={{ fontSize: '12px', color: '#475569' }}>
                      Demonstrações Contábeis: <b>{sinais.demonstracao_contabil_status || 'Demonstração contábil pública indisponível'}</b>
                    </span>
                  </div>
                  <div style={{ display: 'grid', gap: '8px' }}>
                    {sinais.fontes_consultadas.map((f, idx) => (
                      <div key={idx} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '12px', padding: '8px 10px', background: '#fff', borderRadius: '6px', border: '1px solid #edf2f7', flexWrap: 'wrap', gap: '8px' }}>
                        <span style={{ color: '#334155' }}>
                          <strong>{f.fonte}:</strong> {f.detalhe}
                        </span>
                        <span style={{
                          fontWeight: 600,
                          padding: '2px 8px',
                          borderRadius: '4px',
                          fontSize: '11px',
                          background: f.status === 'Regular' ? '#dcfce7' : f.status === 'Restrição identificada' ? '#fee2e2' : '#f1f5f9',
                          color: f.status === 'Regular' ? '#166534' : f.status === 'Restrição identificada' ? '#991b1b' : '#475569',
                        }}>
                          {f.status}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          <p>Status da varredura: {translateScanStatus(data.scanStatus)} · Desfechos mapeados: {formatNumber(classified)} · Transparência: {secrecyEntries.length ? "Parcial" : "Indisponível"}</p><p>Estimativa baseada no histórico e porte; faixa provável de variação (90% de confiança) quando disponível.</p></details>
        );
      })()}

    </div>
  );
}

function Processes({ data }) {
  return <ProcessResults key={data?.searchId ?? 'none'} data={data} />;
}

function ProcessResults({ data }) {
  const [filters, setFilters] = useState({...EMPTY_FILTERS});
  const [page, setPage] = useState(1);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const validation = validateFilters(filters);
  const searchId = validSearchId(data?.searchId);
  const query = processQuery(filters, page);
  const resultKey = `${searchId}:${query}:${data?.total}`;
  const current = result?.key === resultKey ? result.body : null;
  const list = current?.processes || [];
  useEffect(() => {
    if (!searchId || validation) return;
    const controller = new AbortController();
    setError('');
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const body = await request(`/searches/${searchId}/processes?${query}`, {signal:controller.signal});
        if (controller.signal.aborted) return;
        if (!advancedFiltersConfirmed(filters, body)) throw new Error('A API ainda não confirmou os filtros de período/valor (US-08).');
        if (!Array.isArray(body.processes) || !Number.isFinite(body.total)) throw new Error('Resposta de listagem inválida.');
        setResult({key:resultKey,body});
      } catch (err) {
        if (!controller.signal.aborted) setError(err.message || 'Falha ao listar processos.');
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, 300);
    return () => {clearTimeout(timer);controller.abort();};
  }, [resultKey, validation]);
  const updateFilters = next => {setFilters(next);setPage(1);setError('');};
  return (
    <div className="stack">
      <div className="section-intro">
        <div>
          <div className="eyebrow">EMPRESA / PROCESSOS</div>
          <h1>Processos e movimentações</h1>
          <p>{data ? `${data.name} · registros extraídos do Comunica PJe e DataJud.` : 'Aguardando consulta de uma empresa.'}</p>
        </div>
      </div>
      <Panel title="Registros individuais" subtitle="Lista de processos judiciais vinculados à empresa">
        <ProcessFiltersBar filters={filters} onChange={updateFilters} disabled={!searchId} error={validation}/>
        {searchId && !validation && !current && !error && <p role="status">Consultando processos…</p>}
        {!validation && error && <p role="alert" className="process-filter-error">{error}</p>}
        {current && !validation && <div className="process-pagination" aria-live="polite">
          <span>Exibindo {list.length} de {current.total} processos{data?.total != null && current.total !== data.total ? ` (${data.total} na consulta)` : ''}</span>
          <div className="process-pagination-controls">
            <button className="secondary" disabled={page<=1 || loading} onClick={()=>setPage(p=>p-1)}>Anterior</button>
            <span>Página {page} de {current.total_pages || 1}</span>
            <button className="secondary" disabled={page >= (current.total_pages || 1) || loading} onClick={()=>setPage(p=>p+1)}>Próxima</button>
          </div>
        </div>}
        {current && !validation ? (
          <div className="table-scroll">
            <table className="responsive-table">
              <thead>
                <tr>
                  <th>Número do processo</th>
                  <th>Assunto Principal</th>
                  <th>Desfecho</th>
                  <th>Sigilo</th>
                  <th>Observação Técnica</th>
                </tr>
              </thead>
              <tbody>
                {list.map((process, index) => (
                  <tr key={process.id || process.numero_processo || index}>
                    <td data-label="Processo" className="mono full-width">
                      <div className="process-cell-content">
                        <span className="process-number-text">{formatProcessNumber(process.numero_processo || process.process_number)}</span>
                        {(process.is_tst_recurso || String(process.tribunal || '').toUpperCase() === 'TST' || String(process.grau || '').includes('TST')) && (
                          <Tag type="purple">Instância Superior TST</Tag>
                        )}
                      </div>
                    </td>
                    <td data-label="Assunto Principal" className="full-width">
                      <b>{Array.isArray(process.subjects) ? process.subjects.join(', ') : process.primary_subject || 'Não informado'}</b>
                    </td>
                    <td data-label="Desfecho">
                      <Tag type={process.sentence_outcome === 'improcedentes' ? 'green' : process.sentence_outcome === 'condenacoes' ? 'red' : 'blue'}>
                        {OUTCOME_LABELS[process.sentence_outcome] || process.sentence_outcome || 'Em aberto'}
                      </Tag>
                    </td>
                    <td data-label="Segredo de Justiça">
                      {(process.secrecy_level ?? process.nivel_sigilo) != null ? (
                        <Tag type={Number(process.secrecy_level ?? process.nivel_sigilo) > 0 ? 'amber' : 'gray'}>
                          Nível {process.secrecy_level ?? process.nivel_sigilo}
                        </Tag>
                      ) : '—'}
                    </td>
                    <td data-label="Observação Técnica" className="full-width">
                      {process.inferred_from_djen_text ? (
                        <span style={{ color: '#245a81', fontSize: '12px' }}>
                          Assunto identificado via publicação PJe
                        </span>
                      ) : (
                        <span style={{ color: '#8899a6', fontSize: '11px' }}>{process.observation || (process.inferred_from_djen_text === false ? 'Metadados TPU oficiais' : 'Não informado')}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!list.length && <Empty title="Nenhum resultado para o filtro" description="Experimente outro termo." />}
          </div>
        ) : !searchId ? (
          <Empty icon={FileText} title="Nenhum processo disponível" description={data ? 'A API não forneceu registros individuais nesta consulta.' : 'A lista será preenchida quando uma pesquisa retornar processos individuais.'} />
        ) : null}
      </Panel>
      <div className="info-banner">
        <Info size={19} /> Processos rotulados com "Assunto identificado via publicação PJe" tiveram seu tema extraído diretamente do texto da publicação quando os dados oficiais registravam apenas "Outros Assuntos".
      </div>
    </div>
  );
}

function Predictions({ data }) {
  const predictions = data?.predictions || [];
  return (
    <div className="stack">
      <div className="section-intro">
        <div>
          <div className="eyebrow">EMPRESA / ESTIMATIVAS</div>
          <h1>Previsão de Desfecho nos Processos Abertos</h1>
          <p>{data ? `Estimativa baseada no histórico (Vitória, Acordo e Condenação), com faixa provável de variação (90% de confiança) (${data.name}).` : 'Aguardando dados de uma consulta.'}</p>
        </div>
        <Tag type="blue">Estimativa baseada no histórico</Tag>
      </div>

      <div className="info-banner warning">
        <Activity size={20} />
        <div>
          <b>Estimativa baseada no histórico e faixa provável de variação (90% de confiança)</b>
          <p>Estima os possíveis desfechos dos processos abertos com base no histórico disponível e na incerteza da amostra.</p>
        </div>
      </div>

      <Panel title="Estimativa de desfechos nos processos abertos" subtitle="Faixa provável de variação (90% de confiança) por motivo">
        {predictions.length ? (
          <div className="table-scroll">
            <table className="responsive-table">
              <thead>
                <tr>
                  <th>Motivo da Ação (TPU/PJe)</th>
                  <th>Vitória (Improcedência)</th>
                  <th>Acordo</th>
                  <th>Condenação</th>
                  <th>Faixa provável (90% de confiança)</th>
                  <th>Abertos (Cond. Esperadas)</th>
                  <th>Diagnóstico</th>
                </tr>
              </thead>
              <tbody>
                {predictions.map((prediction, index) => {
                  const probVencer = prediction.probabilidade_vencer_pct ?? Math.max(0, 100 - (prediction.probabilidade_condenacao_pct || 0) - (prediction.probabilidade_acordo_pct || 0));
                  const probAcordo = prediction.probabilidade_acordo_pct ?? 0;
                  const probCond = prediction.probabilidade_condenacao_pct ?? 0;
                  const isHighWin = probVencer >= 45.0;
                  const isMidWin = probCond < 55.0;
                  const hasIC = prediction.ic_90_inferior_pct != null && prediction.ic_90_superior_pct != null;
                  return (
                    <tr key={`${prediction.motivo}-${index}`}>
                      <td data-label="Motivo da Ação" className="full-width">
                        <b>{prediction.motivo || 'Não informado'}</b>
                      </td>
                      <td data-label="Vitória (Improcedência)">
                        <span className="percent" style={{ color: '#25865a', fontWeight: 'bold' }}>
                          {formatPercent(probVencer)}
                        </span>
                      </td>
                      <td data-label="Acordo">
                        <span className="percent" style={{ color: '#245a81', fontWeight: 600 }}>
                          {formatPercent(probAcordo)}
                        </span>
                      </td>
                      <td data-label="Condenação">
                        <span className="percent" style={{ color: probCond >= 55 ? '#c34a4a' : '#667788', fontWeight: 'bold' }}>
                          {formatPercent(probCond)}
                        </span>
                      </td>
                      <td data-label="Faixa provável (90% de confiança)">
                        {hasIC ? (
                          <span style={{ fontSize: '12px', color: '#556677' }}>
                            [{formatPercent(prediction.ic_90_inferior_pct)} – {formatPercent(prediction.ic_90_superior_pct)}]
                            <small style={{ display: 'block', color: '#8899a6' }}>
                              Precisão: {formatPercent(prediction.confianca_previsao_pct)} ({prediction.nivel_confianca || 'Amostral'})
                            </small>
                          </span>
                        ) : (
                          formatPercent(prediction.confianca_previsao_pct)
                        )}
                      </td>
                      <td data-label="Processos Abertos">
                        <b>{formatNumber(prediction.processos_abertos)}</b>
                        {prediction.condenacoes_esperadas_abertos != null && prediction.processos_abertos > 0 && (
                          <small style={{ display: 'block', color: '#8899a6' }}>
                            ~{prediction.condenacoes_esperadas_abertos.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} esperadas
                          </small>
                        )}
                      </td>
                      <td data-label="Diagnóstico" className="full-width">
                        <Tag type={isHighWin ? 'green' : isMidWin ? 'amber' : 'red'}>
                          {businessText(prediction.diagnostico_chance_vitoria) || (isHighWin ? 'Alta probabilidade de êxito' : isMidWin ? 'Risco equilibrado' : 'Alto risco de condenação')}
                        </Tag>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="card" style={{ padding: '32px 24px', textAlign: 'center', background: '#fff' }}>
            <div style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '8px',
              padding: '7px 16px',
              borderRadius: '20px',
              background: '#f0fdf4',
              color: '#166534',
              border: '1px solid #bbf7d0',
              fontWeight: 700,
              fontSize: '13px',
              marginBottom: '16px'
            }}>
              <CheckCircle2 size={16} /> Previsão Bloqueada: 0,0% de Condenação
            </div>
            <h3 style={{ margin: '0 0 10px', fontSize: '18px', color: '#17283a' }}>
              Ausência de Registros Condenatórios Prévios
            </h3>
            <p style={{ maxWidth: '640px', margin: '0 auto 18px', color: '#556677', fontSize: '13px', lineHeight: 1.65 }}>
              {data?.motivoBloqueioPrevisao || 'Previsão de condenação desativada por ausência de passivo condenatório prévio na amostra (presunção de conformidade e integridade probatória - Súmula 331 TST).'}
            </p>
            <div style={{ display: 'flex', justifyContent: 'center', gap: '16px', flexWrap: 'wrap', marginTop: '16px' }}>
              <div style={{ padding: '14px 24px', background: '#f8fafb', border: '1px solid #e1e7ec', borderRadius: '10px' }}>
                <small style={{ display: 'block', color: '#68798a', fontSize: '11px', marginBottom: '4px' }}>Impacto Subsidiário Estimado:</small>
                <strong style={{ fontSize: '22px', color: '#245a81' }}>Risco Mitigado (0,0%)</strong>
              </div>
            </div>
          </div>
        )}
      </Panel>

      <Panel title="Como interpretar estes indicadores">
        <div className="explain-grid">
          <div>
            <b>Vitória vs. Acordo vs. Condenação</b>
            <p>Acordos não são contabilizados como vitória da empresa. As três probabilidades estimadas somam 100% em cada motivo.</p>
          </div>
          <div>
            <b>Faixa provável de variação (90% de confiança)</b>
            <p>Expressa a incerteza da estimativa considerando o histórico e os processos ainda em aberto.</p>
          </div>
          <div>
            <b>Impactos Esperados nos Abertos</b>
            <p>Projeta o passivo esperado multiplicando o volume de processos abertos pela probabilidade estimada de impacto financeiro do motivo.</p>
          </div>
        </div>
      </Panel>
    </div>
  );
}

function HistoryPage({ onOpen, history, busy, error, onReload }) {
  return (
    <div className="stack">
      <div className="section-intro">
        <div>
          <div className="eyebrow">PESQUISAS / HISTÓRICO</div>
          <h1>Pesquisas anteriores</h1>
          <p>Consulte diagnósticos armazenados no PostgreSQL sem custo adicional de API.</p>
        </div>
        <button className="secondary" onClick={onReload} disabled={busy}>
          <RefreshCw size={16} className={busy ? 'spin' : ''} /> Atualizar
        </button>
      </div>
      {error && <div className="alert error" role="alert"><AlertCircle size={18} />{error}</div>}
      <Panel title="Consultas salvas" subtitle="Ordenadas pela atualização mais recente">
        {busy ? (
          <div className="loading"><RefreshCw className="spin" /> Carregando histórico...</div>
        ) : history.length ? (
          <div className="history-list">
            {history.map((item, index) => (
              <button className="history-item" key={item.id ?? index} onClick={() => onOpen(item.id)} disabled={item.id == null}>
                <div className="history-icon"><Building2 size={20} /></div>
                <div className="history-name">
                  <b>{item.company_name || 'Empresa não informada'}</b>
                  <small>Consultado em {formatDate(item.updated_at || item.created_at)}</small>
                </div>
                <span>{formatNumber(item.total_processes)} registros</span>
                <ChevronRight size={19} />
              </button>
            ))}
          </div>
        ) : (
          <Empty icon={History} title="Nenhuma pesquisa salva" description="As pesquisas aparecerão aqui quando o backend persistir os resultados." />
        )}
      </Panel>
    </div>
  );
}

function CompanyShareActions({ data }) {
  const [message, setMessage] = useState('');
  const [pdfBusy, setPdfBusy] = useState(false);
  const [manualUrl, setManualUrl] = useState('');
  const id = validSearchId(data.searchId);

  async function copyLink() {
    const url = companyShareUrl(window.location, id);
    setManualUrl('');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard indisponível');
      await navigator.clipboard.writeText(url);
      setMessage('Link copiado.');
    } catch {
      setManualUrl(url);
      setMessage('Selecione e copie o link abaixo.');
    }
  }

  async function downloadPdf() {
    setPdfBusy(true);
    setMessage('');
    let objectUrl;
    try {
      const pdfPayload = {
        search_id: id || null,
        empresa: data.name,
        company_name: data.name,
        total_processos: data.total,
        indice_exposicao_financeira: data.riskScore,
        score_risco_terceirizacao: data.overallRiskScore ?? data.riskScore,
        desfechos_sentencas: data.outcomes || {},
        ranking_motivos: data.reasons || [],
        ranking_motivos_condenacao: data.rankingCondenacoes || [],
        condenacao_por_motivo: data.condenacaoPorMotivo || [],
        plus_previsao_condenacao: data.predictions || [],
        idoneidade_economica: data.idoneidadeEconomica || {},
        ai_analysis: data.aiAnalysis || {},
        scan_status: data.scanStatus || 'COMPLETED',
      };

      // Prioriza POST com os dados já carregados na tela (elimina 404 mesmo sem persistência prévia)
      let response = await fetch(`${API_BASE}/api/v1/report/pdf`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pdfPayload),
      }).catch(() => null);

      // Fallback para GET por ID ou query string ?empresa=...
      if (!response || !response.ok) {
        const fallbackUrl = id
          ? `${API_BASE}/api/v1/history/${id}/summary.pdf`
          : `${API_BASE}/api/v1/report/pdf?empresa=${encodeURIComponent(data.name || '')}`;
        response = await fetch(fallbackUrl);
      }

      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.error || `Não foi possível baixar o PDF (HTTP ${response.status}).`);
      }
      if (!response.headers.get('content-type')?.includes('application/pdf')) {
        throw new Error('A API não retornou um PDF válido.');
      }
      objectUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      const safeSlug = String(data.name || '')
        .replace(/\(.*?\)/g, '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-zA-Z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .toLowerCase()
        .slice(0, 40);
      anchor.download = `radarjus-${safeSlug || (id ? `empresa-${id}` : 'relatorio')}.pdf`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setMessage('Download do PDF iniciado.');
    } catch (failure) {
      setMessage(failure.message || 'Não foi possível baixar o PDF.');
    } finally {
      if (objectUrl) setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      setPdfBusy(false);
    }
  }

  return (
    <section className="company-share card" aria-label="Compartilhar consulta">
      <div className="company-share-row">
        <div><b>Compartilhar consulta</b><small>{id ? 'Compartilhe a análise salva ou baixe o resumo em PDF.' : 'Baixe o relatório executivo em PDF desta análise.'}</small></div>
        <div className="company-share-buttons">
          <button type="button" className="secondary" onClick={copyLink} disabled={!id}><Share2 size={16} /> Compartilhar link</button>
          <button type="button" className="secondary" onClick={downloadPdf} disabled={!data || pdfBusy}><Download size={16} /> {pdfBusy ? 'Gerando PDF...' : 'Baixar PDF'}</button>
        </div>
      </div>
      {message && <p className="company-share-message" role="status">{message}</p>}
      {manualUrl && <input className="company-share-url" aria-label="Link da consulta" value={manualUrl} readOnly onFocus={(e) => e.target.select()} />}
      {id && data.scanStatus !== 'COMPLETED' && <small>Varredura ainda não concluída: o resumo pode mudar após a atualização da consulta.</small>}
    </section>
  );
}

export default function App() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const { page, searchId } = route;
  const [mobileOpen, setMobileOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [history, setHistory] = useState([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [sharedError, setSharedError] = useState(null);
  const [reloadShared, setReloadShared] = useState(0);
  const needsSharedData = Boolean(searchId && validSearchId(data?.searchId) !== searchId);
  const visibleData = needsSharedData ? null : data;
  const [catalog, setCatalog] = useState({ term: '', items: [], busy: false, error: '', unavailable: false, suggestion: '' });
  const [analyzing, setAnalyzing] = useState('');
  const activeSearch = useRef(null);
  const [scanProgress, setScanProgress] = useState(null);
  function cancelSearch() {
    activeSearch.current?.abort();
    activeSearch.current = null;
    setScanProgress(null);
    setBusy(false);
    setAnalyzing('');
  }
  useEffect(() => () => activeSearch.current?.abort(), []);

  function navigate(destination, id = searchId ?? data?.searchId) {
    if (!Object.hasOwn(PAGE_LABELS, destination)) return;
    const hash = companyHash(destination, id);
    setRoute(parseRoute(hash));
    window.location.hash = hash;
  }

  useEffect(() => {
    const syncHash = () => {
      const next = parseRoute(window.location.hash);
      if (activeSearch.current?.searchId && next.searchId && next.searchId !== activeSearch.current.searchId) cancelSearch();
      setRoute(next);
    };
    window.addEventListener('hashchange', syncHash);
    return () => window.removeEventListener('hashchange', syncHash);
  }, []);

  useEffect(() => {
    if (!needsSharedData) return;
    const controller = new AbortController();
    setSharedError(null);
    request(`/history/${searchId}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        const normalized = normalize(result);
        if (!normalized || validSearchId(normalized.searchId) !== searchId) {
          throw new Error('A consulta salva não contém dados válidos.');
        }
        setData(normalized);
        setQuery(normalized.name);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setSharedError({ id: searchId, message: failure.message });
      });
    return () => controller.abort();
  }, [searchId, needsSharedData, reloadShared]);

  // Polling automático para atualizar tela quando o Deep Scan em segundo plano finalizar
  useEffect(() => {
    if (scanProgress?.running || !data?.searchId || !['PARTIAL_FAST_PATH', 'SCANNING_BACKGROUND', 'BROWSER_PENDING'].includes(data?.scanStatus)) return;
    let active = true;
    const intervalId = setInterval(async () => {
      try {
        const statusRes = await request(`/search_status/${data.searchId}`);
        if (active && (statusRes?.is_terminal || statusRes?.is_completed) && statusRes.data) {
          const updated = normalize(statusRes.data);
          if (updated) {
            setData((prev) => validSearchId(prev?.searchId) !== validSearchId(data.searchId) ? prev : ({
              ...updated,
              idoneidadeEconomica: updated.idoneidadeEconomica || prev?.idoneidadeEconomica,
              logo: updated.logo || prev?.logo,
              rankingCondenacoes: (updated.rankingCondenacoes && updated.rankingCondenacoes.length > 0)
                ? updated.rankingCondenacoes
                : prev?.rankingCondenacoes || [],
              condenacaoPorMotivo: (updated.condenacaoPorMotivo && updated.condenacaoPorMotivo.length > 0)
                ? updated.condenacaoPorMotivo
                : prev?.condenacaoPorMotivo || [],
            }));
          }
          clearInterval(intervalId);
        }
      } catch (err) {
        console.debug('Polling background scan status:', err);
      }
    }, 3000);

    return () => { active = false; clearInterval(intervalId); };
  }, [data?.searchId, data?.scanStatus, scanProgress?.running]);

  async function search(term = query, legalName = null) {
    const text = String(term || '').trim();
    if (text.length < 2) return;
    activeSearch.current?.abort();
    const controller = new AbortController();
    activeSearch.current = controller;
    const { signal } = controller;
    const current = () => activeSearch.current === controller && !signal.aborted;
    setScanProgress(null);
    setBusy(true);
    setError('');
    try {
      const cnpj = onlyDigits(text).length === 14 ? onlyDigits(text) : null;
      // O endpoint público pesquisa pelo nome da parte. Para CNPJ, resolve o nome
      // cadastral antes de consultar o PJe, sem trocar o CNPJ enviado ao backend.
      let pjeTerm = cnpj ? legalName : text;
      if (cnpj && !pjeTerm) {
        try {
          const registration = await fetch(`https://brasilapi.com.br/api/cnpj/v1/${cnpj}`, {
            signal: AbortSignal.any([signal, AbortSignal.timeout(3000)]),
          });
          if (registration.ok) pjeTerm = (await registration.json()).razao_social;
        } catch { /* sem nome: mantém o fallback original do servidor */ }
      }
      const first = pjeTerm && !signal.aborted
        ? await fetchPjePage(pjeTerm, cnpj, 1, signal, 4000).catch(() => ({ items: [], count: null }))
        : { items: [], count: null };
      if (!current()) return;
      const payload = { company_name: text, browser_deep_scan: true,
        browser_query_term: pjeTerm, browser_page1_count: first.count };
      if (first.items.length) payload.client_prefetched_items = first.items;
      const result = await request('/search-company', {
        method: 'POST', signal, body: JSON.stringify(payload),
      });
      if (!current()) return;
      const normalized = normalize(result);
      if (!normalized) throw new Error('A API não retornou dados de empresa válidos.');
      controller.searchId = validSearchId(normalized.searchId);
      setData(normalized);
      navigate('overview', normalized.searchId);
      setBusy(false);
      setAnalyzing('');
      if (!validSearchId(normalized.searchId) || normalized.scanStatus !== 'BROWSER_PENDING') return;
      setScanProgress({ running: true, message: `Consolidando varredura: ${first.items.length}/${Math.min(first.count, 1000)} comunicações...` });
      try {
        const scan = await runBrowserDeepScan(pjeTerm, cnpj, first.count, progress => {
          if (current()) setScanProgress({ running: true, message: `Consolidando varredura: ${progress.received}/${progress.total} comunicações...` });
        }, { signal, page1Items: first.items });
        if (!current()) return;
        {
          await request('/ingest-deep-scan', {
            method: 'POST', signal, body: JSON.stringify({ search_id: Number(normalized.searchId), items: scan.items, browser_scan_finished: true, is_final_batch: scan.failedPages.length === 0 && scan.received >= first.count && first.count <= 1000 }),
          });
          if (!current()) return;
          const updated = normalize(await request(`/history/${normalized.searchId}`, { signal }));
          if (current() && validSearchId(updated?.searchId) === validSearchId(normalized.searchId)) {
            setData(prev => validSearchId(prev?.searchId) === validSearchId(normalized.searchId) ? updated : prev);
          }
        }
        if (current()) setScanProgress({ running: false, message: scan.failedPages.length || (first.count <= 1000 && scan.received < first.count) ? 'Varredura parcial: nem todas as comunicações foram coletadas.' : first.count > 1000 ? 'Varredura limitada às primeiras 10 páginas.' : 'Varredura do navegador concluída.' });
      } catch {
        if (current()) setScanProgress({ running: false, message: 'Não foi possível consolidar toda a varredura. Os resultados iniciais foram preservados.' });
      }
    } catch (failure) {
      if (current()) setError(failure.message || 'Não foi possível pesquisar a empresa.');
    } finally {
      if (current()) { setBusy(false); setAnalyzing(''); }
    }
  }

  // Nome → catálogo para o usuário escolher. CNPJ completo → análise direta.
  function submitSearch(override) {
    const text = (typeof override === 'string' ? override : query).trim();
    if (text.length < 3) return;
    if (onlyDigits(text).length === 14) {
      search(onlyDigits(text));
      return;
    }
    cancelSearch();
    navigate('catalog');
    loadCatalog(text);
  }

  async function loadCatalog(term) {
    setError('');
    setCatalog({ term, items: [], busy: true, error: '', unavailable: false, suggestion: '' });
    try {
      const result = await request(`/company-catalog?q=${encodeURIComponent(term)}&limit=100`);
      const { items, suggestion } = normalizeCatalog(result);
      setCatalog({ term, items, busy: false, error: '', unavailable: false, suggestion });
    } catch (failure) {
      // 404/405/501 sem corpo JSON = rota inexistente no backend. 404 com JSON = busca sem resultados.
      const routeMissing = [404, 405, 501].includes(failure.status) && !failure.hasBody;
      const notFound = failure.status === 404 && failure.hasBody;
      setCatalog({
        term, items: [], busy: false, unavailable: routeMissing, suggestion: '',
        error: routeMissing || notFound ? '' : (failure.message || 'Não foi possível consultar o catálogo.'),
      });
    }
  }

  // Seleção no catálogo: o CNPJ escolhido isola exatamente aquela empresa (modo STRICT_CNPJ).
  async function analyzeCatalogItem(item) {
    setAnalyzing(item.cnpj);
    // Envia o CNPJ (não a razão social): é ele que isola a empresa escolhida no backend.
    await search(item.cnpj, item.razao);
  }

  // Marca inteira (modo BROAD_BRAND): consolida todas as empresas e filiais do nome pesquisado.
  async function analyzeGroup() {
    setAnalyzing('GROUP');
    await search(catalog.term);
  }

  async function loadHistory() {
    setHistoryBusy(true);
    setHistoryError('');
    try {
      const result = await request('/history?limit=50');
      setHistory(Array.isArray(result) ? result : Array.isArray(result.items) ? result.items : Array.isArray(result.history) ? result.history : []);
    } catch (failure) {
      setHistoryError(failure.message || 'Falha ao carregar histórico.');
    } finally {
      setHistoryBusy(false);
    }
  }

  async function openHistory(id) {
    cancelSearch();
    setHistoryBusy(true);
    setHistoryError('');
    try {
      const result = await request(`/history/${encodeURIComponent(id)}`);
      const normalized = normalize(result);
      if (!normalized) throw new Error('A consulta salva não contém dados válidos.');
      setData(normalized);
      setQuery(normalized.name);
      navigate('overview', normalized.searchId);
    } catch (failure) {
      setHistoryError(failure.message || 'Não foi possível abrir a pesquisa.');
    } finally {
      setHistoryBusy(false);
    }
  }

  useEffect(() => {
    if (page === 'history' || page === 'compare') loadHistory();
  }, [page]);

  return (
    <div className="app">
      <Sidebar page={page}navigate={navigate}mobileOpen={mobileOpen}setMobileOpen={setMobileOpen}showCatalog={Boolean(catalog.term)}/>
      <div className="main">
        <header className="topbar">
          <div className="topbar-left">
            <button className="menu-button" onClick={() => setMobileOpen(true)} aria-label="Abrir menu"><Menu size={23} /></button>
            <span className="breadcrumb">
              <span className="breadcrumb-brand">RadarJus</span>
              <ChevronRight size={14} className="breadcrumb-sep" />
              <b>{PAGE_LABELS[page]}</b>
            </span>
          </div>
          <div className="region"><span className="region-dot" /><span className="region-text"> CNJ DataJud · Comunica PJe</span></div>
        </header>
        <main className="content">
          {page === 'search' && <SearchPage onSearch={submitSearch} query={query} setQuery={setQuery} busy={busy} error={error} navigate={navigate} result={data} />}
          {page === 'catalog' && (
            <CatalogPage
              query={query} setQuery={setQuery} catalog={catalog} onSubmit={submitSearch}
              onAnalyze={analyzeCatalogItem} onAnalyzeGroup={analyzeGroup}
              analyzing={analyzing} analysisBusy={busy} analysisError={error}
            />
          )}
          {isCompanyPage(page) && needsSharedData && (
            sharedError?.id === searchId ? (
              <div className="alert error" role="alert">
                <AlertCircle size={18} /> {sharedError.message}
                <button className="secondary" onClick={() => { setSharedError(null); setReloadShared((n) => n + 1); }}>Tentar novamente</button>
              </div>
            ) : <div className="loading" role="status"><RefreshCw className="spin" /> Carregando consulta compartilhada...</div>
          )}
          {isCompanyPage(page) && !needsSharedData && <>
            {scanProgress && <p role="status" className="company-share-message">{scanProgress.message}</p>}
            {visibleData && <CompanyShareActions key={visibleData.searchId ?? visibleData.name} data={visibleData} />}
            {page === 'overview' && <Overview data={visibleData} navigate={navigate} catalogTerm={catalog.term} />}
            {page === 'processes' && <Processes data={visibleData} />}
            {page === 'predictions' && <Predictions data={visibleData} />}
          </>}
          {page === 'compare' && (
            <ComparePage
              request={request}
              history={history}
              currentCompany={visibleData}
              navigate={navigate}
            />
          )}
          {page === 'history' && <HistoryPage history={history} busy={historyBusy} error={historyError} onOpen={openHistory} onReload={loadHistory} />}
        </main>
        <footer className="footer">RadarJus · Inteligência Preditiva</footer>
      </div>
    </div>
  );
}
