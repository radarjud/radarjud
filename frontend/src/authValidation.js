// Validações dos formulários de Login e Cadastro.
// Cada validador devolve '' quando o valor é válido, ou a mensagem de erro.

const FREE_EMAIL_DOMAINS = new Set([
  'gmail.com', 'googlemail.com', 'hotmail.com', 'outlook.com', 'live.com', 'msn.com',
  'yahoo.com', 'yahoo.com.br', 'icloud.com', 'me.com', 'bol.com.br', 'uol.com.br',
  'terra.com.br', 'ig.com.br', 'globo.com', 'proton.me', 'protonmail.com',
]);

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export function validateEmail(value, { corporate = false } = {}) {
  const email = String(value ?? '').trim().toLowerCase();
  if (!email) return 'Informe seu e-mail.';
  if (email.length > 254 || !EMAIL_RE.test(email)) return 'Informe um e-mail válido, como nome@empresa.com.br.';
  if (corporate && FREE_EMAIL_DOMAINS.has(email.split('@')[1])) {
    return 'Use seu e-mail corporativo (não são aceitos e-mails pessoais).';
  }
  return '';
}

export function validateFullName(value) {
  const name = String(value ?? '').trim().replace(/\s+/g, ' ');
  if (!name) return 'Informe seu nome completo.';
  const parts = name.split(' ');
  if (parts.length < 2 || parts.some((p) => p.length < 2)) return 'Informe nome e sobrenome.';
  if (name.length > 120) return 'O nome pode ter no máximo 120 caracteres.';
  if (!/^[\p{L}][\p{L}'’.\- ]*$/u.test(name)) return 'Use apenas letras no nome.';
  return '';
}

export function validateCompany(value) {
  const company = String(value ?? '').trim();
  if (!company) return 'Informe o nome da empresa.';
  if (company.length < 2) return 'O nome da empresa é muito curto.';
  if (company.length > 150) return 'O nome da empresa pode ter no máximo 150 caracteres.';
  return '';
}

export function maskCnpj(value) {
  const d = String(value ?? '').replace(/\D/g, '').slice(0, 14);
  let out = d.slice(0, 2);
  if (d.length > 2) out += `.${d.slice(2, 5)}`;
  if (d.length > 5) out += `.${d.slice(5, 8)}`;
  if (d.length > 8) out += `/${d.slice(8, 12)}`;
  if (d.length > 12) out += `-${d.slice(12, 14)}`;
  return out;
}

function cnpjCheckDigit(base) {
  const weights = base.length === 12
    ? [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    : [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2];
  const sum = base.split('').reduce((acc, n, i) => acc + Number(n) * weights[i], 0);
  const rest = sum % 11;
  return rest < 2 ? 0 : 11 - rest;
}

// CNPJ é opcional no cadastro: vazio é válido.
export function validateCnpj(value) {
  const d = String(value ?? '').replace(/\D/g, '');
  if (!d) return '';
  if (d.length !== 14) return 'O CNPJ deve ter 14 dígitos.';
  if (/^(\d)\1{13}$/.test(d)) return 'Informe um CNPJ válido.';
  const d1 = cnpjCheckDigit(d.slice(0, 12));
  const d2 = cnpjCheckDigit(d.slice(0, 12) + d1);
  if (d1 !== Number(d[12]) || d2 !== Number(d[13])) return 'Informe um CNPJ válido.';
  return '';
}

const LEVEL_LABELS = ['Muito fraca', 'Fraca', 'Razoável', 'Boa', 'Forte'];

// Requisitos ao vivo + nível de força (0 a 4) usados pelo medidor.
export function checkPassword(value) {
  const pwd = String(value ?? '');
  const checks = [
    { id: 'length', label: 'Mínimo de 8 caracteres', ok: pwd.length >= 8 },
    { id: 'upper', label: 'Uma letra maiúscula', ok: /\p{Lu}/u.test(pwd) },
    { id: 'lower', label: 'Uma letra minúscula', ok: /\p{Ll}/u.test(pwd) },
    { id: 'digit', label: 'Um número', ok: /\d/.test(pwd) },
    { id: 'symbol', label: 'Um símbolo (ex.: ! @ # $)', ok: /[^\p{L}\d\s]/u.test(pwd) },
  ];
  const passed = checks.filter((c) => c.ok).length;
  let level = passed >= 5 ? 4 : passed === 4 ? 3 : passed === 3 ? 2 : passed >= 1 ? 1 : 0;
  if (!checks[0].ok) level = Math.min(level, 2);
  return { checks, level, levelLabel: LEVEL_LABELS[level] };
}

export function validatePassword(value) {
  if (!value) return 'Crie uma senha.';
  if (String(value).length > 128) return 'A senha pode ter no máximo 128 caracteres.';
  const missing = checkPassword(value).checks.find((c) => !c.ok);
  return missing ? `A senha precisa ter: ${missing.label.toLowerCase()}.` : '';
}