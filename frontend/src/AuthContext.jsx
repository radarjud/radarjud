import {
  createContext,
  useState,
  useEffect,
  useCallback,
  useMemo,
  useContext,
} from 'react';

import {
  AUTH_KEY,
  EVENTS,
  apiFetch,
  apiRequest,
  clearAuth,
  clearReturnTo,
  readAuth,
  takeSessionNotice,
  writeAuth,
} from './apiClient.js';

import {
  EMPTY_QUOTA,
  mergeQuota,
  normalizeSession,
} from './Session.js';

const AuthContext = createContext(null);

const anonymousState = () => ({
  status: 'anonymous',
  user: null,
  tenant: null,
  quota: { ...EMPTY_QUOTA },
});

function initialState() {
  const stored = readAuth();

  if (!stored?.access_token && !stored?.refresh_token) {
    return anonymousState();
  }

  // Mantém o último perfil salvo enquanto a API confirma a sessão.
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

  // Aplica os dados atualizados do perfil.
  const applyProfile = useCallback((body) => {
    const previous = readAuth();

    if (previous) {
      writeAuth(
        {
          ...previous,
          user: body.user,
          tenant: body.tenant,
        },
        previous.remember
      );
    }

    const next = normalizeSession(body);

    setState((current) => ({
      status: 'authenticated',
      user: next.user,
      tenant: next.tenant,
      quota: mergeQuota(current.quota, next.quota),
    }));
  }, []);

  // Confirma a sessão salva ao abrir ou recarregar a página.
  useEffect(() => {
    if (state.status !== 'loading') return undefined;

    let cancelled = false;

    apiRequest('/auth/me')
      .then((body) => {
        if (!cancelled) {
          applyProfile(body);
        }
      })
      .catch((failure) => {
        if (cancelled) return;

        const rejected =
          !readAuth() ||
          failure?.status === 401 ||
          failure?.status === 403;

        if (rejected) {
          clearAuth();
          setState(anonymousState());
        } else {
          // Se a API estiver indisponível, mantém o perfil em cache.
          setState((current) =>
            current.user
              ? { ...current, status: 'authenticated' }
              : anonymousState()
          );
        }
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Escuta eventos de sessão, cotas e alterações em outras abas.
  useEffect(() => {
    const onExpired = (event) => {
      setNotice(event.detail?.message || '');
      setState(anonymousState());
    };

    const onQuota = (event) => {
      setState((current) => ({
        ...current,
        quota: mergeQuota(current.quota, event.detail),
      }));
    };

    const onStorage = (event) => {
      if (event.key === AUTH_KEY && !event.newValue && !readAuth()) {
        setState(anonymousState());
      }
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

  // Salva os dados de autenticação.
  const persist = useCallback((body, remember) => {
    if (!body?.access_token || !body?.user) {
      throw new Error(
        'A API não retornou os dados de acesso esperados.'
      );
    }

    writeAuth(body, remember);

    const next = normalizeSession(body);

    setNotice('');

    setState({
      status: 'authenticated',
      user: next.user,
      tenant: next.tenant,
      quota: next.quota,
    });
  }, []);

  // Login do usuário.
  const login = useCallback(
    async ({ email, password, remember = false }) => {
      const body = await apiRequest('/auth/login', {
        method: 'POST',
        auth: false,
        body: JSON.stringify({
          email,
          password,
        }),
      });

      persist(body, remember);
    },
    [persist]
  );

  // Cadastro da organização e do usuário administrador.
  const register = useCallback(
    async ({
      name,
      email,
      company,
      cnpj,
      password,
      remember = false,
    }) => {
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
    },
    [persist]
  );

  // Logout do usuário.
  const logout = useCallback(async () => {
    try {
      await apiFetch('/auth/logout', { method: 'POST' });
    } catch {
      // Ignora falhas de rede para não impedir a saída do usuário
    } finally {
      clearAuth();
      clearReturnTo();
      setNotice('');

      // Redireciona para o login.
      window.location.hash = '#/login';

      setState(anonymousState());
    }
  }, []);

  // Atualiza os dados do perfil e as cotas.
  const refreshProfile = useCallback(async () => {
    if (!readAuth()) return;

    try {
      const body = await apiRequest('/auth/me');
      applyProfile(body);
    } catch {
      // Mantém os dados atuais caso a API esteja indisponível.
    }
  }, [applyProfile]);

  // Valores disponibilizados pelo contexto.
  const value = useMemo(
    () => ({
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
    }),
    [
      state,
      notice,
      login,
      register,
      logout,
      refreshProfile,
    ]
  );

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

// Hook para acessar os dados de autenticação.
export function useAuth() {
  const context = useContext(AuthContext);

  if (!context) {
    throw new Error(
      'useAuth deve ser usado dentro de <AuthProvider>.'
    );
  }

  return context;
}