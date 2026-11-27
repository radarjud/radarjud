import React from 'react';
import { RefreshCw, ShieldCheck } from 'lucide-react';
import logoHorizontal from './assets/logo-radarjus-horizontal.png';

// Marca exibida nas telas de acesso (logo oficial RadarJus, PNG com fundo transparente para fundos escuros).
export function BrandLockup() {
  return (
    <div className="auth-brand">
      <img
        src={logoHorizontal}
        alt="RadarJus - Inteligência Preditiva"
        width="541"
        height="173"
        style={{ display: 'block', width: '100%', maxWidth: 240, height: 'auto' }}
      />
    </div>
  );
}

export function AuthSplash({ label = 'Verificando sua sessão...' }) {
  return (
    <div className="auth-splash" role="status">
      <RefreshCw className="spin" size={20} /> {label}
    </div>
  );
}

// Estrutura comum de Login e Cadastro: painel institucional à esquerda, formulário à direita.
export default function AuthLayout({ prompt, linkLabel, linkHref, children }) {
  return (
    <div className="auth-shell">
      <aside className="auth-panel">
        <BrandLockup />
        <svg className="auth-radar" viewBox="0 0 400 400" aria-hidden="true" focusable="false">
          <circle cx="200" cy="200" r="190" />
          <circle cx="200" cy="200" r="130" />
          <circle cx="200" cy="200" r="70" />
          <path className="auth-pulse" pathLength="1000" d="M0 232 H118 L150 128 L192 304 L222 188 L242 232 H400" />
        </svg>
        <div className="auth-panel-copy">
          <span className="auth-rule" aria-hidden="true" />
          <h2>Decisões mais seguras começam com dados confiáveis.</h2>
          <p>Inteligência preditiva empresarial para análises claras, ágeis e fundamentadas.</p>
          <div className="auth-panel-seal"><ShieldCheck size={16} /> Seus dados estão protegidos</div>
        </div>
      </aside>

      <div className="auth-side">
        <header className="auth-topbar">
          <span>Plataforma de inteligência preditiva</span>
          <span className="auth-switch">{prompt} <a href={linkHref}>{linkLabel}</a></span>
        </header>
        <main className="auth-main">{children}</main>
        <footer className="auth-footer">© 2026 RadarJus · Inteligência Preditiva</footer>
      </div>
    </div>
  );
}