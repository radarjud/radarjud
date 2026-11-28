"""
Testes automatizados para a US-23:
Paginação Diluída com Persistência por Lote, Freio Anti-Loop e Limite Dinâmico (Local vs. Render).
"""

import os
from unittest.mock import MagicMock, patch
import pytest

from app.services.cnj_searcher import (
    CNJProcessSearcher,
    get_scan_config,
)
from app.models.company_search import CompanySearch
from app.models.process_record import ProcessRecord
from app.extensions import db


def _build_dummy_item(cnj_num: str, company: str = "EMPRESA TESTE"):
    return {
        "numeroProcesso": cnj_num,
        "siglaTribunal": "TRT02",
        "texto": f"Intimação da reclamada {company} nos autos do processo {cnj_num}.",
        "dataDisponibilizacao": "2024-05-10",
        "nomeParte": company,
        "classe": "ATSum",
    }


def test_dynamic_env_config_local_vs_render():
    """
    Cenário 3 da US-23:
    Valida a configuração dinâmica por ambiente (Local vs. Render).
    - Local (RENDER ausente ou != 'true'): SCAN_MAX_PAGES=0 (ilimitado), SCAN_BATCH_DELAY=0.0, SCAN_CONCURRENCY=1, gc=False.
    - Render (RENDER='true'): SCAN_MAX_PAGES=300, SCAN_BATCH_DELAY=0.0 (pacing adaptativo), SCAN_CONCURRENCY=1, gc=True.
    - Suporta override de variáveis de ambiente SCAN_MAX_PAGES, SCAN_BATCH_DELAY e SCAN_CONCURRENCY.
    """
    # 1. Ambiente Local puro (sem RENDER)
    with patch.dict(os.environ, {}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is False
        assert cfg["max_pages"] == 0
        assert cfg["page_size"] == 100
        assert cfg["batch_delay"] == 0.0
        assert cfg["concurrency"] == 1
        assert cfg["gc_collect"] is False

    # 2. Ambiente Render
    with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is True
        assert cfg["max_pages"] == 300
        assert cfg["page_size"] == 100
        assert cfg["batch_delay"] == 0.0
        assert cfg["concurrency"] == 1
        assert cfg["gc_collect"] is True

    # 3. Override de variáveis em ambiente Local
    with patch.dict(os.environ, {"SCAN_MAX_PAGES": "25", "SCAN_BATCH_DELAY": "0.15", "SCAN_CONCURRENCY": "2"}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is False
        assert cfg["max_pages"] == 25
        assert cfg["batch_delay"] == 0.15
        assert cfg["concurrency"] == 2

    # 4. Override de variáveis em ambiente Render
    with patch.dict(os.environ, {"RENDER": "true", "SCAN_MAX_PAGES": "40", "SCAN_BATCH_DELAY": "0.2", "SCAN_CONCURRENCY": "2"}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is True
        assert cfg["max_pages"] == 40
        assert cfg["batch_delay"] == 0.2
        assert cfg["concurrency"] == 2


def test_multiple_valid_pages_streaming_and_completed():
    """
    Cenário 1 da US-23:
    Múltiplas páginas válidas retornadas pela API.
    Valida:
    - Persistência streaming página por página via on_batch_persist.
    - Atualização progressiva de contadores via on_page_callback.
    - Encerramento natural (página 3 com menos de 50 itens) com status canônico COMPLETED.
    """
    searcher = CNJProcessSearcher()

    page1_items = [_build_dummy_item(f"10000000120245020{i:03d}") for i in range(50)]
    page2_items = [_build_dummy_item(f"20000000120245020{i:03d}") for i in range(50)]
    page3_items = [_build_dummy_item(f"30000000120245020{i:03d}") for i in range(15)]

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        if p == 1:
            resp.json.return_value = {"items": page1_items}
        elif p == 2:
            resp.json.return_value = {"items": page2_items}
        elif p == 3:
            resp.json.return_value = {"items": page3_items}
        else:
            resp.json.return_value = {"items": []}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    batches_persisted = []
    def on_batch(page, batch_procs, new_count):
        batches_persisted.append((page, len(batch_procs), new_count))

    progress_updates = []
    def on_page(page, total_procs, procs_map):
        progress_updates.append((page, total_procs))

    strat = {"comunica_query_term": "EMPRESA MULTIPAGINAS", "mode": "STRICT_RAZAO"}
    res = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages=10,
        on_batch_persist=on_batch,
        on_page_callback=on_page,
        batch_delay=0.0,
    )

    # 1. Leu exatamente as 3 páginas
    assert res["pages_read"] == 3
    # 2. Total de processos únicos foi 115 (50 + 50 + 15)
    assert res["total_processes"] == 115
    # 3. Terminou naturalmente: is_completed = True, sem corte por teto
    assert res["is_completed"] is True
    assert res["hit_max_pages"] is False
    assert res["anti_loop_triggered"] is False

    # 4. Valida persistência lote a lote: 3 lotes com 50, 50 e 15 CNJs novos
    assert len(batches_persisted) == 3
    assert batches_persisted[0] == (1, 50, 50)
    assert batches_persisted[1] == (2, 50, 50)
    assert batches_persisted[2] == (3, 15, 15)

    # 5. Callback progressivo chamado a cada página
    assert len(progress_updates) == 3
    assert progress_updates[0] == (1, 50)
    assert progress_updates[1] == (2, 100)
    assert progress_updates[2] == (3, 115)


def test_anti_loop_brake_zero_new_cnjs():
    """
    Cenário 2 da US-23:
    Simula repetição da última página pela API (cenário clássico Bradesco / DJEN em loop).
    A API retorna página 1 com 50 itens e página 2 repetindo exatamente os mesmos itens.
    Valida:
    - O freio anti-loop detecta 0 novos CNJs na página 2.
    - A varredura é encerrada imediatamente sem entrar em loop infinito.
    - Status final é canônico COMPLETED (pois todos os processos reais já foram coletados).
    - Não gera falso status parcial.
    """
    searcher = CNJProcessSearcher()

    same_items = [_build_dummy_item(f"50000000120245020{i:03d}") for i in range(50)]

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        # A API retorna sempre os mesmos 50 itens independentemente da página solicitada
        resp.json.return_value = {"items": same_items}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    batches_persisted = []
    def on_batch(page, batch_procs, new_count):
        batches_persisted.append((page, len(batch_procs), new_count))

    strat = {"comunica_query_term": "BANCO REPETIDOR S.A.", "mode": "BROAD_BRAND"}
    res = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages=20,
        on_batch_persist=on_batch,
        batch_delay=0.0,
    )

    # Validações cruciais do freio anti-loop:
    # 1. Parou na página 2 ao identificar 0 CNJs inéditos
    assert res["pages_read"] == 2
    assert res["anti_loop_triggered"] is True
    # 2. Total de processos únicos coletados é 50
    assert res["total_processes"] == 50
    # 3. Status final é COMPLETED (fim real de registros novos, não corte por teto)
    assert res["is_completed"] is True
    assert res["hit_max_pages"] is False
    # 4. Apenas o primeiro lote (com 50 itens novos) foi persistido
    assert len(batches_persisted) == 1
    assert batches_persisted[0] == (1, 50, 50)


def test_coherent_final_status_completed_vs_partial():
    """
    Cenário 4 da US-23:
    Valida coerência do status final entre conclusão integral vs. atingimento de teto.
    - Conclusão natural com menos de 50 itens -> is_completed = True, hit_max_pages = False.
    - Corte por teto com mais itens disponíveis -> is_completed = False, hit_max_pages = True.
    """
    searcher = CNJProcessSearcher()

    full_page = [_build_dummy_item(f"70000000120245020{i:03d}") for i in range(50)]

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        # Gera itens distintos para cada página
        page_items = [_build_dummy_item(f"7{p:03d}0000120245020{i:03d}") for i in range(50)]
        resp.json.return_value = {"items": page_items}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    strat = {"comunica_query_term": "EMPRESA GIGANTE", "mode": "BROAD_BRAND"}
    # Teto restrito a 2 páginas
    res = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages=2,
        batch_delay=0.0,
    )

    # Como a página 2 ainda tinha 50 itens inéditos e atingiu max_pages=2:
    assert res["hit_max_pages"] is True
    assert res["is_completed"] is False
    assert res["pages_read"] == 2


