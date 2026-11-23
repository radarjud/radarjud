import test from 'node:test';
import assert from 'node:assert/strict';
import { initialsOf, mergeQuota, normalizeQuota, normalizeSession, parseQuotaHeader, planLabel, quotaTone, roleLabel } from './session.js';

test('perfil e plano ganham rótulos em português', () => {
  assert.equal(roleLabel('ADMIN'), 'Admin');
  assert.equal(roleLabel('analyst'), 'Analista');
  assert.equal(roleLabel(''), 'Usuário');
  assert.equal(planLabel('pro'), 'Pro');
  assert.equal(planLabel('FREE'), 'Gratuito');
  assert.equal(planLabel('custom'), 'Custom');
  assert.equal(initialsOf('Ana Maria Souza'), 'AS');
  assert.equal(initialsOf('', 'ana@x.com'), 'AN');
});

test('cota é lida de nomes de campo diferentes', () => {
  assert.deepEqual(normalizeQuota({ monthly_quota: 50, quota_used: 12 }), { limit: 50, used: 12, remaining: 38 });
  assert.deepEqual(normalizeQuota({ quota: { limit: 10, remaining: 4 } }), { limit: 10, used: 6, remaining: 4 });
  assert.deepEqual(normalizeQuota({ quota: 30 }), { limit: 30, used: null, remaining: null });
  assert.deepEqual(normalizeQuota(null), { limit: null, used: null, remaining: null });
});

test('cabeçalho X-Quota-Remaining aceita "n" e "n/limite"', () => {
  assert.deepEqual(parseQuotaHeader('42'), { remaining: 42 });
  assert.deepEqual(parseQuotaHeader('42/50'), { remaining: 42, limit: 50 });
  assert.equal(parseQuotaHeader('unlimited'), null);
  assert.equal(parseQuotaHeader(null), null);
});

test('mergeQuota mantém used/remaining coerentes com o limite', () => {
  assert.deepEqual(mergeQuota({ limit: 50, used: 10, remaining: 40 }, { remaining: 35 }), { limit: 50, used: 15, remaining: 35 });
  assert.deepEqual(mergeQuota({ limit: 50, used: 10, remaining: 40 }, { limit: 50, used: 50, remaining: 0 }), { limit: 50, used: 50, remaining: 0 });
  assert.deepEqual(mergeQuota({ limit: null, used: null, remaining: null }, { remaining: 5 }), { limit: null, used: null, remaining: 5 });
});

test('tom da barra de cota', () => {
  assert.equal(quotaTone({ limit: 10, used: 5 }), 'ok');
  assert.equal(quotaTone({ limit: 10, used: 8 }), 'warn');
  assert.equal(quotaTone({ limit: 10, used: 10 }), 'full');
  assert.equal(quotaTone({ limit: null, used: null }), 'ok');
});

test('normalizeSession junta usuário, organização e cota', () => {
  const s = normalizeSession({
    user: { id: 1, name: 'Ana Souza', email: 'ana@x.com.br', role: 'ADMIN' },
    tenant: { id: 7, name: 'Radar Ltda', plan: 'PRO', monthly_quota: 100, quota_used: 20 },
  });
  assert.equal(s.user.roleLabel, 'Admin');
  assert.equal(s.user.initials, 'AS');
  assert.equal(s.tenant.planLabel, 'Pro');
  assert.deepEqual(s.quota, { limit: 100, used: 20, remaining: 80 });
});