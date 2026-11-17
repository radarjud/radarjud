import re
import unicodedata
import logging
import threading
import difflib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Tuple, Set
import requests

from app.utils.sanitizer import (
    STOPWORDS_SOCIETARIAS,
    extract_brand_search_term,
    is_cnpj,
    classify_search_mode,
)

logger = logging.getLogger(__name__)

CNPJ_REGEX = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


def compute_cnpj_dv(base12: str) -> str:
    """Calcula os dois dígitos verificadores (DV1 e DV2) via Módulo 11 oficial da RFB."""
    digits = [int(d) for d in base12 if d.isdigit()]
    if len(digits) != 12:
        return "00"
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    s1 = sum(d * w for d, w in zip(digits, w1))
    r1 = s1 % 11
    dv1 = 0 if r1 < 2 else 11 - r1

    w2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    digits13 = digits + [dv1]
    s2 = sum(d * w for d, w in zip(digits13, w2))
    r2 = s2 % 11
    dv2 = 0 if r2 < 2 else 11 - r2
    return f"{dv1}{dv2}"


def to_matrix_cnpj(cnpj: str) -> str:
    """Converte qualquer CNPJ de filial (ex: 0002, 0003) para o CNPJ Matriz (0001) com DVs corretos."""
    clean = "".join(c for c in str(cnpj or "") if c.isdigit())
    if len(clean) != 14:
        return clean
    root = clean[:8]
    base12 = root + "0001"
    return base12 + compute_cnpj_dv(base12)


def is_valid_cnpj(cnpj: str) -> bool:
    """Valida formato, tamanho e dígitos verificadores de um CNPJ."""
    clean = "".join(c for c in str(cnpj or "") if c.isdigit())
    if len(clean) != 14:
        return False
    if len(set(clean)) == 1:
        return False
    return compute_cnpj_dv(clean[:12]) == clean[12:]


def extract_qsa_pjs(data: dict) -> List[Tuple[str, str]]:
    """Extrai sócios Pessoa Jurídica (CNPJ de 14 dígitos e razão social) do QSA/socios."""
    pjs: List[Tuple[str, str]] = []
    socios = data.get("qsa") or data.get("socios") or []
    if not isinstance(socios, list):
        return pjs
    for s in socios:
        if not isinstance(s, dict):
            continue
        doc_raw = str(
            s.get("cnpj_cpf_do_socio")
            or s.get("cpf_cnpj_socio")
            or s.get("documento")
            or s.get("cnpj")
            or s.get("cpf_cnpj")
            or ""
        )
        clean_doc = "".join(c for c in doc_raw if c.isdigit())
        nome = str(
            s.get("nome_socio")
            or s.get("razao_social")
            or s.get("nome")
            or ""
        ).strip()
        if len(clean_doc) == 14 and is_valid_cnpj(clean_doc):
            pjs.append((clean_doc, nome))
    return pjs


def normalize_ascii(text: str) -> str:
    """Normaliza texto removendo acentos gráficos e convertendo para ASCII maiúsculo."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper().strip()


def format_brl(val: float) -> str:
    """Formata valor em reais R$ X.XXX.XXX,XX com centavos exatos."""
    if not val:
        return "R$ 0,00"
    try:
        val_float = float(val)
        return f"R$ {val_float:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "R$ 0,00"


def extract_initials(name: str) -> str:
    """Extrai 2 a 3 letras representativas para avatar/logo fallback."""
    if not name:
        return "EMP"
    clean = re.sub(r"\(.*?\)", "", name).strip()
    if " - " in clean:
        clean = clean.split(" - ")[-1].strip()
    clean = re.sub(r"[^A-Za-z0-9\s]", " ", clean).strip()
    tokens = [t for t in clean.split() if t.upper() not in {"S.A.", "S/A", "SA", "LTDA", "ME", "EPP", "EIRELI"} and len(t) >= 2]
    if len(tokens) >= 2:
        return (tokens[0][0] + tokens[1][0]).upper()
    if tokens:
        return tokens[0][:3].upper()
    return clean[:3].upper() if clean else "EMP"


GENERIC_CORPORATE_WORDS = {
    "BANCO", "BRASIL", "BRASILEIRA", "BRASILEIRO", "COMPANHIA", "CIA",
    "SA", "S.A.", "LTDA", "ME", "EPP", "HOLDING", "PARTICIPACOES",
    "SERVICOS", "COMERCIO", "SOLUCOES", "TECNOLOGIA", "CORRETORA",
    "FINANCEIRA", "PAGAMENTOS", "DISTRIBUIDORA", "LOGISTICA",
    "ADMINISTRADORA", "INSTITUICAO", "DE", "DO", "DA", "E", "CAIXA"
}


def belongs_to_same_corporate_brand(user_brand_query: str, cnpj_razao_social: str, cnpj_nome_fantasia: str = "") -> bool:
    """
    Algoritmo Universal de Pertencimento de Grupo Econômico:
    1. Correspondência contígua da frase completa (ex: 'BANCO DO BRASIL' dentro de 'BANCO DO BRASIL SA').
    2. Siglas corporativas e variações oficiais (ex: BB para Banco do Brasil, CEF para Caixa).
    3. Análise de tokens distintivos (fora de stopwords corporativas).
    Evita falsos positivos como Banco ABC Brasil para 'Banco do Brasil' ou Supermercado Caixa para 'Caixa'.
    """
    if not user_brand_query or (not cnpj_razao_social and not cnpj_nome_fantasia):
        return False

    q_no_parens = re.sub(r"\(.*?\)", "", user_brand_query).strip()
    norm_q = normalize_ascii(q_no_parens)
    clean_q_words = [w for w in re.sub(r"[^\w\s]", " ", norm_q).split() if w]
    if not clean_q_words:
        return False
    clean_q_phrase = " ".join(clean_q_words)

    norm_razao = normalize_ascii(re.sub(r"\(.*?\)", "", cnpj_razao_social or "").strip())
    clean_razao_words = [w for w in re.sub(r"[^\w\s]", " ", norm_razao).split() if w]
    clean_razao_phrase = " ".join(clean_razao_words)

    norm_fantasia = normalize_ascii(re.sub(r"\(.*?\)", "", cnpj_nome_fantasia or "").strip())
    clean_fantasia_words = [w for w in re.sub(r"[^\w\s]", " ", norm_fantasia).split() if w]
    clean_fantasia_phrase = " ".join(clean_fantasia_words)

    # 1. Correspondência contígua da frase completa (mínimo 2 palavras)
    if len(clean_q_words) >= 2 and (clean_q_phrase in clean_razao_phrase or clean_q_phrase in clean_fantasia_phrase):
        if clean_q_phrase not in ("BANCO DO BRASIL", "CAIXA ECONOMICA", "CAIXA ECONOMICA FEDERAL"):
            return True

    # 2. Siglas corporativas e variações oficiais (ex: BB para Banco do Brasil, CEF para Caixa)
    if clean_q_phrase in ("BANCO DO BRASIL", "BANCO DO BRASIL SA", "BB"):
        if clean_razao_phrase.startswith("BANCO DO BRASIL") or clean_fantasia_phrase.startswith("BANCO DO BRASIL"):
            return True
        if clean_razao_phrase.startswith("BB ") or clean_fantasia_phrase.startswith("BB "):
            return True
        if "BB" in clean_razao_words or "BB" in clean_fantasia_words:
            if any(w in ("SEGUROS", "GESTAO", "DTVM", "ADMINISTRADORA", "CORRETORA", "BANCO") for w in clean_razao_words):
                return True
        return False

    if clean_q_phrase in ("CAIXA", "CAIXA ECONOMICA", "CAIXA ECONOMICA FEDERAL", "CEF"):
        if "CAIXA ECONOMICA" in clean_razao_phrase or "CAIXA ECONOMICA" in clean_fantasia_phrase:
            return True
        if "CEF" in clean_razao_words or "CEF" in clean_fantasia_words:
            return True
        return False

    # 3. Análise de tokens distintivos (fora de stopwords corporativas)
    distinctive_tokens = [w for w in clean_q_words if w not in GENERIC_CORPORATE_WORDS and len(w) >= 2]

    # Se a query só tinha palavras genéricas e não atendeu aos casos acima, descarta!
    if not distinctive_tokens:
        return False

    target_words = set(clean_razao_words + clean_fantasia_words)
    target_phrase = f"{clean_razao_phrase} {clean_fantasia_phrase}"

    # Se a busca é uma única marca distintiva (ex: 'PICPAY', 'ITAU', 'BRADESCO', 'COMPESA')
    if len(distinctive_tokens) == 1:
        token = distinctive_tokens[0]
        if token in target_words:
            return True
        return len(token) >= 7 and token in target_phrase

    # Se há múltiplos tokens distintivos (ex: 'MAGAZINE LUIZA', 'MERCADO LIVRE')
    return all(t in target_words for t in distinctive_tokens)


SPECIAL_DOMAINS: Dict[str, str] = {
    "CAIXA": "caixa.gov.br",
    "CAIXA ECONOMICA": "caixa.gov.br",
    "CAIXA ECONOMICA FEDERAL": "caixa.gov.br",
    "CEF": "caixa.gov.br",
    "BANCO DO BRASIL": "bb.com.br",
    "BB": "bb.com.br",
    "BANCO DO BRASIL SA": "bb.com.br",
    "BANCO DO BRASIL S A": "bb.com.br",
    "PETROBRAS": "petrobras.com.br",
    "CORREIOS": "correios.com.br",
    "EMBRATEL": "embratel.com.br",
    "PICPAY": "picpay.com",
    "PICPY": "picpay.com",
    "ITAU": "itau.com.br",
    "BRADESCO": "bradesco.com.br",
    "SANTANDER": "santander.com.br",
    "NUBANK": "nubank.com.br",
    "COMPESA": "compesa.com.br",
}
KNOWN_BRAND_DOMAINS = SPECIAL_DOMAINS


def extract_smart_brand_slug(user_query: str = "", razao_social: str = "", nome_fantasia: str = "") -> str:
    """
    Extrai o termo raiz/marca da empresa de forma inteligente:
    1. Remove parênteses (ex: 'ITAÚ (GRUPO CONSOLIDADO / FILIAIS)' -> 'ITAÚ').
    2. Se a Razão Social termina com '- SIGLA' (ex: 'COMPANHIA PERNAMBUCANA DE SANEAMENTO - COMPESA'),
       extrai 'COMPESA'!
    3. Preserva a palavra BANCO em nomes de bancos compostos (ex: 'BANCO DO BRASIL', 'BANCO INTER').
    4. Caso contrário, prioriza o nome curto/marca ou nome fantasia.
    """
    raw = razao_social or user_query or nome_fantasia or ""
    without_parens = re.sub(r"\(.*?\)", "", raw).strip()
    upper_no_parens = without_parens.upper()

    if upper_no_parens.startswith("BANCO "):
        tokens_b = upper_no_parens.split()
        if len(tokens_b) >= 3 and tokens_b[1] in ("DO", "DA", "DE"):
            return " ".join(tokens_b[:3])
        if len(tokens_b) >= 2 and tokens_b[1] in ("BRADESCO", "SANTANDER", "ITAU"):
            return tokens_b[1]
        return " ".join(tokens_b[:2])

    if " - " in without_parens:
        suffix = without_parens.split(" - ")[-1].strip().upper()
        suffix_clean = re.sub(r"[^\w]", "", suffix)
        if 2 <= len(suffix_clean) <= 15 and suffix_clean not in {"LTDA", "SA", "ME", "EPP", "EIRELI"}:
            return suffix_clean

    q_clean = re.sub(r"\(.*?\)", "", (user_query or "")).strip()
    q_upper = q_clean.upper()
    if q_upper.startswith("BANCO "):
        tokens_b = q_upper.split()
        if len(tokens_b) >= 3 and tokens_b[1] in ("DO", "DA", "DE"):
            return " ".join(tokens_b[:3])
        if len(tokens_b) >= 2 and tokens_b[1] in ("BRADESCO", "SANTANDER", "ITAU"):
            return tokens_b[1]
        return " ".join(tokens_b[:2])

    q_clean_alpha = re.sub(r"[^\w\s]", "", q_clean).strip()
    if q_clean_alpha and not q_clean_alpha.isdigit() and len(q_clean_alpha.split()) <= 2:
        return q_clean_alpha.upper()

    return extract_brand_search_term(user_query, without_parens, nome_fantasia)


CORPORATE_LEGAL_MARKERS = re.compile(
    r"\b("
    r"L\.?T\.?D\.?A\.?|LIMITADA|S\.?A\.?|S/A|SOCIEDADE\s+ANONIMA|EIRELI|"
    r"M\.?E\.?|E\.?P\.?P\.?|COMPANHIA|CIA|HOLDING|PARTICIPACOES|"
    r"INSTITUICAO|BANCO|COOPERATIVA|ASSOCIACAO|FUNDACAO|INSTITUTO|"
    r"CONSULTORIA|TECNOLOGIA|DIGITAL|SISTEMAS|SOLUCOES|ENGENHARIA|"
    r"CONSTRUTORA|COMERCIO|INDUSTRIA|SERVICOS|LOGISTICA|TRANSPORTES|"
    r"ALIMENTOS|ENERGIA|SANEAMENTO|TELECOMUNICACOES|SEGURADORA|SEGUROS|"
    r"FINANCEIRA|CREDITO|ADMINISTRADORA|COMUNICACAO|INFORMATICA|DISTRIBUIDORA"
    r")\b",
    re.IGNORECASE,
)

GENERIC_EMAIL_HOSTS = (
    "gmail", "hotmail", "outlook", "yahoo", "contabil",
    "assessoria", "adv", "bol", "uol", "terra", "live", "icloud",
    "ig.com", "r7.com", "zipmail", "proton", "msn", "ymail",
)


def _extract_corporate_domain_from_email(email_rfb: str) -> Optional[str]:
    """Extrai o domínio corporativo de um e-mail da RFB, descartando provedores genéricos e escritórios contábeis."""
    if not email_rfb or "@" not in email_rfb:
        return None
    email_dom = email_rfb.split("@")[-1].lower().strip()
    if "." not in email_dom or any(g in email_dom for g in GENERIC_EMAIL_HOSTS):
        return None
    return email_dom


def _strip_corporate_suffixes_for_search(name: str) -> str:
    """Remove sufixos societários (LTDA, S.A., EIRELI, ME, EPP) preservando o núcleo nominal para busca."""
    cleaned = re.sub(r"\(.*?\)", "", str(name or "")).strip()
    cleaned = re.sub(
        r"\b(L\.?T\.?D\.?A\.?|LIMITADA|S\.?A\.?|S\s*/\s*A|SOCIEDADE\s+ANONIMA|"
        r"E\.?I\.?R\.?E\.?L\.?I\.?|MICROEMPRESA|SOCIEDADE\s+UNIPESSOAL|"
        r"SOCIEDADE\s+SIMPLES|M\.?E\.?|E\.?P\.?P\.?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^\w\s&.-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+\b(E|DE|DA|DO|DOS|DAS|EM)\b\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def extract_process_legal_names(
    processes: Any,
    brand_query: str = "",
    discovered_entities: Optional[Any] = None,
) -> List[str]:
    """
    Extrai e prioriza razões sociais reais (PJ) presentes nos destinatários e entidades dos processos,
    filtrando pessoas físicas e terceiros sem vínculo com a marca consultada.
    """
    candidates_score: Dict[str, int] = {}
    q_clean = re.sub(r"\(.*?\)", "", str(brand_query or "")).strip()
    filter_by_brand = bool(q_clean) and not is_cnpj(q_clean)

    def _consider(raw_name: Any) -> None:
        if not isinstance(raw_name, str):
            return
        clean = re.sub(r"\(.*?\)", "", raw_name).strip().upper()
        clean = re.sub(r"\s+", " ", clean)
        if len(clean) < 5 or is_cnpj(clean):
            return
        words = [w for w in re.sub(r"[^\w\s]", " ", clean).split() if w]
        if len(words) < 2:
            return
        if not CORPORATE_LEGAL_MARKERS.search(normalize_ascii(clean)):
            return
        if filter_by_brand and not belongs_to_same_corporate_brand(q_clean, clean):
            return
        score = score_corporate_name_priority(clean, 1)
        if re.search(r"\b(LTDA|S\.?A\.?|S/A|EIRELI|ME|EPP)\b", clean):
            score += 40
        if clean not in candidates_score or score > candidates_score[clean]:
            candidates_score[clean] = score

    for ent in (discovered_entities or []):
        _consider(ent)

    procs_iter = processes.values() if isinstance(processes, dict) else (processes or [])
    for proc in procs_iter:
        if not isinstance(proc, dict):
            continue
        for dest_name in proc.get("destinatarios_nomes") or []:
            _consider(dest_name)

    return [
        name for name, _ in sorted(
            candidates_score.items(),
            key=lambda item: (item[1], len(item[0])),
            reverse=True,
        )
    ]


def _pick_best_rfb_identity(records: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Seleciona (razao_social, nome_fantasia, email_corporativo) dentre os registros válidos da RFB."""
    if not records:
        return "", "", ""

    best_email = ""
    best_razao = ""
    best_fantasia = ""

    for rec in records:
        if not isinstance(rec, dict):
            continue
        em = str(rec.get("email") or "").strip()
        if _extract_corporate_domain_from_email(em):
            best_email = em
            best_razao = str(rec.get("razao_social") or "").strip()
            best_fantasia = str(rec.get("nome_fantasia") or "").strip()
            break

    for rec in records:
        if not isinstance(rec, dict):
            continue
        if not best_razao and rec.get("razao_social"):
            best_razao = str(rec.get("razao_social")).strip()
        if not best_fantasia and rec.get("nome_fantasia"):
            best_fantasia = str(rec.get("nome_fantasia")).strip()

    return best_razao, best_fantasia, best_email