def test_deep_scan_background_worker_streaming_persistence(app):
    """
    Teste de integração do worker em background:
    Valida que os processos são persistidos página a página no banco de dados
    e que o scan_status final é marcado como COMPLETED sem o falso selo de parcial.
    """
    from app.routes.api import _deep_scan_background_worker

    with app.app_context():
        # Cria busca inicial
        search = CompanySearch(
            company_name="EMPRESA STREAMING LTDA",
            normalized_name="EMPRESA STREAMING LTDA",
            total_processes=10,
            scan_status="PARTIAL_FAST_PATH",
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        # Simula processos da página 1 (já existentes)
        page1_procs = [
            {"numero_processo": f"80000000120245020{i:03d}", "tribunal": "TRT02"}
            for i in range(10)
        ]
        for p in page1_procs:
            rec = ProcessRecord(
                company_search_id=search_id,
                numero_processo=p["numero_processo"],
                tribunal="TRT02",
                sentence_outcome="em_aberto",
            )
            db.session.add(rec)
        db.session.commit()

        # Mock da busca de páginas 2 e 3
        page2_items = [_build_dummy_item(f"80000000220245020{i:03d}") for i in range(50)]
        page3_items = [_build_dummy_item(f"80000000320245020{i:03d}") for i in range(10)]

        def mock_request(method, url, params=None, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            p = params.get("pagina", 1) if params else 1
            if p == 2:
                resp.json.return_value = {"items": page2_items}
            elif p == 3:
                resp.json.return_value = {"items": page3_items}
            else:
                resp.json.return_value = {"items": []}
            return resp

        strat = {
            "comunica_query_term": "EMPRESA STREAMING",
            "mode": "BROAD_BRAND",
            "is_broad_brand": True,
        }

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request):
            with patch.dict(os.environ, {"RENDER": "false", "SCAN_MAX_PAGES": "5"}, clear=False):
                _deep_scan_background_worker(
                    app=app,
                    search_id=search_id,
                    company_name="EMPRESA STREAMING LTDA",
                    search_strategy=strat,
                    job_id="test-job-us23",
                    page1_processes=page1_procs,
                )

        # Recarrega a busca do banco
        updated_search = db.session.get(CompanySearch, search_id)
        assert updated_search is not None
        # O status final DEVE ser COMPLETED (conclusão natural de todas as páginas)
        assert updated_search.scan_status == "COMPLETED"
        # Total de processos deve ser 10 (pág 1) + 50 (pág 2) + 10 (pág 3) = 70
        assert updated_search.total_processes == 70

        # Verifica se todos os registros foram persistidos no PostgreSQL / SQLite
        db_records_count = db.session.query(ProcessRecord).filter(
            ProcessRecord.company_search_id == search_id
        ).count()
        assert db_records_count == 70


def test_deep_scan_background_worker_partial_on_max_pages_ceiling(app):
    """
    Valida que quando a varredura atinge o teto configurado de SCAN_MAX_PAGES
    com mais registros disponíveis na API oficial, o status final é marcado
    como PARTIAL_FAST_PATH (refletindo fielmente a varredura parcial).
    """
    from app.routes.api import _deep_scan_background_worker

    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA COM TETO ATINGIDO",
            normalized_name="EMPRESA COM TETO ATINGIDO",
            total_processes=5,
            scan_status="PARTIAL_FAST_PATH",
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        page1_procs = [
            {"numero_processo": f"90000000120245020{i:03d}", "tribunal": "TRT02"}
            for i in range(5)
        ]
        for p in page1_procs:
            rec = ProcessRecord(
                company_search_id=search_id,
                numero_processo=p["numero_processo"],
                tribunal="TRT02",
                sentence_outcome="em_aberto",
            )
            db.session.add(rec)
        db.session.commit()

        # Cada página tem 50 itens inéditos
        def mock_request(method, url, params=None, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            p = params.get("pagina", 1) if params else 1
            page_items = [_build_dummy_item(f"9{p:03d}0000120245020{i:03d}") for i in range(50)]
            resp.json.return_value = {"items": page_items}
            return resp

        strat = {
            "comunica_query_term": "EMPRESA COM TETO",
            "mode": "STRICT_RAZAO",
            "is_broad_brand": False,
        }

        # Configura teto de apenas 2 páginas no ambiente
        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request):
            with patch.dict(os.environ, {"RENDER": "false", "SCAN_MAX_PAGES": "2"}, clear=False):
                _deep_scan_background_worker(
                    app=app,
                    search_id=search_id,
                    company_name="EMPRESA COM TETO ATINGIDO",
                    search_strategy=strat,
                    job_id="test-job-ceiling",
                    page1_processes=page1_procs,
                )

        updated_search = db.session.get(CompanySearch, search_id)
        assert updated_search is not None
        # Como parou por teto e a página ainda tinha 50 itens, o status canônico deve ser PARTIAL_FAST_PATH
        assert updated_search.scan_status == "PARTIAL_FAST_PATH"
        # Total de processos persistidos: 5 (pág 1) + 50 (pág 2) = 55
        assert updated_search.total_processes == 55


def test_generic_normalization_and_fallback():
    """
    Valida normalização 100% genérica (hífen, barra, pontuação colada, múltiplos espaços)
    e fallback automático quando a consulta com razão social completa retorna 200 OK com 0 itens.
    """
    from app.utils.sanitizer import normalize_search_term_generic, extract_corporate_core_name

    # 1. Normalização genérica pura
    assert normalize_search_term_generic("ALFA-BETA COMERCIO & SERVICOS S.A.") == "ALFA BETA COMERCIO SERVICOS S A"
    assert normalize_search_term_generic("XPTO/RJ - SOLUCOES TECNOLOGICAS LTDA.") == "XPTO RJ SOLUCOES TECNOLOGICAS LTDA"
    assert normalize_search_term_generic("   EMPRESA   COM   ESPACOS   DUPLOS   ") == "EMPRESA COM ESPACOS DUPLOS"

    # 2. Extração genérica do núcleo da razão social
    assert extract_corporate_core_name("ALFA-BETA COMERCIO & SERVICOS S.A.") == "ALFA BETA COMERCIO SERVICOS"
    assert extract_corporate_core_name("XPTO/RJ - SOLUCOES TECNOLOGICAS LTDA.") == "XPTO RJ SOLUCOES TECNOLOGICAS"

    # 3. Fallback automático em fetch_single_comunica_page
    searcher = CNJProcessSearcher()
    fallback_items = [_build_dummy_item(f"60000000120245020{i:03d}", company="ALFA BETA") for i in range(10)]

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        p_name = params.get("nomeParte", "")
        # Se for o termo original com sufixo "S A", retorna 0 itens
        if "S A" in p_name:
            resp.json.return_value = {"items": []}
        else:
            # Fallback sem o sufixo retorna os itens
            resp.json.return_value = {"items": fallback_items}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)
    procs = searcher.fetch_single_comunica_page("ALFA-BETA COMERCIO S.A.", page_num=1)
    assert len(procs) == 10
    assert searcher.last_page_raw_count == 10


def test_pagination_intermediate_zero_new_cnjs_does_not_abort():
    """
    Valida que uma página intermediária com 0 novos CNJs (intimações repetidas de processos já vistos)
    NÃO interrompe a varredura se as páginas posteriores continuarem trazendo processos.
    """
    searcher = CNJProcessSearcher()

    page1_items = [_build_dummy_item(f"11000000120245020{i:03d}") for i in range(50)]
    # Página 2: 50 intimações repetindo processos da página 1 (mas com IDs de comunicação diferentes)
    page2_items = [
        {**_build_dummy_item(f"11000000120245020{i:03d}"), "id": f"comm_p2_{i}"}
        for i in range(50)
    ]
    # Página 3: 50 itens (50 novos CNJs inéditos)
    page3_items = [_build_dummy_item(f"13000000120245020{i:03d}") for i in range(50)]
    # Página 4: 10 novos CNJs (< 50 itens, fim natural)
    page4_items = [_build_dummy_item(f"14000000120245020{i:03d}") for i in range(10)]

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        if p == 1:
            resp.json.return_value = {"items": page1_items}
        elif p == 2:
            resp.json.return_value = {"items": page2_items}
        elif p == 3:
            resp.json.return_value = {"items": page3_items}
        elif p == 4:
            resp.json.return_value = {"items": page4_items}
        else:
            resp.json.return_value = {"items": []}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    strat = {"comunica_query_term": "EMPRESA VOLUME ALTO", "mode": "BROAD_BRAND"}
    res = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages=10,
        batch_delay=0.0,
    )

    # Não abortou na página 2! Leu as 4 páginas completas
    assert res["pages_read"] == 4
    # Total de processos: 50 (pág 1) + 50 (pág 3) + 10 (pág 4) = 110 processos únicos
    assert res["total_processes"] == 110
    assert res["is_completed"] is True
    assert res["hit_max_pages"] is False


