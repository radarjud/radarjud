import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import {
  AUTH_KEY, EVENTS, apiRequest, clearAuth, clearReturnTo, readAuth, takeSessionNotice, writeAuth,
} from '../services/apiClient.js';
import { EMPTY_QUOTA, mergeQuota, normalizeSession } from '../services/session.js';

const AuthContext = createContext(null);

const anonymousState = () => ({ status: 'anonymous', user: null, tenant: null, quota: { ...EMPTY_QUOTA } });

function initialState() {
  const stored = readAuth();
  if (!stored?.access_token && !stored?.refresh_token) return anonymousState();
  // Mostra o último perfil salvo enquanto /auth/me confirma a sessão (evita piscar após o F5).
  const cached = stored.user ? normalizeSession(stored) : null;
  return {
    status: 'loading',
    user: cached?.user ?? null,
    tenant: cached?.tenant ?? null,
    quota: cached?.quota ?? { ...EMPTY_QUOTA },
  };
}

export function AuthProvider({ children }) {
  const [state, setState] = useState(initialState);
  const [notice, setNotice] = useState(() => takeSessionNotice());

  const applyProfile = useCallback((body) => {
    const previous = readAuth();
    if (previous) writeAuth({ ...previous, user: body.user, tenant: body.tenant }, previous.remember);
    const next = normalizeSession(body);
    setState((s) => ({
      status: 'authenticated',
      user: next.user,
      tenant: next.tenant,
      quota: mergeQuota(s.quota, next.quota),
    }));
  }, []);

  // Confirma a sessão salva ao abrir/recarregar a página.
  useEffect(() => {
    if (state.status !== 'loading') return undefined;
    let cancelled = false;
    apiRequest('/auth/me')
      .then((body) => { if (!cancelled) applyProfile(body); })
      .catch((failure) => {
        if (cancelled) return;
        const rejected = !readAuth() || failure?.status === 401 || failure?.status === 403;
        if (rejected) {
          clearAuth();
          setState(anonymousState());
        } else {
          // API fora do ar: mantém o perfil salvo em vez de derrubar a sessão.
          setState((s) => (s.user ? { ...s, status: 'authenticated' } : anonymousState()));
        }
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onExpired = (event) => {
      setNotice(event.detail?.message || '');
      setState(anonymousState());
    };
    const onQuota = (event) => setState((s) => ({ ...s, quota: mergeQuota(s.quota, event.detail) }));
    const onStorage = (event) => {
      if (event.key === AUTH_KEY && !event.newValue && !readAuth()) setState(anonymousState());
    };
    window.addEventListener(EVENTS.expired, onExpired);
    window.addEventListener(EVENTS.quota, onQuota);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(EVENTS.expired, onExpired);
      window.removeEventListener(EVENTS.quota, onQuota);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const persist = useCallback((body, remember) => {
    if (!body?.access_token || !body?.user) throw new Error('A API não retornou os dados de acesso esperados.');
    writeAuth(body, remember);
    const next = normalizeSession(body);
    setNotice('');
    setState({ status: 'authenticated', user: next.user, tenant: next.tenant, quota: next.quota });
  }, []);

  const login = useCallback(async ({ email, password, remember = false }) => {
    const body = await apiRequest('/auth/login', {
      method: 'POST',
      auth: false,
      body: JSON.stringify({ email, password }),
    });
    persist(body, remember);
  }, [persist]);

  // O backend cria a organização + usuário ADMIN e já devolve os tokens: login automático.
  const register = useCallback(async ({ name, email, company, cnpj, password, remember = false }) => {
    const digits = String(cnpj || '').replace(/\D/g, '');
    const body = await apiRequest('/auth/register', {
      method: 'POST',
      auth: false,
      body: JSON.stringify({
        name,
        email,
        password,
        tenant_name: company,
        ...(digits ? { cnpj: digits } : {}),
      }),
    });
    persist(body, remember);
  }, [persist]);

  const logout = useCallback(() => {
    clearAuth();
    clearReturnTo();
    setNotice('');
    // Troca o hash antes do estado, para a rota atual não ser guardada como "voltar para".
    window.location.hash = '#/login';
    setState(anonymousState());
  }, []);

  // Atualiza plano e cota (ex.: depois de uma consulta que consome cota).
  const refreshProfile = useCallback(async () => {
    if (!readAuth()) return;
    try { applyProfile(await apiRequest('/auth/me')); } catch { /* mantém os valores atuais */ }
  }, [applyProfile]);

  const value = useMemo(() => ({
    status: state.status,
    isAuthenticated: state.status === 'authenticated',
    user: state.user,
    tenant: state.tenant,
    quota: state.quota,
    notice,
    clearNotice: () => setNotice(''),
    login,
    register,
    logout,
    refreshProfile,
  }), [state, notice, login, register, logout, refreshProfile]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth deve ser usado dentro de <AuthProvider>.');
  return context;
}