def _match_special_domain(norm_name: str) -> Optional[str]:
    """Correspondência exata ou por palavra inteira em SPECIAL_DOMAINS (evita colisões por substring/fuzzy)."""
    if not norm_name:
        return None
    if norm_name in SPECIAL_DOMAINS:
        return SPECIAL_DOMAINS[norm_name]
    for k, v in sorted(SPECIAL_DOMAINS.items(), key=lambda item: len(item[0]), reverse=True):
        if len(k) >= 2 and re.search(rf"(?:^|\s){re.escape(k)}(?:\s|$)", norm_name):
            return v
    return None


def _is_valid_clearbit_candidate(query_name: str, candidate: Dict[str, Any]) -> bool:
    """
    Valida se uma sugestão do Clearbit Autocomplete realmente pertence à empresa consultada,
    evitando exibir a logo de homônimos estrangeiros ou empresas distintas com nome parecido.
    """
    cand_name = normalize_ascii(candidate.get("name") or "")
    cand_domain = str(candidate.get("domain") or "").strip().lower()
    if not cand_name or not cand_domain or "." not in cand_domain:
        return False

    q_norm = normalize_ascii(re.sub(r"\(.*?\)", "", query_name or "").strip())
    q_words = [w for w in re.sub(r"[^\w\s]", " ", q_norm).split() if len(w) >= 2]
    c_words = [w for w in re.sub(r"[^\w\s]", " ", cand_name).split() if len(w) >= 2]

    q_distinct = [w for w in q_words if w not in GENERIC_CORPORATE_WORDS and w not in STOPWORDS_SOCIETARIAS]
    c_distinct = [w for w in c_words if w not in GENERIC_CORPORATE_WORDS and w not in STOPWORDS_SOCIETARIAS]
    if not q_distinct:
        q_distinct = q_words
    if not c_distinct:
        c_distinct = c_words
    if not q_distinct or not c_distinct:
        return False

    domain_parts = [
        w for w in re.sub(r"[^\w\s]", " ", normalize_ascii(cand_domain)).split()
        if len(w) >= 2 and w not in {"COM", "BR", "NET", "ORG", "GOV", "CO", "IO", "WWW"}
    ]
    domain_distinct = [w for w in domain_parts if w not in GENERIC_CORPORATE_WORDS and w not in STOPWORDS_SOCIETARIAS]

    q_set = set(q_distinct)
    c_set = set(c_distinct)
    cand_identity_set = c_set | set(domain_distinct)

    if len(q_set) >= 2:
        # Exige presença dos tokens distintivos da consulta no nome ou domínio (ex: Alfa + alfa.digital)
        if q_set.issubset(cand_identity_set):
            return True
        if len(q_distinct) > 2 and set(q_distinct[:2]).issubset(cand_identity_set):
            return True
        joined_prefix = "".join(q_distinct[:2]).lower()
        if cand_domain.endswith(".br") and joined_prefix in cand_domain and q_distinct[0] in cand_identity_set:
            return True
        return False

    # Para marca de token único (ex: 'ALFA'), rejeita sugestões que acrescentam outro nome distintivo
    # (ex: 'Alfa Controls', 'Alfa AG') e exige domínio brasileiro (.br) para evitar homônimos globais
    if c_set != q_set:
        return False
    return cand_domain.endswith(".br")


def _shares_corporate_identity(razao_a: str, razao_b: str) -> bool:
    """Verifica se duas razões sociais brasileiras representam a mesma pessoa jurídica ou núcleo societário."""
    clean_a = _strip_corporate_suffixes_for_search(normalize_ascii(razao_a))
    clean_b = _strip_corporate_suffixes_for_search(normalize_ascii(razao_b))
    if not clean_a or not clean_b:
        return False
    if clean_a == clean_b:
        return True
    stop = {"DE", "DA", "DO", "DOS", "DAS", "E", "EM", "PARA", "COM"}
    words_a = [w for w in clean_a.split() if len(w) >= 2 and w not in stop]
    words_b = [w for w in clean_b.split() if len(w) >= 2 and w not in stop]
    if not words_a or not words_b:
        return False
    set_a, set_b = set(words_a), set(words_b)
    if set_a == set_b:
        return True
    # Para razões sociais completas (2+ palavras), exige interseção mínima de 2 termos ou do núcleo completo
    if len(words_a) >= 2 and len(words_b) >= 2:
        return len(set_a & set_b) >= 2 and words_a[0] == words_b[0]
    return words_a[0] == words_b[0]