def _comm_page(prefix_cnj: str, comm_prefix: str, size: int = 100):
    """Página sintética de 100 comunicações com IDs próprios."""
    return [
        {**_build_dummy_item(f"{prefix_cnj}{i:03d}"), "id": f"{comm_prefix}_{i}"}
        for i in range(size)
    ]


def test_zero_new_cnjs_with_unseen_communication_ids_never_stops_scan():
    """
    Empresa de alto volume: 5 páginas consecutivas só com intimações de processos já vistos
    (0 CNJs novos), mas com IDs de comunicação inéditos, seguidas de uma página com CNJs novos.
    A varredura não pode parar no meio; só encerra quando a lista vem vazia.
    """
    searcher = CNJProcessSearcher()
    seen_prefix = "2100000012024502"

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        if p == 1:
            items = _comm_page(seen_prefix + "0", "p1")
        elif 2 <= p <= 6:
            # Mesmos 100 processos da página 1, porém comunicações diferentes
            items = _comm_page(seen_prefix + "0", f"p{p}")
        elif p == 7:
            items = _comm_page("2200000012024502" + "0", "p7")  # 100 CNJs novos
        else:
            items = []
        resp.json.return_value = {"items": items}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    res = searcher.paginate_comunica_pje_standard(
        search_strategy={"comunica_query_term": "EMPRESA ALTO VOLUME", "mode": "STRICT_RAZAO"},
        start_page=1,
        max_pages=0,
        batch_delay=0.0,
    )

    assert res["pages_read"] == 7
    assert res["total_processes"] == 200
    assert res["anti_loop_triggered"] is False
    assert res["motivo_encerramento"] == "FIM_NATURAL_LISTA_VAZIA"
    assert res["is_completed"] is True


def test_scan_stops_only_when_communication_ids_repeat():
    """Loop real da API: a página 4 devolve exatamente as comunicações da página 3."""
    searcher = CNJProcessSearcher()

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        if p <= 3:
            items = _comm_page(f"2{p}00000012024502" + "0", f"p{p}")
        else:
            items = _comm_page("2300000012024502" + "0", "p3")  # repete a página 3
        resp.json.return_value = {"items": items}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)
    res = searcher.paginate_comunica_pje_standard(
        search_strategy={"comunica_query_term": "EMPRESA LOOP API", "mode": "STRICT_RAZAO"},
        start_page=1,
        max_pages=0,
        batch_delay=0.0,
    )

    assert res["anti_loop_triggered"] is True
    assert res["motivo_encerramento"] == "REPETICAO_EXATA_IDS_COMUNICACAO"
    assert res["total_processes"] == 300
    assert res["is_completed"] is True


def test_repeated_page_detected_across_http_batches():
    """A última página do lote anterior chega via prev_page_signature e detecta loop na fronteira."""
    searcher = CNJProcessSearcher()
    same_page = _comm_page("2400000012024502" + "0", "fixo")

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"items": same_page}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)
    strat = {"comunica_query_term": "EMPRESA FRONTEIRA", "mode": "STRICT_RAZAO"}

    first = searcher.paginate_comunica_pje_standard(
        search_strategy=strat, start_page=2, max_pages=0, max_pages_per_call=1, batch_delay=0.0,
    )
    assert first["is_completed"] is False
    assert first["next_page"] == 3
    assert first["last_page_signature"]

    second = searcher.paginate_comunica_pje_standard(
        search_strategy=strat, start_page=3, max_pages=0, max_pages_per_call=1, batch_delay=0.0,
        prev_page_signature=first["last_page_signature"],
    )
    assert second["motivo_encerramento"] == "REPETICAO_EXATA_IDS_COMUNICACAO"
    assert second["is_completed"] is True


def test_count_metadata_ends_scan_at_real_last_page():
    """Com "count" informado pela API, encerra em pagina >= ceil(count / itensPorPagina)."""
    searcher = CNJProcessSearcher()

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        resp.json.return_value = {"count": 250, "items": _comm_page(f"25{p}0000012024502" + "0", f"p{p}")}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)
    res = searcher.paginate_comunica_pje_standard(
        search_strategy={"comunica_query_term": "EMPRESA COUNT", "mode": "STRICT_RAZAO"},
        start_page=1, max_pages=0, batch_delay=0.0,
    )
    assert res["pages_read"] == 3
    assert res["motivo_encerramento"] == "TOTAL_PAGINAS_METADADOS_ATINGIDO"


def test_upstream_http_error_resumes_same_page(client, app):
    """Falha 5xx do PJe no meio do lote não encerra a busca: devolve UPSTREAM_RETRY na mesma página."""
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA INSTABILIDADE LTDA",
            normalized_name="EMPRESA INSTABILIDADE LTDA",
            total_processes=0,
            scan_status="PARTIAL_FAST_PATH",
            economic_standing={
                "labor_scan_cursor": {
                    "courts_queue": ["TRT2"],
                    "layer": 2,
                    "layer2_queue": [{"court": "TRT2", "page": 2}],
                    "court_index": 0,
                    "page": 2,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                }
            },
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        def mock_request(method, url, params=None, **kwargs):
            resp = MagicMock()
            p = params.get("pagina", 1) if params else 1
            if p == 2:
                resp.ok = True
                resp.status_code = 200
                resp.json.return_value = {"items": _comm_page("2600000012024502" + "0", "p2")}
            else:
                import time as _t
                _t.sleep(0.55)
                resp.ok = False
                resp.status_code = 503
            return resp

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request), \
                patch("app.routes.api.get_scan_config", return_value={"is_render": False, "max_pages": 0, "max_pages_per_court": 0, "page_size": 100, "batch_delay": 0.0, "concurrency": 1, "gc_collect": False}):
            # Simula time_budget curto via paginate_labor_courts_batch para retornar UPSTREAM_RETRY na pág 3
            orig_fn = CNJProcessSearcher.paginate_labor_courts_batch
            def _short_budget(self, *args, **kwargs):
                kwargs["time_budget_seconds"] = 0.5
                return orig_fn(self, *args, **kwargs)
            with patch.object(CNJProcessSearcher, "paginate_labor_courts_batch", _short_budget):
                data = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 2, "batch_size": 3}).get_json()

        assert data["status"] == "UPSTREAM_RETRY"
        assert data["has_more"] is True
        assert data["is_completed"] is False
        assert data["next_page"] == 3
        assert data["retry_after_seconds"] >= 1
        assert data["total_acumulado"] == 100


def test_batch_endpoint_ignores_render_global_page_ceiling(client, app):
    """No Render, SCAN_MAX_PAGES=50 não pode cortar a varredura em lotes (página 60+ sem teto por tribunal)."""
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA GIGANTE RENDER S.A.",
            normalized_name="EMPRESA GIGANTE RENDER S A",
            total_processes=0,
            scan_status="IN_PROGRESS",
            economic_standing={
                "labor_scan_cursor": {
                    "courts_queue": ["TRT2"],
                    "layer": 2,
                    "layer2_queue": [{"court": "TRT2", "page": 60}],
                    "court_index": 0,
                    "page": 60,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                }
            },
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        def mock_request(method, url, params=None, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            p = params.get("pagina", 1) if params else 1
            if p <= 62:
                resp.json.return_value = {"items": _comm_page(f"27{p:03d}000120245020", f"p{p}")}
            else:
                resp.json.return_value = {"items": []}
            return resp

        with patch.dict(os.environ, {"RENDER": "true", "SCAN_BATCH_DELAY": "0", "SCAN_MAX_PAGES_PER_COURT": "0"}), \
                patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request):
            data = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 60, "batch_size": 2}).get_json()

        assert data["status"] == "COMPLETED"
        assert data["total_acumulado"] == 300
        assert data["has_more"] is False


