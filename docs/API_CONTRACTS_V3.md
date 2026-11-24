# RadarJus API v3.0 — Contratos JSON Congelados

Este documento formaliza e congela os contratos de dados JSON (Request/Response) da API v3.0 do RadarJus, servindo como especificação canônica para integrações entre o frontend (Next.js/React/Vite), backend (Flask) e microsserviços/extensões.

---

## 1. Módulo de Autenticação e Multitenancy (`/api/v1/auth/*`)
*Referência canônica para US-01 e US-04.*

### 1.1 `POST /api/v1/auth/register`
Criação de nova conta de usuário vinculada a um tenant corporativo.

- **Headers:** `Content-Type: application/json`
- **Request Body:**
  ```json
  {
    "name": "Mayara Ferreira",
    "email": "mayara@empresa.com.br",
    "password": "SenhaForteSegura@2026",
    "tenant_name": "Empresa Compliance S.A.",
    "cnpj": "12.345.678/0001-90"
  }
  ```
- **Responses:**
  - `201 Created`:
    ```json
    {
      "status": "success",
      "message": "Usuário registrado com sucesso.",
      "user": {
        "id": 1,
        "name": "Mayara Ferreira",
        "email": "mayara@empresa.com.br",
        "tenant_id": 101,
        "tenant_name": "Empresa Compliance S.A.",
        "role": "admin",
        "created_at": "2026-11-22T21:00:00Z"
      },
      "access_token": "eyJhbGciOiJIUzI1NiIsInR5c...",
      "refresh_token": "def502003a7..."
    }
    ```
  - `400 Bad Request`: `{ "error": "Email já cadastrado ou dados inválidos." }`

### 1.2 `POST /api/v1/auth/login`
Autenticação de usuário com emissão de token JWT.

- **Headers:** `Content-Type: application/json`
- **Request Body:**
  ```json
  {
    "email": "mayara@empresa.com.br",
    "password": "SenhaForteSegura@2026"
  }
  ```
- **Responses:**
  - `200 OK`:
    ```json
    {
      "status": "success",
      "access_token": "eyJhbGciOiJIUzI1NiIsInR5c...",
      "refresh_token": "def502003a7...",
      "expires_in": 3600,
      "user": {
        "id": 1,
        "name": "Mayara Ferreira",
        "email": "mayara@empresa.com.br",
        "tenant_id": 101,
        "role": "admin"
      }
    }
    ```
  - `401 Unauthorized`: `{ "error": "Credenciais inválidas." }`

### 1.3 `POST /api/v1/auth/refresh`
Renovação do token de acesso expirado.

- **Headers:** `Content-Type: application/json`
- **Request Body:**
  ```json
  {
    "refresh_token": "def502003a7..."
  }
  ```
- **Responses:**
  - `200 OK`:
    ```json
    {
      "access_token": "eyJhbGciOiJIUzI1NiIsInR5c...",
      "expires_in": 3600
    }
    ```
  - `401 Unauthorized`: `{ "error": "Refresh token inválido ou expirado." }`

### 1.4 `GET /api/v1/auth/me`
Perfil do usuário autenticado no contexto do tenant.

- **Headers:** `Authorization: Bearer <access_token>`
- **Responses:**
  - `200 OK`:
    ```json
    {
      "id": 1,
      "name": "Mayara Ferreira",
      "email": "mayara@empresa.com.br",
      "tenant_id": 101,
      "role": "admin"
    }
    ```
  - `401 Unauthorized`: `{ "error": "Token ausente ou inválido." }`

---

