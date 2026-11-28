import React, { useRef, useState } from 'react';
import { AlertCircle, ArrowRight, RefreshCw } from 'lucide-react';
import AuthLayout from './AuthLayout.jsx';
import { CheckField, PasswordField, PasswordStrength, TextField } from './AuthFields.jsx';
import { useAuth } from './AuthContext.jsx';
import { friendlyError } from './apiClient.js';
import {
  maskCnpj, validateCnpj, validateCompany, validateEmail, validateFullName, validatePassword,
} from './authValidation.js';

// Aponte para as páginas reais com VITE_TERMS_URL e VITE_PRIVACY_URL. Sem elas, o texto aparece sem link.
const TERMS_URL = import.meta.env?.VITE_TERMS_URL || '';
const PRIVACY_URL = import.meta.env?.VITE_PRIVACY_URL || '';

function LegalLink({ href, children }) {
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer"><b>{children}</b></a>
    : <b>{children}</b>;
}

const FIELDS = ['name', 'email', 'company', 'cnpj', 'password', 'terms'];

export default function RegisterPage() {
  const { register } = useAuth();
  const [form, setForm] = useState({ name: '', email: '', company: '', cnpj: '', password: '', terms: false });
  const [touched, setTouched] = useState({});
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState('');
  const [emailTaken, setEmailTaken] = useState('');
  const refs = {
    name: useRef(null), email: useRef(null), company: useRef(null),
    cnpj: useRef(null), password: useRef(null), terms: useRef(null),
  };

  const errors = {
    name: validateFullName(form.name),
    email: validateEmail(form.email, { corporate: true }),
    company: validateCompany(form.company),
    cnpj: validateCnpj(form.cnpj),
    password: validatePassword(form.password),
    terms: form.terms ? '' : 'Aceite os Termos de uso e a Política de privacidade para continuar.',
  };
  const emailError = emailTaken && emailTaken === form.email.trim().toLowerCase()
    ? (<>Este e-mail já tem cadastro. <a href="#/login">Entrar na plataforma</a></>)
    : (touched.email ? errors.email : '');
  const shown = (field) => (field === 'email' ? emailError : (touched[field] ? errors[field] : ''));

  const set = (field) => (event) => {
    let value = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    if (field === 'cnpj') value = maskCnpj(value);
    setForm((f) => ({ ...f, [field]: value }));
    if (serverError) setServerError('');
  };
  const blur = (field) => () => setTouched((t) => ({ ...t, [field]: true }));

  async function onSubmit(event) {
    event.preventDefault();
    if (busy) return;
    setTouched(Object.fromEntries(FIELDS.map((f) => [f, true])));
    const firstInvalid = FIELDS.find((field) => errors[field]);
    if (firstInvalid) { refs[firstInvalid].current?.focus(); return; }
    setBusy(true);
    setServerError('');
    try {
      await register({
        name: form.name.trim().replace(/\s+/g, ' '),
        email: form.email.trim().toLowerCase(),
        company: form.company.trim(),
        cnpj: form.cnpj,
        password: form.password,
      });
      // Sucesso: login automático; o App leva para a busca (ou para a rota que o usuário tentou abrir).
    } catch (failure) {
      if (failure?.status === 409) {
        setEmailTaken(form.email.trim().toLowerCase());
        refs.email.current?.focus();
      } else if (failure?.status === 400) {
        setServerError(failure.message);
      } else {
        setServerError(friendlyError(failure, 'Não foi possível criar a conta agora. Tente novamente em instantes.'));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout prompt="Já possui uma conta?" linkLabel="Entrar" linkHref="#/login">
      <form className="auth-form" onSubmit={onSubmit} noValidate>
        <div className="auth-eyebrow">NOVO CADASTRO</div>
        <h1>Crie sua conta</h1>
        <p className="auth-lead">Preencha os dados abaixo para começar a usar o RadarJus.</p>

        <TextField
          id="reg-name"
          label="Nome completo"
          autoComplete="name"
          placeholder="Digite seu nome completo"
          value={form.name}
          onChange={set('name')}
          onBlur={blur('name')}
          error={shown('name')}
          inputRef={refs.name}
          autoFocus
        />
        <TextField
          id="reg-email"
          label="E-mail"
          type="email"
          inputMode="email"
          autoComplete="email"
          placeholder="seuemail@empresa.com.br"
          hint="Use seu e-mail corporativo."
          value={form.email}
          onChange={set('email')}
          onBlur={blur('email')}
          error={shown('email')}
          inputRef={refs.email}
        />
        <TextField
          id="reg-company"
          label="Empresa"
          autoComplete="organization"
          placeholder="Nome da sua empresa"
          value={form.company}
          onChange={set('company')}
          onBlur={blur('company')}
          error={shown('company')}
          inputRef={refs.company}
        />
        <TextField
          id="reg-cnpj"
          label="CNPJ da empresa contratante"
          labelAside={<span className="auth-optional">Opcional</span>}
          inputMode="numeric"
          autoComplete="off"
          placeholder="00.000.000/0000-00"
          value={form.cnpj}
          onChange={set('cnpj')}
          onBlur={blur('cnpj')}
          error={shown('cnpj')}
          inputRef={refs.cnpj}
        />
        <PasswordField
          id="reg-password"
          label="Senha"
          autoComplete="new-password"
          placeholder="Mínimo de 8 caracteres"
          value={form.password}
          onChange={set('password')}
          onBlur={blur('password')}
          error={shown('password')}
          inputRef={refs.password}
          below={<PasswordStrength value={form.password} />}
        />

        <CheckField id="reg-terms" checked={form.terms} onChange={set('terms')} error={shown('terms')} inputRef={refs.terms}>
          Li e concordo com os <LegalLink href={TERMS_URL}>Termos de uso</LegalLink> e a <LegalLink href={PRIVACY_URL}>Política de privacidade</LegalLink>.
        </CheckField>

        {serverError && <div className="alert error auth-alert" role="alert"><AlertCircle size={18} />{serverError}</div>}

        <button type="submit" className="primary auth-submit" disabled={busy}>
          {busy ? <><RefreshCw size={17} className="spin" /> Criando conta</> : <>Criar minha conta <ArrowRight size={17} /></>}
        </button>
      </form>
    </AuthLayout>
  );
}