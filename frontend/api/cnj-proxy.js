export const config = {
  runtime: 'edge',
  regions: ['gru1'],
};

const ALLOWED_CNJ_HOSTS = new Set([
  'comunicaapi.pje.jus.br',
  'api-publica.datajud.cnj.jus.br',
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
    request.headers.get('x-target-url') ||
    reqUrl.searchParams.get('url') ||
    ''
  ).trim();

  if (rawTarget) {
    let parsed;
    try {
      parsed = new URL(rawTarget);
    } catch {
      return null;
    }
    if (parsed.protocol !== 'https:' || !ALLOWED_CNJ_HOSTS.has(parsed.hostname)) {
      return null;
    }
    return parsed.toString();
  }

  const service = (reqUrl.searchParams.get('service') || '').trim().toLowerCase();
  const alias = (reqUrl.searchParams.get('alias') || '').trim();
  if (service === 'datajud' || alias) {
    if (!/^api_publica_[a-z0-9_]+$/i.test(alias)) {
      return null;
    }
    return `${DEFAULT_DATAJUD_BASE_URL}/${alias}/_search`;
  }

  const forwardParams = new URLSearchParams(reqUrl.searchParams);
  forwardParams.delete('url');
  forwardParams.delete('service');
  forwardParams.delete('alias');
  const qs = forwardParams.toString();
  return qs ? `${DEFAULT_COMUNICA_URL}?${qs}` : DEFAULT_COMUNICA_URL;
}

export default async function handler(request) {
  const expectedSecret = (process.env.PROXY_SHARED_SECRET || '').trim();
  const providedSecret = (request.headers.get('x-proxy-secret') || '').trim();

  if (!expectedSecret || !providedSecret || !constantTimeEqual(providedSecret, expectedSecret)) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
      },
    });
  }

  const upstreamUrl = resolveUpstreamUrl(request);
  if (!upstreamUrl) {
    return new Response(JSON.stringify({ error: 'Invalid or disallowed CNJ target URL' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const upstreamHeaders = new Headers();
  for (const headerName of ['accept', 'content-type', 'authorization', 'user-agent']) {
    const val = request.headers.get(headerName);
    if (val) {
      upstreamHeaders.set(headerName, val);
    }
  }
  if (!upstreamHeaders.has('accept')) {
    upstreamHeaders.set('accept', 'application/json');
  }

  const method = request.method.toUpperCase();
  const fetchInit = {
    method,
    headers: upstreamHeaders,
  };
  if (method !== 'GET' && method !== 'HEAD') {
    fetchInit.body = await request.arrayBuffer();
  }

  try {
    const upstreamResponse = await fetch(upstreamUrl, fetchInit);
    const responseHeaders = new Headers();
    const contentType = upstreamResponse.headers.get('content-type');
    if (contentType) {
      responseHeaders.set('Content-Type', contentType);
    }
    responseHeaders.set('Cache-Control', 'no-store');

    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      headers: responseHeaders,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: 'Upstream CNJ request failed', detail: String(err?.message || err) }),
      {
        status: 502,
        headers: { 'Content-Type': 'application/json' },
      }
    );
  }
}
