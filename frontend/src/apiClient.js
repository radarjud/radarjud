// Camada central de acesso à API do RadarJus.
// - Anexa Authorization: Bearer <token> em toda chamada autenticada.
// - Em HTTP 401, renova o access token via /auth/refresh (uma única renovação por vez) e repete a chamada.
// - Se a sessão não puder ser renovada, limpa as credenciais e redireciona para #/login,
//   guardando a rota original para voltar a ela depois do login.
import { isAuthPage, isProtectedRoute, parseRoute } from './companyNavigation.js';
import { parseQuotaHeader, planLabel } from './Session.js';

// Vite encaminha /api ao Flask. Defina VITE_API_BASE_URL se a API estiver em outro host.
export const API_BASE = (import.meta.env?.VITE_API_BASE_URL || '').replace(/\/$/, '');

export const AUTH_KEY = 'radarjus.auth.v1';
const RETURN_KEY = 'radarjus.returnTo';
const NOTICE_KEY = 'radarjus.sessionNotice';
export const EVENTS = {
  expired: 'radarjus:session-expired',
  quota: 'radarjus:quota',
};

const AUTH_PATHS = ['/auth/login', '/auth/register', '/auth/refresh', '/auth/logout'];

/* ------------------------------ armazenamento ------------------------------ */

function store(kind) {
  try { return window[kind] ?? null; } catch { return null; }
}

// "Manter acesso neste dispositivo" => localStorage. Caso contrário, sessionStorage
// (sobrevive ao F5, mas some ao fechar a aba).
export function readAuth() {
  for (const kind of ['localStorage', 'sessionStorage']) {
    try {
      const raw = store(kind)?.getItem(AUTH_KEY);
      if (!raw) continue;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') return { ...parsed, remember: kind === 'localStorage' };
    } catch { /* armazenamento indisponível ou JSON corrompido */ }
  }
  return null;
}

export function writeAuth(data, remember = false) {
  const keep = store(remember ? 'localStorage' : 'sessionStorage');
  const drop = store(remember ? 'sessionStorage' : 'localStorage');
  const payload = {
    access_token: data.access_token ?? null,
    refresh_token: data.refresh_token ?? null,
    user: data.user ?? null,
    tenant: data.tenant ?? null,
  };
  try {
    drop?.removeItem(AUTH_KEY);
    keep?.setItem(AUTH_KEY, JSON.stringify(payload));
  } catch { /* sem armazenamento: a sessão vale só até o recarregamento */ }
}

export function clearAuth() {
  for (const kind of ['localStorage', 'sessionStorage']) {
    try { store(kind)?.removeItem(AUTH_KEY); } catch { /* ignora */ }
  }
}

export function saveReturnTo(hash) {
  const target = String(hash || '');
  if (isAuthPage(parseRoute(target).page)) return;
  try { store('sessionStorage')?.setItem(RETURN_KEY, target.startsWith('#/') ? target : '#/search'); } catch { /* ignora */ }
}

// Devolve a rota que o usuário tentou abrir antes do login (ou #/search) e esquece o valor.
export function consumeReturnTo() {
  try {
    const target = store('sessionStorage')?.getItem(RETURN_KEY);
    store('sessionStorage')?.removeItem(RETURN_KEY);
    if (target && target.startsWith('#/') && !isAuthPage(parseRoute(target).page)) return target;
  } catch { /* ignora */ }
  return '#/search';
}

export function clearReturnTo() {
  try { store('sessionStorage')?.removeItem(RETURN_KEY); } catch { /* ignora */ }
}

export function takeSessionNotice() {
  try {
    const notice = store('sessionStorage')?.getItem(NOTICE_KEY) || '';
    store('sessionStorage')?.removeItem(NOTICE_KEY);
    return notice;
  } catch { return ''; }
}

/* --------------------------- renovação e expiração --------------------------- */

let refreshing = null;

// 'ok' | 'invalid' (refresh token recusado) | 'network' (sem resposta: não derruba a sessão)
function refreshTokens() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    const auth = readAuth();
    if (!auth?.refresh_token) return 'invalid';
    let response;
    try {
      response = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: auth.refresh_token }),
      });
    } catch {
      return 'network';
    }
    if (!response.ok) return response.status >= 500 ? 'network' : 'invalid';
    const body = await response.json().catch(() => null);
    if (!body?.access_token) return 'invalid';
    writeAuth({
      ...auth,
      access_token: body.access_token,
      refresh_token: body.refresh_token || auth.refresh_token,
    }, auth.remember);
    return 'ok';
  })().finally(() => { refreshing = null; });
  return refreshing;
}