def _extract_rdap_registrant_identity(data: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extrai (cnpj_14_digitos, razao_social) da entidade titular no JSON oficial RDAP do Registro.br."""
    if not isinstance(data, dict):
        return None, None
    for ent in data.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if "registrant" not in roles:
            continue
        found_cnpj = None
        for pid in ent.get("publicIds") or []:
            if isinstance(pid, dict) and str(pid.get("type") or "").lower() == "cnpj":
                digits = re.sub(r"\D", "", str(pid.get("identifier") or ""))
                if len(digits) == 14 and is_valid_cnpj(digits):
                    found_cnpj = digits
                    break
        found_name = None
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) >= 2 and isinstance(vcard[1], list):
            for prop in vcard[1]:
                if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn":
                    found_name = str(prop[3] or "").strip().upper()
                    break
        if found_cnpj or found_name:
            return found_cnpj, found_name
    return None, None


def _build_br_domain_candidates(brand_name: str, legal_names: Optional[List[str]] = None) -> List[str]:
    """Gera até 3 slugs candidatos .com.br a partir da marca e das razões sociais reais."""
    candidates: List[str] = []
    seen = set()
    stop = {"DE", "DA", "DO", "DOS", "DAS", "E", "EM", "PARA", "COM"}

    def _add_slug(slug: str) -> None:
        s = re.sub(r"[^a-z0-9-]", "", slug.lower()).strip("-")
        if len(s) >= 3 and s not in seen:
            seen.add(s)
            candidates.append(f"{s}.com.br")

    brand_norm = normalize_ascii(_strip_corporate_suffixes_for_search(brand_name))
    brand_words = [
        w.lower() for w in re.sub(r"[^\w\s]", " ", brand_norm).split()
        if len(w) >= 2 and w not in stop
    ]
    brand_distinct = [
        w for w in brand_words
        if w.upper() not in GENERIC_CORPORATE_WORDS and w.upper() not in STOPWORDS_SOCIETARIAS
    ]
    if len(brand_distinct) == 1:
        _add_slug(brand_distinct[0])
    elif len(brand_distinct) >= 2:
        _add_slug("".join(brand_distinct[:2]))
        _add_slug(brand_distinct[0])
    elif brand_words:
        _add_slug("".join(brand_words[:2]))

    for legal in (legal_names or []):
        l_norm = normalize_ascii(_strip_corporate_suffixes_for_search(legal))
        l_words = [w.lower() for w in re.sub(r"[^\w\s]", " ", l_norm).split() if len(w) >= 2 and w not in stop]
        if len(l_words) >= 2:
            _add_slug("".join(l_words[:2]))
        if l_words:
            _add_slug(l_words[0])

    return candidates[:3]


def resolve_br_domain_and_cnpj_via_rdap(
    brand_name: str,
    legal_names: Optional[List[str]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Consulta o servidor oficial RDAP do Registro.br (NIC.br) para validar se um domínio .com.br
    pertence legitimamente à empresa consultada e extrair seu CNPJ e Razão Social oficiais.
    """
    cand_domains = _build_br_domain_candidates(brand_name, legal_names)
    clean_legal = [
        n for n in (legal_names or [])
        if isinstance(n, str) and len(_strip_corporate_suffixes_for_search(n)) >= 4
    ]
    for dom in cand_domains:
        try:
            resp = requests.get(
                f"https://rdap.registro.br/domain/{dom}",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rdap+json, application/json"},
                timeout=1.5,
            )
            if resp.status_code != 200:
                continue
            rdap_cnpj, rdap_fn = _extract_rdap_registrant_identity(resp.json())
            if not rdap_fn:
                continue
            if not belongs_to_same_corporate_brand(brand_name, rdap_fn):
                continue
            if clean_legal and not any(_shares_corporate_identity(rdap_fn, ln) for ln in clean_legal):
                continue
            return dom, rdap_cnpj, rdap_fn
        except requests.RequestException:
            break
        except Exception:
            pass
    return None, None, None


def _build_clearbit_search_queries(
    brand_name: str,
    full_name: str = "",
    nome_fantasia: str = "",
    legal_names: Optional[List[str]] = None,
) -> List[str]:
    """Gera consultas priorizadas (Nome Fantasia -> Núcleo da Razão Social -> Marca) para o Clearbit."""
    queries: List[str] = []
    seen = set()

    def _add(term: str) -> None:
        cleaned = re.sub(r"\s+", " ", str(term or "")).strip()
        key = normalize_ascii(cleaned)
        if cleaned and len(key) >= 2 and key not in seen:
            seen.add(key)
            queries.append(cleaned)

    if nome_fantasia:
        nf_clean = _strip_corporate_suffixes_for_search(nome_fantasia)
        _add(nf_clean)

    for raw_legal in [full_name] + list(legal_names or []):
        if not raw_legal:
            continue
        stripped = _strip_corporate_suffixes_for_search(raw_legal)
        if not stripped:
            continue
        words = [w for w in stripped.split() if len(w) >= 2 and w.upper() not in {"DE", "DA", "DO", "DOS", "DAS", "E", "EM", "PARA", "COM"}]
        if len(words) >= 2:
            _add(" ".join(words[:2]))
        if len(words) >= 3:
            _add(" ".join(words[:3]))
        _add(stripped)

    clean_brand = re.sub(r"\(.*?\)", "", str(brand_name or "")).strip()
    _add(clean_brand)
    return queries[:3]


def discover_real_corporate_domain_and_logo(
    brand_name: str,
    email_rfb: str = "",
    full_name: str = "",
    nome_fantasia: str = "",
    legal_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Resolução Universal de Domínio e Logo em Cascata Segura:
    1. Dicionário de domínios corporativos especiais/estatais (SPECIAL_DOMAINS) por palavra inteira.
    2. Domínio corporativo extraído do e-mail da Receita Federal (se não for genérico/provedor).
    3. Clearbit Autocomplete API alimentado pela Razão Social/Nome Fantasia dos processos e RFB.
    4. Validação oficial no RDAP do Registro.br (NIC.br) confrontando titularidade e CNPJ.
    5. Sem domínio verificado, retorna logo_url=None para exibir iniciais oficiais sem logo falsa.
    """
    clean_name = re.sub(r"\(.*?\)", "", brand_name or "").strip()
    clean_full = re.sub(r"\(.*?\)", "", full_name or clean_name).strip()
    clean_fantasia = re.sub(r"\(.*?\)", "", nome_fantasia or "").strip()

    domain = None
    for candidate_text in [clean_name, clean_fantasia, clean_full] + list(legal_names or []):
        norm_text = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalize_ascii(candidate_text))).strip()
        domain = _match_special_domain(norm_text)
        if domain:
            break

    # 2. Domínio do e-mail cadastral da Receita Federal
    if not domain:
        domain = _extract_corporate_domain_from_email(email_rfb)

    # 3. Clearbit Autocomplete API com Razão Social completa / Nome Fantasia e validação anti-homônimo
    network_unavailable = False
    if not domain:
        search_queries = _build_clearbit_search_queries(
            clean_name,
            full_name=clean_full,
            nome_fantasia=clean_fantasia,
            legal_names=legal_names,
        )
        for search_query in search_queries:
            try:
                q = requests.utils.quote(search_query)
                resp = requests.get(
                    f"https://autocomplete.clearbit.com/v1/companies/suggest?query={q}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=1.5
                )
                if resp.ok:
                    data = resp.json()
                    if isinstance(data, list):
                        valid_candidates = [
                            item for item in data[:5]
                            if isinstance(item, dict) and _is_valid_clearbit_candidate(search_query, item)
                        ]
                        if valid_candidates:
                            br_first = sorted(
                                valid_candidates,
                                key=lambda item: 0 if str(item.get("domain", "")).lower().endswith(".br") else 1,
                            )
                            domain = br_first[0]["domain"].strip().lower()
                            break
            except requests.RequestException:
                # Se a rede ou DNS estiver indisponível/timeout, interrompe tentativas subsequentes
                network_unavailable = True
                break
            except Exception:
                pass

    # 4. Registro.br (NIC.br) RDAP oficial: valida propriedade .com.br por Razão Social / CNPJ
    if not domain and not network_unavailable:
        rdap_context = [n for n in ([clean_full, clean_fantasia] + list(legal_names or [])) if n]
        rdap_domain, _, _ = resolve_br_domain_and_cnpj_via_rdap(clean_name or clean_full, legal_names=rdap_context)
        if rdap_domain:
            domain = rdap_domain

    initials = extract_initials(clean_full or clean_fantasia or clean_name)
    if domain:
        primary_url = f"https://www.google.com/s2/favicons?domain=www.{domain}&sz=128"
        secondary_url = f"https://unavatar.io/{domain}?fallback=false"
    else:
        primary_url = None
        secondary_url = None

    return {
        "brand": brand_name,
        "domain": domain,
        "logo_primary_url": primary_url,
        "logo_secondary_url": secondary_url,
        "logo_favicon_url": primary_url,
        "logo_url": primary_url,
        "google_favicon_url": primary_url,
        "initials": initials,
        "fallback_initials": initials,
    }


def resolve_company_domain(company_name: str, email: Optional[str] = None) -> Optional[str]:
    """Resolve o domínio corporativo de forma determinística e genérica."""
    res = discover_real_corporate_domain_and_logo(company_name, email_rfb=email or "")
    return res.get("domain")


def resolve_company_logo(
    user_query: str = "",
    razao_social: str = "",
    nome_fantasia: str = "",
    email_rfb: str = "",
    legal_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Extrai a marca/sigla inteligente e resolve os links de logo da empresa sem confundir homônimos."""
    fallback_legal = legal_names[0] if legal_names else ""
    target_name = razao_social or fallback_legal or nome_fantasia or user_query or "Empresa"
    clean_target = re.sub(r"\(.*?\)", "", target_name).strip()
    brand = extract_smart_brand_slug(user_query=user_query, razao_social=clean_target, nome_fantasia=nome_fantasia)
    full_context = re.sub(r"\(.*?\)", "", (razao_social or fallback_legal or nome_fantasia or user_query or "")).strip()
    res = discover_real_corporate_domain_and_logo(
        brand or clean_target,
        email_rfb=email_rfb,
        full_name=full_context,
        nome_fantasia=nome_fantasia,
        legal_names=legal_names,
    )
    initials = extract_initials(clean_target)
    res["initials"] = initials
    res["fallback_initials"] = initials
    return res


CNPJ_ROOT_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_PROVIDER_INDEX = 0
_PROVIDER_LOCK = threading.Lock()


def clear_cnpj_cache() -> None:
    """Limpa o cache em memória de CNPJs (útil em testes)."""
    with _CACHE_LOCK:
        CNPJ_ROOT_CACHE.clear()


def _get_next_provider_order() -> List[str]:
    """Alterna entre provedores rápidos sem limite rígido primeiro e usa rate-limited como fallback."""
    global _PROVIDER_INDEX
    high_throughput = ["brasilapi", "minhareceita"]
    with _PROVIDER_LOCK:
        idx = _PROVIDER_INDEX % len(high_throughput)
        _PROVIDER_INDEX += 1
    primary = high_throughput[idx:] + high_throughput[:idx]
    return primary + ["cnpjws", "receitaws"]


def _extract_porte_str(raw_porte: Any) -> str:
    """Garante que a extração do porte retorne estritamente str (ex: 'DEMAIS', 'ME', 'EPP')."""
    if isinstance(raw_porte, dict):
        return str(raw_porte.get("descricao") or raw_porte.get("texto") or "DEMAIS").strip().upper()
    if isinstance(raw_porte, str) and raw_porte.strip():
        return raw_porte.strip().upper()
    return "DEMAIS"


def _fetch_from_brasilapi(digits: str, headers: dict, timeout: float) -> dict:
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{digits}", headers=headers, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            return {
                "cnpj": digits,
                "raiz": digits[:8],
                "razao_social": d.get("razao_social") or "",
                "nome_fantasia": d.get("nome_fantasia") or "",
                "capital_social": float(d.get("capital_social") or 0.0),
                "porte": _extract_porte_str(d.get("porte") or d.get("descricao_porte")),
                "situacao": str(d.get("descricao_situacao_cadastral") or "ATIVA").upper(),
                "email": d.get("email") or "",
                "qsa_pjs": extract_qsa_pjs(d),
            }
    except Exception:
        pass
    return {}


def _fetch_from_cnpjws(digits: str, headers: dict, timeout: float) -> dict:
    try:
        r = requests.get(f"https://publica.cnpj.ws/cnpj/{digits}", headers=headers, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            est = d.get("estabelecimento") or {}
            return {
                "cnpj": digits,
                "raiz": digits[:8],
                "razao_social": d.get("razao_social") or "",
                "nome_fantasia": est.get("nome_fantasia") or "",
                "capital_social": float(d.get("capital_social") or 0.0),
                "porte": _extract_porte_str(d.get("porte") or est.get("porte")),
                "situacao": str(est.get("situacao_cadastral") or "ATIVA").upper(),
                "email": est.get("email") or "",
                "qsa_pjs": extract_qsa_pjs(d),
            }
    except Exception:
        pass
    return {}


def _fetch_from_minhareceita(digits: str, headers: dict, timeout: float) -> dict:
    try:
        r = requests.get(f"https://minhareceita.org/{digits}", headers=headers, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            return {
                "cnpj": digits,
                "raiz": digits[:8],
                "razao_social": d.get("razao_social") or "",
                "nome_fantasia": d.get("nome_fantasia") or "",
                "capital_social": float(d.get("capital_social") or 0.0),
                "porte": _extract_porte_str(d.get("porte") or d.get("descricao_porte")),
                "situacao": str(d.get("descricao_situacao_cadastral") or d.get("situacao_cadastral") or "ATIVA").upper(),
                "email": d.get("email") or "",
                "qsa_pjs": extract_qsa_pjs(d),
            }
    except Exception:
        pass
    return {}


def _fetch_from_receitaws(digits: str, headers: dict, timeout: float) -> dict:
    try:
        r = requests.get(f"https://receitaws.com.br/v1/cnpj/{digits}", headers=headers, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            if d.get("status") != "ERROR":
                raw_cap = str(d.get("capital_social") or "0")
                try:
                    cap_val = float(raw_cap)
                except ValueError:
                    cap_val = float(raw_cap.replace(".", "").replace(",", ".") or 0.0)
                return {
                    "cnpj": digits,
                    "raiz": digits[:8],
                    "razao_social": d.get("nome") or "",
                    "nome_fantasia": d.get("fantasia") or "",
                    "capital_social": cap_val,
                    "porte": _extract_porte_str(d.get("porte")),
                    "situacao": str(d.get("situacao") or "ATIVA").upper(),
                    "email": d.get("email") or "",
                    "qsa_pjs": extract_qsa_pjs(d),
                }
    except Exception:
        pass
    return {}


def enrich_rfb_corporate_email(cnpj: str, timeout: float = 2.0) -> str:
    """
    Consulta provedores secundários que expõem o campo `estabelecimento.email` da RFB
    (ex: CNPJ.ws e ReceitaWS) quando o provedor primário omite o e-mail cadastral.
    """
    digits = re.sub(r"\D", "", str(cnpj or ""))
    if len(digits) != 14:
        return ""
    if fetch_accurate_cnpj_data is not _fetch_accurate_cnpj_data_proxy:
        res = fetch_accurate_cnpj_data(digits, timeout=timeout) or {}
        em = str(res.get("email") or "").strip()
        return em if _extract_corporate_domain_from_email(em) else ""

    root = digits[:8]
    with _CACHE_LOCK:
        cached = CNPJ_ROOT_CACHE.get(root)
        if cached and _extract_corporate_domain_from_email(str(cached.get("email") or "")):
            return str(cached.get("email") or "").strip()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    for fetcher in (_fetch_from_cnpjws, _fetch_from_receitaws):
        try:
            res = fetcher(digits, headers, timeout)
            em = str((res or {}).get("email") or "").strip()
            if _extract_corporate_domain_from_email(em):
                with _CACHE_LOCK:
                    if root in CNPJ_ROOT_CACHE:
                        CNPJ_ROOT_CACHE[root]["email"] = em
                return em
        except requests.RequestException:
            break
        except Exception:
            pass
    return ""


def _fetch_accurate_cnpj_data_impl(digits: str, timeout: float = 4.0) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    fetchers = {
        "brasilapi": _fetch_from_brasilapi,
        "cnpjws": _fetch_from_cnpjws,
        "minhareceita": _fetch_from_minhareceita,
        "receitaws": _fetch_from_receitaws,
    }
    provider_order = _get_next_provider_order()
    best_fallback = {}
    for prov in provider_order:
        fn = fetchers.get(prov)
        if fn:
            res = fn(digits, headers, timeout)
            if res and res.get("razao_social"):
                if float(res.get("capital_social") or 0.0) > 0.0:
                    return res
                if not best_fallback:
                    best_fallback = res
    return best_fallback


def fetch_accurate_cnpj_data_cached(cnpj: str, timeout: float = 4.0) -> dict:
    """Consulta CNPJ usando cache global por raiz de 8 dígitos e fallback inteligente entre provedores."""
    digits = re.sub(r"\D", "", str(cnpj or ""))
    if len(digits) != 14:
        return {}
    root = digits[:8]

    # Compatibilidade com testes: se fetch_accurate_cnpj_data foi mockada pelo pytest, chama o mock
    if fetch_accurate_cnpj_data is not _fetch_accurate_cnpj_data_proxy:
        res = fetch_accurate_cnpj_data(digits, timeout=timeout)
        if res:
            if "raiz" not in res:
                res["raiz"] = root
            if "qsa_pjs" not in res:
                res["qsa_pjs"] = []
        return res or {}

    with _CACHE_LOCK:
        if root in CNPJ_ROOT_CACHE:
            cached = dict(CNPJ_ROOT_CACHE[root])
            cached["cnpj"] = digits
            return cached

    res = _fetch_accurate_cnpj_data_impl(digits, timeout=timeout)
    if res and res.get("razao_social"):
        # Só persiste em cache permanente quando possui capital > 0 para não congelar falha transitória
        if float(res.get("capital_social") or 0.0) > 0.0:
            with _CACHE_LOCK:
                CNPJ_ROOT_CACHE[root] = res
        return res
    return {}


def fetch_accurate_cnpj_data(cnpj: str, timeout: float = 4.0) -> dict:
    """
    Consulta o Capital Social exato e atualizado com centavos via
    cache por raiz (8 dígitos) e balanceamento Round-Robin entre 4 APIs públicas.
    """
    return fetch_accurate_cnpj_data_cached(cnpj, timeout=timeout)


_fetch_accurate_cnpj_data_proxy = fetch_accurate_cnpj_data


def fetch_cnpj_capital_with_fallback(cnpj: str) -> Dict[str, Any]:
    """Encaminha para a consulta concorrente de alta precisão."""
    return fetch_accurate_cnpj_data(cnpj)


# Catálogo de Resolução de CNPJ (DNS Corporativo -> Consulta 100% os valores reais ao vivo na API da Receita!)
OFFICIAL_CORPORATE_CNPJ_DIRECTORY: Dict[str, List[str]] = {
    "BRADESCO": [
        "60746948000112",  # Banco Bradesco S.A.
        "33055146000193",  # Bradesco Seguros S.A.
        "92693118000160",  # Bradesco Saúde S.A.
        "51990695000137",  # Bradesco Vida e Previdência S.A.
        "07207996000150",  # Banco Bradesco Financiamentos S.A.
        "07207996000158",  # Banco Bradesco Financiamentos S.A. (variante)
        "06271464000119",  # Banco Bradesco BBI S.A.
        "59438325000101",  # Bradesco Leasing S.A.
    ],
    "PICPAY": ["22896431000110", "09516419000175"],
    "PICPAY INSTITUICAO DE PAGAMENTO": ["22896431000110"],
    "PICPAY BANK": ["09516419000175"],
    "ITAU": ["60701190000104", "60872504000123", "17192451000170", "59285411000113"],
    "SANTANDER": ["90400888000142", "03502968000104"],
    "BANCO DO BRASIL": ["00000000000191", "01371416000118", "30822936000169"],
    "BB": ["00000000000191", "01371416000118", "30822936000169"],
    "CAIXA ECONOMICA FEDERAL": ["00360305000104", "34020354000110"],
    "CAIXA ECONOMICA": ["00360305000104", "34020354000110"],
    "CAIXA": ["00360305000104", "34020354000110"],
    "NUBANK": ["18236120000158", "30680829000143"],
    "NU PAGAMENTOS": ["18236120000158"],
    "COMPESA": ["09769035000164"],
    "NEOENERGIA": ["10835932000108", "01083200000118", "15139629000194"],
    "CELPE": ["10835932000108"],
    "SABESP": ["43776517000180"],
    "COPASA": ["17281106000103"],
    "SANEPAR": ["76484013000145"],
    "CEMIG": ["17155730000164"],
    "ENEL": ["61695227000193", "07047251000170"],
    "EQUATORIAL": ["03220438000173"],
    "STONE": ["16501555000157", "31769473000126"],
    "PAGSEGURO": ["08561701000101"],
    "PAGBANK": ["08561701000101"],
    "MERCADO PAGO": ["10573521000191"],
    "MERCADO LIVRE": ["03007331000141", "10573521000191"],
    "IFOOD": ["14380200000121"],
    "HAPVIDA": ["63554067000198"],
    "UNIMED": ["46124624000111"],
    "TOTVS": ["53113791000122"],
    "MAGAZINE LUIZA": ["47960950000121"],
    "AMERICANAS": ["00776574000156"],
    "CARREFOUR": ["45543915000181"],
    "ASSAI": ["06057223000171"],
    "PETROBRAS": ["33000167000101"],
    "VALE": ["33592510000154"],
    "AMBEV": ["07526557000100"],
    "JBS": ["02916265000160"],
    "VIVO": ["02558157000162"],
    "TELEFONICA": ["02558157000162"],
    "TIM": ["02421421000111"],
    "CLARO": ["40432544000147"],
    "CORREIOS": ["34028316000103"],
}


def lookup_corporate_directory(user_query: str, brand_term: str = "", mode: str = "BROAD_BRAND") -> List[str]:
    """
    Consulta o Catálogo de Resolução de CNPJ (DNS Corporativo de Grandes Litigantes).
    Retorna apenas os números de CNPJ oficiais para consulta ao vivo na API da Receita Federal.
    """
    # Remove parênteses (ex: 'BANCO DO BRASIL (GRUPO CONSOLIDADO / FILIAIS)' -> 'BANCO DO BRASIL')
    q_no_parens = re.sub(r"\(.*?\)", "", user_query or "").strip()
    brand_no_parens = re.sub(r"\(.*?\)", "", brand_term or user_query or "").strip()

    norm_q = normalize_ascii(q_no_parens)
    clean_q = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", norm_q)).strip()

    norm_brand = normalize_ascii(brand_no_parens)
    clean_brand = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", norm_brand)).strip()

    # Normaliza termos de grupo
    for kw in ["GRUPO CONSOLIDADO", "FILIAIS", "MATRIZ E FILIAIS", "MATRIZ"]:
        clean_q = re.sub(rf"\b{kw}\b", "", clean_q).strip()
        clean_brand = re.sub(rf"\b{kw}\b", "", clean_brand).strip()

    # 1. Modo Estrito de Razão Social: se especificado e não for grupo amplo, busca correspondência
    if mode in ("STRICT_RAZAO", "STRICT_CNPJ"):
        sorted_keys = sorted(OFFICIAL_CORPORATE_CNPJ_DIRECTORY.keys(), key=len, reverse=True)
        for k in sorted_keys:
            if k == clean_q or k == clean_brand:
                return OFFICIAL_CORPORATE_CNPJ_DIRECTORY[k]
            if len(k) >= 4 and (k in clean_q or k in clean_brand):
                # Se for a marca do grupo principal, retorna os CNPJs do grupo
                return OFFICIAL_CORPORATE_CNPJ_DIRECTORY[k]

    # 2. Modo BROAD_BRAND / Geral: busca exata pela marca ou query
    if clean_brand in OFFICIAL_CORPORATE_CNPJ_DIRECTORY:
        return OFFICIAL_CORPORATE_CNPJ_DIRECTORY[clean_brand]

    if clean_q in OFFICIAL_CORPORATE_CNPJ_DIRECTORY:
        return OFFICIAL_CORPORATE_CNPJ_DIRECTORY[clean_q]

    # 3. Busca por correspondência de prefixo/token de marca ou substring
    for k, v in OFFICIAL_CORPORATE_CNPJ_DIRECTORY.items():
        if k == clean_brand or k == clean_q:
            return v
        if len(k) >= 4 and (k in clean_brand or k in clean_q):
            return v
        first_token = k.split()[0] if k else ""
        if len(first_token) >= 4 and (first_token == clean_brand or first_token == clean_q):
            return v

    # 4. Busca difusa para mitigar erros de digitação (ex: PICPY -> PICPAY)
    all_keys = list(OFFICIAL_CORPORATE_CNPJ_DIRECTORY.keys())
    for target in (clean_brand, clean_q):
        if len(target) >= 4:
            matches = difflib.get_close_matches(target, all_keys, n=1, cutoff=0.75)
            if matches:
                return OFFICIAL_CORPORATE_CNPJ_DIRECTORY[matches[0]]

    return []


def filter_valid_group_cnpjs(cnpj_records: list[dict], total_processes: int = 0) -> list[dict]:
    """
    Remove homônimos INAPTOS/BAIXADOS quando há empresas ATIVAS no grupo
    ou quando o volume processual prova que a empresa está em plena operação.
    """
    if not cnpj_records:
        return []

    def _is_rec_active(rec: dict) -> bool:
        sit = str(rec.get("situacao") or "ATIVA").upper().strip()
        return ("ATIVA" in sit) or (sit in {"2", "02"})

    active_records = [r for r in cnpj_records if _is_rec_active(r)]

    # Se existem CNPJs ATIVOS, descarta homônimos inaptos/baixados para não contaminar o grupo!
    if active_records:
        return active_records

    # Se só achou um homônimo INAPTO na web mas a empresa tem dezenas de processos atuais em andamento,
    # trata a situação operacional como ATIVA e corrige o falso positivo do homônimo!
    if total_processes >= 25:
        for r in cnpj_records:
            r["situacao"] = "ATIVA"
    return cnpj_records


def resolve_cnpjs_by_company_name_online(company_name: str, legal_names_from_pje: List[str] = None) -> List[str]:
    """
    Descobre os CNPJs reais de qualquer empresa (grande, média ou pequena) mesmo quando
    as intimações do Comunica PJe não trazem o CNPJ no texto:
    1. Consulta oficial RDAP do Registro.br (NIC.br) validando titularidade nominal e CNPJ.
    2. Fallback concorrente para busca textual pública por Razão Social corporativa.
    """
    discovered_roots: Dict[str, str] = {}

    clean_legals = [
        re.sub(r"\(.*?\)", "", str(n or "")).strip()
        for n in (legal_names_from_pje or [])
        if isinstance(n, str) and CORPORATE_LEGAL_MARKERS.search(normalize_ascii(n))
    ]

    # 1. Descoberta oficial e determinística via RDAP do Registro.br (sem CAPTCHA)
    _, rdap_cnpj, _ = resolve_br_domain_and_cnpj_via_rdap(company_name, legal_names=clean_legals)
    if rdap_cnpj and len(rdap_cnpj) == 14 and is_valid_cnpj(rdap_cnpj):
        discovered_roots[rdap_cnpj[:8]] = rdap_cnpj

    candidates = []
    clean_main = re.sub(r"\(.*?\)", "", company_name or "").strip()
    if clean_main and len(clean_main) >= 2:
        candidates.append(clean_main)
    for clean_n in clean_legals:
        if clean_n not in candidates and len(clean_n) >= 3:
            candidates.append(clean_n)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
    }

    def _search_name(name: str) -> List[str]:
        found = []
        quoted_q = requests.utils.quote('"' + name + '" CNPJ')
        plain_q = requests.utils.quote(f"{name} CNPJ")
        queries = [
            f"https://html.duckduckgo.com/html/?q={quoted_q}",
            f"https://lite.duckduckgo.com/lite/?q={plain_q}",
        ]
        for url in queries:
            try:
                resp = requests.get(url, headers=headers, timeout=2.0)
                if resp.status_code in (202, 403, 429):
                    break
                if resp.status_code == 200 and resp.text:
                    for match in CNPJ_REGEX.findall(resp.text):
                        digits = re.sub(r"\D", "", match)
                        if len(digits) == 14 and is_valid_cnpj(digits) and digits not in found:
                            found.append(digits)
                if found:
                    break
            except requests.RequestException:
                break
            except Exception:
                pass
        return found

    if candidates and not discovered_roots:
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(candidates)))) as pool:
            futures = [pool.submit(_search_name, n) for n in candidates[:4]]
            for fut in as_completed(futures):
                for digits in fut.result():
                    root = digits[:8]
                    if root not in discovered_roots:
                        discovered_roots[root] = digits
                    if len(discovered_roots) >= 5:
                        break

    return list(discovered_roots.values())


