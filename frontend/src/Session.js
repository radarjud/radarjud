// Helpers puros para transformar a resposta de /auth/* no formato usado pela interface.
// Tolerante a variações de nomes de campo (mesma abordagem do normalizeCatalog),
// já que Tenant.to_dict() e QuotaService podem expor a cota com nomes diferentes.

const isRecord = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

const toNumber = (v) => {
  if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

const firstNumber = (obj, keys) => {
  if (!isRecord(obj)) return null;
  for (const key of keys) {
    const n = toNumber(obj[key]);
    if (n !== null) return n;
  }
  return null;
};

export const EMPTY_QUOTA = Object.freeze({ limit: null, used: null, remaining: null });

const ROLE_LABELS = {
  ADMIN: 'Admin',
  ADMINISTRATOR: 'Admin',
  ANALYST: 'Analista',
  ANALISTA: 'Analista',
  VIEWER: 'Leitor',
};

const PLAN_LABELS = {
  FREE: 'Gratuito',
  GRATUITO: 'Gratuito',
  TRIAL: 'Teste',
  STARTER: 'Starter',
  BASIC: 'Básico',
  BASICO: 'Básico',
  PRO: 'Pro',
  PROFESSIONAL: 'Profissional',
  BUSINESS: 'Business',
  ENTERPRISE: 'Enterprise',
};

const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();

export function roleLabel(role) {
  const key = String(role ?? '').trim().toUpperCase();
  if (!key) return 'Usuário';
  return ROLE_LABELS[key] || capitalize(key);
}

export function planLabel(plan) {
  const key = String(plan ?? '').trim().toUpperCase();
  if (!key) return '';
  return PLAN_LABELS[key] || capitalize(key);
}

export function initialsOf(name, email) {
  const words = String(name ?? '').trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return `${words[0][0]}${words[words.length - 1][0]}`.toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return String(email ?? '??').replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase() || 'RJ';
}

export function normalizeQuota(tenant) {
  if (!isRecord(tenant)) return { ...EMPTY_QUOTA };
  const box = isRecord(tenant.quota) ? tenant.quota : tenant;
  const limit = firstNumber(box, ['limit', 'monthly_quota', 'monthly_limit', 'quota_limit', 'query_limit', 'queries_limit', 'cota_mensal'])
    ?? toNumber(tenant.quota);
  let used = firstNumber(box, ['used', 'quota_used', 'queries_used', 'monthly_used', 'usage', 'consultas_realizadas']);
  let remaining = firstNumber(box, ['remaining', 'quota_remaining']);
  if (used === null && limit !== null && remaining !== null) used = Math.max(0, limit - remaining);
  if (remaining === null && limit !== null && used !== null) remaining = Math.max(0, limit - used);
  return { limit, used, remaining };
}

export function normalizeUser(raw) {
  if (!isRecord(raw)) return null;
  const email = String(raw.email ?? '');
  const name = String(raw.name ?? raw.nome ?? '').trim() || email.split('@')[0];
  return {
    id: raw.id ?? null,
    name,
    email,
    role: String(raw.role ?? '').toUpperCase(),
    roleLabel: roleLabel(raw.role),
    initials: initialsOf(name, email),
  };
}

export function normalizeTenant(raw) {
  if (!isRecord(raw)) return null;
  const plan = raw.plan ?? raw.plano ?? '';
  return {
    id: raw.id ?? null,
    name: String(raw.name ?? raw.nome ?? '').trim(),
    cnpj: raw.cnpj ?? null,
    plan: String(plan),
    planLabel: planLabel(plan),
  };
}

export function normalizeSession(raw) {
  const tenantRaw = isRecord(raw) ? raw.tenant : null;
  return {
    user: normalizeUser(isRecord(raw) ? raw.user : null),
    tenant: normalizeTenant(tenantRaw),
    quota: normalizeQuota(tenantRaw),
  };
}

// Cabeçalho X-Quota-Remaining: aceita "42" (restantes) ou "42/50" (restantes/limite).
export function parseQuotaHeader(value) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  const pair = text.match(/^(\d+)\s*\/\s*(\d+)$/);
  if (pair) return { remaining: Number(pair[1]), limit: Number(pair[2]) };
  if (/^\d+$/.test(text)) return { remaining: Number(text) };
  return null;
}

// Aplica uma atualização parcial (cabeçalho, erro 402, /auth/me) mantendo os números coerentes.
export function mergeQuota(prev, patch) {
  const base = prev || EMPTY_QUOTA;
  const next = patch || {};
  const limit = next.limit ?? base.limit;
  let used = next.used ?? base.used;
  let remaining = next.remaining ?? base.remaining;
  if (limit !== null && limit !== undefined) {
    if (next.remaining != null && next.used == null) used = Math.max(0, limit - next.remaining);
    if (next.used != null && next.remaining == null) remaining = Math.max(0, limit - next.used);
  }
  return { limit: limit ?? null, used: used ?? null, remaining: remaining ?? null };
}

export function quotaTone(quota) {
  if (!quota || !quota.limit) return 'ok';
  const ratio = (quota.used ?? 0) / quota.limit;
  if (ratio >= 1) return 'full';
  if (ratio >= 0.8) return 'warn';
  return 'ok';
}