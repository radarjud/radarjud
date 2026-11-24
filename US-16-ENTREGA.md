# US-16 — Issue #46: paginação e coordenação do backend

Base: arquivo radarjud.tar.xz fornecido e ZIP frontend da US-16. Aplicar o patch sobre a branch que já contém a primeira implementação. Não substitui arquivos inteiros da master; se o patch não aplicar, revisar divergências antes de continuar.

## Fluxo

- Navegador pesquisa pelo `nomeParte` (razão social quando entrada é CNPJ) e envia página 1, `browser_deep_scan`, `browser_query_term` e `browser_page1_count` ao backend. O backend compara o termo com sua estratégia e valida a estrutura e CNJs dos itens. O filtro por nome não comprova identidade de filial por CNPJ.
- Se o prefetch estiver válido e houver mais páginas, o servidor registra `BROWSER_PENDING` e não dispara o worker legado. Sem prefetch válido, mantém o fluxo legado do backend.
- O navegador consulta páginas 2 a 10, lotes de 3, timeout de 6s, deduplicação por CNJ e cancelamento. Erros de conexão/CORS podem acionar proxy; HTTP 403/429 não acionam proxy. O primeiro fetch tem deadline de 4s.
- POST `/api/v1/ingest-deep-scan` com `browser_scan_finished: true` encerra a coleta mesmo com lote vazio. `is_final_batch: true` apenas quando todas as comunicações informadas pelo count foram coletadas dentro do limite. Caso contrário, `BROWSER_PARTIAL` (terminal, não equivale a completo). Registros anteriores são preservados e as métricas recalculadas. Não há nova paginação do PJe no servidor nesse fluxo.
- Ingestão conserva estratégia econômica da busca, exige JWT do tenant quando associado, serializa ingestões do mesmo ID no PostgreSQL e persiste lote + métricas em uma transação. A leitura do status/histórico converte busca abandonada após 120s em parcial; polling para ao receber estado terminal. O frontend não modifica o dashboard de uma busca substituída.
- Cache L0 legado não é usado pelo novo cliente; cache L1 só reaproveita resultado completo da mesma estratégia e tenant com marcador da nova versão. Chaves L0 relacionadas ao ID são invalidadas após ingestão.
- Sem migração de banco nem alteração de fórmulas Bayes/TST/capital. A análise usa os dados coletados; não inclui automaticamente o enriquecimento DataJud do worker legado.

## QA

Executados neste ambiente: 10 testes JavaScript, build Vite e compilação Python dos arquivos modificados. Não foi possível executar pytest: Flask/Flask-SQLAlchemy ausentes e instalação indisponível. Testes adicionados em `tests/test_browser_deep_scan.py`; execute junto aos testes US-14 e suíte completa localmente. Teste a implantação conjunta de frontend/backend, cancelamento manual, consistência dos números CNJ entre empresas e desempenho hospedado. Registros antigos possivelmente contaminados não foram excluídos; refaça consultas após deploy. `count` representa comunicações, não processos únicos. Não há garantia de 900+ registros nem de 12s; resultados dependem da API, rede, filtro e volume. Para falha parcial de páginas não é disparada varredura antiga simultânea; resultado parcial é sinalizado e a pesquisa pode ser refeita. O fallback Vercel precisa ser validado no deploy; autenticação e verificação da origem de todos os itens recebidos ainda exigem revisão de segurança da US-14.