export function expireSession(message = 'Sua sessão expirou. Entre novamente para continuar.') {
  if (!readAuth()) return; // outra chamada simultânea já encerrou a sessão
  clearAuth();
  try { store('sessionStorage')?.setItem(NOTICE_KEY, message); } catch { /* ignora */ }
  window.dispatchEvent(new CustomEvent(EVENTS.expired, { detail: { message } }));
  const { page, searchId } = parseRoute(window.location.hash);
  if (isProtectedRoute(page, searchId)) {
    saveReturnTo(window.location.hash || '#/search');
    window.location.hash = '#/login';
  }
}

/* --------------------------------- chamadas --------------------------------- */

function connectionError() {
  if (!API_BASE && typeof window !== 'undefined' && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
    return new Error('A variável de ambiente VITE_API_BASE_URL não está configurada na Vercel apontando para o Render.');
  }
  return new Error('Não foi possível conectar à API. Se o serviço no Render for gratuito, aguarde cerca de 1 minuto para inicialização (cold start).');
}

function publishQuota(detail) {
  if (detail) window.dispatchEvent(new CustomEvent(EVENTS.quota, { detail }));
}

// Retorna a Response crua (útil para PDF/blob), já com Bearer, refresh automático e controle de sessão.
// options.auth === false => não envia o token (login e cadastro).
export async function apiFetch(path, options = {}, attempt = 0) {
  const { auth: withAuth = true, ...init } = options;
  const headers = { ...init.headers };
  const stored = withAuth ? readAuth() : null;
  if (stored?.access_token) headers.Authorization = `Bearer ${stored.access_token}`;

  let response;
  try {
    response = await fetch(`${API_BASE}/api/v1${path}`, { ...init, headers });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw connectionError();
  }

  publishQuota(parseQuotaHeader(response.headers?.get?.('X-Quota-Remaining')));

  const refreshable = withAuth && stored?.refresh_token && !AUTH_PATHS.some((p) => path.startsWith(p));
  if (response.status === 401 && refreshable) {
    if (attempt === 0) {
      const outcome = await refreshTokens();
      if (outcome === 'ok') return apiFetch(path, options, 1);
      if (outcome === 'invalid') expireSession();
    } else {
      expireSession();
    }
  }
  return response;
}

// Mesmo contrato do antigo request() do App.jsx: devolve o JSON ou lança Error com
// status, hasBody, code e data (corpo da resposta).
export async function apiRequest(path, options = {}) {
  const response = await apiFetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const failure = new Error(body?.error || body?.message || `Erro HTTP ${response.status}`);
    failure.status = response.status;
    failure.hasBody = body !== null;
    failure.code = body?.code ?? null;
    failure.data = body;
    if (failure.code === 'QUOTA_EXCEEDED') {
      publishQuota({
        limit: Number.isFinite(Number(body?.limit)) ? Number(body.limit) : undefined,
        used: Number.isFinite(Number(body?.used)) ? Number(body.used) : undefined,
        remaining: 0,
      });
    }
    throw failure;
  }
  if (body === null) throw new Error('A API não retornou JSON válido.');
  return body;
}

// Mensagem em português, sem jargão, para erros comuns da API.
export function friendlyError(failure, fallback = 'Não foi possível concluir a operação.') {
  if (!failure) return fallback;
  if (failure.code === 'QUOTA_EXCEEDED' || failure.status === 402) {
    const { used, limit, plan } = failure.data || {};
    const hasLimit = limit != null && Number.isFinite(Number(limit));
    const usage = hasLimit
      ? ` Você usou ${used ?? limit} de ${limit} consultas${plan ? ` do plano ${planLabel(plan)}` : ''} neste mês.`
      : '';
    return `Cota mensal de consultas esgotada.${usage} Peça um upgrade de plano ao administrador ou aguarde a renovação no próximo mês.`;
  }
  if (failure.status === 401) return 'Sua sessão expirou. Entre novamente para continuar.';
  if (failure.status === 429) return 'Muitas tentativas em sequência. Aguarde um instante e tente de novo.';
  return failure.message || fallback;
}