def test_rate_limit_429_returns_rate_limited_and_never_completed(app):
    """
    Valida que respostas HTTP 429 na página 1 ou na paginação NUNCA marcam a busca como COMPLETED
    com 0 processos e retornam sinalização explícita de RATE_LIMITED com retry_after.
    """
    searcher = CNJProcessSearcher()

    def mock_429(method, url, **kwargs):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 429
        resp.headers = {"Retry-After": "2"}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_429)

    # 1. Página 1 sob 429
    procs = searcher.fetch_single_comunica_page("EMPRESA RATE LIMITED", page_num=1)
    assert len(procs) == 0
    assert searcher.last_page_rate_limited is True
    assert searcher.last_page_status_code == 429

    # 2. Paginação sob 429
    strat = {"comunica_query_term": "EMPRESA RATE LIMITED", "mode": "BROAD_BRAND"}
    res = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages=5,
        batch_delay=0.0,
    )
    assert res["rate_limited"] is True
    assert res["is_completed"] is False
    assert res["retry_after_seconds"] >= 2


def test_deep_scan_batch_endpoint_short_batches(client, app):
    """
    Valida o endpoint /deep-scan-batch/<search_id> processando lotes curtos com time-box
    no cursor trabalhista sem estourar timeout HTTP no Render Free.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA BATCH SCAN S.A.",
            normalized_name="EMPRESA BATCH SCAN S A",
            total_processes=5,
            scan_status="PARTIAL_FAST_PATH",
            economic_standing={
                "labor_scan_cursor": {
                    "courts_queue": ["TRT2", "TRT15"],
                    "layer": 1,
                    "layer2_queue": [],
                    "court_index": 0,
                    "page": 2,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                }
            },
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        batch_items = [_build_dummy_item(f"33000000120245020{i:03d}") for i in range(50)]

        def mock_request(method, url, params=None, **kwargs):
            import time as _t
            _t.sleep(0.03)
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            p = params.get("pagina", 1) if params else 1
            trib = params.get("siglaTribunal") if params else None
            if trib == "TRT2" and p == 2:
                resp.json.return_value = {"items": batch_items}
            else:
                resp.json.return_value = {"items": []}
            return resp

        orig_fn = CNJProcessSearcher.paginate_labor_courts_batch

        def _short_budget(self, *args, **kwargs):
            kwargs["time_budget_seconds"] = 0.01
            return orig_fn(self, *args, **kwargs)

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request), \
                patch.object(CNJProcessSearcher, "paginate_labor_courts_batch", _short_budget):
            resp = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 2, "batch_size": 1})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["search_id"] == search_id
            assert data["status"] == "IN_PROGRESS"
            assert data["has_more"] is True
            assert data["next_page"] == 3
            assert data["total_acumulado"] >= 50


def test_deep_scan_batch_10_pages_multi_batch_flow(client, app):
    """
    Cenário Crítico Multi-Lote no cursor trabalhista:
    Simula uma empresa com 10 páginas (465 processos ao todo: 50 iniciais + 8 págs x 50 + 1 pág x 15).
    Valida persistência contínua e transição perfeita até COMPLETED.
    """
    with app.app_context():
        p1_records = [
            ProcessRecord(
                numero_processo=f"10000000120245020{i:03d}",
                tribunal="TRT2",
                sentence_outcome="em_aberto",
            )
            for i in range(50)
        ]
        search = CompanySearch(
            company_name="EMPRESA GIGANTE MULTIPAGINAS S.A.",
            normalized_name="EMPRESA GIGANTE MULTIPAGINAS S A",
            total_processes=50,
            scan_status="PARTIAL_FAST_PATH",
            economic_standing={
                "labor_scan_cursor": {
                    "courts_queue": ["TRT2"],
                    "layer": 2,
                    "layer2_queue": [{"court": "TRT2", "page": 2}],
                    "court_index": 0,
                    "page": 2,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                }
            },
            processes=p1_records,
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        pages_dict = {}
        for p in range(2, 10):
            pages_dict[p] = [_build_dummy_item(f"{p}0000000120245020{i:03d}") for i in range(50)]
        pages_dict[10] = [_build_dummy_item(f"99000000120245020{i:03d}") for i in range(15)]

        def mock_request(method, url, params=None, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            p = params.get("pagina", 1) if params else 1
            resp.json.return_value = {"items": pages_dict.get(p, [])}
            return resp

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request):
            resp_final = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 2, "batch_size": 10})
            assert resp_final.status_code == 200
            data_final = resp_final.get_json()
            assert data_final["status"] == "COMPLETED"
            assert data_final["has_more"] is False
            assert data_final["is_completed"] is True
            assert data_final["next_page"] is None
            assert data_final["total_acumulado"] == 465

        db.session.refresh(search)
        assert search.scan_status == "COMPLETED"
        assert search.total_processes == 465
        assert len(search.processes) == 465


def test_deep_scan_batch_rate_limit_resumption(client, app):
    """
    Valida comportamento de resiliência a rate limit (HTTP 429) no cursor trabalhista:
      - Página 2 processa com sucesso;
      - Página 3 recebe HTTP 429 e retorna RATE_LIMITED com next_page=3;
      - Retry retoma na página 3 e conclui como COMPLETED.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA RATE LIMIT RECOVERY LTDA",
            normalized_name="EMPRESA RATE LIMIT RECOVERY LTDA",
            total_processes=10,
            scan_status="PARTIAL_FAST_PATH",
            economic_standing={
                "labor_scan_cursor": {
                    "courts_queue": ["TRT2"],
                    "layer": 2,
                    "layer2_queue": [{"court": "TRT2", "page": 2}],
                    "court_index": 0,
                    "page": 2,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                }
            },
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        rate_limit_tested = False

        def mock_request(method, url, params=None, **kwargs):
            nonlocal rate_limit_tested
            p = params.get("pagina", 1) if params else 1
            if p == 2:
                resp = MagicMock()
                resp.ok = True
                resp.status_code = 200
                resp.json.return_value = {"items": [_build_dummy_item(f"44000000120245020{i:03d}") for i in range(50)]}
                return resp
            elif p == 3 and not rate_limit_tested:
                rate_limit_tested = True
                resp = MagicMock()
                resp.ok = False
                resp.status_code = 429
                resp.headers = {"Retry-After": "2"}
                return resp
            elif p == 3 and rate_limit_tested:
                resp = MagicMock()
                resp.ok = True
                resp.status_code = 200
                resp.json.return_value = {"items": [_build_dummy_item(f"55000000120245020{i:03d}") for i in range(10)]}
                return resp

            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = {"items": []}
            return resp

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request):
            r2 = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 2, "batch_size": 5})
            d2 = r2.get_json()
            assert d2["status"] == "RATE_LIMITED"
            assert d2["next_page"] == 3
            assert d2["has_more"] is True
            assert d2["retry_after_seconds"] >= 2

            r3 = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 3, "batch_size": 5})
            d3 = r3.get_json()
            assert d3["status"] == "COMPLETED"
            assert d3["is_completed"] is True
            assert d3["has_more"] is False
            assert d3["next_page"] is None


def test_tst_called_only_once_on_initial_batch():
    """
    Valida que a consulta complementar ao TST (siglaTribunal=TST) é disparada
    APENAS no lote inicial (start_page == 1) e NUNCA em lotes posteriores (start_page > 1).
    """
    searcher = CNJProcessSearcher()
    tst_calls = []

    def mock_request(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        p = params.get("pagina", 1) if params else 1
        sigla = params.get("siglaTribunal") if params else None

        if sigla == "TST":
            tst_calls.append(params)
            resp.json.return_value = {"items": []}
            return resp

        # Páginas do Comunica retornam 100 itens
        resp.json.return_value = {"items": [_build_dummy_item(f"77000000120245020{i:03d}") for i in range(100)]}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    strat = {"comunica_query_term": "EMPRESA TST ONCE", "mode": "BROAD_BRAND", "is_broad_brand": True}

    # Lote 1: start_page=1
    res1 = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages_per_call=1,
        batch_delay=0.0,
    )
    assert res1["pages_read"] == 1
    assert len(tst_calls) == 1  # TST chamado na página 1!

    # Lote 2: start_page=2
    res2 = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=2,
        max_pages_per_call=1,
        batch_delay=0.0,
    )
    assert res2["pages_read"] == 1
    assert len(tst_calls) == 1  # NÃO foi chamado novamente no lote 2!

    # Lote 3: start_page=3
    res3 = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=3,
        max_pages_per_call=1,
        batch_delay=0.0,
    )
    assert res3["pages_read"] == 1
    assert len(tst_calls) == 1  # Permanece com exatamente 1 chamada!


