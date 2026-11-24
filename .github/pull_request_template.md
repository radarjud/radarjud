## 📌 Descrição do Pull Request

<!-- Forneça um resumo claro e conciso das alterações implementadas neste PR. -->

### 🏷️ Tipo de Mudança
- [ ] `feat`: Nova funcionalidade
- [ ] `fix`: Correção de bug
- [ ] `refactor`: Refatoração sem alteração de comportamento
- [ ] `perf`: Melhoria de performance
- [ ] `chore`: Manutenção, dependências ou infraestrutura
- [ ] `ci`: Alterações nos pipelines de CI/CD
- [ ] `test`: Adição ou ajuste de testes unitários/integração
- [ ] `docs`: Documentação

---

### 🛡️ Checklist de Invariantes do RadarJus
*Marque os itens abaixo para confirmar a conformidade estrita com a arquitetura do projeto:*

- [ ] **100% de Análise de Processos:** Nenhum fatiamento arbitrário (`[:50]`, `[:100]`). `Desfechos Mapeados == Eventos Identificados`.
- [ ] **Inferência Bayesiana:** Regra de Bloqueio preservada para 0 condenações (`0.0%` + Súmula 331 TST) e simplex somando `100%`.
- [ ] **LGPD by Design:** Nenhum nome ou CPF de pessoa física (reclamante) foi persistido em banco de dados ou exposto em logs.
- [ ] **Render Free (512 MB RAM):** Sem novas dependências pesadas em memória; chamadas HTTP possuem `timeout` e tratamento de erros.
- [ ] **Sem Hardcode Empresarial:** Nenhuma condição específica por nome/CNPJ de empresa inserida fora de seeds ou testes.
- [ ] **Frontend Seguro e Responsivo:** Build com `sourcemap: false`, sem erros 404 no console e sem scroll horizontal em mobile (`<= 768px`).
- [ ] **Bateria de Testes:** Todos os testes unitários e de integração continuam 100% verdes localmente (`pytest` e `npm test`).

---

### 🧪 Como Testar Localmente
1. Executar os testes do backend:
   ```bash
   pytest -v --disable-warnings
   ```
2. Executar os testes e o build do frontend:
   ```bash
   cd frontend && node --test src/companyNavigation.test.js && npm run build
   ```

