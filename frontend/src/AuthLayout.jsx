import React from 'react';
import { Activity, RefreshCw, ShieldCheck } from 'lucide-react';

// Marca exibida nas telas de acesso. Para trocar pela logo final em SVG, altere só este componente.
export function BrandLockup() {
  return (
    <div className="auth-brand">
      <div className="auth-brand-mark"><Activity size={26} /></div>
      <div>
        <strong>Radar<span>Jus</span></strong>
        <small>INTELIGÊNCIA PREDITIVA</small>
      </div>
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