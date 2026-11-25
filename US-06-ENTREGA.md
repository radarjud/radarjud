# US-06 (#58) — filtros e tokens PDF

Base: arquivo radarjud.tar.xz fornecido, não main remota. Patch exclusivamente frontend.

## API
GET /api/v1/searches/{search_id}/processes?page=1&per_page=20&tribunal=TST&outcome=condenacoes&search=...

A base possui esses parâmetros, mas não possui data/valor. Proposta de contrato para US-08: `date_from`, `date_to` (YYYY-MM-DD, limites inclusivos, data processual a acordar), `value_min`, `value_max` (BRL decimal, inclusive). A resposta deve confirmar os parâmetros avançados aplicados em `applied_filters` com as mesmas chaves e valores. Ex.: `{"processes":[],"total":0,"total_pages":1,"applied_filters":{"date_from":"2026-01-01","value_min":0}}`. Sem confirmação a UI exibe erro, não resultados falsamente filtrados. Alinhar nomes e semântica com US-08. A rota antiga usa comparação parcial de tribunal (TRT1 também pode encontrar TRT10); US-08 deve garantir correspondência exata. A rota antiga não expõe nível de sigilo/flag de inferência completos; não inferir sigilo ausente. Validar contrato e autenticação na main atual.

## CSS US-03
`frontend/src/report-print-tokens.css` é independente do SPA. Incluir no template Jinja2 por caminho resolvido pelo renderizador; usar wrapper `.report-print`, `.report-badge.scenario-a`, `.report-badge.scenario-b`, `.report-badge.tst`, `.report-kpis` contendo `.report-kpi`, `.report-table`. A4, margem 15mm, tabela de KPIs para compatibilidade com motores PDF. Cores extraídas do SPA; badge TST usa azul da paleta pois não havia regra purple específica na base. Inter deve estar instalada no ambiente PDF para paridade tipográfica; fallback Arial/sans-serif. Validar com Matheus, inclusive quebra de página e suporte CSS do motor.

## QA
`cd frontend && node --test src/companyNavigation.test.js src/processFilters.test.js && npm run build` (8 testes aprovados, build aprovado na base enviada). Testes unitários cobrem montagem da query, limpeza, validação e confirmação do contrato; não são testes de integração real. Validar manualmente filtros, debounce, cancelamento, navegação, mobile e PDF A4. Nenhum arquivo de backend alterado.
