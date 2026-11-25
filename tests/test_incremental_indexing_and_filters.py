# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import logging
import time
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.company_search import CompanySearch, get_utc_now
from app.models.process_record import ProcessRecord
from app.models.tenant import Tenant
from app.services.orchestrator import CNJMetadataOrchestrator


def _make_cnj(idx: int, court_digit: str = "06") -> str:
    """Gera número CNJ sintético válido no formato NNNNNNN-DD.AAAA.J.TR.OOOO."""
    return f"{idx:07d}-45.2024.5.{court_digit}.0001"


def test_process_filtering_combination_and_latency(client, app):
    """
    Critério DoD 1:
    Testa a combinação de filtros SQL (tribunal + intervalo de datas + valor mínimo/máximo + outcome)
    no endpoint GET /api/v1/searches/{company_search_id}/processes, garantindo latência < 150ms.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="ENGENHARIA NORDESTE S.A.",
            normalized_name="ENGENHARIA NORDESTE SA",
            total_processes=40,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

        records = []
        for i in range(1, 41):
            if i <= 10:
                # Alvo do filtro: TRT6, 2025, R$ 50.000, Condenação
                trib = "TRT6"
                dist_date = date(2025, 4, 10)
                val = Decimal("50000.00")
                out = "condenacoes"
            elif i <= 20:
                # TRT6, mas valor fora (R$ 5.000)
                trib = "TRT6"
                dist_date = date(2025, 1, 15)
                val = Decimal("5000.00")
                out = "condenacoes"
            elif i <= 30:
                # TRT6, valor R$ 80.000, mas Acordo
                trib = "TRT6"
                dist_date = date(2025, 6, 20)
                val = Decimal("80000.00")
                out = "acordos"
            else:
                # TRT2, data antiga (2022)
                trib = "TRT2"
                dist_date = date(2022, 11, 5)
                val = Decimal("30000.00")
                out = "condenacoes"

            rec = ProcessRecord(
                company_search_id=sid,
                numero_processo=_make_cnj(i, "06" if trib == "TRT6" else "02"),
                tribunal=trib,
                grau="G1",
                data_distribuicao=dist_date,
                valor_causa=val,
                primary_subject="Adicional de Periculosidade",
                sentence_outcome=out,
                has_expert_report=True,
                observation=f"Processo número {i}",
            )
            records.append(rec)

        db.session.bulk_save_objects(records)
        db.session.commit()

    start_time = time.perf_counter()
    url = (
        f"/api/v1/searches/{sid}/processes"
        f"?tribunal=TRT6&date_from=2024-01-01&date_to=2026-12-31"
        f"&min_value=10000&max_value=500000&outcome=condenacao&page=1&per_page=50"
    )
    resp = client.get(url)
    elapsed = time.perf_counter() - start_time

    assert resp.status_code == 200
    assert elapsed < 0.150, f"Latência SQL deve ser < 150ms, obtido: {elapsed * 1000:.2f}ms"

    payload = resp.get_json()
    assert payload["search_id"] == sid
    assert payload["total"] == 10
    assert payload["page"] == 1
    assert payload["per_page"] == 50
    assert payload["pages"] == 1
    assert len(payload["items"]) == 10
    assert len(payload["processes"]) == 10

    for item in payload["items"]:
        assert item["tribunal"] == "TRT6"
        assert item["sentence_outcome"] == "condenacoes"
        assert item["valor_causa"] == 50000.0
        assert item["data_distribuicao"] == "2025-04-10"


def test_process_filtering_anti_idor_and_tenant_isolation(client, app):
    """
    Valida segurança e Anti-IDOR:
    - Registro de tenant isolado não pode ser acessado por outro tenant (HTTP 403).
    - Requisição sem autenticação/tenant para registro privado recebe HTTP 403.
    - Tenant correto acessa normalmente com HTTP 200.
    - Busca inexistente retorna HTTP 404.
    """
    with app.app_context():
        tenant_a = Tenant(name="Tenant Alpha", cnpj="11111111000101")
        tenant_b = Tenant(name="Tenant Beta", cnpj="22222222000102")
        db.session.add_all([tenant_a, tenant_b])
        db.session.commit()

        search_a = CompanySearch(
            tenant_id=tenant_a.id,
            company_name="EMPRESA CONFIDENCIAL A LTDA",
            normalized_name="EMPRESA CONFIDENCIAL A LTDA",
            total_processes=1,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search_a)
        db.session.commit()

        p = ProcessRecord(
            company_search_id=search_a.id,
            numero_processo=_make_cnj(999),
            tribunal="TRT1",
            sentence_outcome="em_aberto",
        )
        db.session.add(p)
        db.session.commit()

        sid_a = search_a.id
        tid_a = tenant_a.id
        tid_b = tenant_b.id

    # 1. Tenant B tentando acessar dados de Tenant A via Header -> 403
    r_idor = client.get(
        f"/api/v1/searches/{sid_a}/processes",
        headers={"X-Tenant-ID": str(tid_b)},
    )
    assert r_idor.status_code == 403
    assert "Acesso não autorizado" in r_idor.get_json()["error"]

    # 2. Requisição sem identificação de tenant em registro privado -> 403
    r_unauth = client.get(f"/api/v1/searches/{sid_a}/processes")
    assert r_unauth.status_code == 403

    # 3. Tenant A acessando seus próprios dados -> 200
    r_owner = client.get(
        f"/api/v1/searches/{sid_a}/processes",
        headers={"X-Tenant-ID": str(tid_a)},
    )
    assert r_owner.status_code == 200
    assert r_owner.get_json()["total"] == 1

    # 4. Search ID inexistente -> 404
    r_not_found = client.get(
        "/api/v1/searches/999999/processes",
        headers={"X-Tenant-ID": str(tid_a)},
    )
    assert r_not_found.status_code == 404


def test_process_filtering_validation_and_error_cases(client, app):
    """Testa validações defensivas de parâmetros de query com respostas HTTP 400 amigáveis."""
    with app.app_context():
        search = CompanySearch(
            company_name="VALIDATION TEST CORP",
            normalized_name="VALIDATION TEST CORP",
            total_processes=0,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

    # Data inicial inválida
    r1 = client.get(f"/api/v1/searches/{sid}/processes?date_from=invalid-date")
    assert r1.status_code == 400
    assert "date_from" in r1.get_json()["error"]

    # Data final inválida
    r2 = client.get(f"/api/v1/searches/{sid}/processes?date_to=32-13-2024")
    assert r2.status_code == 400
    assert "date_to" in r2.get_json()["error"]

    # Valor mínimo negativo
    r3 = client.get(f"/api/v1/searches/{sid}/processes?min_value=-50")
    assert r3.status_code == 400
    assert "min_value" in r3.get_json()["error"]

    # Valor máximo não-numérico
    r4 = client.get(f"/api/v1/searches/{sid}/processes?max_value=xyz")
    assert r4.status_code == 400
    assert "max_value" in r4.get_json()["error"]


def test_reconsultation_incremental_diff_200_processes(client, app, caplog):
    """
    Critério DoD 2:
    Simula reconsulta de empresa com 200 processos persistidos:
    - 150 já encerrados (condenações, acordos, improcedentes)
    - 50 com status 'Em aberto'
    - Confirma via mock que DataJud é invocado APENAS para os 50 em aberto (0 chamadas para os 150 encerrados).
    - Confirma log estruturado com exatos 75.0% de redução.
    - Confirma integridade estatística total: universo consolidado possui 200 processos e simplex soma 100%.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="BANCO METROPOLITANO S.A.",
            normalized_name="BANCO METROPOLITANO SA",
            total_processes=200,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

        existing_procs = []
        incoming_map = {}

        # Cria 150 processos encerrados e 50 em aberto
        for i in range(1, 201):
            cnj = _make_cnj(i, "02")
            if i <= 80:
                outcome = "condenacoes"
            elif i <= 130:
                outcome = "acordos"
            elif i <= 150:
                outcome = "improcedentes"
            else:
                outcome = "em_aberto"

            rec = ProcessRecord(
                company_search_id=sid,
                numero_processo=cnj,
                tribunal="TRT2",
                grau="G1",
                primary_subject="Horas Extras" if i % 2 == 0 else "Dano Moral",
                sentence_outcome=outcome,
                data_distribuicao=date(2024, 2, 1),
                valor_causa=Decimal("25000.00"),
            )
            existing_procs.append(rec)

            # Simula coleta atual do Comunica PJe com os mesmos 200 processos
            incoming_map[cnj] = {
                "siglaTribunal": "TRT2",
                "grau": "G1",
                "publicacoes_texto": [f"Intimação despacho processo {cnj}"],
            }

        db.session.bulk_save_objects(existing_procs)
        db.session.commit()

        orchestrator = CNJMetadataOrchestrator()

        # Mock do método de busca no DataJud para inspecionar exatamente o que é enviado
        with patch.object(
            orchestrator.searcher,
            "enrich_top_tribunals_datajud",
            wraps=orchestrator.searcher.enrich_top_tribunals_datajud,
        ) as mock_datajud:
            mock_datajud.return_value = [
                {
                    "numero_processo": cnj_key,
                    "tribunal": "TRT2",
                    "grau": "G1",
                    "sentence_outcome": "em_aberto",
                    "primary_subject": "Horas Extras",
                    "data_distribuicao": "2024-02-01",
                    "valor_causa": 25000.0,
                }
                for cnj_key in incoming_map
                if int(cnj_key.split("-")[0]) > 150
            ]

            caplog.clear()
            with caplog.at_level(logging.INFO):
                consolidated, stats = orchestrator.process_incremental_diff(
                    company_search_id=sid,
                    all_processes_map=incoming_map,
                    max_tribunals=3,
                    timeout=6.0,
                    sync_db=False,
                )

            # 1. Verifica que o DataJud foi consultado APENAS para os 50 processos em aberto
            assert mock_datajud.call_count == 1
            queried_dict = mock_datajud.call_args[0][0]
            assert len(queried_dict) == 50, f"Esperado 50 processos enviados ao DataJud, obtido: {len(queried_dict)}"

            # 2. Verifica estatísticas e log estruturado
            assert stats["total_cnjs"] == 200
            assert stats["skipped_closed"] == 150
            assert stats["queried_datajud"] == 50
            assert stats["reduction_pct"] == 75.0

            log_text = caplog.text
            assert "Indexação incremental DataJud" in log_text
            assert "total_cnjs=200" in log_text
            assert "skipped_closed=150" in log_text
            assert "queried_datajud=50" in log_text
            assert "reduction_pct=75.0%" in log_text

            # 3. Integridade Estatística: 100% dos processos consolidados (200)
            assert len(consolidated) == 200

            # 4. Cálculo Bayesiano considerando 100% dos processos consolidados
            metrics = orchestrator.predictor.generate_dashboard_metrics(
                company_name="BANCO METROPOLITANO S.A.",
                processes=consolidated,
            )
            assert metrics["total_processos"] == 200
            assert metrics["desfechos_sentencas"]["condenacoes"] == 80
            assert metrics["desfechos_sentencas"]["acordos"] == 50
            assert metrics["desfechos_sentencas"]["improcedentes"] == 20
            assert metrics["desfechos_sentencas"]["em_aberto"] == 50


