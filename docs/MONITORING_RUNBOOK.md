# Guia Operacional de Infraestrutura e QA — Monitoramento Contínuo (RadarJus)

Este documento reúne o mapeamento da arquitetura, variáveis obrigatórias, regras de migração de banco e o roteiro de validação (smoke test) para futuras alterações no pipeline de monitoramento e alertas.

---

## 1. Arquitetura do Fluxo de Monitoramento

1. **Gatilho (`POST /api/v1/internal/run-monitoring`):** Acionado pelo GitHub Actions (`Monitoramento Periódico`) ou manualmente via `curl`, autenticado pelo header `X-Cron-Secret`.
2. **Avaliação (`app/services/monitoring_job.py`):**
   * Consulta a situação cadastral atual na RFB (`fetch_accurate_cnpj_data`).
   * Busca o snapshot mais recente do fornecedor na tabela `company_searches` (filtrando por CNPJ em `company_name` ou `normalized_name`).
   * Compara métricas atuais com a baseline salva em `monitored_suppliers` (`last_condenacoes_count`, `last_tst_count`, `last_rfb_status`, `last_scenario`, `last_movements_hash`).
3. **Enfileiramento (`app/services/task_dispatcher.py`):** Caso haja gatilho crítico, publica o evento `SUPPLIER_ALERT` no **Upstash Redis** e atualiza a baseline do fornecedor no PostgreSQL (garantindo idempotência).
4. **Despacho (`app/services/notifier.py`):** Consome a fila e envia notificações para `webhook_url` (HTTP POST com retries e backoff) e `alert_email` (via API do **Resend**).

---

## 2. Variáveis de Ambiente Obrigatórias

### Render (`radarjud-api > Environment`)
| Variável | Descrição / Observação Operacional |
| :--- | :--- |
| `DATABASE_URL` | String de conexão PostgreSQL do Supabase. |
| `JWT_SECRET_KEY` | **Obrigatório manter valor fixo.** Se ficar ausente/aleatória, cada restart do Render invalida todos os tokens JWT ativos (`Signature verification failed`). |
| `REDIS_URL` | URL TLS do Upstash Redis (`rediss://...`). |
| `CRON_SECRET` | Segredo compartilhado com o GitHub Actions para autorizar `/api/v1/internal/run-monitoring`. |
| `RESEND_API_KEY` | Chave de API do Resend. *(Enquanto usar o remetente `onboarding@resend.dev` sem domínio próprio verificado, o Resend só entrega e-mails para o endereço dono da conta).* |

### GitHub (`Settings > Secrets and variables > Actions`)
| Secret | Valor Esperado |
| :--- | :--- |
| `RENDER_API_URL` | `https://radarjud-api.onrender.com` |
| `CRON_SECRET` | Mesmo valor configurado no Render. |

---

## 3. Regra de Migração de Banco de Dados (Supabase)

O aplicativo executa `db.create_all()` no boot, o que cria tabelas inexistentes, **mas não altera colunas em tabelas já criadas**.

Sempre que adicionar ou alterar colunas nos modelos `CompanySearch` ou `MonitoredSupplier`:
1. Execute o `ALTER TABLE` correspondente no **SQL Editor do Supabase** antes do deploy.
2. Valide a estrutura com:
   ```sql
   SELECT column_name, data_type, is_nullable
   FROM information_schema.columns
   WHERE table_name IN ('company_searches', 'monitored_suppliers')
   ORDER BY table_name, ordinal_position;
