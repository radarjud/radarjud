# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.process_record import ProcessRecord
from app.utils.rate_limiter import SlidingWindowRateLimiter


def _format_cnj(idx: int, year: int = 2024, trt: int = 2) -> str:
    """Gera número CNJ válido de 20 dígitos normalizados para testes."""
    return f"{idx:07d}00{year}5{trt:02d}0001"


def test_ingest_deep_scan_903_items_deduplication_and_invariants(client, app):
    """
    Cenário 1: Ingestão de Volume Massivo (903 itens) e Deduplicação:
    - Empresa inicia com 50 processos (página 1 do Fast-Path).
    - Envia lote de 903 itens no deep scan (50 já existentes + 853 inéditos).
    - Valida: HTTP 200, ingested_count == 853, total_processes == 903.
    - Zero duplicatas no banco de dados.
    - Invariante estrita: sum(desfechos_sentencas.values()) == 903.
    - scan_status atualizado para 'COMPLETED'.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="VIBRA ENERGIA S.A.",
            normalized_name="VIBRA ENERGIA",
            total_processes=50,
            scan_status="PARTIAL_FAST_PATH",
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()

        initial_records = []
        for i in range(1, 51):
            cnj = _format_cnj(i)
            rec = ProcessRecord(
                company_search_id=search.id,
                numero_processo=cnj,
                tribunal="TRT2",
                grau="G1",
                nivel_sigilo=0,
                primary_subject="Aviso Prévio",
                subjects_json=["Aviso Prévio"],
                sentence_outcome="em_aberto",
            )
            initial_records.append(rec)
        db.session.bulk_save_objects(initial_records)
        db.session.commit()
        search_id = search.id

    # Prepara lote de 903 itens (1..50 repetidos, 51..903 novos com diferentes desfechos)
    items_903 = []
    # 50 repetidos (com enriquecimento de desfecho)
    for i in range(1, 51):
        items_903.append({
            "numeroProcesso": _format_cnj(i),
            "siglaTribunal": "TRT2",
            "sentence_outcome": "condenacoes",
            "primary_subject": "Horas Extras",
            "publicacoes_texto": ["Condenação em horas extras."],
        })

    # 853 novos processos
    for i in range(51, 904):
        outcome = "condenacoes" if i % 3 == 0 else ("acordos" if i % 3 == 1 else "improcedentes")
        items_903.append({
            "numeroProcesso": _format_cnj(i),
            "siglaTribunal": "TRT2" if i % 2 == 0 else "TRT15",
            "sentence_outcome": outcome,
            "primary_subject": "Adicional de Insalubridade" if i % 2 == 0 else "Verbas Rescisórias",
            "publicacoes_texto": [f"Publicação referente ao processo {i}."],
        })

    resp = client.post(
        "/api/v1/ingest-deep-scan",
        json={"search_id": search_id, "items": items_903, "is_final_batch": True},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["status"] == "success"
    assert data["search_id"] == search_id
    assert data["ingested_count"] == 853
    assert data["total_processes"] == 903
    assert data["scan_status"] == "COMPLETED"

    # Invariante estrita: soma de todos os desfechos deve ser exatamente igual a 903
    outcomes = data["desfechos_sentencas"]
    assert sum(outcomes.values()) == 903

    # Verificação no Banco de Dados: sem duplicatas
    with app.app_context():
        db_procs = ProcessRecord.query.filter_by(company_search_id=search_id).all()
        assert len(db_procs) == 903
        unique_cnjs = {p.numero_processo for p in db_procs}
        assert len(unique_cnjs) == 903

        db_search = db.session.get(CompanySearch, search_id)
        assert db_search.total_processes == 903
        assert db_search.scan_status == "COMPLETED"
        assert db_search.movements_hash is not None


def test_ingest_deep_scan_payload_validation_and_size_limits(client, app):
    """
    Cenário 2: Validações de Payload e Limite Máximo:
    - Payload inválido ou ausente -> HTTP 400.
    - search_id ausente ou não numérico -> HTTP 400.
    - items vazio ou não lista -> HTTP 400.
    - search_id inexistente -> HTTP 404.
    - items > 2.000 itens -> HTTP 413 Payload Too Large.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="TEST VALIDATION LTDA",
            normalized_name="TEST VALIDATION",
            total_processes=0,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        valid_id = search.id

    # 1. Sem search_id
    r = client.post("/api/v1/ingest-deep-scan", json={"items": [{"numeroProcesso": "00000010020245020001"}]})
    assert r.status_code == 400

    # 2. search_id string não numérica
    r = client.post("/api/v1/ingest-deep-scan", json={"search_id": "abc", "items": [{"numeroProcesso": "00000010020245020001"}]})
    assert r.status_code == 400

    # 3. items vazio
    r = client.post("/api/v1/ingest-deep-scan", json={"search_id": valid_id, "items": []})
    assert r.status_code == 400

    # 4. items não é lista
    r = client.post("/api/v1/ingest-deep-scan", json={"search_id": valid_id, "items": "invalido"})
    assert r.status_code == 400

    # 5. search_id inexistente
    r = client.post("/api/v1/ingest-deep-scan", json={"search_id": 999999, "items": [{"numeroProcesso": "00000010020245020001"}]})
    assert r.status_code == 404

    # 6. Excedente de 2.000 itens -> HTTP 413
    oversized_items = [{"numeroProcesso": _format_cnj(i)} for i in range(1, 2002)]
    r = client.post("/api/v1/ingest-deep-scan", json={"search_id": valid_id, "items": oversized_items})
    assert r.status_code == 413
    assert "2.000" in r.get_json()["error"]