def test_itens_por_pagina_100_and_time_box_budget():
    """
    Valida:
    1. itensPorPagina=100 enviado na requisição HTTP para dobrar a densidade de processos por página.
    2. Time-box seguro: encerra o lote limpo ao atingir o orçamento de tempo (time_budget_seconds),
       devolvendo IN_PROGRESS e next_page sem timeout no Render.
    """
    import time
    searcher = CNJProcessSearcher()
    requested_itens_por_pagina = []

    def mock_request(method, url, params=None, **kwargs):
        if params and "itensPorPagina" in params:
            requested_itens_por_pagina.append(params["itensPorPagina"])

        # Simula lentidão de rede na página 1 que estoura o time-box de 0.1s
        time.sleep(0.12)
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"items": [_build_dummy_item(f"88000000120245020{i:03d}") for i in range(100)]}
        return resp

    searcher._request_gov = MagicMock(side_effect=mock_request)

    strat = {"comunica_query_term": "EMPRESA TIMEBOX", "mode": "BROAD_BRAND"}
    res = searcher.paginate_comunica_pje_standard(
        search_strategy=strat,
        start_page=1,
        max_pages=10,
        max_pages_per_call=5,  # Pediu até 5 páginas, mas o time-box vai limitar
        itens_por_pagina=100,
        time_budget_seconds=0.1,  # Orçamento curto de 100ms
        batch_delay=0.0,
    )

    # 1. itensPorPagina foi 100
    assert 100 in requested_itens_por_pagina
    # 2. Leu 1 página e parou pelo time-box sem avançar para página 2 nesta chamada
    assert res["pages_read"] == 1
    assert res["next_page"] == 2
    assert res["is_completed"] is False
    assert res["motivo_encerramento"] == "LOTE_CURTO_CONCLUIDO"


def test_synchronized_full_recalculation_on_completed(client, app):
    """
    Valida a sincronização total dos indicadores e do Agente ADK sobre 100% dos processos:
    - Inicialmente com 50 processos do fast path.
    - Lote final adiciona 100 processos (condenações, acordos, improcedentes e responsabilidade subsidiária).
    - Valida que o payload final e o banco refletem 100% dos processos nos cards,
      taxas históricas, alertas da Súmula 331 e idoneidade econômica.
    """
    with app.app_context():
        # Busca inicial com 50 processos em aberto
        p1_records = [
            ProcessRecord(
                numero_processo=f"90000000120245020{i:03d}",
                tribunal="TRT02",
                primary_subject="Aviso Prévio",
                sentence_outcome="em_aberto",
            )
            for i in range(50)
        ]
        search = CompanySearch(
            company_name="EMPRESA SINCRONIZACAO TOTAL S.A.",
            normalized_name="EMPRESA SINCRONIZACAO TOTAL S A",
            total_processes=50,
            scan_status="PARTIAL_FAST_PATH",
            outcomes_summary={"em_aberto": 50, "condenacoes": 0, "acordos": 0, "improcedentes": 0},
            economic_standing={
                "labor_scan_cursor": {
                    "courts_queue": ["TRT2"],
                    "layer": 2,
                    "layer2_queue": [{"court": "TRT2", "page": 2}],
                    "court_index": 0,
                    "page": 2,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                }
            },
            ai_analysis={
                "taxa_historica_vitoria_empresa_pct": 0.0,
                "alertas_sumula_331": ["Desfechos analisados: 0 condenações, 0 acordos, 0 improcedentes e 50 em aberto."],
            },
            processes=p1_records,
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        # Simula resposta do lote com 40 condenações, 30 acordos e 30 improcedentes (< 50 na página 3 encerra)
        def mock_request(method, url, params=None, **kwargs):
            p = params.get("pagina", 1) if params else 1
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200

            if p == 2:
                # Página 2: 40 condenações + 30 acordos + 30 improcedentes = 100 itens
                items = []
                for i in range(40):
                    items.append({
                        **_build_dummy_item(f"91000000120245020{i:03d}"),
                        "classe": "ATSum",
                        "texto": "Julgo procedente em parte para condenar a reclamada ao pagamento. Responsabilidade subsidiária da tomadora EMPRESA CONTRATANTE LTDA.",
                    })
                for i in range(30):
                    items.append({
                        **_build_dummy_item(f"92000000120245020{i:03d}"),
                        "classe": "ATSum",
                        "texto": "Homologo o acordo celebrado entre as partes nos autos.",
                    })
                for i in range(30):
                    items.append({
                        **_build_dummy_item(f"93000000120245020{i:03d}"),
                        "classe": "ATSum",
                        "texto": "Julgo improcedente a reclamação trabalhista ajuizada em face da reclamada.",
                    })
                resp.json.return_value = {"items": items}
            else:
                # Página 3: lista vazia (fim natural)
                resp.json.return_value = {"items": []}
            return resp

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_request):
            resp = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 2, "batch_size": 2})
            assert resp.status_code == 200
            data = resp.get_json()

            assert data["status"] == "COMPLETED"
            assert data["is_completed"] is True
            assert data["total_acumulado"] == 150  # 50 iniciais + 100 novos

            db_search = data["data"]
            assert db_search["total_processes"] == 150
            # Sincronização dos desfechos:
            assert db_search["outcomes_summary"]["condenacoes"] == 40
            assert db_search["outcomes_summary"]["acordos"] == 30
            assert db_search["outcomes_summary"]["improcedentes"] == 30
            assert db_search["outcomes_summary"]["em_aberto"] == 50

            # Sincronização do Parecer do Agente ADK com 100% dos dados:
            ai = db_search["ai_analysis"]
            assert ai is not None
            # Taxa histórica calculada sobre processos julgados (100 julgados: 30 acordos + 30 improcedentes = 60 vitórias/acordos)
            assert ai.get("taxa_historica_vitoria_empresa_pct") is not None
            assert ai.get("taxa_historica_acordos_pct") is not None
            # Alertas Súmula 331 sincronizados com o número real de condenações e em aberto:
            alertas = " ".join(ai.get("alertas_sumula_331", []))
            assert "40 condenações" in alertas or "150" in alertas or "40" in alertas


def test_early_finalization_with_finalize_flag(client, app):
    """
    Valida a funcionalidade de encerramento antecipado a pedido do usuário:
    O front-end envia {"finalize": true} e o back-end calcula imediatamente
    as métricas sobre os processos já persistidos até o momento.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA FINALIZACAO ANTECIPADA LTDA",
            normalized_name="EMPRESA FINALIZACAO ANTECIPADA LTDA",
            total_processes=20,
            scan_status="IN_PROGRESS",
            processes=[
                ProcessRecord(
                    numero_processo=f"95000000120245020{i:03d}",
                    tribunal="TRT02",
                    sentence_outcome="condenacoes" if i < 5 else "em_aberto",
                )
                for i in range(20)
            ],
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        resp = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"finalize": True, "manual_stop": True})
        assert resp.status_code == 200
        data = resp.get_json()

        assert data["status"] == "PARTIAL"
        assert data["is_completed"] is False
        assert data["total_acumulado"] == 20
        assert data["data"]["scan_status"] == "PARTIAL"
        assert data["data"]["outcomes_summary"]["condenacoes"] == 5
        assert data["data"]["outcomes_summary"]["em_aberto"] == 15


def test_tst_count_computed_from_all_db_processes(app):
    """
    Valida que tst_count em CompanySearch.to_dict() contabiliza 100% dos processos
    persistidos no banco para a busca (via tribunal == 'TST' ou código '.5.00.' no CNJ),
    e não apenas os retornados na página 1.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA TST COMPLETA LTDA",
            normalized_name="EMPRESA TST COMPLETA LTDA",
            total_processes=6,
            scan_status="COMPLETED",
            processes=[
                # 2 com tribunal == 'TST'
                ProcessRecord(numero_processo="0000001-11.2023.5.02.0001", tribunal="TST", grau="G1"),
                ProcessRecord(numero_processo="0000002-11.2023.5.06.0001", tribunal="tst", grau="G1"),
                # 2 identificados pelo código '.5.00.' no número CNJ mesmo sem tribunal='TST'
                ProcessRecord(numero_processo="0000003-11.2023.5.00.0001", tribunal="", grau="G1"),
                ProcessRecord(numero_processo="0000004-11.2023.5.00.0002", tribunal="TRT2", grau="G1"),
                # 1 pelo grau
                ProcessRecord(numero_processo="0000005-11.2023.5.15.0001", tribunal="TRT15", grau="TST (Recursal)"),
                # 1 de TRT comum (não deve contar)
                ProcessRecord(numero_processo="0000006-11.2023.5.02.0001", tribunal="TRT2", grau="G1"),
            ],
        )
        db.session.add(search)
        db.session.commit()

        d = search.to_dict()
        assert d["tst_count"] == 5


