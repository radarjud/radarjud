import test from 'node:test';
import assert from 'node:assert/strict';
import {
  businessText,
  subsidiaryRisk,
  isExecutionPhaseLabel,
  separateExecutionPhaseData,
} from './businessLabels.js';

test('traduz termos sem modificar numeros', () => {
  assert.equal(businessText('Dirichlet-Multinomial: 0,7%'), 'Estimativa baseada no histórico: 0,7%');
  assert.doesNotMatch(businessText('Intervalo de Credibilidade Beta (90%)'), /Credibilidade Beta/);
});

test('risco subsidiario nao confunde score ou ausencia com zero', () => {
  assert.equal(subsidiaryRisk({ risco_calote_subsidiario_pct: 0.7, indice_exposicao_financeira: 15.7 }), 0.7);
  assert.equal(subsidiaryRisk({}), null);
  assert.equal(subsidiaryRisk({ risco_calote_subsidiario: 0 }), 0);
});

test('isExecutionPhaseLabel detecta rotulos de execucao normalizando acentos e caixa', () => {
  assert.equal(isExecutionPhaseLabel('Execução de Sentença'), true);
  assert.equal(isExecutionPhaseLabel('CUMPRIMENTO PROVISÓRIO'), true);
  assert.equal(isExecutionPhaseLabel('cumprimento de sentenca trabalhista'), true);
  assert.equal(isExecutionPhaseLabel('Liquidação de Sentença'), true);
  assert.equal(isExecutionPhaseLabel('Carta Precatória Executória'), true);
  assert.equal(isExecutionPhaseLabel('execucao'), true);

  assert.equal(isExecutionPhaseLabel('Horas Extras'), false);
  assert.equal(isExecutionPhaseLabel('Verbas Rescisórias'), false);
  assert.equal(isExecutionPhaseLabel('Cargo Executivo'), false);
  assert.equal(isExecutionPhaseLabel(''), false);
  assert.equal(isExecutionPhaseLabel(null), false);
});

test('separateExecutionPhaseData separa execucao e faz fallback defensivo para cache L1 antigo', () => {
  const legacyResult = separateExecutionPhaseData({
    rankingMotivos: [
      { motivo: 'Execução de Sentença', quantidade: 4, porcentagem: 40 },
      { motivo: 'Horas Extras', quantidade: 6, porcentagem: 60 },
    ],
    rankingCondenacoes: [
      { motivo: 'Execução de Sentença', quantidade: 4, condenacoes: 4 },
      { motivo: 'Horas Extras', quantidade: 3, condenacoes: 3 },
    ],
    condenacaoPorMotivo: [
      { motivo: 'Execução / Cumprimento de Sentença', total_processos: 4, condenacoes: 4 },
      { motivo: 'Horas Extras / Jornada', total_processos: 6, condenacoes: 3 },
    ],
    executionPhaseSummary: null,
    totalProcessos: 10,
  });

  assert.equal(legacyResult.rankingMotivos.length, 1);
  assert.equal(legacyResult.rankingMotivos[0].motivo, 'Horas Extras');
  assert.equal(legacyResult.condenacaoPorMotivo.length, 1);
  assert.equal(legacyResult.condenacaoPorMotivo[0].motivo, 'Horas Extras / Jornada');
  assert.equal(legacyResult.executionPhaseSummary.total_processos, 4);
  assert.equal(legacyResult.executionPhaseSummary.percentual_total, 40);
  assert.ok(legacyResult.executionPhaseSummary.rotulos.includes('Execução de Sentença'));
});