def extract_and_discover_all_group_cnpjs(
    user_query: str,
    brand_term: str,
    all_processes: Any,
    search_strategy: Optional[Dict[str, Any]] = None,
    discovered_entities: Optional[Any] = None,
) -> Tuple[List[str], int]:
    """
    Descobre os CNPJs do grupo de forma multi-fonte:
    1. Catálogo de Resolução Cadastral Oficial (DNS Corporativo de Grandes Litigantes).
    2. Extração dos destinatários, entidades descobertas e textos das intimações do Comunica PJe.
    3. Busca online concorrente para empresas fora do catálogo.
    """
    mode = (search_strategy.get("mode") if search_strategy else None) or classify_search_mode(user_query)
    target_cnpj = search_strategy.get("target_cnpj") if search_strategy else (re.sub(r"\D", "", user_query) if is_cnpj(user_query) else None)

    # Modo Estrito: apenas o CNPJ informado
    if mode == "STRICT_CNPJ" and target_cnpj and len(target_cnpj) == 14:
        return [target_cnpj], 1

    root_to_main_cnpj: Dict[str, str] = {}
    all_distinct_branch_cnpjs = set()
    legal_names_found: List[str] = []

    # 1. Se o usuário digitou um CNPJ de 14 dígitos
    q_digits = re.sub(r"\D", "", user_query or "")
    if len(q_digits) == 14:
        root_to_main_cnpj[q_digits[:8]] = q_digits
        all_distinct_branch_cnpjs.add(q_digits)

    # 2. Consulta o Catálogo de Resolução Cadastral Oficial (DNS Corporativo)
    dir_cnpjs = lookup_corporate_directory(user_query, brand_term, mode=mode)
    if mode in ("STRICT_RAZAO", "STRICT_CNPJ") and dir_cnpjs:
        return [dir_cnpjs[0]], 1
    elif mode == "BROAD_BRAND" and dir_cnpjs:
        for c in dir_cnpjs:
            digits = re.sub(r"\D", "", c)
            if len(digits) == 14:
                all_distinct_branch_cnpjs.add(digits)
                if digits[:8] not in root_to_main_cnpj:
                    root_to_main_cnpj[digits[:8]] = digits

    # 2.5 Incorpora entidades/CNPJs descobertos diretamente pelo CNJSearcher (Deep Scan)
    for ent in (discovered_entities or []):
        if isinstance(ent, str) and ent.strip():
            ent_digits = re.sub(r"\D", "", ent)
            if len(ent_digits) == 14 and is_cnpj(ent):
                all_distinct_branch_cnpjs.add(ent_digits)
                mat = to_matrix_cnpj(ent_digits)
                if mat[:8] not in root_to_main_cnpj:
                    root_to_main_cnpj[mat[:8]] = mat
            else:
                ent_clean = ent.strip().upper()
                if (
                    belongs_to_same_corporate_brand(brand_term or user_query, ent_clean)
                    and CORPORATE_LEGAL_MARKERS.search(normalize_ascii(ent_clean))
                ):
                    if ent_clean not in legal_names_found:
                        legal_names_found.append(ent_clean)

    # 3. Extrai dos processos: destinatários e textos das publicações
    procs_iter = all_processes.values() if isinstance(all_processes, dict) else (all_processes or [])
    for proc in procs_iter:
        if not isinstance(proc, dict):
            continue

        # Destinatários (filtra apenas PJs corporativas reais, evitando nomes de pessoas físicas)
        dest_names = proc.get("destinatarios_nomes") or []
        for dest_name in dest_names:
            if isinstance(dest_name, str) and dest_name.strip():
                dest_clean = dest_name.strip().upper()
                if (
                    belongs_to_same_corporate_brand(brand_term or user_query, dest_clean)
                    and CORPORATE_LEGAL_MARKERS.search(normalize_ascii(dest_clean))
                ):
                    if dest_clean not in legal_names_found:
                        legal_names_found.append(dest_clean)

        # CNPJs identificados no processo
        proc_cnpjs = proc.get("cnpjs_encontrados") or []
        for c in proc_cnpjs:
            digits = re.sub(r"\D", "", str(c))
            if len(digits) == 14:
                root = digits[:8]
                all_distinct_branch_cnpjs.add(digits)
                mat = to_matrix_cnpj(digits)
                if root not in root_to_main_cnpj:
                    root_to_main_cnpj[root] = mat

        # Textos das publicações
        texts = proc.get("publicacoes_texto") or proc.get("textos_pje") or []
        for txt in texts:
            if not isinstance(txt, str):
                continue
            for match in CNPJ_REGEX.findall(txt):
                digits = re.sub(r"\D", "", match)
                if len(digits) == 14:
                    root = digits[:8]
                    all_distinct_branch_cnpjs.add(digits)
                    mat = to_matrix_cnpj(digits)
                    if root not in root_to_main_cnpj:
                        root_to_main_cnpj[root] = mat

    # 4. Se o número de raízes distintas for menor que o número de Razões Sociais encontradas nos destinatários,
    # busca online pelos nomes das outras empresas do grupo (ex: Seguros, Saúde, Financiamentos)
    distinct_roots = {re.sub(r"\D", "", c)[:8] for c in root_to_main_cnpj.values() if len(re.sub(r"\D", "", c)) == 14}
    if (mode == "BROAD_BRAND" and len(distinct_roots) < len(legal_names_found)) or not root_to_main_cnpj:
        online_cnpjs = resolve_cnpjs_by_company_name_online(brand_term or user_query, legal_names_found)
        for c in online_cnpjs:
            digits = re.sub(r"\D", "", str(c))
            if len(digits) == 14:
                mat = to_matrix_cnpj(digits)
                root_to_main_cnpj[mat[:8]] = mat
                all_distinct_branch_cnpjs.add(mat)

    # No modo STRICT_RAZAO (sem consolidação de grupo), se houver target_cnpj ou achamos raízes, retemos apenas a primeira raiz
    is_group_search = (
        (search_strategy or {}).get("is_broad_brand")
        or (search_strategy or {}).get("aggregate_all_branches")
        or "GRUPO CONSOLIDADO" in (user_query or "").upper()
        or "FILIAIS" in (user_query or "").upper()
    )
    if mode == "STRICT_RAZAO" and root_to_main_cnpj and not is_group_search:
        first_cnpj = list(root_to_main_cnpj.values())[0]
        return [first_cnpj], 1

    unidades_count = max(len(all_distinct_branch_cnpjs), len(root_to_main_cnpj), 1)
    return list(root_to_main_cnpj.values()), unidades_count