def test_tst_dedicated_call_paginates_up_to_5_pages_with_100_items():
    """
    Valida que a chamada dedicada siglaTribunal=TST na página 1 usa itensPorPagina=100
    e pagina até concluir o TST (com teto seguro de 5 páginas).
    """
    searcher = CNJProcessSearcher()
    tst_pages_requested = []

    def mock_get(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        sigla = (params or {}).get("siglaTribunal")
        pag = (params or {}).get("pagina", 1)
        if sigla == "TST":
            tst_pages_requested.append((pag, (params or {}).get("itensPorPagina")))
            if pag == 1:
                items = _comm_page("30001000120245000", "tst1", size=100)
                resp.json.return_value = {"items": items, "totalPages": 2}
            elif pag == 2:
                items = _comm_page("30002000120245000", "tst2", size=10)
                resp.json.return_value = {"items": items, "totalPages": 2}
            else:
                resp.json.return_value = {"items": []}
        else:
            resp.json.return_value = {"items": _comm_page("30000000120245020", "trt", size=5)}
        return resp

    persisted_tst = []

    def on_persist(page_num, procs, new_count):
        if page_num == 0:
            persisted_tst.extend(procs.keys())

    with patch.object(searcher, "_request_gov", side_effect=mock_get):
        res = searcher.paginate_comunica_pje_standard(
            search_strategy={"comunica_query_term": "EMPRESA TST", "is_broad_brand": True, "mode": "BROAD_BRAND"},
            start_page=1,
            on_batch_persist=on_persist,
            itens_por_pagina=100,
        )

    assert tst_pages_requested == [(1, 100), (2, 100)]
    assert len(persisted_tst) == 110
    assert res["total_processes"] == 115


def test_truncated_count_10000_does_not_stop_at_page_100():
    """
    Simula a API do Comunica PJe retornando count=10000 (truncado pelo Elasticsearch),
    mas continuando a entregar itens inéditos nas páginas 101 e 102.
    A varredura NÃO pode parar na página 100 e só deve concluir quando a página 103 vier vazia.
    """
    searcher = CNJProcessSearcher()
    pages_requested = []

    def mock_get(method, url, params=None, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        pag = (params or {}).get("pagina", 1)
        pages_requested.append(pag)
        if pag in (100, 101, 102):
            resp.json.return_value = {
                "count": 10000,
                "items": _comm_page(f"40{pag:03d}000120245020", f"p{pag}", size=100),
            }
        else:
            resp.json.return_value = {"count": 10000, "items": []}
        return resp

    with patch.object(searcher, "_request_gov", side_effect=mock_get):
        res = searcher.paginate_comunica_pje_standard(
            search_strategy={"comunica_query_term": "EMPRESA GRANDE", "is_broad_brand": False, "mode": "STRICT_CNPJ"},
            start_page=100,
            itens_por_pagina=100,
            max_pages=0,
        )

    assert {100, 101, 102, 103}.issubset(set(pages_requested))
    assert res["is_completed"] is True
    assert res["total_processes"] == 300
    assert res["motivo_encerramento"] == "FIM_NATURAL_LISTA_VAZIA"


def test_concurrent_pagination_and_429_preserves_order_and_next_page():
    """
    Valida que a paginação concorrente controlada (SCAN_CONCURRENCY=3):
    1. Acumula todas as páginas sem pular registros;
    2. Ao receber 429 em uma página do mini-lote paralelo, interrompe imediatamente
       na página correta e devolve RATE_LIMITED com next_page exata.
    """
    searcher = CNJProcessSearcher()

    def mock_get(method, url, params=None, **kwargs):
        resp = MagicMock()
        pag = (params or {}).get("pagina", 1)
        if pag in (1, 2):
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = {"items": _comm_page(f"50{pag:03d}000120245020", f"c{pag}", size=100)}
        elif pag == 3:
            resp.ok = False
            resp.status_code = 429
            resp.headers = {"Retry-After": "4"}
        else:
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = {"items": _comm_page(f"50{pag:03d}000120245020", f"c{pag}", size=100)}
        return resp

    with patch.dict(os.environ, {"RENDER": "false", "SCAN_CONCURRENCY": "3"}, clear=False), \
            patch.object(searcher, "_request_gov", side_effect=mock_get):
        res = searcher.paginate_comunica_pje_standard(
            search_strategy={"comunica_query_term": "EMPRESA PARALELA", "is_broad_brand": False, "mode": "STRICT_CNPJ"},
            start_page=1,
            itens_por_pagina=100,
            max_pages=0,
        )

    assert res["rate_limited"] is True
    assert res["is_completed"] is False
    assert res["next_page"] == 3
    assert res["retry_after_seconds"] == 4
    assert res["total_processes"] == 200


def test_hit_max_pages_saves_partial_status(client, app):
    """Valida que atingir SCAN_BATCH_MAX_PAGES / SCAN_MAX_PAGES grava status PARTIAL."""
    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA TETO PARCIAL LTDA",
            normalized_name="EMPRESA TETO PARCIAL LTDA",
            total_processes=0,
            scan_status="IN_PROGRESS",
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        def mock_get(method, url, params=None, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.status_code = 200
            if (params or {}).get("siglaTribunal") == "TST":
                resp.json.return_value = {"items": []}
                return resp
            pag = (params or {}).get("pagina", 1)
            resp.json.return_value = {"items": _comm_page(f"60{pag:03d}000120245020", f"m{pag}", size=100)}
            return resp

        with patch.dict(os.environ, {"SCAN_BATCH_MAX_PAGES": "2", "SCAN_CONCURRENCY": "1"}, clear=False), \
                patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_get):
            data = client.post(f"/api/v1/deep-scan-batch/{search_id}", json={"page": 1, "batch_size": 5}).get_json()

        assert data["status"] == "PARTIAL"
        assert data["is_completed"] is False
        assert data["data"]["scan_status"] == "PARTIAL"
        assert data["total_acumulado"] == 200


def test_cache_ttl_uses_completed_ignores_partial_and_respects_force_refresh(client, app):
    """
    Valida que:
    - Busca PARTIAL recente NÃO é servida como cache completo;
    - Busca COMPLETED recente (> 0 processos) responde instantaneamente do cache;
    - force_refresh=true ignora o cache COMPLETED.
    """
    from app.routes.api import SEARCH_CACHE
    with app.app_context():
        SEARCH_CACHE.clear()
        s_partial = CompanySearch(
            company_name="EMPRESA CACHE TESTE LTDA",
            normalized_name="EMPRESA CACHE TESTE",
            total_processes=10,
            scan_status="PARTIAL",
            processes=[
                ProcessRecord(numero_processo=f"70000000120245020{i:03d}", tribunal="TRT2")
                for i in range(10)
            ],
        )
        db.session.add(s_partial)
        db.session.commit()

        mock_orch_res = {
            "empresa": "EMPRESA CACHE TESTE LTDA",
            "total_processos": 25,
            "desfechos_sentencas": {"condenacoes": 5, "improcedentes": 5, "acordos": 5, "em_aberto": 10},
            "ranking_motivos": [],
            "plus_previsao_condenacao": [],
            "score_risco_terceirizacao": 20.0,
            "processos": [],
            "scan_status": "COMPLETED",
            "has_more": False,
        }

        # 1. Com busca PARTIAL no banco, NÃO pode usar cache — deve chamar orchestrator
        with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_orch_res) as mock_run:
            resp1 = client.post("/api/v1/search-company", json={"company_name": "EMPRESA CACHE TESTE LTDA"})
            assert resp1.status_code == 200
            assert mock_run.called is True

        # 2. Atualiza para COMPLETED no banco e limpa L0: agora deve usar cache L1 sem chamar orchestrator
        SEARCH_CACHE.clear()
        s_partial.scan_status = "COMPLETED"
        db.session.commit()

        with patch.dict(os.environ, {"SEARCH_CACHE_TTL_HOURS": "24"}, clear=False), \
                patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_orch_res) as mock_run2:
            resp2 = client.post("/api/v1/search-company", json={"company_name": "EMPRESA CACHE TESTE LTDA"})
            assert resp2.status_code == 200
            assert mock_run2.called is False
            assert resp2.get_json().get("cached") is True

        # 3. Com force_refresh=true, ignora o cache e chama orchestrator
        with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_orch_res) as mock_run3:
            resp3 = client.post("/api/v1/search-company", json={"company_name": "EMPRESA CACHE TESTE LTDA", "force_refresh": True})
            assert resp3.status_code == 200
            assert mock_run3.called is True