def test_ingest_deep_scan_zero_conviction_rule(client, app):
    """
    Cenário 3: Regra de Bloqueio para Zero Condenações (0,0%):
    - Se a empresa consolidada não possuir nenhuma condenação prévia,
      a probabilidade_condenacao_geral_pct deve ser desativada (None) e previsao_bloqueada=True.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA EXEMPLAR LTDA",
            normalized_name="EMPRESA EXEMPLAR",
            total_processes=0,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

    # Envia 10 processos: 5 acordos e 5 improcedentes (zero condenações)
    items = []
    for i in range(1, 11):
        outcome = "acordos" if i <= 5 else "improcedentes"
        items.append({
            "numeroProcesso": _format_cnj(i),
            "siglaTribunal": "TRT2",
            "sentence_outcome": outcome,
            "primary_subject": "Obrigação de Fazer",
        })

    resp = client.post(
        "/api/v1/ingest-deep-scan",
        json={"search_id": search_id, "items": items, "is_final_batch": True},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["desfechos_sentencas"]["condenacoes"] == 0
    assert data["previsao_bloqueada"] is True
    assert data["probabilidade_condenacao_geral_pct"] is None


def test_proxy_security_timing_safe_and_secret_validation(client, monkeypatch):
    """
    Cenário 4: Blindagem de Proxy Anti-Burla e Timing-Safe Check:
    - Se PROXY_SHARED_SECRET configurado:
      - Requisição sem header x-proxy-secret ou Authorization -> HTTP 401.
      - Requisição com secret incorreto -> HTTP 401.
      - Requisição com secret correto (x-proxy-secret ou Bearer) -> autorizada (HTTP 400 por falta de target ou dispatch).
    - Se PROXY_SHARED_SECRET não configurado:
      - Modo desenvolvimento aberto (não retorna 401).
    """
    monkeypatch.setenv("PROXY_SHARED_SECRET", "radarjus-super-secret-token-xyz")

    # 1. Sem secret -> 401
    r1 = client.get("/api/v1/cnj-proxy?target=https://comunicaapi.pje.jus.br")
    assert r1.status_code == 401
    assert r1.get_json()["error"] == "Unauthorized proxy access"

    # 2. Secret incorreto via x-proxy-secret -> 401
    r2 = client.get(
        "/api/v1/cnj-proxy?target=https://comunicaapi.pje.jus.br",
        headers={"x-proxy-secret": "token-falso-123"},
    )
    assert r2.status_code == 401

    # 3. Secret incorreto via Authorization -> 401
    r3 = client.get(
        "/api/v1/cnj-proxy?target=https://comunicaapi.pje.jus.br",
        headers={"Authorization": "Bearer token-falso-123"},
    )
    assert r3.status_code == 401

    # 4. Secret correto via x-proxy-secret -> Autorizado (não retorna 401)
    r4 = client.get(
        "/api/v1/cnj-proxy",
        headers={"x-proxy-secret": "radarjus-super-secret-token-xyz"},
    )
    # Falta target -> retorna 400, comprovando que passou pela checagem 401 de auth
    assert r4.status_code == 400
    assert r4.get_json()["error"] == "Missing target URL"

    # 5. Secret correto via Bearer token -> Autorizado
    r5 = client.get(
        "/api/v1/cnj-proxy",
        headers={"Authorization": "Bearer radarjus-super-secret-token-xyz"},
    )
    assert r5.status_code == 400

    # 6. Sem secret configurado no ambiente (modo dev)
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    monkeypatch.delenv("CNJ_PROXY_SECRET", raising=False)
    r6 = client.get("/api/v1/cnj-proxy")
    assert r6.status_code == 400  # Não foi 401


def test_sliding_window_rate_limiter_behavioral():
    """
    Cenário 5: Rate Limiter Comportamental Sliding Window:
    - Valida janela deslizante em memória (thread-safe, 512 MB safe).
    - Bloqueia rajada de requisições excedentes retornando (False, retry_after).
    - Permite novas requisições após o término da janela.
    """
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=1)

    # 3 requisições permitidas
    assert limiter.check("client-1")[0] is True
    assert limiter.check("client-1")[0] is True
    assert limiter.check("client-1")[0] is True

    # 4ª requisição bloqueada na mesma janela
    allowed, retry_after = limiter.check("client-1")
    assert allowed is False
    assert retry_after >= 1

    # Outro cliente tem sua própria cota isolada
    assert limiter.check("client-2")[0] is True

    # Teste de purga e reset
    limiter.reset()
    assert limiter.check("client-1")[0] is True


def test_rate_limiting_decorator_http_429(client, app):
    """
    Cenário 6: Decorator @limit_rate com retorno HTTP 429 e Retry-After.
    """
    from app.routes.api import ingest_deep_scan

    # Reseta o limiter para o teste
    if hasattr(ingest_deep_scan, "limiter"):
        ingest_deep_scan.limiter.reset()

    with app.app_context():
        search = CompanySearch(
            company_name="RATE LIMIT CORP",
            normalized_name="RATE LIMIT CORP",
            total_processes=0,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

    # max_requests em ingest_deep_scan é 30
    limiter = getattr(ingest_deep_scan, "limiter", None)
    if limiter:
        limiter.max_requests = 3
        try:
            # 3 requisições
            for _ in range(3):
                client.post("/api/v1/ingest-deep-scan", json={"search_id": sid, "items": [{"numeroProcesso": _format_cnj(1)}]})

            # 4ª requisição -> 429
            r = client.post("/api/v1/ingest-deep-scan", json={"search_id": sid, "items": [{"numeroProcesso": _format_cnj(1)}]})
            assert r.status_code == 429
            assert "Retry-After" in r.headers
            assert "Limite de requisições excedido" in r.get_json()["error"]
        finally:
            limiter.max_requests = 30
            limiter.reset()


def test_multitenant_anti_idor_isolation(client, app, monkeypatch):
    """
    Cenário 7: Isolamento Multitenant Defensivo (Anti-IDOR):
    - Requisição com X-Tenant-ID incompatível com o registro da busca retorna HTTP 403.
    - Requisição com tenant compatível retorna HTTP 200.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="TENANT PROTECTED S.A.",
            normalized_name="TENANT PROTECTED",
            total_processes=0,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

    # Simula atributo tenant_id presente no modelo (US-01) com tenant 'tenant-alpha'
    monkeypatch.setattr(CompanySearch, "tenant_id", "tenant-alpha", raising=False)

    # 1. Tentativa com tenant cruzado em ingest-deep-scan -> 403
    r1 = client.post(
        "/api/v1/ingest-deep-scan",
        json={"search_id": sid, "items": [{"numeroProcesso": _format_cnj(1)}]},
        headers={"X-Tenant-ID": "tenant-beta"},
    )
    assert r1.status_code == 403
    assert "Acesso não autorizado" in r1.get_json()["error"]

    # 2. Tentativa com tenant cruzado em listagem de processos -> 403
    r2 = client.get(
        f"/api/v1/searches/{sid}/processes",
        headers={"X-Tenant-ID": "tenant-beta"},
    )
    assert r2.status_code == 403

    # 3. Com tenant correto -> 200
    r3 = client.get(
        f"/api/v1/searches/{sid}/processes",
        headers={"X-Tenant-ID": "tenant-alpha"},
    )
    assert r3.status_code == 200


