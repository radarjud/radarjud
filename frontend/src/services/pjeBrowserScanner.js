const PAGE_SIZE = 100;
const MAX_PAGES = 10;
const BASE = 'https://comunicaapi.pje.jus.br/api/v1/comunicacao';

export function processKey(item) {
  const digits = String(item?.numero_processo || item?.numeroProcesso || item?.numeroprocessocommascara || '').replace(/\D/g, '');
  return digits.length === 20 ? digits : null;
}

export async function fetchPjePage(termo, cnpj, page, signal, timeout = 6000) {
  signal?.throwIfAborted();
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => controller.abort(), timeout);
  const url = new URL(BASE);
  if (!termo || !String(termo).trim() || /^\d{14}$/.test(String(termo).replace(/\D/g, ''))) {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    throw new Error('Razão social ou nome empresarial obrigatório para consultar o PJe');
  }
  url.searchParams.set('nomeParte', termo);
  url.searchParams.set('pagina', String(page));
  url.searchParams.set('itensPorPagina', String(PAGE_SIZE));
  const options = { signal: controller.signal, headers: { Accept: 'application/json' } };
  try {
    let response;
    try {
      response = await fetch(url.toString(), options);
    } catch (error) {
      controller.signal.throwIfAborted();
      // Fallback de conectividade/CORS, não para respostas de bloqueio ou rate limit.
      response = await fetch(`/api/cnj-proxy?target=${encodeURIComponent(url.toString())}`, options);
    }
    if (!response.ok) throw new Error(`PJe HTTP ${response.status}`);
    const body = await response.json();
    const items = Array.isArray(body) ? body : body?.items ?? body?.itens;
    if (!Array.isArray(items)) throw new Error('Resposta PJe inválida');
    const count = body?.count == null ? NaN : Number(body.count);
    return { items, count: Number.isFinite(count) && count >= 0 ? Math.floor(count) : null };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export async function runBrowserDeepScan(termo, cnpj, page1Count, onProgress, { signal, page1Items = [], fetchPage = fetchPjePage } = {}) {
  const totalPages = Math.min(Math.ceil(page1Count / PAGE_SIZE), MAX_PAGES);
  const seen = new Set(page1Items.map(processKey).filter(Boolean));
  const items = [];
  const failedPages = [];
  let received = page1Items.length;
  for (let start = 2; start <= totalPages; start += 3) {
    signal?.throwIfAborted();
    const pages = Array.from({ length: Math.min(3, totalPages - start + 1) }, (_, i) => start + i);
    const results = await Promise.allSettled(pages.map(page => fetchPage(termo, cnpj, page, signal)));
    signal?.throwIfAborted();
    results.forEach((result, i) => {
      if (result.status === 'rejected') { failedPages.push(pages[i]); return; }
      received += result.value.items.length;
      for (const item of result.value.items) {
        const key = processKey(item);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        items.push(item);
      }
    });
    onProgress?.({ received, total: Math.min(page1Count, MAX_PAGES * PAGE_SIZE), unique: seen.size, failedPages: [...failedPages] });
  }
  return { items, failedPages, received, totalPages };
}
