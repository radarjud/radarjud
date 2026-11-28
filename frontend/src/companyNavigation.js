const COMPANY_PAGES = new Set(['overview', 'processes', 'predictions']);
const AUTH_PAGES = new Set(['login', 'register']);
const PAGES = new Set(['search', 'catalog', 'overview', 'processes', 'predictions', 'history', 'compare', 'users', 'login', 'register']);

export function isCompanyPage(page) {
  return COMPANY_PAGES.has(page);
}

export function isAuthPage(page) {
  return AUTH_PAGES.has(page);
}

export function validSearchId(value) {
  return /^[1-9]\d*$/.test(String(value ?? '')) && Number.isSafeInteger(Number(value))
    ? String(value) : null;
}

export function isProtectedRoute(page, id) {
  if (isAuthPage(page)) return false;
  if (isCompanyPage(page) && validSearchId(id) !== null) return false;
  return true;
}

export function parseRoute(hash) {
  const [page, id] = hash.replace(/^#\/?/, '').split('/');
  if (!PAGES.has(page)) return { page: 'search', searchId: null };
  return { page, searchId: isCompanyPage(page) ? validSearchId(id) : null };
}

export function companyHash(page, id) {
  const searchId = isCompanyPage(page) ? validSearchId(id) : null;
  return `#/${page}${searchId ? `/${searchId}` : ''}`;
}

export function companyShareUrl(location, id) {
  if (!validSearchId(id)) throw new Error('Consulta não salva.');
  const url = new URL(location.href);
  url.hash = companyHash('overview', id);
  return url.href;
}