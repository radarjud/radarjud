export const config = {
  runtime: 'edge',
  regions: ['gru1'], // Forca execucao fisica em Sao Paulo (Guarulhos - Brasil)
};

const ALLOWED_HOSTS = new Set([
  'comunicaapi.pje.jus.br',
  'api-publica.datajud.cnj.jus.br',
  'api.portaldatransparencia.gov.br',
]);

const DEFAULT_COMUNICA_URL = 'https://comunicaapi.pje.jus.br/api/v1/comunicacao';
const DEFAULT_DATAJUD_BASE_URL = 'https://api-publica.datajud.cnj.jus.br';

/**
 * Comparacao segura em tempo constante para prevenir timing attacks.
 */
function constantTimeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') {
    return false;
  }
  const encoder = new TextEncoder();
  const bufA = encoder.encode(a);
  const bufB = encoder.encode(b);
  const maxLen = Math.max(bufA.length, bufB.length);

  let diff = bufA.length ^ bufB.length;
  for (let i = 0; i < maxLen; i++) {
    const byteA = i < bufA.length ? bufA[i] : 0;
    const byteB = i < bufB.length ? bufB[i] : 0;
    diff |= byteA ^ byteB;
  }
  return diff === 0;
}

function resolveUpstreamUrl(request) {
  const reqUrl = new URL(request.url);
  const rawTarget = (
    reqUrl.searchParams.get('target') ||
    request.headers.get('x-target-url') ||
    reqUrl.searchParams.get('url') ||
    ''
  ).trim();

  if (rawTarget) {
    let parsed;
    try {
      parsed = new URL(rawTarget);
    } catch {
      return { url: null, status: 400, error: 'Invalid target URL' };
    }
    if (parsed.protocol !== 'https:' || !ALLOWED_HOSTS.has(parsed.hostname)) {
      return { url: null, status: 400, error: 'Host not allowed by proxy whitelist' };
    }
    return { url: parsed.toString(), status: 200, error: null };
  }

  const service = (reqUrl.searchParams.get('service') || '').trim().toLowerCase();
  const alias = (reqUrl.searchParams.get('alias') || '').trim();
  if (service === 'datajud' || alias) {
    if (!/^api_publica_[a-z0-9_]+$/i.test(alias)) {
      return { url: null, status: 400, error: 'Invalid DataJud alias' };
    }
    return { url: `${DEFAULT_DATAJUD_BASE_URL}/${alias}/_search`, status: 200, error: null };
  }

  const forwardParams = new URLSearchParams(reqUrl.searchParams);
  forwardParams.delete('target');
  forwardParams.delete('url');
  forwardParams.delete('service');
  forwardParams.delete('alias');
  const qs = forwardParams.toString();
  if (qs) {
    return { url: `${DEFAULT_COMUNICA_URL}?${qs}`, status: 200, error: null };
  }
  return { url: null, status: 400, error: 'Missing target URL' };
}

export default async function handler(req) {
  const expectedSecret = (typeof process !== 'undefined' && process.env && process.env.PROXY_SHARED_SECRET)
    ? String(process.env.PROXY_SHARED_SECRET).trim()
    : '';
  const providedSecret = (req.headers.get('x-proxy-secret') || '').trim();

  // Validacao obrigatoria em tempo constante antes de qualquer requisicao externa
  if (!expectedSecret || !providedSecret || !constantTimeEqual(providedSecret, expectedSecret)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
        'Access-Control-Allow-Origin': '*',
      },
    });
  }

  if (req.method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, chave-api-dados, X-Target-Url, x-proxy-secret',
      },
    });
  }

  const resolved = resolveUpstreamUrl(req);
  if (!resolved.url) {
    return new Response(JSON.stringify({ error: resolved.error }), {
      status: resolved.status,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
      },
    });
  }

  const parsedTarget = new URL(resolved.url);
  const forwardHeaders = new Headers();
  forwardHeaders.set(
    'User-Agent',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
  );
  forwardHeaders.set('Accept', 'application/json, text/plain, */*');
  forwardHeaders.set('Accept-Language', 'pt-BR,pt;q=0.9,en-US;q=0.8');

  const authHeader = req.headers.get('authorization');
  if (authHeader) forwardHeaders.set('Authorization', authHeader);

  const cguHeader = req.headers.get('chave-api-dados');
  if (cguHeader) forwardHeaders.set('chave-api-dados', cguHeader);

  const contentType = req.headers.get('content-type');
  if (contentType) forwardHeaders.set('Content-Type', contentType);

  if (parsedTarget.hostname.includes('pje.jus.br')) {
    forwardHeaders.set('Origin', 'https://comunica.pje.jus.br');
    forwardHeaders.set('Referer', 'https://comunica.pje.jus.br/');
  }

  try {
    const method = req.method.toUpperCase();
    const bodyText =
      method !== 'GET' && method !== 'HEAD' ? await req.text() : undefined;

    const upstreamRes = await fetch(parsedTarget.toString(), {
      method,
      headers: forwardHeaders,
      body: bodyText,
    });

    const responseText = await upstreamRes.text();
    return new Response(responseText, {
      status: upstreamRes.status,
      headers: {
        'Content-Type': upstreamRes.headers.get('content-type') || 'application/json',
        'Cache-Control': 'no-store',
        'Access-Control-Allow-Origin': '*',
        'X-Proxy-Region': 'gru1-sp',
      },
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: String(err?.message || err) }), {
      status: 502,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
      },
    });
  }
}
