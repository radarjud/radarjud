const BROWSER_PAGE_LIMIT = 50; // Limite oficial da API do Comunica PJe por requisição (US-23)
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
  // Limite estrito oficial da API do CNJ é no máximo 50 itens por página (US-23)
  url.searchParams.set('itensPorPagina', String(BROWSER_PAGE_LIMIT));
  const options = { signal: controller.signal, headers: { Accept: 'application/json' } };
  try {
    let response;
    try {
      response = await fetch(url.toString(), options);
      if (response.status === 429) {
        throw new Error('Rate limit 429');
      }
    } catch (error) {
      controller.signal.throwIfAborted();
      if (error?.message === 'Rate limit 429') {
        throw new Error('PJe HTTP 429');
      }
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
  const totalPages = Math.min(Math.ceil(page1Count / 100), MAX_PAGES);
  const seen = new Set(page1Items.map(processKey).filter(Boolean));
  const items = [];
  const failedPages = [];
  let received = page1Items.length;

  for (let start = 2; start <= totalPages; start += 3) {
    signal?.throwIfAborted();
    const pages = Array.from({ length: Math.min(3, totalPages - start + 1) }, (_, i) => start + i);
    const results = await Promise.allSettled(pages.map(page => fetchPage(termo, cnpj, page, signal)));
    signal?.throwIfAborted();

    let stopEarly = false;
    results.forEach((result, i) => {
      if (result.status === 'rejected') {
        failedPages.push(pages[i]);
        return;
      }
      const pageItems = result.value?.items || [];
      received += pageItems.length;

      let newCount = 0;
      for (const item of pageItems) {
        const key = processKey(item);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        items.push(item);
        newCount++;
      }

      // Freio anti-loop determinístico: se a página retornou itens mas nenhum inédito, encerra
      if (pageItems.length > 5 && newCount === 0) {
        stopEarly = true;
      }
    });

    onProgress?.({
      received,
      total: Math.min(page1Count, MAX_PAGES * 100),
      unique: seen.size,
      failedPages: [...failedPages],
    });

    if (stopEarly) {
      break;
    }
  }

  const isCompleted = failedPages.length === 0;
  return { items, failedPages, received, totalPages, isCompleted };
}