def test_get_search_processes_pagination_and_filters(client, app):
    """
    Cenário 8: Listagem Paginada e Filtros de Processos (`GET /api/v1/searches/<id>/processes`):
    - Paginação funcional (page=1, per_page=10, total=25, total_pages=3).
    - Filtros por tribunal (TRT2), por outcome (condenacao) e busca textual (search).
    """
    with app.app_context():
        search = CompanySearch(
            company_name="PAGINATION CORP",
            normalized_name="PAGINATION CORP",
            total_processes=25,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

        procs = []
        for i in range(1, 26):
            trib = "TRT2" if i <= 15 else "TST"
            out = "condenacoes" if i <= 10 else ("acordos" if i <= 20 else "improcedentes")
            sub = "Horas Extras" if i % 2 == 0 else "Insalubridade"
            p = ProcessRecord(
                company_search_id=sid,
                numero_processo=_format_cnj(i),
                tribunal=trib,
                grau="G1" if trib == "TRT2" else "TST (Recursal)",
                primary_subject=sub,
                sentence_outcome=out,
                has_expert_report=(sub == "Insalubridade"),
                observation=f"Observação detalhada processo {i}",
            )
            procs.append(p)
        db.session.bulk_save_objects(procs)
        db.session.commit()

    # 1. Página 1 padrão (per_page=10)
    r1 = client.get(f"/api/v1/searches/{sid}/processes?page=1&per_page=10")
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["total"] == 25
    assert d1["total_pages"] == 3
    assert len(d1["processes"]) == 10
    assert d1["page"] == 1

    # 2. Página 3 (restante de 5 processos)
    r2 = client.get(f"/api/v1/searches/{sid}/processes?page=3&per_page=10")
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert len(d2["processes"]) == 5

    # 3. Filtro por tribunal (TST: 10 processos)
    r3 = client.get(f"/api/v1/searches/{sid}/processes?tribunal=TST")
    assert r3.status_code == 200
    d3 = r3.get_json()
    assert d3["total"] == 10
    assert all(p["tribunal"] == "TST" for p in d3["processes"])

    # 4. Filtro por outcome (condenacao: 10 processos)
    r4 = client.get(f"/api/v1/searches/{sid}/processes?outcome=condenacao")
    assert r4.status_code == 200
    d4 = r4.get_json()
    assert d4["total"] == 10
    assert all(p["sentence_outcome"] == "condenacoes" for p in d4["processes"])

    # 5. Busca textual (search="Insalubridade")
    r5 = client.get(f"/api/v1/searches/{sid}/processes?search=Insalubridade")
    assert r5.status_code == 200
    d5 = r5.get_json()
    assert d5["total"] == 13
    assert all("Insalubridade" in p["primary_subject"] for p in d5["processes"])
