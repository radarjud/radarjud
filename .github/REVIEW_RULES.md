# Invariantes e Regras de Revisão de Código — RadarJus

As seguintes diretrizes são inegociáveis para qualquer Pull Request:

1. **Integridade Estatística e Coleta 100%**:
   - É terminantemente proibido truncar a análise de processos com fatiamento artificial (`[:50]`, `[:100]`).
   - 100% dos processos coletados devem ser processados; `Desfechos Mapeados` deve ser estritamente igual a `Eventos Identificados`.

2. **Inferência Bayesiana e Regra de Bloqueio**:
   - Manter a Regra de Bloqueio de Predição quando houver 0 condenações (risco 0.0% + alerta da Súmula 331 do TST).
   - O simplex de probabilidades deve sempre somar exatamente 100% (1.0).

3. **Privacidade e LGPD by Design**:
   - Proibido armazenar, logar ou expor nomes e CPFs de reclamantes/pessoas físicas. Apenas dados de Pessoas Jurídicas são permitidos.

4. **Resiliência e Limite de Memória (Render Free - 512 MB)**:
   - Proibido introduzir dependências pesadas de memória (Chromium/Playwright, PyTorch, Spacy pesado).
   - Todas as requisições HTTP externas devem conter `timeout` explícito e tratamento de exceção.
   - Proibido carregar lotes de dados em memória sem geradores ou paginação.

5. **Segurança e Anti-IDOR**:
   - Queries de consulta/histórico (`company_searches`, `history`) devem isolar registros por usuário/tenant e validar/sanitizar parâmetros.

6. **Generalização Algorítmica (Sem Hardcode)**:
   - Proibido condicionais hardcoded para empresas específicas (ex.: `if "BRADESCO" in ...` ou razões sociais fixas) fora de seeds controlados e testes.

7. **Frontend e Experiência Mobile**:
   - Manter `sourcemap: false` e remoção de comentários no build (`vite.config.js`).
   - Proibido introduzir links/imagens que gerem erro 404 no console (ex.: favicons sintéticos não validados).
   - Não permitir overflow ou scroll horizontal em resoluções mobile (`<= 768px`).