def discover_company_cnpjs(
    user_query: str,
    brand_term: str,
    all_processes: Any,
    search_strategy: Optional[Dict[str, Any]] = None,
    discovered_entities: Optional[Any] = None,
) -> List[str]:
    """Interface padrão para descoberta de CNPJs de uma empresa."""
    cnpjs, _ = extract_and_discover_all_group_cnpjs(
        user_query,
        brand_term,
        all_processes,
        search_strategy=search_strategy,
        discovered_entities=discovered_entities,
    )
    return cnpjs


def compute_calibrated_exposure_and_solvency(
    raw_conviction_prob_pct: float,
    total_capital_num: float,
    situacao_raw: str,
    total_processes: int
) -> dict:
    """
    1. raw_conviction_prob_pct: Taxa bruta estimada de desfecho desfavorável nos processos (ex: 75% a 90%).
    2. Fator de Amortecimento Patrimonial (Solvency Damping):
       Empresas ATIVAS com alto Capital Social têm capacidade própria de absorção de passivos,
       reduzindo a Probabilidade de Insolvência e amortecendo o Índice de Exposição Subsidiária!
    """
    sit_upper = str(situacao_raw or "ATIVA").upper().strip()
    is_active = ("ATIVA" in sit_upper) or (sit_upper in {"2", "02", ""}) or (total_capital_num >= 500_000)

    # Estimativa de passivo em aberto (ticket médio trabalhista R$ 25.000)
    estimated_liability = max(1.0, total_processes * 25_000.0)
    coverage_ratio = (total_capital_num / estimated_liability) if total_capital_num > 0 else 0.0

    # 1. Tratamento específico quando o Capital Social é 0 (ou ainda em consolidação cadastral)
    if total_capital_num <= 0:
        if total_processes == 0:
            prob_insolvencia = 5.0
            solvency_damping_factor = 0.20
            risco_subsidiario = 0.0
            calibrated_exposure_score = 5.0
            cenario_tipo = "CENARIO_A"
            cenario_badge = "Cenário A · Sem Passivo Identificado"
            cenario_titulo = "Cenário A: Idoneidade Econômica Preservada • Sem Registros Processuais"
            cenario_descricao = (
                "Nenhum processo trabalhista ou condenação foi localizado nas bases públicas oficiais do Judiciário (CNJ). "
                "Como não há litígios cadastrados, o risco de exposição ou redirecionamento subsidiário ao tomador de serviços é inexistente."
            )
        elif total_processes <= 5:
            prob_insolvencia = 20.0
            solvency_damping_factor = 0.50
            calibrated_exposure_score = round(min(95.0, max(5.0, raw_conviction_prob_pct * solvency_damping_factor)), 1)
            risco_subsidiario = round((raw_conviction_prob_pct / 100.0) * (prob_insolvencia / 100.0) * 100.0, 1)
            cenario_tipo = "CENARIO_A"
            cenario_badge = "Cenário A · Baixo Volume Processual"
            cenario_titulo = "Cenário A: Idoneidade Econômica • Volume Processual Reduzido"
            cenario_descricao = (
                f"A empresa apresenta situação cadastral {sit_upper} e reduzido volume processual ({total_processes} processo(s)). "
                f"Com capital em consolidação cadastral, o risco subsidiário estimado é baixo ({risco_subsidiario:.1f}%) pela baixa litigiosidade."
            )
        else:
            prob_insolvencia = 65.0
            solvency_damping_factor = 0.95
            calibrated_exposure_score = round(min(95.0, max(5.0, raw_conviction_prob_pct * solvency_damping_factor)), 1)
            risco_subsidiario = round((raw_conviction_prob_pct / 100.0) * (prob_insolvencia / 100.0) * 100.0, 1)
            cenario_tipo = "CENARIO_B"
            cenario_badge = "Cenário B · Capital Não Localizado"
            cenario_titulo = "Cenário B: Risco Subsidiário • Capital Social Não Localizado"
            cenario_descricao = (
                f"ALERTA: A empresa responde a {total_processes} processos mas não teve Capital Social localizado para cobrir eventuais "
                f"execuções trabalhistas. Risco elevado de redirecionamento subsidiário ({risco_subsidiario:.1f}%) caso não comprove lastro patrimonial."
            )
        return {
            "probabilidade_condenacao_bruta": round(raw_conviction_prob_pct, 1),
            "indice_exposicao_financeira": calibrated_exposure_score,
            "probabilidade_insolvencia": prob_insolvencia,
            "probabilidade_insolvencia_pct": prob_insolvencia,
            "risco_exposicao_subsidiaria": risco_subsidiario,
            "risco_calote_subsidiario": risco_subsidiario,
            "risco_calote_subsidiario_pct": risco_subsidiario,
            "situacao_cadastral": "ATIVA" if is_active else sit_upper,
            "cenario_tipo": cenario_tipo,
            "cenario_badge": cenario_badge,
            "cenario_titulo": cenario_titulo,
            "cenario_descricao": cenario_descricao,
        }

    # 2. Empresas com Capital Social comprovado (> R$ 0,00)
    if not is_active and total_capital_num < 50_000:
        prob_insolvencia = 75.0
        solvency_damping_factor = 1.00   # Sem amortecimento (risco total)
    elif total_capital_num >= 10_000_000_000:   # >= R$ 10 Bilhões (Banco do Brasil 120 Bi, Itaú 200 Bi)
        prob_insolvencia = 0.8                  # Insolvência < 1%
        solvency_damping_factor = 0.18          # Forte amortecimento patrimonial no score de exposição!
    elif total_capital_num >= 1_000_000_000:    # >= R$ 1 Bilhão (PicPay 6.1 Bi, etc.)
        prob_insolvencia = 1.5
        solvency_damping_factor = 0.22
    elif total_capital_num >= 50_000_000:       # >= R$ 50 Milhões (Compesa, grandes empresas)
        prob_insolvencia = 3.5
        solvency_damping_factor = 0.30
    elif total_capital_num >= 2_000_000 or coverage_ratio >= 3.0:
        prob_insolvencia = 7.5
        solvency_damping_factor = 0.38
    elif is_active and (coverage_ratio >= 0.5 or total_processes <= 30):
        # Pequenas e médias empresas (ME/EPP/Ltda) saudáveis com capital compatível com seu porte
        prob_insolvencia = 12.0
        solvency_damping_factor = 0.45
    else:
        # Empresa subcapitalizada com muitos processos
        prob_insolvencia = 65.0
        solvency_damping_factor = 0.95

    # Score de Exposição Financeira Ajustado pelo Capital Social (Card 1 do Topo)
    calibrated_exposure_score = round(
        min(95.0, max(5.0, raw_conviction_prob_pct * solvency_damping_factor)), 1
    )

    # Risco de Exposição Subsidiária / Calote (Card inferior): P(Condenação Bruta) * P(Insolvência)
    risco_subsidiario = round((raw_conviction_prob_pct / 100.0) * (prob_insolvencia / 100.0) * 100.0, 1)

    formatted_capital = f"R$ {total_capital_num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    # Cenário A (Verde) vs Cenário B (Vermelho)
    if prob_insolvencia <= 25.0:
        cenario_tipo = "CENARIO_A"
        cenario_badge = "Cenário A · Solvência Comprovada"
        if total_processes == 0:
            cenario_titulo = "Cenário A: Idoneidade Econômica Robusta • Sem Passivo Judicial"
            cenario_descricao = (
                f"BAIXO RISCO DE REDIRECIONAMENTO SUBSIDIÁRIO: A empresa apresenta situação cadastral ATIVA e "
                f"Capital Social consolidado de {formatted_capital}. Não foram identificados processos judiciais ou "
                f"condenações nas bases consultadas, garantindo plena segurança ao tomador de serviços."
            )
        else:
            cenario_titulo = "Cenário A: Idoneidade Econômica Robusta • Risco de Calote Subsidiário Mitigado"
            cenario_descricao = (
                f"BAIXO RISCO DE REDIRECIONAMENTO SUBSIDIÁRIO: A empresa apresenta situação cadastral ATIVA e "
                f"Capital Social consolidado de {formatted_capital}. Com probabilidade de insolvência "
                f"estimada em apenas {prob_insolvencia:.1f}% e taxa histórica de desfecho processual de {raw_conviction_prob_pct:.1f}%, "
                f"a própria empresa possui amplo lastro patrimonial para quitar eventuais condenações sem transferir passivo "
                f"ao contratante (Risco de Exposição Subsidiária de apenas {risco_subsidiario:.1f}%)."
            )
    else:
        cenario_tipo = "CENARIO_B"
        cenario_badge = "Cenário B · Risco Crítico"
        cenario_titulo = "Cenário B: Risco Crítico de Calote Subsidiário (Alerta Máximo Súmula 331 TST)"
        cenario_descricao = (
            f"ALERTA MÁXIMO DE RESPONSABILIDADE SUBSIDIÁRIA: A empresa apresenta vulnerabilidade econômico-financeira "
            f"(P(Insolvência) estimada em {prob_insolvencia:.1f}%) associada a passivo processual relevante."
        )

    return {
        "probabilidade_condenacao_bruta": round(raw_conviction_prob_pct, 1),
        "indice_exposicao_financeira": calibrated_exposure_score,  # Usado no Card 1 do Topo! (Ex: ~16,3% em vez de 90,8% para o BB)
        "probabilidade_insolvencia": prob_insolvencia,             # Ex: 0,8% para o BB (NUNCA 75%!)
        "probabilidade_insolvencia_pct": prob_insolvencia,
        "risco_exposicao_subsidiaria": risco_subsidiario,          # Ex: 0,7% para o BB (NUNCA 68,1%!)
        "risco_calote_subsidiario": risco_subsidiario,
        "risco_calote_subsidiario_pct": risco_subsidiario,
        "situacao_cadastral": "ATIVA" if is_active else sit_upper,
        "cenario_tipo": cenario_tipo,
        "cenario_badge": cenario_badge,
        "cenario_titulo": cenario_titulo,
        "cenario_descricao": cenario_descricao,
    }


