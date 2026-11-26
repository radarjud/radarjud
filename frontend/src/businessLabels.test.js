import test from 'node:test';
import assert from 'node:assert/strict';
import {businessText,subsidiaryRisk} from './businessLabels.js';
test('traduz termos sem modificar numeros',()=>{assert.equal(businessText('Dirichlet-Multinomial: 0,7%'),'Estimativa baseada no histórico: 0,7%');assert.doesNotMatch(businessText('Intervalo de Credibilidade Beta (90%)'),/Credibilidade Beta/);});
test('risco subsidiario nao confunde score ou ausencia com zero',()=>{assert.equal(subsidiaryRisk({risco_calote_subsidiario_pct:0.7,indice_exposicao_financeira:15.7}),0.7);assert.equal(subsidiaryRisk({}),null);assert.equal(subsidiaryRisk({risco_calote_subsidiario:0}),0);});
