import React from 'react';
import { LogIn, LogOut, UserPlus } from 'lucide-react';
import { useAuth } from './AuthContext.jsx';
import { quotaTone } from './Session.js';

// Bloco de sessão no rodapé da sidebar: empresa, perfil, plano, cota mensal e botão Sair.
export default function SessionCard() {
  const { status, user, tenant, quota, logout } = useAuth();

  if (!user) {
    return (
      <div className="session-card session-guest">
        <b>{status === 'loading' ? 'Verificando sessão...' : 'Você não está conectado'}</b>
        {status !== 'loading' && (
          <>
            <small>Entre para consultar empresas e salvar o histórico.</small>
            <div className="session-guest-actions">
              <a className="session-button" href="#/login"><LogIn size={15} /> Entrar</a>
              <a className="session-button ghost" href="#/register"><UserPlus size={15} /> Criar conta</a>
            </div>
          </>
        )}
      </div>
    );
  }

  const hasLimit = Number.isFinite(quota.limit) && quota.limit > 0;
  const used = quota.used ?? 0;
  const percent = hasLimit ? Math.min(100, Math.round((used / quota.limit) * 100)) : 0;
  const tone = quotaTone(quota);

  return (
    <section className="session-card" aria-label="Sessão e plano">
      <div className="session-head">
        <span className="avatar" aria-hidden="true">{user.initials}</span>
        <div className="session-id">
          <b title={tenant?.name || undefined}>{tenant?.name || 'Sua organização'}</b>
          <small title={user.email}>{user.name}</small>
        </div>
      </div>

      <div className="session-pills">
        <span className="session-pill">{user.roleLabel}</span>
        <span className="session-pill plan">{tenant?.planLabel ? `Plano ${tenant.planLabel}` : 'Plano não informado'}</span>
      </div>

      {hasLimit && (
        <div className="session-quota">
          <div className="session-quota-top">
            <span>Consultas no mês</span>
            <b>{used} de {quota.limit}</b>
          </div>
          <div
            className="session-track"
            role="progressbar"
            aria-label="Consultas usadas no mês"
            aria-valuemin={0}
            aria-valuemax={quota.limit}
            aria-valuenow={Math.min(used, quota.limit)}
          >
            <div className={tone} style={{ width: `${percent}%` }} />
          </div>
          {tone === 'full' && <small className="session-quota-note">Cota esgotada. Peça um upgrade ao administrador.</small>}
          {tone === 'warn' && <small className="session-quota-note">Restam {quota.remaining ?? quota.limit - used} consultas neste mês.</small>}
        </div>
      )}

      <button type="button" className="session-logout" onClick={logout}>
        <LogOut size={15} /> Sair
      </button>
    </section>
  );
}