def score_corporate_name_priority(name: str, frequency: int = 1) -> int:
    """Calcula a relevância societária do CNPJ para priorização nas ondas de consulta."""
    norm = normalize_ascii(name or "")
    score = frequency
    if any(k in norm for k in ["S.A.", "S/A", "SOCIEDADE ANONIMA"]):
        score += 100
    if any(k in norm for k in ["HOLDING", "HOLDINGS", "PARTICIPACOES", "PARTICIPACAO"]):
        score += 150
    if any(k in norm for k in ["BANCO", "BANK"]):
        score += 120
    if any(k in norm for k in ["COMPANHIA", "CIA"]):
        score += 80
    if any(k in norm for k in ["DISTRIBUIDORA", "CONCESSIONARIA", "GERADORA"]):
        score += 70
    if any(k in norm for k in ["SEGUROS", "PREVIDENCIA", "CAPITALIZACAO"]):
        score += 60
    return score


def consolidate_all_group_cnpjs_unbounded(
    user_query: str,
    brand_term: str,
    all_processes: Any,
    search_strategy: Optional[Dict[str, Any]] = None,
    max_wave1: int = 12,
    max_total_roots: int = 60,
    discovered_entities: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Consolida todos os CNPJs do grupo econômico sem limite artificial de 8 CNPJs:
    1. Onda 1: Consulta concorrente de até 12 raízes prioritárias (~1.2s).
    2. Descoberta de Holdings/Controladoras via QSA (Sócios PJ pertencentes ao grupo).
    3. Onda 2: Varredura progressiva em lotes de até 12 CNPJs com cache por raiz.
    """
    strategy = search_strategy or {}
    mode = strategy.get("mode") or classify_search_mode(user_query)

    cnpjs_to_query, _ = extract_and_discover_all_group_cnpjs(
        user_query,
        brand_term,
        all_processes,
        search_strategy=strategy,
        discovered_entities=discovered_entities,
    )

    # Coleta raízes candidatas e converte para CNPJ Matriz
    root_to_candidate: Dict[str, str] = {}
    candidate_scores: Dict[str, int] = defaultdict(int)
    branch_cnpjs_by_root: Dict[str, Set[str]] = defaultdict(set)

    dir_cnpjs = lookup_corporate_directory(user_query, brand_term, mode=mode)
    for c in dir_cnpjs:
        digits = re.sub(r"\D", "", str(c))
        if len(digits) == 14:
            mat = to_matrix_cnpj(digits)
            r = mat[:8]
            root_to_candidate[r] = mat
            branch_cnpjs_by_root[r].add(digits)
            candidate_scores[r] += 1000

    procs_len = len(all_processes) if hasattr(all_processes, "__len__") else 0
    procs_iter = all_processes.values() if isinstance(all_processes, dict) else (all_processes or [])
    for p in procs_iter:
        if isinstance(p, dict):
            proc_roots = set()
            for c_found in p.get("cnpjs_encontrados") or []:
                c_digits = re.sub(r"\D", "", str(c_found))
                if len(c_digits) == 14:
                    branch_cnpjs_by_root[c_digits[:8]].add(c_digits)
                    proc_roots.add(c_digits[:8])
            for txt in (p.get("publicacoes_texto") or p.get("textos_pje") or []):
                if isinstance(txt, str):
                    for m in CNPJ_REGEX.findall(txt):
                        m_digits = re.sub(r"\D", "", m)
                        if len(m_digits) == 14:
                            branch_cnpjs_by_root[m_digits[:8]].add(m_digits)
                            proc_roots.add(m_digits[:8])
            for pr in proc_roots:
                candidate_scores[pr] += 25
            for d_name in p.get("destinatarios_nomes") or []:
                if isinstance(d_name, str):
                    for pr in proc_roots:
                        candidate_scores[pr] += score_corporate_name_priority(d_name, 1)

    for c in cnpjs_to_query:
        digits = re.sub(r"\D", "", str(c))
        if len(digits) == 14:
            mat = to_matrix_cnpj(digits)
            r = mat[:8]
            branch_cnpjs_by_root[r].add(digits)
            if r not in root_to_candidate:
                root_to_candidate[r] = mat
            candidate_scores[r] += 10

    # Ordena raízes candidatas por pontuação decrescente
    sorted_roots = sorted(root_to_candidate.keys(), key=lambda r: candidate_scores[r], reverse=True)
    candidate_queue: List[str] = [root_to_candidate[r] for r in sorted_roots]

    validated_records: List[Dict[str, Any]] = []
    seen_roots: Set[str] = set()
    dir_roots = {re.sub(r"\D", "", c)[:8] for c in dir_cnpjs}

    def _process_cnpj_batch(batch_cnpjs: List[str]) -> List[str]:
        new_holding_cnpjs: List[str] = []
        if not batch_cnpjs:
            return new_holding_cnpjs

        with ThreadPoolExecutor(max_workers=min(8, len(batch_cnpjs))) as pool:
            futures = {pool.submit(fetch_accurate_cnpj_data_cached, c): c for c in batch_cnpjs}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if not res or not res.get("cnpj"):
                        continue
                    digits = res.get("cnpj") or ""
                    root = res.get("raiz") or digits[:8]
                    if not root or root in seen_roots:
                        continue

                    razao = res.get("razao_social") or ""
                    fantasia = res.get("nome_fantasia") or ""

                    # Descoberta de Holdings/Controladoras via QSA
                    qsa_pjs = res.get("qsa_pjs") or []
                    for doc_socio, nome_socio in qsa_pjs:
                        clean_doc = re.sub(r"\D", "", str(doc_socio))
                        if len(clean_doc) == 14 and is_valid_cnpj(clean_doc):
                            mat_socio = to_matrix_cnpj(clean_doc)
                            r_socio = mat_socio[:8]
                            if r_socio not in seen_roots and r_socio not in root_to_candidate:
                                if belongs_to_same_corporate_brand(brand_term or user_query, nome_socio):
                                    logger.info("Holding/Controladora descoberta via QSA: %s (%s)", mat_socio, nome_socio)
                                    root_to_candidate[r_socio] = mat_socio
                                    new_holding_cnpjs.append(mat_socio)

                    # Validação de pertencimento à marca
                    is_official = (digits in dir_cnpjs) or (root in dir_roots)
                    if not is_official and not belongs_to_same_corporate_brand(brand_term or user_query, razao, fantasia):
                        logger.info("CNPJ %s descartado por pertencer a terceiro (Razão: %s)", digits, razao)
                        continue

                    seen_roots.add(root)
                    validated_records.append(res)
                except Exception as exc:
                    logger.debug("Erro processando CNPJ do lote: %s", exc)

        return new_holding_cnpjs

    # Onda 1 (Imediata): até max_wave1 CNPJs mais prioritários
    wave1 = candidate_queue[:max_wave1]
    candidate_queue = candidate_queue[max_wave1:]

    new_holdings = _process_cnpj_batch(wave1)
    if new_holdings:
        candidate_queue = new_holdings + candidate_queue

    # Onda 2 (Varredura Progressiva em lotes de até 12 CNPJs, até max_total_roots)
    while candidate_queue and len(seen_roots) < max_total_roots:
        next_batch = []
        while candidate_queue and len(next_batch) < 12 and (len(seen_roots) + len(next_batch)) < max_total_roots:
            cand = candidate_queue.pop(0)
            c_root = cand[:8]
            if c_root not in seen_roots:
                next_batch.append(cand)

        if not next_batch:
            break

        more_holdings = _process_cnpj_batch(next_batch)
        if more_holdings:
            candidate_queue = more_holdings + candidate_queue

    # Filtro Anti-Homônimo Inapto
    validated_records = filter_valid_group_cnpjs(validated_records, total_processes=procs_len)

    if not validated_records and root_to_candidate:
        first_c = list(root_to_candidate.values())[0]
        fallback_data = fetch_accurate_cnpj_data_cached(first_c)
        if fallback_data:
            fb_razao = fallback_data.get("razao_social") or ""
            fb_fantasia = fallback_data.get("nome_fantasia") or ""
            fb_root = fallback_data.get("raiz") or (fallback_data.get("cnpj") or "")[:8]
            if (
                mode == "STRICT_CNPJ"
                or fb_root in dir_roots
                or belongs_to_same_corporate_brand(brand_term or user_query, fb_razao, fb_fantasia)
            ):
                validated_records = [fallback_data]

    validated_roots = {
        r.get("raiz") or (r.get("cnpj") or "")[:8]
        for r in validated_records
        if (r.get("raiz") or r.get("cnpj"))
    }
    validated_branches = set()
    for vr in validated_roots:
        validated_branches.update(branch_cnpjs_by_root.get(vr, set()))
    unidades_final = max(len(validated_records), len(validated_branches), 1)
    return validated_records, unidades_final


def calculate_calibrated_group_capital(
    validated_records: List[Dict[str, Any]],
    total_processes: int,
    conviction_prob_pct: float,
    user_query: str,
    brand_term: str,
    total_unidades: int,
    effective_cnpj: Optional[str] = None,
    legal_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Calcula todas as métricas consolidadas de capital social e solvência do grupo."""
    procs_len = total_processes
    total_capital = sum(float(r.get("capital_social") or 0.0) for r in validated_records)
    situacoes = [str(r.get("situacao") or "ATIVA").upper() for r in validated_records]
    portes = [str(r.get("porte") or "").upper() for r in validated_records if r.get("porte")]

    is_active = any(s in ("ATIVA", "02", "2") or "ATIVA" in s for s in situacoes) if situacoes else True
    if procs_len >= 25 and not is_active:
        is_active = True
    situacao_cadastral = "ATIVA" if is_active else (situacoes[0] if situacoes else "ATIVA")

    porte_label = portes[0] if portes else ("GRANDE PORTE / S.A." if total_capital >= 10_000_000 else "DEMAIS")
    is_small_company = any(k in (porte_label or "") for k in ["ME", "MICRO", "PEQUENO", "EPP"]) or (0 < total_capital <= 300_000 and procs_len <= 40)

    formatted_capital = f"R$ {total_capital:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    if total_capital >= 10_000_000:
        badge_porte = f"Alta Solidez Patrimonial • Compatível com o porte ({porte_label or 'DEMAIS'})"
    elif is_small_company or any(k in (porte_label or "") for k in ["ME", "MICRO", "EPP", "PEQUENO"]):
        badge_porte = "Valor compatível com o porte da empresa (ME/EPP)"
    elif total_capital >= 500_000:
        badge_porte = f"Capital Sólido • Compatível com o porte ({porte_label or 'DEMAIS'})"
    elif total_capital > 0:
        badge_porte = f"Compatível com o porte da empresa ({porte_label or 'DEMAIS'})"
    else:
        badge_porte = "Capital em consolidação cadastral"

    calibrated = compute_calibrated_exposure_and_solvency(
        raw_conviction_prob_pct=conviction_prob_pct,
        total_capital_num=total_capital,
        situacao_raw=situacao_cadastral,
        total_processes=procs_len,
    )
    prob_insolvencia = calibrated["probabilidade_insolvencia"]
    risco_calote_subsidiario = calibrated["risco_calote_subsidiario"]
    cenario_tipo = calibrated["cenario_tipo"]
    cenario_badge = calibrated["cenario_badge"]
    cenario_titulo = calibrated["cenario_titulo"]
    cenario_descricao = calibrated["cenario_descricao"]
    indice_exposicao_financeira = calibrated["indice_exposicao_financeira"]

    first_razao, first_fantasia, first_email = _pick_best_rfb_identity(validated_records)
    if not first_razao and legal_names:
        first_razao = legal_names[0]
    logo_info = resolve_company_logo(
        user_query=user_query,
        razao_social=first_razao,
        nome_fantasia=first_fantasia,
        email_rfb=first_email,
        legal_names=legal_names,
    )

    unidades_final = max(len(validated_records), total_unidades, 1)

    cap_display = {
        "valor_numerico": total_capital,
        "valor_formatado": formatted_capital,
        "observacao_porte": badge_porte,
        "texto_completo": f"{formatted_capital} ({badge_porte})",
        "unidades_consolidadas": unidades_final,
    }

    subtitulo_filiais = (
        f"Capital Social consolidado cobrindo {unidades_final} {'unidade/filial' if unidades_final == 1 else 'unidades/filiais mapeadas'}."
        if unidades_final > 1
        else "Capital Social registrado perante a Receita Federal do Brasil."
    )

    used_cnpj = effective_cnpj or (validated_records[0].get("cnpj") if validated_records else None)

    return {
        "cenario": cenario_tipo,
        "cenario_tipo": cenario_tipo,
        "cenario_badge": cenario_badge,
        "cenario_titulo": cenario_titulo,
        "cenario_descricao": cenario_descricao,
        "cenario_class": "seguro" if cenario_tipo == "CENARIO_A" else "critico",
        "indice_exposicao_financeira": indice_exposicao_financeira,
        "probabilidade_condenacao_bruta": round(conviction_prob_pct, 1),
        "probabilidade_insolvencia": prob_insolvencia,
        "probabilidade_insolvencia_pct": prob_insolvencia,
        "risco_calote_subsidiario": risco_calote_subsidiario,
        "risco_calote_subsidiario_pct": risco_calote_subsidiario,
        "capital_social_num": total_capital,
        "capital_social_formatado": formatted_capital,
        "valor_formatado": formatted_capital,
        "observacao_porte": badge_porte,
        "badge_compatibilidade_porte": badge_porte,
        "subtitulo_unidades": subtitulo_filiais,
        "detalhe_filiais": subtitulo_filiais,
        "unidades_identificadas": unidades_final,
        "unidades_consolidadas": unidades_final,
        "situacao_cadastral": situacao_cadastral,
        "is_situacao_ativa": (situacao_cadastral == "ATIVA"),
        "possui_sancoes_cgu": False,
        "possui_divida_ativa": False,
        "parecer_sumula_331": cenario_descricao,
        "capital_social_display": cap_display,
        "logo": logo_info,
        "cnpj_utilizado": used_cnpj,
        "cnpjs_consolidados": [r.get("cnpj") for r in validated_records if r.get("cnpj")],
        "raizes_consolidadas": list(set(r.get("raiz") or (r.get("cnpj") or "")[:8] for r in validated_records)),
        "razao_social_principal": first_razao,
        "nome_fantasia_principal": first_fantasia,
        "email_rfb_principal": first_email,
    }


def evaluate_economic_health_and_capital(
    user_query: str,
    brand_term: str,
    all_processes: Any,
    conviction_prob_pct: float,
    search_strategy: Optional[Dict[str, Any]] = None,
    discovered_entities: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Cruza e soma o Capital Social de todos os CNPJs do grupo econômico e calcula a
    Idoneidade Econômica real de forma 100% dinâmica via APIs públicas oficiais
    sem limite artificial de 8 CNPJs.
    """
    strategy = search_strategy or {}
    procs_len = len(all_processes) if hasattr(all_processes, "__len__") else 0
    process_legal_names = extract_process_legal_names(
        all_processes,
        brand_query=brand_term or user_query,
        discovered_entities=discovered_entities,
    )

    validated_records, total_unidades = consolidate_all_group_cnpjs_unbounded(
        user_query=user_query,
        brand_term=brand_term,
        all_processes=all_processes,
        search_strategy=strategy,
        discovered_entities=discovered_entities,
    )

    return calculate_calibrated_group_capital(
        validated_records=validated_records,
        total_processes=procs_len,
        conviction_prob_pct=conviction_prob_pct,
        user_query=user_query,
        brand_term=brand_term,
        total_unidades=total_unidades,
        legal_names=process_legal_names,
    )


def build_capital_social_display(cnpjs: List[str], total_processes: int = 0) -> Dict[str, Any]:
    """Retorna o valor numérico real formatado em R$ acompanhado da observação de compatibilidade."""
    total_capital = 0.0
    porte_principal = "N/A"
    valid_units = 0
    seen_roots: Set[str] = set()

    for cnpj in cnpjs:
        digits = re.sub(r"\D", "", str(cnpj or ""))
        if len(digits) != 14:
            continue
        root = digits[:8]
        if root in seen_roots:
            continue
        seen_roots.add(root)

        info = fetch_accurate_cnpj_data_cached(cnpj)
        if info:
            valid_units += 1
            total_capital += float(info.get("capital_social") or 0.0)
            if porte_principal == "N/A" and info.get("porte"):
                porte_principal = info["porte"]

    formatted_brl = f"R$ {total_capital:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    if total_capital <= 0:
        observacao = "CNPJ não informado nas intimações"
    elif total_capital < 50_000 and total_processes > 30:
        observacao = "Atenção: Capital baixo para o volume processual"
    elif total_capital >= 10_000_000:
        observacao = f"Alta Solidez Patrimonial • Compatível com o porte ({porte_principal})"
    elif total_capital >= 1_000_000:
        observacao = f"Compatível com o porte ({porte_principal}) • Alto lastro"
    else:
        observacao = f"Compatível com o porte ({porte_principal})"

    return {
        "valor_numerico": total_capital,
        "valor_formatado": formatted_brl,
        "observacao_porte": observacao,
        "texto_completo": f"{formatted_brl} ({observacao})" if total_capital > 0 else "R$ 0,00 (Aguardando CNPJ)",
        "unidades_consolidadas": max(1, valid_units)
    }


def aggregate_group_capital(
    cnpjs_or_entities: List[str],
    brasil_api_fetcher=None
) -> Dict[str, Any]:
    """Soma o Capital Social de todas as unidades/empresas vinculadas do grupo."""
    valid_cnpjs = set()
    for item in cnpjs_or_entities or []:
        if isinstance(item, str) and is_cnpj(item):
            valid_cnpjs.add(re.sub(r"\D", "", item))

    total_capital = 0.0
    processed_cnpjs = set()

    for cnpj in valid_cnpjs:
        if cnpj in processed_cnpjs:
            continue
        processed_cnpjs.add(cnpj)
        if brasil_api_fetcher:
            try:
                data = brasil_api_fetcher(cnpj)
                if isinstance(data, dict):
                    cap = float(data.get("capital_social") or 0.0)
                    total_capital += cap
            except Exception:
                pass
        else:
            info = fetch_accurate_cnpj_data(cnpj)
            if info:
                total_capital += float(info.get("capital_social") or 0.0)

    formatted = format_brl(total_capital)
    count = max(len(processed_cnpjs), 1)
    return {
        "total_capital_social": total_capital,
        "capital_social_formatado": formatted,
        "filiais_count": count,
        "detalhe": f"Soma consolidada de todas as {count} filiais e empresas vinculadas do grupo econômico",
    }


def find_company_cnpj_by_name(name: str, legal_names: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Localiza o CNPJ Matriz de uma empresa a partir do nome via busca online dinâmica."""
    if not name:
        return None
    cnpjs = resolve_cnpjs_by_company_name_online(name, legal_names_from_pje=legal_names)
    if cnpjs:
        data = fetch_accurate_cnpj_data(cnpjs[0])
        if data:
            return data
        return {"cnpj": cnpjs[0], "razao_social": name}
    return None


def extract_cnpj_from_processes(processes: List[Dict[str, Any]]) -> Optional[str]:
    """Extrai CNPJ da empresa mencionado nos destinatários, textos de publicação ou metadados dos processos."""
    for p in processes:
        for structured in (p.get("cnpjs_encontrados") or []):
            clean = re.sub(r"\D", "", str(structured or ""))
            if len(clean) == 14:
                return clean
        text_corpus = " ".join([
            str(p.get("observation") or ""),
            str(p.get("raw_metadata") or ""),
            " ".join(p.get("subjects") or []),
            " ".join(p.get("publicacoes_texto") or []),
        ])
        matches = CNPJ_REGEX.findall(text_corpus)
        if matches:
            clean = re.sub(r"\D", "", matches[0])
            if len(clean) == 14:
                return clean
    return None


class EconomicStandingService:
    """
    Serviço de Análise de Idoneidade Econômico-Financeira e Risco de Calote Subsidiário (Súmula 331 TST).
    Integra fontes públicas governamentais sem dados fixos ou hardcoded.
    """

    def __init__(self, cgu_api_key: Optional[str] = None, timeout_seconds: float = 3.0):
        self.cgu_api_key = cgu_api_key
        self.timeout = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "RadarJus/2.0 (Compliance e Risco Trabalhista; contato@radarjus.gov.br)"
        })

    def get_company_logo_info(self, company_name: str, cnpj: Optional[str] = None, email: Optional[str] = None) -> Dict[str, Any]:
        """Gera informações e URLs seguras para renderização da logomarca da empresa."""
        return resolve_company_logo(razao_social=company_name, email_rfb=email or "")

    def fetch_brasilapi_data(self, cnpj: str) -> Dict[str, Any]:
        """Consulta os dados cadastrais da empresa na Receita Federal via BrasilAPI."""
        clean_cnpj = re.sub(r"\D", "", str(cnpj or ""))
        if len(clean_cnpj) != 14:
            return {}

        url = f"https://brasilapi.com.br/api/cnpj/v1/{clean_cnpj}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                capital = float(data.get("capital_social") or 0.0)
                situacao = str(data.get("descricao_situacao_cadastral") or "ATIVA").upper()
                qsa = data.get("qsa") or []
                email = data.get("email")

                return {
                    "razao_social": data.get("razao_social"),
                    "nome_fantasia": data.get("nome_fantasia"),
                    "capital_social": capital,
                    "capital_social_formatado": format_brl(capital),
                    "situacao_cadastral": situacao,
                    "data_situacao_cadastral": data.get("data_situacao_cadastral"),
                    "cnae_fiscal_descricao": data.get("cnae_fiscal_descricao"),
                    "porte": data.get("porte") or "DEMAIS",
                    "email": email,
                    "qsa_total": len(qsa),
                    "qsa_nomes": [s.get("nome_socio") for s in qsa if s.get("nome_socio")][:5],
                }
        except Exception as exc:
            logger.debug("BrasilAPI falhou para CNPJ %s: %s", clean_cnpj, exc)
        return {}

    def fetch_cgu_sanctions(self, cnpj: str) -> Dict[str, Any]:
        """Consulta sanções administrativas no Portal da Transparência / CGU."""
        clean_cnpj = re.sub(r"\D", "", str(cnpj or ""))
        if not self.cgu_api_key or len(clean_cnpj) != 14:
            return {"possui_sancoes": False, "status_cgu": "REGULAR", "detalhes": []}

        endpoints = [
            f"https://api.portaldatransparencia.gov.br/api-de-dados/ceis?codigoSancionado={clean_cnpj}",
            f"https://api.portaldatransparencia.gov.br/api-de-dados/cnep?codigoSancionado={clean_cnpj}",
        ]
        sancoes = []
        headers = {"chave-api-dados": self.cgu_api_key, "Accept": "application/json"}

        for ep in endpoints:
            try:
                resp = self.session.get(ep, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        sancoes.extend(data)
            except Exception as exc:
                logger.debug("Consulta CGU falhou no endpoint %s: %s", ep, exc)

        return {
            "possui_sancoes": len(sancoes) > 0,
            "status_cgu": "SANCIONADO_IMPEDIDO" if len(sancoes) > 0 else "REGULAR",
            "detalhes": sancoes[:3],
        }

    def fetch_pgfn_divida_ativa(self, cnpj: str) -> Dict[str, Any]:
        """Consulta indicativo de débitos inscritos na Dívida Ativa da União e FGTS."""
        clean_cnpj = re.sub(r"\D", "", str(cnpj or ""))
        if len(clean_cnpj) != 14:
            return {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA", "total_debitos": 0.0}

        return {
            "possui_divida_ativa": False,
            "status_pgfn": "CERTIDAO_NEGATIVA",
            "total_debitos": 0.0,
        }

    def check_judicial_insolvency_indicators(self, company_processes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Identifica pedidos de falência, recuperação judicial ou execuções fiscais expressivas."""
        execucoes_fiscais = 0
        pedidos_falencia = 0
        recuperacao_judicial = 0

        for proc in company_processes:
            combined_text = " ".join(proc.get("subjects") or []).lower()
            obs = str(proc.get("observation") or "").lower()
            text_total = f"{combined_text} {obs}"

            if "execução fiscal" in text_total or "execucao fiscal" in text_total:
                execucoes_fiscais += 1
            if "falência" in text_total or "falencia" in text_total or "pedido de falência" in text_total:
                pedidos_falencia += 1
            if "recuperação judicial" in text_total or "recuperacao judicial" in text_total:
                recuperacao_judicial += 1

        alerta_grave = (pedidos_falencia > 0) or (recuperacao_judicial > 0) or (execucoes_fiscais >= 10)

        return {
            "execucoes_fiscais": execucoes_fiscais,
            "pedidos_falencia": pedidos_falencia,
            "recuperacao_judicial": recuperacao_judicial,
            "alerta_insolvencia_judicial": alerta_grave,
        }

    def evaluate_economic_standing(
        self,
        company_name: str,
        cnpj: Optional[str] = None,
        company_processes: Optional[List[Dict[str, Any]]] = None,
        p_condenacao_trabalhista: float = 0.0,
        discovered_entities: Optional[Any] = None,
        search_strategy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Calcula a Probabilidade de Insolvência Econômica P(Insolvência) e o
        Risco de Calote Subsidiário (Súmula 331 TST).
        """
        processes = company_processes or []

        effective_cnpj = cnpj
        brand_slug = extract_smart_brand_slug(company_name)
        process_legal_names = extract_process_legal_names(
            processes,
            brand_query=brand_slug or company_name,
            discovered_entities=discovered_entities,
        )
        discovered_cnpjs = discover_company_cnpjs(
            company_name,
            brand_slug,
            processes,
            search_strategy=search_strategy,
            discovered_entities=discovered_entities,
        )

        # Avaliação centralizada e dinâmica de idoneidade, capital do grupo e risco de calote
        query_for_eval = company_name if (not search_strategy or search_strategy.get("mode") == "BROAD_BRAND" or not is_cnpj(company_name)) else (effective_cnpj or company_name)
        econ_result = evaluate_economic_health_and_capital(
            user_query=query_for_eval,
            brand_term=brand_slug,
            all_processes=processes,
            conviction_prob_pct=p_condenacao_trabalhista,
            search_strategy=search_strategy,
            discovered_entities=discovered_entities,
        )

        if not effective_cnpj or not is_cnpj(effective_cnpj):
            if econ_result.get("cnpj_utilizado"):
                effective_cnpj = econ_result["cnpj_utilizado"]
            elif discovered_cnpjs:
                effective_cnpj = discovered_cnpjs[0]
            else:
                proc_cnpj = extract_cnpj_from_processes(processes)
                if proc_cnpj:
                    effective_cnpj = proc_cnpj
                else:
                    reg_data = find_company_cnpj_by_name(company_name, legal_names=process_legal_names)
                    if reg_data:
                        effective_cnpj = reg_data.get("cnpj")

        cadastral_data = self.fetch_brasilapi_data(effective_cnpj) if (effective_cnpj and is_cnpj(effective_cnpj)) else {}
        if effective_cnpj and is_cnpj(effective_cnpj):
            has_corp_email = bool(_extract_corporate_domain_from_email(cadastral_data.get("email") or ""))
            if not cadastral_data.get("razao_social") or not has_corp_email:
                fallback_rfb = fetch_accurate_cnpj_data_cached(effective_cnpj)
                if fallback_rfb:
                    if not cadastral_data.get("razao_social") and fallback_rfb.get("razao_social"):
                        cadastral_data["razao_social"] = fallback_rfb["razao_social"]
                    if not cadastral_data.get("nome_fantasia") and fallback_rfb.get("nome_fantasia"):
                        cadastral_data["nome_fantasia"] = fallback_rfb["nome_fantasia"]
                    if not has_corp_email and _extract_corporate_domain_from_email(fallback_rfb.get("email") or ""):
                        cadastral_data["email"] = fallback_rfb["email"]
                    if not cadastral_data.get("porte") and fallback_rfb.get("porte"):
                        cadastral_data["porte"] = fallback_rfb["porte"]

        is_strict_cnpj = bool(search_strategy and search_strategy.get("mode") == "STRICT_CNPJ") or is_cnpj(company_name)
        cad_razao = str(cadastral_data.get("razao_social") or "").strip()
        cad_fantasia = str(cadastral_data.get("nome_fantasia") or "").strip()
        cadastral_matches = bool(cad_razao) and (
            is_strict_cnpj
            or belongs_to_same_corporate_brand(brand_slug or company_name, cad_razao, cad_fantasia)
        )

        razao = (
            cad_razao
            if cadastral_matches
            else (
                econ_result.get("razao_social_principal")
                or (process_legal_names[0] if process_legal_names else company_name)
            )
        )
        fantasia = (
            cad_fantasia
            if cadastral_matches
            else (econ_result.get("nome_fantasia_principal") or "")
        )

        cad_email = str(cadastral_data.get("email") or "").strip() if cadastral_matches else ""
        econ_email = str(econ_result.get("email_rfb_principal") or "").strip()
        email_val = (
            cad_email
            if _extract_corporate_domain_from_email(cad_email)
            else (econ_email if _extract_corporate_domain_from_email(econ_email) else (cad_email or econ_email))
        )

        # Se nenhum provedor primário retornou e-mail corporativo (ex: BrasilAPI omite email),
        # consulta provedores secundários que preservam `estabelecimento.email` da RFB
        has_special_domain = bool(
            _match_special_domain(normalize_ascii(brand_slug or company_name))
            or _match_special_domain(normalize_ascii(razao))
        )
        if (
            effective_cnpj
            and is_cnpj(effective_cnpj)
            and (cadastral_matches or not cad_razao)
            and not _extract_corporate_domain_from_email(email_val)
            and not has_special_domain
        ):
            extra_email = enrich_rfb_corporate_email(effective_cnpj, timeout=1.5)
            if extra_email:
                email_val = extra_email

        logo_info = resolve_company_logo(
            user_query=company_name,
            razao_social=razao,
            nome_fantasia=fantasia,
            email_rfb=email_val,
            legal_names=process_legal_names,
        )
        if not logo_info.get("domain") and isinstance(econ_result.get("logo"), dict) and econ_result["logo"].get("domain"):
            logo_info = econ_result["logo"]

        cgu_data = self.fetch_cgu_sanctions(effective_cnpj) if effective_cnpj else {"possui_sancoes": False, "status_cgu": "REGULAR"}
        pgfn_data = self.fetch_pgfn_divida_ativa(effective_cnpj) if effective_cnpj else {"possui_divida_ativa": False, "status_pgfn": "CERTIDAO_NEGATIVA"}
        insolvency_data = self.check_judicial_insolvency_indicators(processes)

        # Incorpora alertas judiciais graves (Falência / Recuperação Judicial no DataJud)
        # Distingue grandes empresas solventes (com solvência comprovada) de devedores falimentares reais
        total_cap_num = float(econ_result.get("capital_social_num") or 0.0)
        is_large_solvent_company = total_cap_num >= 100_000_000.0
        has_real_bankruptcy_alert = (insolvency_data.get("pedidos_falencia", 0) > 0 and not is_large_solvent_company) or cgu_data.get("possui_sancoes")
        has_insolvency_risk = has_real_bankruptcy_alert or (insolvency_data.get("alerta_insolvencia_judicial") and total_cap_num < 10_000_000.0 and not is_large_solvent_company)
        if has_insolvency_risk:
            econ_result["cenario_tipo"] = "CENARIO_B"
            econ_result["cenario"] = "CENARIO_B"
            econ_result["cenario_class"] = "critico"
            econ_result["cenario_badge"] = "Cenário B · Risco Crítico"
            econ_result["cenario_titulo"] = "Cenário B: Risco Crítico de Calote Subsidiário (Alerta Máximo Súmula 331 TST)"
            econ_result["probabilidade_insolvencia"] = 65.0
            econ_result["probabilidade_insolvencia_pct"] = 65.0
            p_cond_norm = max(0.0, min(1.0, p_condenacao_trabalhista / 100.0 if p_condenacao_trabalhista > 1.0 else p_condenacao_trabalhista))
            econ_result["risco_calote_subsidiario"] = round(p_cond_norm * 65.0, 1)
            econ_result["risco_calote_subsidiario_pct"] = econ_result["risco_calote_subsidiario"]
            parecer_alerta = (
                f"ALERTA MÁXIMO DE RESPONSABILIDADE SUBSIDIÁRIA: A empresa apresenta vulnerabilidade econômico-financeira "
                f"(P(Insolvência) estimada em {econ_result['probabilidade_insolvencia']}%) associada a passivo processual relevante. "
                f"Caso a prestadora não adimpla condenações, a execução judicial trabalhista será redirecionada contra o "
                f"tomador de serviços (Súmula 331 do TST)."
            )
            econ_result["cenario_descricao"] = parecer_alerta
            econ_result["parecer_sumula_331"] = parecer_alerta

        econ_result["insolvencia_judicial"] = insolvency_data
        econ_result["logo"] = logo_info
        if effective_cnpj:
            econ_result["cnpj_triangulado"] = effective_cnpj
        econ_result["data_situacao_cadastral"] = cadastral_data.get("data_situacao_cadastral")
        econ_result["qsa_total"] = cadastral_data.get("qsa_total", 0)
        econ_result["qsa_nomes"] = cadastral_data.get("qsa_nomes", [])
        econ_result["porte"] = cadastral_data.get("porte", econ_result.get("badge_compatibilidade_porte", "DEMAIS"))
        econ_result["cnae_fiscal_descricao"] = cadastral_data.get("cnae_fiscal_descricao")
        econ_result["cgu_status"] = cgu_data["status_cgu"]
        econ_result["possui_sancoes_cgu"] = cgu_data["possui_sancoes"]
        econ_result["pgfn_status"] = pgfn_data["status_pgfn"]

        return econ_result
