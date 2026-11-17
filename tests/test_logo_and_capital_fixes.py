import re
import requests
from unittest.mock import MagicMock

import app.services.economic_service as econ_module
from app.services.cnj_searcher import CNJProcessSearcher
from app.services.economic_service import (
    belongs_to_same_corporate_brand,
    clear_cnpj_cache,
    discover_real_corporate_domain_and_logo,
    enrich_rfb_corporate_email,
    evaluate_economic_health_and_capital,
    fetch_accurate_cnpj_data_cached,
    resolve_br_domain_and_cnpj_via_rdap,
    resolve_company_logo,
)


def test_logo_rejects_homonyms_and_fuzzy_special_domain_collisions(monkeypatch):
    """
    Garante que empresas com nomes parecidos (ex: KRONOS vs Kronos Controls estrangeira,
    KEYSTONE vs STONE, VALENCA vs VALE) não exibam a logo de outra empresa.
    """
    def mock_clearbit(url, *args, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200 if "rdap.registro.br" not in url else 404
        if "KRONOS" in url.upper():
            resp.json.return_value = [
                {"name": "Kronos Controls AG", "domain": "kronos-controls.com"},
                {"name": "Kronos Building Control", "domain": "kronos.ch"},
            ]
        else:
            resp.json.return_value = []
        return resp

    monkeypatch.setattr(requests, "get", mock_clearbit)

    # 1. Marca nacional não deve pegar logo de empresa estrangeira homônima nem chutar domínio não validado
    kronos_logo = resolve_company_logo(user_query="KRONOS", razao_social="KRONOS DIGITAL LTDA")
    assert kronos_logo["domain"] is None
    assert kronos_logo["logo_url"] is None
    assert kronos_logo["fallback_initials"] == "KD"

    # 2. KEYSTONE não deve casar por substring com STONE
    keystone = discover_real_corporate_domain_and_logo("KEYSTONE SERVICOS LTDA")
    assert keystone["domain"] is None
    assert keystone["logo_url"] is None

    # 3. VALENCA / SANTANA não devem casar por substring ou fuzzy com VALE / SANTANDER
    valenca = discover_real_corporate_domain_and_logo("VALENCA ENGENHARIA LTDA")
    santana = discover_real_corporate_domain_and_logo("SANTANA LOGISTICA LTDA")
    assert valenca["domain"] is None
    assert santana["domain"] is None


def test_logo_accepts_verified_brazilian_clearbit_or_rfb_email(monkeypatch):
    """
    Aceita domínio verificado via e-mail corporativo da RFB ou Clearbit validado,
    mantendo os domínios especiais oficiais intactos.
    """
    # 1. E-mail corporativo da RFB
    logo_rfb = resolve_company_logo(
        user_query="KRONOS",
        razao_social="KRONOS DIGITAL LTDA",
        email_rfb="contato@kronosdigital.com.br",
    )
    assert logo_rfb["domain"] == "kronosdigital.com.br"
    assert "kronosdigital.com.br" in logo_rfb["logo_url"]

    # 2. Clearbit validado com domínio .br e correspondência nominal exata
    def mock_clearbit(url, *args, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200 if "rdap.registro.br" not in url else 404
        resp.json.return_value = [
            {"name": "Kronos Controls AG", "domain": "kronos-controls.com"},
            {"name": "Kronos Digital", "domain": "kronosdigital.com.br"},
        ]
        return resp

    monkeypatch.setattr(requests, "get", mock_clearbit)
    logo_cb = resolve_company_logo(
        user_query="KRONOS",
        razao_social="KRONOS DIGITAL LTDA",
    )
    assert logo_cb["domain"] == "kronosdigital.com.br"


def test_belongs_to_same_corporate_brand_rejects_short_substring_false_positives():
    """Tokens curtos não podem casar como substring dentro de outra palavra."""
    assert belongs_to_same_corporate_brand("SOL", "CONSOLACAO SERVICOS LTDA") is False
    assert belongs_to_same_corporate_brand("ORION", "ORIONIS COMERCIO LTDA") is False
    assert belongs_to_same_corporate_brand("ORION", "ORION DIGITAL LTDA") is True


def test_consolidate_does_not_leak_rejected_third_party_cnpj_or_fake_unit_count(monkeypatch):
    """
    Se os CNPJs encontrados pertencerem a terceiros (não passam em belongs_to_same_corporate_brand),
    o fallback NÃO deve somar o capital de terceiros nem exibir dezenas de filiais com R$ 0,00.
    """
    clear_cnpj_cache()

    def mock_fetch(cnpj, timeout=4.0):
        return {
            "cnpj": "11222333000181",
            "raiz": "11222333",
            "razao_social": "OUTRA EMPRESA TERCEIRA LTDA",
            "nome_fantasia": "TERCEIRA",
            "capital_social": 5_000_000.0,
            "porte": "DEMAIS",
            "situacao": "ATIVA",
            "qsa_pjs": [],
        }

    monkeypatch.setattr(econ_module, "fetch_accurate_cnpj_data", mock_fetch)
    monkeypatch.setattr(
        econ_module,
        "resolve_cnpjs_by_company_name_online",
        lambda *args, **kwargs: [],
    )

    procs = [
        {
            "numero_processo": f"00000{i:02d}20245020001",
            "cnpjs_encontrados": ["11222333000181"],
            "destinatarios_nomes": [f"VORTEX UNIDADE {i} LTDA"],
        }
        for i in range(10)
    ]

    res = evaluate_economic_health_and_capital(
        user_query="VORTEX",
        brand_term="VORTEX",
        all_processes=procs,
        conviction_prob_pct=40.0,
        search_strategy={"mode": "BROAD_BRAND"},
    )

    assert res["capital_social_num"] == 0.0
    assert res["unidades_consolidadas"] == 1
    assert res["cnpjs_consolidados"] == []


def test_fetch_provider_continues_past_zero_capital_to_real_capital(monkeypatch):
    """
    Se o primeiro provedor retornar capital_social=0.0, tenta o próximo provedor
    antes de aceitar e não envenena o cache permanente com 0.0.
    """
    clear_cnpj_cache()

    def mock_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "brasilapi.com.br" in url:
            resp.json.return_value = {
                "razao_social": "VORTEX DIGITAL LTDA",
                "capital_social": 0.0,
                "descricao_situacao_cadastral": "ATIVA",
            }
        elif "minhareceita.org" in url:
            resp.json.return_value = {
                "razao_social": "VORTEX DIGITAL LTDA",
                "capital_social": 1500000.0,
                "descricao_situacao_cadastral": "ATIVA",
            }
        else:
            resp.status_code = 500
        return resp

    monkeypatch.setattr(requests, "get", mock_get)

    data = fetch_accurate_cnpj_data_cached("33000167000101")
    assert data["capital_social"] == 1500000.0


def test_merge_process_data_fast_preserves_cnpjs_encontrados():
    """Garante que _merge_process_data_fast preserva cnpjs_encontrados extraídos do Comunica PJe."""
    searcher = CNJProcessSearcher()
    merged = searcher._merge_process_data_fast(
        "00001234520245020001",
        {
            "siglaTribunal": "TRT2",
            "publicacoes_texto": ["Intimação referente a horas extras."],
            "cnpjs_encontrados": ["33000167000101"],
            "destinatarios_nomes": ["EMPRESA TESTE S.A."],
            "classes_pje": ["ATOrd"],
        },
    )
    assert merged["cnpjs_encontrados"] == ["33000167000101"]


def test_logo_uses_process_corporate_razao_social_and_filters_individuals(monkeypatch):
    """
    Quando os processos contêm tanto pessoas físicas quanto a Razão Social completa
    em destinatarios_nomes, a resolução de logo ignora pessoas físicas e consulta
    o Clearbit com o núcleo da Razão Social corporativa, resolvendo o domínio correto.
    """
    clear_cnpj_cache()
    queries_seen = []

    def mock_get(url, *args, **kwargs):
        queries_seen.append(url)
        resp = MagicMock()
        resp.ok = True
        if "rdap.registro.br" in url:
            resp.status_code = 404
            resp.ok = False
            return resp
        resp.status_code = 200
        if "VORTEX%20DIGITAL" in url.upper():
            resp.json.return_value = [
                {"name": "Vortex", "domain": "vortex.digital", "logo": None},
            ]
        elif "query=VORTEX" in url.upper():
            resp.json.return_value = [
                {"name": "Vortex Controls AG", "domain": "vortex-controls.com", "logo": None},
                {"name": "vortexshop", "domain": "vortexshop.de", "logo": None},
            ]
        else:
            resp.json.return_value = []
        return resp

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(
        econ_module,
        "resolve_cnpjs_by_company_name_online",
        lambda *args, **kwargs: [],
    )

    processes = [
        {
            "numero_processo": "00001001020245020001",
            "destinatarios_nomes": ["DANIEL VORTEX", "VORTEX DIGITAL CONSULTORIA EM TECNOLOGIA LTDA"],
            "cnpjs_encontrados": [],
            "publicacoes_texto": [],
        }
    ]

    legal_names = econ_module.extract_process_legal_names(processes, brand_query="VORTEX")
    assert legal_names == ["VORTEX DIGITAL CONSULTORIA EM TECNOLOGIA LTDA"]

    svc = econ_module.EconomicStandingService()
    monkeypatch.setattr(svc, "fetch_brasilapi_data", lambda *args, **kwargs: {})
    monkeypatch.setattr(svc, "fetch_cgu_sanctions", lambda *args, **kwargs: {"possui_sancoes": False, "status_cgu": "REGULAR"})
    monkeypatch.setattr(svc, "fetch_pgfn_divida_ativa", lambda *args, **kwargs: {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA"})

    res = svc.evaluate_economic_standing(
        company_name="VORTEX (GRUPO CONSOLIDADO / FILIAIS)",
        company_processes=processes,
        p_condenacao_trabalhista=35.0,
        search_strategy={"mode": "BROAD_BRAND", "is_broad_brand": True},
    )

    assert any("VORTEX%20DIGITAL" in u.upper() for u in queries_seen)
    assert res["logo"]["domain"] == "vortex.digital"
    assert "vortex.digital" in res["logo"]["logo_url"]


def test_first_process_cnpj_feeds_rfb_email_into_domain_resolver(monkeypatch):
    """
    Assim que o primeiro processo revelar o CNPJ da empresa, o e-mail cadastral
    da Receita Federal alimenta o resolvedor de domínios (incluindo fallback entre
    provedores quando BrasilAPI não retorna e-mail).
    """
    clear_cnpj_cache()
    target_cnpj = "33000167000101"

    def mock_fetch_accurate(cnpj, timeout=4.0):
        return {
            "cnpj": target_cnpj,
            "raiz": target_cnpj[:8],
            "razao_social": "NEXUS CONSULTORIA E SISTEMAS LTDA",
            "nome_fantasia": "NEXUS DIGITAL",
            "capital_social": 250000.0,
            "porte": "DEMAIS",
            "situacao": "ATIVA",
            "email": "juridico@nexusdigital.com.br",
            "qsa_pjs": [],
        }

    monkeypatch.setattr(econ_module, "fetch_accurate_cnpj_data", mock_fetch_accurate)

    svc = econ_module.EconomicStandingService()
    # Simula BrasilAPI retornando sem email, forçando aproveitamento do email via fallback RFB
    monkeypatch.setattr(
        svc,
        "fetch_brasilapi_data",
        lambda c: {"razao_social": "NEXUS CONSULTORIA E SISTEMAS LTDA", "nome_fantasia": "", "email": ""},
    )
    monkeypatch.setattr(svc, "fetch_cgu_sanctions", lambda *args, **kwargs: {"possui_sancoes": False, "status_cgu": "REGULAR"})
    monkeypatch.setattr(svc, "fetch_pgfn_divida_ativa", lambda *args, **kwargs: {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA"})

    processes = [
        {
            "numero_processo": "00002002020245020001",
            "destinatarios_nomes": ["NEXUS CONSULTORIA E SISTEMAS LTDA"],
            "cnpjs_encontrados": [target_cnpj],
            "publicacoes_texto": ["Intime-se a ré inscrita no CNPJ 33.000.167/0001-01."],
        }
    ]

    res = svc.evaluate_economic_standing(
        company_name="NEXUS (GRUPO CONSOLIDADO / FILIAIS)",
        company_processes=processes,
        p_condenacao_trabalhista=40.0,
        search_strategy={"mode": "BROAD_BRAND", "is_broad_brand": True},
    )

    assert res["cnpj_triangulado"] == target_cnpj
    assert res["logo"]["domain"] == "nexusdigital.com.br"
    assert "nexusdigital.com.br" in res["logo"]["logo_url"]


def test_rdap_discovers_verified_domain_and_cnpj_from_process_legal_name(monkeypatch):
    """
    Garante que o fluxo via RDAP do Registro.br descobre CNPJ e domínio corporativo
    para qualquer empresa nacional com base na Razão Social extraída dos processos,
    rejeitando homônimos de outro ramo.
    """
    clear_cnpj_cache()

    def mock_rdap_get(url, *args, **kwargs):
        resp = MagicMock()
        if "rdap.registro.br/domain/atlas.com.br" in url:
            resp.status_code = 200
            resp.json.return_value = {
                "entities": [
                    {
                        "roles": ["registrant"],
                        "publicIds": [{"type": "cnpj", "identifier": "33.000.167/0001-01"}],
                        "vcardArray": ["vcard", [["fn", {}, "text", "ATLAS ENGENHARIA E SISTEMAS LTDA"]]],
                    }
                ]
            }
        else:
            resp.status_code = 404
            resp.json.return_value = {}
        return resp

    monkeypatch.setattr(requests, "get", mock_rdap_get)

    # Homônimo de outro segmento deve ser rejeitado
    dom_bad, cnpj_bad, _ = resolve_br_domain_and_cnpj_via_rdap(
        "ATLAS",
        legal_names=["ATLAS CONFECCOES TEXTEIS LTDA"],
    )
    assert dom_bad is None
    assert cnpj_bad is None

    # Empresa com mesma identidade corporativa deve resolver domínio e CNPJ oficiais
    dom_ok, cnpj_ok, fn_ok = resolve_br_domain_and_cnpj_via_rdap(
        "ATLAS",
        legal_names=["ATLAS ENGENHARIA E SISTEMAS LTDA"],
    )
    assert dom_ok == "atlas.com.br"
    assert cnpj_ok == "33000167000101"
    assert fn_ok == "ATLAS ENGENHARIA E SISTEMAS LTDA"


def test_enrich_rfb_corporate_email_falls_back_to_cnpjws_when_brasilapi_omits_email(monkeypatch):
    """
    Quando o provedor primário omite o campo email, enrich_rfb_corporate_email
    consulta o provedor secundário (CNPJ.ws) para recuperar o domínio corporativo real.
    """
    clear_cnpj_cache()

    def mock_get(url, *args, **kwargs):
        resp = MagicMock()
        if "publica.cnpj.ws/cnpj/33000167000101" in url:
            resp.status_code = 200
            resp.json.return_value = {
                "razao_social": "ATLAS ENGENHARIA E SISTEMAS LTDA",
                "capital_social": "1000000.00",
                "estabelecimento": {
                    "nome_fantasia": "ATLAS",
                    "situacao_cadastral": "ATIVA",
                    "email": "fiscal@atlas.digital",
                },
            }
        else:
            resp.status_code = 404
            resp.json.return_value = {}
        return resp

    monkeypatch.setattr(requests, "get", mock_get)
    email = enrich_rfb_corporate_email("33000167000101")
    assert email == "fiscal@atlas.digital"
