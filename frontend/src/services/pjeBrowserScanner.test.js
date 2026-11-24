import test from 'node:test';
import assert from 'node:assert/strict';
import { runBrowserDeepScan, fetchPjePage } from './pjeBrowserScanner.js';
const item = n => ({ numeroProcesso: String(n).padStart(20, '0') });
test('páginas 2 a 10, máximo de três concorrentes, deduplicação e progresso', async () => {
  let active = 0, max = 0; const pages = [], progress = [];
  const result = await runBrowserDeepScan('Empresa', null, 903, p => progress.push(p), { page1Items: [item(1)], fetchPage: async (_, __, page) => {
    pages.push(page); active++; max = Math.max(max, active);
    await new Promise(r => setTimeout(r, 2)); active--;
    return { items: [item(1), item(page)] };
  }});
  assert.deepEqual(pages, [2,3,4,5,6,7,8,9,10]); assert.equal(max,3); assert.equal(result.items.length,9); assert.equal(progress.length,3);
});
test('falha da página 5 não descarta as outras', async () => {
  const result = await runBrowserDeepScan('Empresa', null, 700, null, { fetchPage: async (_, __, page) => {
    if (page === 5) throw Error('falha'); return { items: [item(page)] };
  }});
  assert.deepEqual(result.failedPages,[5]); assert.equal(result.items.length,5);
});
test('cancelamento interrompe novos lotes', async () => {
  const controller = new AbortController(); const pages = [];
  await assert.rejects(runBrowserDeepScan('Empresa', null, 903, null, { signal: controller.signal, fetchPage: async (_, __, page) => { pages.push(page); controller.abort(); return { items: [] }; } }), { name: 'AbortError' });
  assert.deepEqual(pages,[2,3,4]);
});
test('fallback de rede usa proxy e mantém parâmetros', async t => {
  const calls=[]; t.mock.method(globalThis,'fetch',async url => { calls.push(url); if(calls.length===1) throw new TypeError('network'); return {ok:true,json:async()=>({count:903,items:[item(2)]})}; });
  const result=await fetchPjePage('Empresa', '12345678000199',2); assert.equal(result.count,903); assert.match(calls[0],/nomeParte=Empresa/); assert.doesNotMatch(calls[0],/numeroDocumento/); assert.match(calls[1],/^\/api\/cnj-proxy/);
});
test('rate limit não aciona proxy', async t => {
  const mock=t.mock.method(globalThis,'fetch',async()=>({ok:false,status:429}));
  await assert.rejects(fetchPjePage('Empresa',null,2),/429/); assert.equal(mock.mock.callCount(),1);
});
test('não pagina quando count é até 100', async () => {
  const result=await runBrowserDeepScan('Empresa',null,100,null,{fetchPage:()=>{throw Error('unexpected');}}); assert.deepEqual(result.items,[]); assert.equal(result.totalPages,1);
});

test('CNPJ sem razão social não dispara busca genérica no PJe', async t => {
  const mock = t.mock.method(globalThis, 'fetch', async () => { throw Error('unexpected'); });
  await assert.rejects(fetchPjePage('12345678000199', '12345678000199', 1), /Razão social/);
  assert.equal(mock.mock.callCount(), 0);
});
