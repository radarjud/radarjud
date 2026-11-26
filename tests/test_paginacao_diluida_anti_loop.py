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
    - Local (RENDER ausente ou != 'true'): SCAN_MAX_PAGES=0 (ilimitado), SCAN_BATCH_DELAY=0.0, gc=False.
    - Render (RENDER='true'): SCAN_MAX_PAGES=50, SCAN_BATCH_DELAY=0.4, gc=True.
    - Suporta override de variáveis de ambiente SCAN_MAX_PAGES e SCAN_BATCH_DELAY.
    """
    # 1. Ambiente Local puro (sem RENDER)
    with patch.dict(os.environ, {}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is False
        assert cfg["max_pages"] == 0
        assert cfg["batch_delay"] == 0.0
        assert cfg["gc_collect"] is False

    # 2. Ambiente Render
    with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is True
        assert cfg["max_pages"] == 50
        assert cfg["batch_delay"] == 0.4
        assert cfg["gc_collect"] is True

    # 3. Override de variáveis em ambiente Local
    with patch.dict(os.environ, {"SCAN_MAX_PAGES": "25", "SCAN_BATCH_DELAY": "0.15"}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is False
        assert cfg["max_pages"] == 25
        assert cfg["batch_delay"] == 0.15

    # 4. Override de variáveis em ambiente Render
    with patch.dict(os.environ, {"RENDER": "true", "SCAN_MAX_PAGES": "40", "SCAN_BATCH_DELAY": "0.2"}, clear=True):
        cfg = get_scan_config()
        assert cfg["is_render"] is True
        assert cfg["max_pages"] == 40
        assert cfg["batch_delay"] == 0.2


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