def test_incremental_diff_with_new_processes_and_deduplication(client, app):
    """
    Critério DoD 3:
    Verifica que reindexações repetidas para o mesmo company_search_id garantem zero
    registros duplicados por (company_search_id, numero_processo) e que a UniqueConstraint
    impede duplicatas em nível de banco.
    """
    with app.app_context():
        search = CompanySearch(
            company_name="LOGISTICA DEDUP S.A.",
            normalized_name="LOGISTICA DEDUP SA",
            total_processes=5,
            last_checked_at=get_utc_now(),
        )
        db.session.add(search)
        db.session.commit()
        sid = search.id

        # Insere 5 processos iniciais
        for i in range(1, 6):
            p = ProcessRecord(
                company_search_id=sid,
                numero_processo=_make_cnj(i),
                tribunal="TRT3",
                sentence_outcome="em_aberto",
            )
            db.session.add(p)
        db.session.commit()

        initial_count = ProcessRecord.query.filter_by(company_search_id=sid).count()
        assert initial_count == 5

        # Simula varredura que traz os 5 processos existentes + 3 novos (total 8)
        incoming_map = {
            _make_cnj(i): {"siglaTribunal": "TRT3", "grau": "G1"}
            for i in range(1, 9)
        }

        orchestrator = CNJMetadataOrchestrator()

        def _mock_enrich(sub_map, *args, **kwargs):
            return [
                {
                    "numero_processo": k,
                    "tribunal": "TRT3",
                    "grau": "G1",
                    "sentence_outcome": "condenacoes" if k == _make_cnj(1) else "em_aberto",
                }
                for k in sub_map
            ]

        with patch.object(
            orchestrator.searcher,
            "enrich_top_tribunals_datajud",
            side_effect=_mock_enrich,
        ):
            # 1ª Execução de Reindexação
            consolidated1, _ = orchestrator.process_incremental_diff(
                company_search_id=sid,
                all_processes_map=incoming_map,
                sync_db=True,
            )
            assert len(consolidated1) == 8
            count1 = ProcessRecord.query.filter_by(company_search_id=sid).count()
            assert count1 == 8

            # 2ª Execução Idempotente com o mesmo mapa
            consolidated2, _ = orchestrator.process_incremental_diff(
                company_search_id=sid,
                all_processes_map=incoming_map,
                sync_db=True,
            )
            assert len(consolidated2) == 8
            count2 = ProcessRecord.query.filter_by(company_search_id=sid).count()
            assert count2 == 8, f"Não deve haver duplicatas: esperado 8, obtido {count2}"

        # Verifica se o processo 1 teve seu status atualizado para 'condenacoes'
        rec1 = ProcessRecord.query.filter_by(company_search_id=sid, numero_processo=_make_cnj(1)).first()
        assert rec1.sentence_outcome == "condenacoes"

        # Valida que o banco de dados bloqueia tentativa explícita de duplicata via UniqueConstraint
        with pytest.raises(IntegrityError):
            dup = ProcessRecord(
                company_search_id=sid,
                numero_processo=_make_cnj(1),  # Mesmo processo e mesma busca!
                tribunal="TRT3",
                sentence_outcome="em_aberto",
            )
            db.session.add(dup)
            db.session.commit()
        db.session.rollback()


def test_incremental_empty_and_null_safeguards(client, app):
    """Garante robustez caso a lista de processos seja vazia ou search_id seja nulo."""
    orchestrator = CNJMetadataOrchestrator()

    # Mapa vazio
    procs, stats = orchestrator.process_incremental_diff(
        company_search_id=None,
        all_processes_map={},
    )
    assert procs == []
    assert stats["total_cnjs"] == 0
    assert stats["reduction_pct"] == 0.0

    # Sem search_id existente: delega tudo ao DataJud sem crash
    sample_map = {_make_cnj(1): {"siglaTribunal": "TRT2"}}
    with patch.object(orchestrator.searcher, "enrich_top_tribunals_datajud", return_value=[{"numero_processo": _make_cnj(1)}]):
        res, stats2 = orchestrator.process_incremental_diff(
            company_search_id=None,
            all_processes_map=sample_map,
            sync_db=False,
        )
        assert len(res) == 1
        assert stats2["total_cnjs"] == 1
        assert stats2["skipped_closed"] == 0
        assert stats2["reduction_pct"] == 0.0