def test_adaptive_rate_limit_pacing():
    """
    Valida o pacing adaptativo via headers:
    - Com cota folgada (X-RateLimit-Remaining: 15), pausa 0.0s;
    - Com cota baixa (X-RateLimit-Remaining: 1), pausa brevemente sem sleep fixo global.
    """
    from app.services.cnj_searcher import _adaptive_rate_limit_pause

    resp_ok = MagicMock()
    resp_ok.headers = {"X-RateLimit-Remaining": "15"}
    with patch("app.services.cnj_searcher.time.sleep") as mock_sleep:
        paused = _adaptive_rate_limit_pause(resp_ok)
        assert paused == 0.0
        mock_sleep.assert_not_called()

    resp_low = MagicMock()
    resp_low.headers = {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "1.2"}
    with patch("app.services.cnj_searcher.time.sleep") as mock_sleep_low:
        paused_low = _adaptive_rate_limit_pause(resp_low)
        assert paused_low == 1.2
        mock_sleep_low.assert_called_once_with(1.2)


def test_deep_page_500_fallback_to_50_items_per_page():
    """
    Valida que HTTP 500 em página profunda (> 100) com itensPorPagina=100
    faz fallback automático para itensPorPagina=50 na página equivalente (279)
    sem encerrar falsamente como COMPLETED.
    """
    searcher = CNJProcessSearcher()
    calls = []

    def mock_get(method, url, params=None, **kwargs):
        pag = (params or {}).get("pagina", 1)
        sz = (params or {}).get("itensPorPagina", 100)
        calls.append((pag, sz))
        resp = MagicMock()
        if pag == 140 and sz == 100:
            resp.ok = False
            resp.status_code = 500
            resp.headers = {}
            return resp
        if pag == 279 and sz == 50:
            resp.ok = True
            resp.status_code = 200
            resp.headers = {"X-RateLimit-Remaining": "18"}
            resp.json.return_value = {"items": _comm_page("80279000120245020", "f279", size=50)}
            return resp
        resp.ok = True
        resp.status_code = 200
        resp.headers = {"X-RateLimit-Remaining": "17"}
        resp.json.return_value = {"items": []}
        return resp

    with patch.object(searcher, "_request_gov", side_effect=mock_get):
        res = searcher.paginate_comunica_pje_standard(
            search_strategy={"comunica_query_term": "EMPRESA PROFUNDA", "is_broad_brand": False, "mode": "STRICT_CNPJ"},
            start_page=140,
            itens_por_pagina=100,
            max_pages=0,
            batch_delay=0.0,
        )

    assert calls == [(140, 100), (279, 50), (280, 50)]
    assert res["is_completed"] is True
    assert res["total_processes"] == 50


def test_bulk_upsert_process_rows_idempotent_on_conflict(app):
    """
    Valida que _bulk_upsert_process_rows insere o lote em SQL Core e ignora
    duplicatas por (company_search_id, numero_processo) via ON CONFLICT DO NOTHING.
    """
    from app.routes.api import _bulk_upsert_process_rows

    with app.app_context():
        search = CompanySearch(
            company_name="EMPRESA BULK SQL LTDA",
            normalized_name="EMPRESA BULK SQL LTDA",
            total_processes=0,
            scan_status="IN_PROGRESS",
        )
        db.session.add(search)
        db.session.commit()

        rows = [
            {
                "company_search_id": search.id,
                "numero_processo": f"99000000120245020{i:03d}",
                "tribunal": "TRT2",
                "grau": "G1",
                "nivel_sigilo": 0,
                "sentence_outcome": "em_aberto",
                "has_expert_report": False,
                "has_subsidiary_liability": False,
            }
            for i in range(5)
        ]
        _bulk_upsert_process_rows(rows)
        db.session.commit()

        # Reenvia lote sobreposto (2 duplicados + 1 novo)
        overlap = rows[:2] + [
            {
                "company_search_id": search.id,
                "numero_processo": "99000000120245020999",
                "tribunal": "TRT2",
                "grau": "G1",
                "nivel_sigilo": 0,
                "sentence_outcome": "em_aberto",
                "has_expert_report": False,
                "has_subsidiary_liability": False,
            }
        ]
        _bulk_upsert_process_rows(overlap)
        db.session.commit()

        total = ProcessRecord.query.filter_by(company_search_id=search.id).count()
        assert total == 6


def test_prune_old_searches_respects_retention_days(app):
    """
    Valida que prune_old_searches remove apenas buscas expiradas além de
    SEARCH_RETENTION_DAYS (em cascata), preservando buscas recentes.
    """
    from datetime import timedelta
    from app.routes.api import prune_old_searches
    from app.models.company_search import get_utc_now

    with app.app_context():
        now = get_utc_now()
        old_search = CompanySearch(
            company_name="EMPRESA ANTIGA EXPURGO",
            normalized_name="EMPRESA ANTIGA EXPURGO",
            total_processes=1,
            scan_status="COMPLETED",
            updated_at=now - timedelta(days=45),
            processes=[ProcessRecord(numero_processo="99100000120245020001", tribunal="TRT2")],
        )
        recent_search = CompanySearch(
            company_name="EMPRESA RECENTE MANTIDA",
            normalized_name="EMPRESA RECENTE MANTIDA",
            total_processes=1,
            scan_status="COMPLETED",
            updated_at=now - timedelta(days=2),
            processes=[ProcessRecord(numero_processo="99200000120245020001", tribunal="TRT2")],
        )
        db.session.add_all([old_search, recent_search])
        db.session.commit()
        old_id = old_search.id
        recent_id = recent_search.id

        deleted = prune_old_searches(retention_days=30)
        assert deleted == 1
        assert db.session.get(CompanySearch, old_id) is None
        assert db.session.get(CompanySearch, recent_id) is not None
        assert ProcessRecord.query.filter_by(company_search_id=old_id).count() == 0


def test_labor_courts_queue_all_25_and_uf_priority():
    """
    Valida que a fila trabalhista contém exatamente os 25 tribunais (TST + TRT1..TRT24)
    sem repetições, iniciando no TST e priorizando o(s) TRT(s) da UF da empresa.
    """
    from app.services.cnj_searcher import VALID_LABOR_COURTS, build_labor_courts_queue

    q_sp = build_labor_courts_queue("SP")
    assert len(q_sp) == 25
    assert set(q_sp) == VALID_LABOR_COURTS
    assert q_sp[:3] == ["TST", "TRT2", "TRT15"]

    q_pe = build_labor_courts_queue("pe")
    assert len(q_pe) == 25
    assert set(q_pe) == VALID_LABOR_COURTS
    assert q_pe[:2] == ["TST", "TRT6"]

    q_mg = build_labor_courts_queue("MG")
    assert q_mg[:2] == ["TST", "TRT3"]

    q_default = build_labor_courts_queue(None)
    assert len(q_default) == 25
    assert q_default[0] == "TST"


def test_is_labor_process_blocks_civil_and_federal():
    """
    Valida a dupla blindagem: apenas processos da Justiça do Trabalho (.5.00. a .5.24.
    e sigla TST/TRT*) são aceitos; processos Cíveis (TJs .8.xx.) e Federais (TRFs .4.xx.)
    são bloqueados tanto em is_labor_process quanto no parser _parse_comunica_items.
    """
    from app.services.cnj_searcher import is_labor_process

    assert is_labor_process("0001234-56.2024.5.00.0001", "TST") is True
    assert is_labor_process("0001234-56.2024.5.02.0001", "TRT2") is True
    assert is_labor_process("0001234-56.2024.5.24.0001", "") is True

    assert is_labor_process("0001234-56.2024.8.26.0001", "TJSP") is False
    assert is_labor_process("0001234-56.2024.4.03.6100", "TRF3") is False
    assert is_labor_process("0001234-56.2024.8.19.0001", "TRT1") is False

    searcher = CNJProcessSearcher()
    procs = {}
    searcher._parse_comunica_items(
        [
            {"numeroProcesso": "0001234-56.2024.8.26.0001", "siglaTribunal": "TJSP", "texto": "Ação cível"},
            {"numeroProcesso": "0001234-56.2024.4.03.6100", "siglaTribunal": "TRF3", "texto": "Execução fiscal"},
            {"numeroProcesso": "0001234-56.2024.5.02.0001", "siglaTribunal": "TRT2", "texto": "Horas extras"},
            {"numeroProcesso": "0009999-11.2024.5.00.0001", "siglaTribunal": "TST", "texto": "Recurso de revista"},
        ],
        procs,
    )
    assert set(procs.keys()) == {"00012345620245020001", "00099991120245000001"}


