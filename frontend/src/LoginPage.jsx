import React, { useRef, useState } from 'react';
import { AlertCircle, ArrowRight, Info, RefreshCw, ShieldCheck } from 'lucide-react';
import AuthLayout from './AuthLayout.jsx';
import { CheckField, PasswordField, TextField } from './AuthFields.jsx';
import { useAuth } from './context/AuthContext.jsx';
import { friendlyError } from './services/apiClient.js';
import { validateEmail } from './authValidation.js';

function loginMessage(failure) {
  if (failure?.status === 401) return 'E-mail ou senha incorretos. Confira os dados e tente novamente.';
  if (failure?.status === 403 || failure?.status === 400) return failure.message;
  return friendlyError(failure, 'Não foi possível entrar agora. Tente novamente em instantes.');
}

export default function LoginPage() {
  const { login, notice, clearNotice } = useAuth();
  const [form, setForm] = useState({ email: '', password: '', remember: false });
  const [touched, setTouched] = useState({});
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState('');
  const [showReset, setShowReset] = useState(false);
  const refs = { email: useRef(null), password: useRef(null) };

  const errors = {
    email: validateEmail(form.email),
    password: form.password ? '' : 'Informe sua senha.',
  };
  const shown = (field) => (touched[field] ? errors[field] : '');
  const set = (field) => (event) => {
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    setForm((f) => ({ ...f, [field]: value }));
    if (serverError) setServerError('');
  };
  const blur = (field) => () => setTouched((t) => ({ ...t, [field]: true }));

  async function onSubmit(event) {
    event.preventDefault();
    if (busy) return;
    setTouched({ email: true, password: true });
    const firstInvalid = ['email', 'password'].find((field) => errors[field]);
    if (firstInvalid) { refs[firstInvalid].current?.focus(); return; }
    setBusy(true);
    setServerError('');
    try {
      await login({ email: form.email.trim(), password: form.password, remember: form.remember });
      // Sucesso: o App redireciona para a rota original (ou para a busca).
    } catch (failure) {
      setServerError(loginMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout prompt="Novo por aqui?" linkLabel="Criar conta" linkHref="#/register">
      <form className="auth-form" onSubmit={onSubmit} noValidate>
        <div className="auth-eyebrow">ACESSO À PLATAFORMA</div>
        <h1>Bem-vindo de volta</h1>
        <p className="auth-lead">Informe seus dados para acessar o RadarJus.</p>

        {notice && (
          <div className="info-banner warning auth-notice" role="status">
            <Info size={18} /><div><p>{notice}</p></div>
            <button type="button" className="auth-notice-close" onClick={clearNotice} aria-label="Dispensar aviso">×</button>
          </div>
        )}

        <TextField
          id="login-email"
          label="E-mail"
          type="email"
          inputMode="email"
          autoComplete="username"
          placeholder="seuemail@empresa.com.br"
          value={form.email}
          onChange={set('email')}
          onBlur={blur('email')}
          error={shown('email')}
          inputRef={refs.email}
          autoFocus
        />

        <PasswordField
          id="login-password"
          label="Senha"
          autoComplete="current-password"
          placeholder="Digite sua senha"
          value={form.password}
          onChange={set('password')}
          onBlur={blur('password')}
          error={shown('password')}
          inputRef={refs.password}
          labelAside={(
            <button type="button" className="auth-link" onClick={() => setShowReset((v) => !v)} aria-expanded={showReset}>
              Esqueci minha senha
            </button>
          )}
        />

        {showReset && (
          <div className="info-banner auth-reset" role="status">
            <Info size={18} />
            <div>
              <b>Redefinição de senha</b>
              <p>A recuperação por e-mail ainda não está disponível. Peça ao administrador da sua organização para redefinir o seu acesso.</p>
            </div>
          </div>
        )}

        <CheckField id="login-remember" checked={form.remember} onChange={set('remember')}>
          Manter acesso neste dispositivo
        </CheckField>

        {serverError && <div className="alert error auth-alert" role="alert"><AlertCircle size={18} />{serverError}</div>}

        <button type="submit" className="primary auth-submit" disabled={busy}>
          {busy ? <><RefreshCw size={17} className="spin" /> Entrando</> : <>Entrar na plataforma <ArrowRight size={17} /></>}
        </button>

        <p className="auth-secure"><ShieldCheck size={15} /> Ambiente seguro e dados protegidos</p>
      </form>
    </AuthLayout>
  );
}