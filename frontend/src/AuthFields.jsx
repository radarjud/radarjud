import React, { useState } from 'react';
import { Check, Eye, EyeOff } from 'lucide-react';
import { checkPassword } from './authValidation.js';

// Campo de texto com rótulo, mensagem de erro e dica, ligados por aria-describedby.
export function TextField({ id, label, error, hint, labelAside, adornment, inputRef, below, ...input }) {
  const describedBy = [error && `${id}-error`, !error && hint && `${id}-hint`].filter(Boolean).join(' ') || undefined;
  return (
    <div className={`auth-field${error ? ' invalid' : ''}`}>
      <div className="auth-label-row">
        <label htmlFor={id}>{label}</label>
        {labelAside}
      </div>
      <div className="auth-control">
        <input
          id={id}
          ref={inputRef}
          aria-invalid={error ? 'true' : undefined}
          aria-describedby={describedBy}
          {...input}
        />
        {adornment}
      </div>
      {error && <p className="auth-error" id={`${id}-error`}>{error}</p>}
      {!error && hint && <p className="auth-hint" id={`${id}-hint`}>{hint}</p>}
      {below}
    </div>
  );
}

export function PasswordField({ id, ...props }) {
  const [visible, setVisible] = useState(false);
  return (
    <TextField
      id={id}
      {...props}
      type={visible ? 'text' : 'password'}
      adornment={(
        <button
          type="button"
          className="auth-eye"
          onClick={() => setVisible((v) => !v)}
          aria-label={visible ? 'Ocultar senha' : 'Mostrar senha'}
          aria-pressed={visible}
        >
          {visible ? <Eye size={18} /> : <EyeOff size={18} />}
        </button>
      )}
    />
  );
}

// Medidor e checklist ao vivo dos requisitos de senha forte.
export function PasswordStrength({ value }) {
  const { checks, level, levelLabel } = checkPassword(value);
  if (!value) {
    return <p className="auth-hint">Mínimo de 8 caracteres, com maiúscula, minúscula, número e símbolo.</p>;
  }
  return (
    <div className="auth-strength" aria-live="polite">
      <div className={`auth-meter level-${level}`} aria-hidden="true">
        {[0, 1, 2, 3].map((i) => <span key={i} className={i < Math.max(1, level) ? 'on' : ''} />)}
      </div>
      <p className="auth-strength-label">Senha {levelLabel.toLowerCase()}</p>
      <ul className="auth-rules">
        {checks.map((rule) => (
          <li key={rule.id} className={rule.ok ? 'ok' : ''}>
            <span className="auth-rule-icon" aria-hidden="true">{rule.ok && <Check size={11} strokeWidth={3} />}</span>
            {rule.label}
            <span className="sr-only">{rule.ok ? ' (atendido)' : ' (pendente)'}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function CheckField({ id, error, inputRef, children, ...input }) {
  return (
    <div className={`auth-check${error ? ' invalid' : ''}`}>
      <label htmlFor={id}>
        <input id={id} type="checkbox" ref={inputRef} aria-invalid={error ? 'true' : undefined} aria-describedby={error ? `${id}-error` : undefined} {...input} />
        <span>{children}</span>
      </label>
      {error && <p className="auth-error" id={`${id}-error`}>{error}</p>}
    </div>
  );
}