def test_labor_courts_batch_cursor_advances_and_circuit_breaker_marks_partial(client, app):
    """
    Valida o fluxo completo de cursor por tribunal em /deep-scan-batch/<id>:
    1. Avança por múltiplos tribunais vazios dentro do mesmo lote;
    2. Aciona circuit-breaker após 3 erros HTTP 500 seguidos em um TRT (sem travar);
    3. Conclui os 25 tribunais marcando status PARTIAL (nunca COMPLETED) e recalcula
       métricas (incluindo tst_count) apenas sobre processos trabalhistas.
    """
    from app.routes.api import LABOR_SCOPE_VERSION
    from app.services.cnj_searcher import build_labor_courts_queue

    queue = build_labor_courts_queue("SP")
    with app.app_context():
        search = CompanySearch(
            company_name="BANCO TRABALHISTA TESTE S.A.",
            normalized_name="BANCO TRABALHISTA TESTE S A",
            total_processes=0,
            scan_status="PARTIAL_FAST_PATH",
            economic_standing={
                "uf": "SP",
                "scan_scope_version": LABOR_SCOPE_VERSION,
                "labor_scan_cursor": {
                    "courts_queue": queue,
                    "court_index": 0,
                    "page": 1,
                    "consecutive_court_errors": 0,
                    "partial_courts": [],
                },
            },
        )
        db.session.add(search)
        db.session.commit()
        search_id = search.id

        def mock_get(method, url, params=None, **kwargs):
            trib = (params or {}).get("siglaTribunal")
            resp = MagicMock()
            if trib == "TST":
                resp.ok = True
                resp.status_code = 200
                resp.headers = {"X-RateLimit-Remaining": "18"}
                resp.json.return_value = {
                    "count": 2,
                    "items": [
                        {
                            "id": "tst_1",
                            "numeroProcesso": "0001000-11.2024.5.00.0001",
                            "siglaTribunal": "TST",
                            "texto": "Recurso de revista julgo procedente para condenar a reclamada em horas extras.",
                            "classe": "RR",
                        },
                        {
                            "id": "tst_2",
                            "numeroProcesso": "0001001-11.2024.5.00.0002",
                            "siglaTribunal": "TST",
                            "texto": "Agravo de instrumento.",
                            "classe": "AIRR",
                        },
                    ],
                }
                return resp
            if trib == "TRT2":
                resp.ok = False
                resp.status_code = 500
                resp.headers = {}
                return resp
            if trib == "TRT15":
                resp.ok = True
                resp.status_code = 200
                resp.headers = {"X-RateLimit-Remaining": "15"}
                resp.json.return_value = {
                    "count": 2,
                    "items": [
                        {
                            "id": "trt15_1",
                            "numeroProcesso": "0002000-22.2024.5.15.0001",
                            "siglaTribunal": "TRT15",
                            "texto": "Julgo procedente o pedido de verbas rescisórias.",
                            "classe": "ATOrd",
                        },
                        {
                            "id": "civel_leak",
                            "numeroProcesso": "0009000-99.2024.8.26.0100",
                            "siglaTribunal": "TJSP",
                            "texto": "Ação cível que deve ser descartada.",
                            "classe": "Procedimento Comum Cível",
                        },
                    ],
                }
                return resp
            resp.ok = True
            resp.status_code = 200
            resp.headers = {"X-RateLimit-Remaining": "19"}
            resp.json.return_value = {"count": 0, "items": []}
            return resp

        with patch("app.services.cnj_searcher.CNJProcessSearcher._request_gov", side_effect=mock_get):
            data = client.post(
                f"/api/v1/deep-scan-batch/{search_id}",
                json={"page": 1, "batch_size": 10},
            ).get_json()

        assert data["status"] == "PARTIAL"
        assert data["is_completed"] is False
        assert data["has_more"] is False
        assert data["partial_courts"] == ["TRT2"]
        assert data["courts_completed"] == 25
        assert data["total_courts"] == 25
        assert data["total_acumulado"] == 3
        assert data["data"]["scan_status"] == "PARTIAL"
        assert data["data"]["tst_count"] == 2
        assert data["data"]["outcomes_summary"]["condenacoes"] == 2


def test_cache_rejects_legacy_non_labor_search(client, app):
    """
    Valida que uma busca COMPLETED antiga contendo processo não trabalhista (.8.26. TJSP)
    e sem scan_scope_version LABOR_ONLY_V1 NÃO é reaproveitada do cache.
    """
    from app.routes.api import SEARCH_CACHE

    with app.app_context():
        SEARCH_CACHE.clear()
        legacy = CompanySearch(
            company_name="EMPRESA LEGADA MISTA LTDA",
            normalized_name="EMPRESA LEGADA MISTA LTDA",
            total_processes=2,
            scan_status="COMPLETED",
            economic_standing={"situacao_cadastral": "ATIVA"},
            processes=[
                ProcessRecord(numero_processo="0001111-11.2024.8.26.0001", tribunal="TJSP"),
                ProcessRecord(numero_processo="0002222-22.2024.5.02.0001", tribunal="TRT2"),
            ],
        )
        db.session.add(legacy)
        db.session.commit()

        mock_orch_res = {
            "empresa": "EMPRESA LEGADA MISTA LTDA",
            "total_processos": 1,
            "desfechos_sentencas": {"condenacoes": 0, "improcedentes": 0, "acordos": 0, "em_aberto": 1},
            "ranking_motivos": [],
            "plus_previsao_condenacao": [],
            "score_risco_terceirizacao": 0.0,
            "processos": [
                {
                    "numero_processo": "00022222220245020001",
                    "tribunal": "TRT2",
                    "grau": "G1",
                    "sentence_outcome": "em_aberto",
                }
            ],
            "scan_status": "PARTIAL_FAST_PATH",
            "has_more": True,
        }

        with patch("app.routes.api.orchestrator.analyze_company_by_name", return_value=mock_orch_res) as mock_run:
            resp = client.post("/api/v1/search-company", json={"company_name": "EMPRESA LEGADA MISTA LTDA"})
            assert resp.status_code == 200
            assert mock_run.called is True


def test_labor_scan_page_size_stepdown_500_to_100_to_50_and_since_date():
    """
    Valida que paginate_labor_courts_batch envia itensPorPagina=500 e since_date,
    e realiza redução progressiva (500 -> 100 -> 50) em caso de HTTP 500/422 sem perder registros.
    """
    searcher = CNJProcessSearcher()
    calls = []

    def mock_get(method, url, params=None, **kwargs):
        pag = (params or {}).get("pagina", 1)
        sz = (params or {}).get("itensPorPagina", 500)
        since = (params or {}).get("dataDisponibilizacaoInicio")
        calls.append((pag, sz, since))
        resp = MagicMock()
        if pag == 1 and sz == 500:
            resp.ok = False
            resp.status_code = 500
            resp.headers = {}
            return resp
        if pag == 1 and sz == 100:
            resp.ok = True
            resp.status_code = 200
            resp.headers = {"X-RateLimit-Remaining": "18"}
            resp.json.return_value = {"count": 10, "items": _comm_page("00001000120245020", "s100", size=10)}
            return resp
        resp.ok = True
        resp.status_code = 200
        resp.headers = {"X-RateLimit-Remaining": "18"}
        resp.json.return_value = {"count": 0, "items": []}
        return resp

    with patch.object(searcher, "_request_gov", side_effect=mock_get):
        res = searcher.paginate_labor_courts_batch(
            search_strategy={"comunica_query_term": "EMPRESA STEPDOWN", "mode": "STRICT_CNPJ"},
            courts_queue=["TRT2"],
            itens_por_pagina=500,
            since_date="2024-11-27",
            batch_delay=0.0,
        )

    assert calls == [(1, 500, "2024-11-27"), (1, 100, "2024-11-27")]
    assert res["is_completed"] is True
    assert res["total_processes"] == 10










