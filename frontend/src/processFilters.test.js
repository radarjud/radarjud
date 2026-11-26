import test from 'node:test';
import assert from 'node:assert/strict';
import {EMPTY_FILTERS,TRIBUNALS,validateFilters,processQuery,advancedFiltersConfirmed} from './processFilters.js';
test('lista de tribunais completa',()=>{assert.equal(TRIBUNALS.length,25);assert.equal(TRIBUNALS.at(-1),'TRT24');});
test('combina filtros e paginação',()=>{const p=new URLSearchParams(processQuery({...EMPTY_FILTERS,tribunal:'TST',date_from:'2026-01-01',value_min:'0',search:'ação'},2));assert.equal(p.get('tribunal'),'TST');assert.equal(p.get('date_from'),'2026-01-01');assert.equal(p.get('value_min'),'0');assert.equal(p.get('search'),'ação');assert.equal(p.get('page'),'2');});
test('limpar restaura consulta sem filtros',()=>assert.equal(processQuery(EMPTY_FILTERS),'page=1&per_page=20'));
test('intervalos inválidos',()=>{assert.ok(validateFilters({...EMPTY_FILTERS,date_from:'2026-02-01',date_to:'2026-01-01'}));assert.ok(validateFilters({...EMPTY_FILTERS,value_min:'10',value_max:'9'}));assert.ok(validateFilters({...EMPTY_FILTERS,value_min:'-1'}));assert.equal(validateFilters({...EMPTY_FILTERS,value_min:'0'}),'');});
test('não aceita resposta sem confirmação dos filtros US-08',()=>{const f={...EMPTY_FILTERS,date_from:'2026-01-01',value_min:'0'};assert.equal(advancedFiltersConfirmed(f,{}),false);assert.equal(advancedFiltersConfirmed(f,{applied_filters:{date_from:'2026-01-01',value_min:0}}),true);assert.equal(advancedFiltersConfirmed(EMPTY_FILTERS,{}),true);assert.equal(advancedFiltersConfirmed(f,{applied_filters:{date_from:'2026-01-01',value_min:''}}),false);});