## 2. Motor de Benchmarking Setorial (`POST /api/v1/compare-companies`)
*Referência canônica para US-05, US-12 e US-13 (#51).*

Permite comparar de 2 a 5 fornecedores em ranqueamento bayesiano com IC 90% e paridade matemática absoluta com o `/overview`.

- **Headers:** `Content-Type: application/json`, `X-Tenant-ID: <id opcional>`
- **Request Body:**
  Suporta `search_ids`, `cnpjs`, `companies` ou `items`:
  ```json
  {
    "search_ids": [136, 137]
  }
  ```
- **Responses:**
  - `200 OK`:
    ```json
    {
      "empresas_ranqueadas": [
        {
          "posicao_ranking": 1,
          "empresa": "BANCO BRADESCO S.A.",
          "cnpj": "51990695000137",
          "capital_social": "R$ 117.847.056.845,00",
          "capital_social_num": 117847056845.0,
          "risco_subsidiario": 15.5,
          "probabilidade_condenacao": 86.2,
          "faixa_decisao": "CAUTELA",
          "recomendacao": "Contratar com cautela. Recomendação de retenção técnica de 10% do valor contratual para contingências.",
          "ic_90": {
            "inferior": 43.1,
            "superior": 97.5,
            "largura_pp": 54.4,
            "formatado": "43.1% a 97.5%"
          },
          "cenario": "CENARIO_A",
          "total_processos": 1013,
          "previsao_bloqueada": false
        },
        {
          "posicao_ranking": 2,
          "empresa": "SAUTER (GRUPO CONSOLIDADO / FILIAIS)",
          "cnpj": "30615825000181",
          "capital_social": "R$ 20.000,00",
          "capital_social_num": 20000.0,
          "risco_subsidiario": 84.2,
          "probabilidade_condenacao": 88.6,
          "faixa_decisao": "REPROVAR",
          "recomendacao": "Reprovar / Alto risco subsidiário de calote trabalhista (Súmula 331 TST).",
          "ic_90": {
            "inferior": 45.9,
            "superior": 94.4,
            "largura_pp": 48.5,
            "formatado": "45.9% a 94.4%"
          },
          "cenario": "CENARIO_B",
          "total_processos": 404,
          "previsao_bloqueada": false
        }
      ],
      "resumo_setorial": {
        "total_empresas_comparadas": 2,
        "mesmo_segmento": true,
        "cnae_comum": {
          "codigo": "6201-5/00",
          "descricao": "Desenvolvimento de programas de computador"
        },
        "alerta_setorial": null,
        "fator_risco_relativo": 5.43,
        "fator_risco_relativo_texto": "A fornecedora de maior risco apresenta exposição subsidiária 5.43x superior à melhor colocada.",
        "melhor_opcao": {
          "posicao_ranking": 1,
          "empresa": "BANCO BRADESCO S.A.",
          "cnpj": "51990695000137",
          "risco_subsidiario": 15.5,
          "faixa_decisao": "CAUTELA"
        },
        "pior_opcao": {
          "posicao_ranking": 2,
          "empresa": "SAUTER (GRUPO CONSOLIDADO / FILIAIS)",
          "cnpj": "30615825000181",
          "risco_subsidiario": 84.2,
          "faixa_decisao": "REPROVAR"
        }
      },
      "grafico_comparativo": {
        "labels": ["BANCO BRADESCO S.A.", "SAUTER (GRUPO CONSOLIDADO / FILIAIS)"],
        "series": {
          "probabilidade_condenacao_bayes": [86.2, 88.6],
          "risco_subsidiario_sumula_331": [15.5, 84.2]
        }
      },
      "parecer_comparativo": {
        "recomendacao_compra": "Recomenda-se a contratação prioritária de BANCO BRADESCO S.A., classificada em 1º lugar.",
        "primeiro_lugar": "BANCO BRADESCO S.A.",
        "alerta_critico": "Alerta crítico: SAUTER apresenta risco subsidiário de 84.2%...",
        "economia_potencial_texto": "A contratação prioritária mitiga a exposição em até 68.7 pontos percentuais...",
        "justificativa_executiva": "Comparativo técnico entre 2 fornecedores com base em inferência bayesiana..."
      }
    }
    ```
  - `400 Bad Request`: `{ "error": "É necessário informar entre 2 e 5 empresas para realizar o comparativo setorial." }`
  - `403 Forbidden`: `{ "error": "Acesso não autorizado aos dados da busca solicitada." }`
  - `429 Too Many Requests`: `{ "error": "Limite de requisições excedido.", "retry_after": 60 }`

---

## 3. Ingestão de Varredura Completa (`POST /api/v1/ingest-deep-scan`)
*Referência canônica para US-14 e US-16.*

Permite que o navegador ou robôs de coleta assíncrona transmitam lotes de publicações e processos do Comunica PJe / DataJud para persistência progressiva, consolidação de desfechos e reavaliação de risco em tempo real.

- **Headers:** `Content-Type: application/json`, `X-Tenant-ID: <id opcional>`
- **Request Body:**
  ```json
  {
    "search_id": 136,
    "is_final_batch": true,
    "items": [
      {
        "numeroProcesso": "00001234520245020001",
        "siglaTribunal": "TRT2",
        "publicacoes_texto": [
          "Intimação da empresa sobre pagamento de horas extras e insalubridade."
        ],
        "classes_pje": ["ATOrd"],
        "destinatarios": [
          { "nome": "EMPRESA ALVO S.A.", "numeroDocumento": "11.222.333/0001-44" }
        ]
      }
    ]
  }
  ```
- **Limites e Regras:**
  - `items`: Lista de 1 a 2.000 itens (lotes maiores retornam `413 Payload Too Large`).
  - `is_final_batch`: Se `true`, define `scan_status = 'COMPLETED'`; se `false`, define `scan_status = 'PARTIAL_FAST_PATH'`.
- **Responses:**
  - `200 OK`:
    ```json
    {
      "status": "success",
      "search_id": 136,
      "company_name": "EMPRESA ALVO S.A.",
      "scan_status": "COMPLETED",
      "total_processes": 903,
      "ingested_count": 853,
      "desfechos_sentencas": {
        "condenacoes": 210,
        "acordos": 340,
        "improcedentes": 280,
        "em_aberto": 73
      },
      "score_risco_terceirizacao": 23.4,
      "probabilidade_condenacao_geral_pct": 25.1,
      "previsao_bloqueada": false
    }
    ```
  - `400 Bad Request`: `{ "error": "Campo 'search_id' obrigatório e 'items' deve ser uma lista não vazia." }`
  - `404 Not Found`: `{ "error": "Registro de busca ID 136 não encontrado." }`
  - `413 Payload Too Large`: `{ "error": "O lote de ingestão excede o limite máximo de 2.000 itens por requisição." }`
  - `429 Too Many Requests`: `{ "error": "Limite de requisições excedido.", "retry_after": 60 }`

---

## 4. Consulta e Filtros de Processos (`GET /api/v1/searches/<id>/processes`)
*Referência canônica para US-06 e US-08.*

Fornece paginação, ordenação e filtros detalhados sobre os processos consolidados de uma empresa analisada.

- **Path:** `/api/v1/searches/<int:search_id>/processes`
- **Query Parameters:**
  - `page`: Número da página (inteiro, padrão `1`).
  - `per_page`: Itens por página (inteiro, padrão `20`, máximo `100`).
  - `tribunal`: Sigla do tribunal (ex: `TRT2`, `TST`).
  - `outcome`: Desfecho classificado (`condenacao`, `acordo`, `improcedente`, `em_aberto`).
  - `min_value`: Valor mínimo da causa/condenação (float).
  - `max_value`: Valor máximo da causa/condenação (float).
  - `search`: Busca textual por termo ou número CNJ.
- **Responses:**
  - `200 OK`:
    ```json
    {
      "search_id": 136,
      "company_name": "EMPRESA ALVO S.A.",
      "page": 1,
      "per_page": 20,
      "total": 903,
      "total_pages": 46,
      "processes": [
        {
          "id": 1042,
          "numero_processo": "0000123-45.2024.5.02.0001",
          "tribunal": "TRT2",
          "grau": "1º Grau",
          "primary_subject": "Horas Extras",
          "sentence_outcome": "condenacao",
          "has_expert_report": true,
          "observation": "Condenação em horas extras e reflexos.",
          "created_at": "2026-11-22T21:15:00Z"
        }
      ]
    }
    ```
  - `404 Not Found`: `{ "error": "Pesquisa ID 136 não encontrada." }`

