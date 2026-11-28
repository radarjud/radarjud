import React, { useState, useEffect } from 'react';
import {
  Users, Shield, ShieldCheck, CheckCircle2, AlertCircle, RefreshCw,
  Zap, CreditCard, ChevronDown, Check, UserCheck, Calendar, Mail
} from 'lucide-react';
import { apiRequest } from './apiClient.js';
import { planLabel, roleLabel } from './Session.js';

export default function AdminUsersPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');
  const [updatingUserId, setUpdatingUserId] = useState(null);
  const [selectedPlan, setSelectedPlan] = useState({});

  async function loadUsers() {
    setLoading(true);
    setError('');
    try {
      const res = await apiRequest('/auth/admin/users');
      setData(res);
      // Inicializa estado dos planos selecionados por usuário
      const initialPlans = {};
      (res.users || []).forEach(u => {
        initialPlans[u.id] = u.plan || res.tenant?.plan || 'ESSENCIAL';
      });
      setSelectedPlan(initialPlans);
    } catch (err) {
      setError(err.message || 'Falha ao carregar lista de usuários da organização.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadUsers();
  }, []);

  async function handlePlanChange(userId, newPlan) {
    if (!newPlan) return;
    setUpdatingUserId(userId);
    setError('');
    setSuccessMsg('');
    try {
      const res = await apiRequest(`/auth/admin/users/${userId}/plan`, {
        method: 'PUT',
        body: JSON.stringify({ plan: newPlan }),
      });
      setSuccessMsg(`Plano do usuário atualizado para "${res.user?.plan || newPlan}" com sucesso!`);
      // Atualiza os dados locais apenas do usuário modificado
      setData(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          users: prev.users.map(u => (u.id === userId ? {
            ...u,
            plan: res.user?.plan || newPlan,
            user_plan: res.user?.plan || newPlan,
            effective_plan: res.user?.effective_plan || newPlan,
          } : u)),
        };
      });
      setSelectedPlan(prev => ({ ...prev, [userId]: newPlan }));
      setTimeout(() => setSuccessMsg(''), 5000);
    } catch (err) {
      setError(err.message || 'Não foi possível alterar o plano do usuário.');
    } finally {
      setUpdatingUserId(null);
    }
  }

  return (
    <div className="admin-users-page" style={{ padding: '4px 0' }}>
      <div className="section-intro" style={{ marginBottom: '24px' }}>
        <div className="eyebrow" style={{ color: '#245a81', fontWeight: 700, letterSpacing: '0.08em' }}>
          PAINEL ADMINISTRATIVO · GESTÃO DE ACESSOS
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '16px' }}>
          <div>
            <h1 style={{ margin: '4px 0', fontSize: '26px', color: '#13283f', display: 'flex', alignItems: 'center', gap: '10px' }}>
              <Users size={26} color="#245a81" /> Gestão de Usuários e Planos
            </h1>
            <p style={{ margin: 0, color: '#53697e', fontSize: '14px' }}>
              Visualize todos os membros da sua organização, papéis de acesso e gerencie os planos e cotas contratadas.
            </p>
          </div>
          <button
            type="button"
            className="secondary"
            onClick={loadUsers}
            disabled={loading}
            style={{ display: 'flex', alignItems: 'center', gap: '8px' }}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
            Atualizar
          </button>
        </div>
      </div>

      {error && (
        <div className="alert error" role="alert" style={{ marginBottom: '18px' }}>
          <AlertCircle size={18} /> {error}
        </div>
      )}

      {successMsg && (
        <div className="alert success" role="status" style={{ marginBottom: '18px', background: '#eaf7ed', color: '#1c6934', border: '1px solid #c2e8cd' }}>
          <CheckCircle2 size={18} /> {successMsg}
        </div>
      )}

      {/* Resumo da Organização e Quota Atual */}
      {data?.tenant && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '16px', marginBottom: '24px' }}>
          <div className="card" style={{ padding: '16px 20px', background: '#ffffff', borderRadius: '12px', border: '1px solid #e2e8f0' }}>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#64748b', fontWeight: 600, marginBottom: '6px' }}>Organização</div>
            <div style={{ fontSize: '17px', fontWeight: 700, color: '#0f172a' }}>{data.tenant.name}</div>
            <div style={{ fontSize: '12px', color: '#64748b', marginTop: '4px' }}>CNPJ: {data.tenant.cnpj || 'Não informado'}</div>
          </div>
          <div className="card" style={{ padding: '16px 20px', background: '#ffffff', borderRadius: '12px', border: '1px solid #e2e8f0' }}>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#64748b', fontWeight: 600, marginBottom: '6px' }}>Plano Atual</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span className="tag blue" style={{ fontSize: '13px', fontWeight: 700, padding: '4px 10px' }}>
                <Zap size={14} style={{ marginRight: '4px' }} />
                {planLabel(data.tenant.plan)}
              </span>
            </div>
            <div style={{ fontSize: '12px', color: '#64748b', marginTop: '6px' }}>
              Cota mensal: {data.quota?.is_unlimited ? 'Consultas ilimitadas' : `${data.quota?.limit ?? 30} consultas/mês`}
            </div>
          </div>
          <div className="card" style={{ padding: '16px 20px', background: '#ffffff', borderRadius: '12px', border: '1px solid #e2e8f0' }}>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#64748b', fontWeight: 600, marginBottom: '6px' }}>Consumo do Mês</div>
            <div style={{ fontSize: '18px', fontWeight: 700, color: '#0f172a' }}>
              {data.quota?.used ?? 0} {data.quota?.limit ? `/ ${data.quota.limit}` : 'consultas'}
            </div>
            <div style={{ fontSize: '12px', color: '#16a34a', marginTop: '4px' }}>
              {data.quota?.is_unlimited ? 'Sem restrição de cota' : `${data.quota?.remaining ?? 0} disponíveis`}
            </div>
          </div>
        </div>
      )}

      {/* Tabela / Board de Usuários */}
      <div className="card panel" style={{ background: '#ffffff', borderRadius: '12px', border: '1px solid #e2e8f0', padding: '20px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 style={{ margin: 0, fontSize: '18px', color: '#0f172a' }}>Usuários da Plataforma</h3>
            <span style={{ fontSize: '13px', color: '#64748b' }}>Total de {data?.total ?? 0} usuário(s) ativos</span>
          </div>
        </div>

        {loading ? (
          <div style={{ textAlign: 'center', padding: '40px', color: '#64748b' }}>
            <RefreshCw size={24} className="spin" style={{ marginBottom: '8px' }} />
            <div>Carregando usuários...</div>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #e2e8f0', color: '#64748b', fontWeight: 600 }}>
                  <th style={{ padding: '12px 14px' }}>Usuário</th>
                  <th style={{ padding: '12px 14px' }}>E-mail</th>
                  <th style={{ padding: '12px 14px' }}>Organização</th>
                  <th style={{ padding: '12px 14px' }}>Perfil de Acesso</th>
                  <th style={{ padding: '12px 14px' }}>Plano do Usuário</th>
                  <th style={{ padding: '12px 14px' }}>Status</th>
                  <th style={{ padding: '12px 14px', textAlign: 'right' }}>Ação de Plano</th>
                </tr>
              </thead>
              <tbody>
                {(data?.users || []).map((u) => {
                  const isCurrentUserUpdating = updatingUserId === u.id;
                  const currentPlan = selectedPlan[u.id] || u.plan || data?.tenant?.plan || 'ESSENCIAL';
                  const activeUserPlan = u.plan || data?.tenant?.plan || 'ESSENCIAL';
                  return (
                    <tr key={u.id} style={{ borderBottom: '1px solid #f1f5f9' }}>
                      <td style={{ padding: '14px', fontWeight: 600, color: '#0f172a' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <div style={{
                            width: '32px', height: '32px', borderRadius: '50%',
                            background: u.role === 'ADMIN' ? '#e0f2fe' : '#f1f5f9',
                            color: u.role === 'ADMIN' ? '#0369a1' : '#475569',
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            fontWeight: 700, fontSize: '12px'
                          }}>
                            {u.name?.slice(0, 2).toUpperCase() || 'US'}
                          </div>
                          <div>
                            <div>{u.name}</div>
                            {u.role === 'ADMIN' && (
                              <span style={{ fontSize: '10px', color: '#0369a1', fontWeight: 700 }}>ADMINISTRADOR</span>
                            )}
                          </div>
                        </div>
                      </td>
                      <td style={{ padding: '14px', color: '#475569' }}>
                        <span style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <Mail size={13} color="#94a3b8" /> {u.email}
                        </span>
                      </td>
                      <td style={{ padding: '14px', color: '#334155', fontWeight: 500 }}>
                        <span style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <CreditCard size={13} color="#94a3b8" /> {u.tenant_name || 'RadarJus Matriz'}
                        </span>
                      </td>
                      <td style={{ padding: '14px' }}>
                        <span className={`tag ${u.role === 'ADMIN' ? 'purple' : 'blue'}`} style={{ fontSize: '11px' }}>
                          <Shield size={12} style={{ marginRight: '4px' }} />
                          {roleLabel(u.role)}
                        </span>
                      </td>
                      <td style={{ padding: '14px' }}>
                        <span className="tag green" style={{ fontSize: '11px', fontWeight: 600 }}>
                          {planLabel(activeUserPlan)}
                        </span>
                      </td>
                      <td style={{ padding: '14px' }}>
                        <span style={{
                          display: 'inline-flex', alignItems: 'center', gap: '4px',
                          color: u.is_active ? '#16a34a' : '#dc2626', fontWeight: 600, fontSize: '12px'
                        }}>
                          <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: u.is_active ? '#16a34a' : '#dc2626' }} />
                          {u.is_active ? 'Ativo' : 'Inativo'}
                        </span>
                      </td>
                      <td style={{ padding: '14px', textAlign: 'right' }}>
                        <div style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
                          <select
                            value={currentPlan}
                            onChange={(e) => setSelectedPlan(prev => ({ ...prev, [u.id]: e.target.value }))}
                            disabled={isCurrentUserUpdating}
                            style={{
                              padding: '6px 10px', borderRadius: '6px', border: '1px solid #cbd5e1',
                              fontSize: '12px', background: '#ffffff', color: '#0f172a', cursor: 'pointer'
                            }}
                          >
                            <option value="ESSENCIAL">Essencial (30 consultas)</option>
                            <option value="PRO">Pro (150 consultas)</option>
                            <option value="ENTERPRISE">Enterprise (Ilimitado)</option>
                          </select>
                          <button
                            type="button"
                            className="primary"
                            onClick={() => handlePlanChange(u.id, currentPlan)}
                            disabled={isCurrentUserUpdating || currentPlan === activeUserPlan}
                            style={{ fontSize: '12px', padding: '6px 12px', whiteSpace: 'nowrap' }}
                          >
                            {isCurrentUserUpdating ? (
                              <RefreshCw size={13} className="spin" />
                            ) : (
                              'Aplicar'
                            )}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Catálogo de Planos */}
      <div style={{ marginTop: '24px' }}>
        <h3 style={{ fontSize: '16px', color: '#1e293b', marginBottom: '12px' }}>Planos Disponíveis para a Organização</h3>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px' }}>
          {(data?.available_plans || [
            { id: 'ESSENCIAL', label: 'Essencial', quota: 30, description: 'Até 30 consultas mensais e monitoramento padrão.' },
            { id: 'PRO', label: 'Pro', quota: 150, description: 'Até 150 consultas mensais com inteligência recursal TST avançada.' },
            { id: 'ENTERPRISE', label: 'Enterprise', quota: 'Ilimitado', description: 'Consultas ilimitadas, relatórios executivos customizados e SLA dedicado.' }
          ]).map(plan => {
            const isCurrent = data?.tenant?.plan === plan.id;
            return (
              <div
                key={plan.id}
                style={{
                  padding: '18px', background: isCurrent ? '#f0f9ff' : '#ffffff',
                  border: isCurrent ? '2px solid #0284c7' : '1px solid #e2e8f0',
                  borderRadius: '10px'
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <strong style={{ fontSize: '16px', color: '#0f172a' }}>{plan.label}</strong>
                  {isCurrent && <span className="tag blue" style={{ fontSize: '10px' }}>PLANO ATUAL</span>}
                </div>
                <div style={{ fontSize: '20px', fontWeight: 800, color: '#0284c7', margin: '8px 0 4px' }}>
                  {plan.quota} {typeof plan.quota === 'number' ? 'consultas/mês' : ''}
                </div>
                <p style={{ fontSize: '12px', color: '#64748b', margin: 0 }}>{plan.description}</p>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
