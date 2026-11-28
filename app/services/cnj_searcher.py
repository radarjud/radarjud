# Copyright (c) 2026 Equipe RadarJus - Todos os direitos reservados (Lei nº 9.609/1998).
# Proibida a reprodução, distribuição ou uso comercial sem autorização expressa dos autores.

import gc
import html
import os
import re
import hashlib
import math
import time
import datetime
from decimal import Decimal, InvalidOperation
import unicodedata
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Set, Tuple
import requests
from requests.adapters import HTTPAdapter

from app.utils.sanitizer import (
    STOPWORDS_SOCIETARIAS,
    extract_brand_search_term,
    is_cnpj,
    CORPORATE_SUFFIX_REGEX,
    classify_search_mode,
    resolve_search_strategy,
    normalize_search_term_generic,
    extract_corporate_core_name,
)
from app.utils.http_client import request_gov_br_with_fallback

logger = logging.getLogger(__name__)

# Calibragem de coleta para Render Free (512 MB RAM, pico < 380 MB, Gunicorn --timeout 120)
SYNC_SCAN_TIME_BUDGET_SECONDS = 50.0
SYNC_SCAN_MAX_PAGES = 15
SYNC_SCAN_MAX_RECORDS = 1500
MAX_SNIPPET_CHARS = 1200
MAX_SNIPPETS_PER_PROCESS = 2
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_CNPJ_IN_TEXT_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


def _strip_html_to_text(raw_text: Any, max_len: int = MAX_SNIPPET_CHARS) -> str:
    """Remove tags HTML brutas da publicação do PJe e compacta o resumo em texto plano."""
    if not raw_text:
        return ""
    cleaned = html.unescape(str(raw_text))
    if "<" in cleaned:
        cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if max_len and len(cleaned) > max_len:
        return cleaned[:max_len]
    return cleaned

TJ_MAP = {
    1: "tjac", 2: "tjal", 3: "tjap", 4: "tjam", 5: "tjba", 6: "tjce",
    7: "tjdft", 8: "tjes", 9: "tjgo", 10: "tjma", 11: "tjmt", 12: "tjms",
    13: "tjmg", 14: "tjpa", 15: "tjpb", 16: "tjpr", 17: "tjpe", 18: "tjpi",
    19: "tjrj", 20: "tjrn", 21: "tjrs", 22: "tjro", 23: "tjrr", 24: "tjsc",
    25: "tjse", 26: "tjsp", 27: "tjto"
}


def parse_iso_date(raw: Any, text_fallback: str = "") -> Optional[datetime.date]:
    """
    Extrai e valida datas de distribuição judicial (ISO, YYYYMMDD ou BR DD/MM/YYYY).
    Valida rigorosamente o calendário (meses de 1 a 12, dias de 1 a 28..31, anos bissextos).
    Datas inválidas (ex: 32/13/2024, 2024-02-31) retornam None de forma segura.
    Se raw estiver ausente ou inválido, busca padrões textuais em text_fallback.
    """
    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw

    def _try_parse_date_str(s: str) -> Optional[datetime.date]:
        if not s:
            return None
        s = str(s).strip()
        # Se contiver timestamp com 'T' ou espaço
        if "T" in s:
            s = s.split("T")[0]
        elif " " in s:
            s = s.split(" ")[0]
        s = s.strip()

        # 1. Padrão ISO YYYY-MM-DD
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            try:
                dt = datetime.datetime.strptime(s, "%Y-%m-%d").date()
                if 1900 <= dt.year <= 2100:
                    return dt
            except ValueError:
                return None

        # 2. Padrão Brasileiro DD/MM/YYYY
        if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            try:
                dt = datetime.datetime.strptime(s, "%d/%m/%Y").date()
                if 1900 <= dt.year <= 2100:
                    return dt
            except ValueError:
                return None

        # 3. Padrão Compacto YYYYMMDD
        if re.match(r"^\d{8}$", s):
            try:
                dt = datetime.datetime.strptime(s, "%Y%m%d").date()
                if 1900 <= dt.year <= 2100:
                    return dt
            except ValueError:
                return None

        return None

    if raw is not None:
        parsed = _try_parse_date_str(str(raw))
        if parsed:
            return parsed

    # Fallback em texto de publicações
    if text_fallback:
        patterns = [
            r"(?:distribu[ií]d[oa]|ajuizad[oa]|ajuizamento|distribui[cç][aã]o)\s*(?:no\s+dia|em|:)?\s*(\d{2}/\d{2}/\d{4})",
            r"(?:data\s+de\s+ajuizamento|data\s+da\s+distribui[cç][aã]o)\s*(?:em|:)?\s*(\d{2}/\d{2}/\d{4})",
            r"(?:termo\s+de\s+distribui[cç][aã]o)\s*(?:no\s+dia|em|:)?\s*(\d{2}/\d{2}/\d{4})",
        ]
        for pat in patterns:
            m = re.search(pat, text_fallback, re.IGNORECASE)
            if m:
                parsed = _try_parse_date_str(m.group(1))
                if parsed:
                    return parsed

    return None


def parse_currency_value(raw: Any, text_fallback: str = "") -> Optional[Decimal]:
    """
    Normaliza valores da causa para Decimal(15, 2).
    Suporta:
      - Números numéricos (int, float, Decimal)
      - Strings com notação BR ('R$ 45.000,00', '1.250.300,75')
      - Strings com notação internacional ('45000.00', '1250300.75')
      - Fallback em regex textual em publicações ('dá-se à causa o valor de R$ 50.000,00')
    Valores nulos, negativos ou zerados retornam None.
    """
    def _to_decimal(val_str: str) -> Optional[Decimal]:
        if not val_str:
            return None
        val_str = str(val_str).strip()
        if val_str.startswith("-") or " - " in val_str or "(" in val_str:
            return None
        cleaned = re.sub(r"[^\d,\.]", "", val_str).strip()
        if not cleaned:
            return None

        # Se houver ponto e vírgula (ex: 1.250.300,75)
        if "." in cleaned and "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Apenas vírgula (ex: 45000,00 ou 1,25)
            cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            # Ex: 45.000 (milhar) ou 45000.00 (decimal)
            parts = cleaned.split(".")
            if len(parts) > 2:
                # Múltiplos pontos de milhar: 1.250.300
                cleaned = "".join(parts)
            elif len(parts) == 2 and len(parts[1]) == 3:
                try:
                    if int(parts[0]) > 0:
                        cleaned = parts[0] + parts[1]
                except ValueError:
                    pass

        try:
            dec = Decimal(cleaned).quantize(Decimal("0.01"))
            if dec > Decimal("0.00"):
                return dec
        except (InvalidOperation, ValueError, TypeError):
            pass
        return None

    if raw is not None:
        if isinstance(raw, (int, float, Decimal)):
            if raw <= 0:
                return None
            try:
                dec = Decimal(str(raw)).quantize(Decimal("0.01"))
                if dec > Decimal("0.00"):
                    return dec
            except (InvalidOperation, ValueError, TypeError):
                pass
        else:
            raw_s = str(raw).strip()
            if raw_s.startswith("-") or " - " in raw_s or "(" in raw_s:
                return None
            dec = _to_decimal(raw_s)
            if dec:
                return dec

    # Fallback em texto de publicações
    if text_fallback:
        patterns = [
            r"(?:valor\s+da\s+causa|d[aá]-se\s+[aà]\s+causa\s+o\s+valor\s+de|atribu[ií]-se\s+[aà]\s+causa\s+o\s+valor\s+de)\s*(?:em|:)?\s*(?:r\$)?\s*([\d\.,]+)",
            r"(?:condena[cç][aã]o\s+no\s+valor\s+de|arbitrad[oa]\s+em)\s*(?:r\$)?\s*([\d\.,]+)",
        ]
        for pat in patterns:
            m = re.search(pat, text_fallback, re.IGNORECASE)
            if m:
                dec = _to_decimal(m.group(1))
                if dec:
                    return dec

    return None


def normalize_court_code(raw: Any, cnj: Optional[str] = None) -> Optional[str]:
    """
    Normaliza a sigla do tribunal para o formato canônico oficial.
    Exemplos:
      - 'TRT-2', 'TRT 02', 'trt2', 'TRT02', 'Tribunal Regional do Trabalho da 2ª Região' -> 'TRT2'
      - 'TRT-15', 'trt15' -> 'TRT15'
      - 'TST', 'Tribunal Superior do Trabalho' -> 'TST'
      - 'TJSP', 'Tribunal de Justiça de São Paulo' -> 'TJSP'
      - Processo CNJ NNNNNNN-DD.AAAA.J.TR.OOOO -> inferência canônica precisa pelo ramo J.TR
    """
    raw_str = str(raw or "").strip()

    if raw_str and raw_str.upper() not in {"N/A", "NONE", "NULL", ""}:
        clean_upper = unicodedata.normalize("NFKD", raw_str)
        clean_upper = "".join(c for c in clean_upper if not unicodedata.combining(c)).upper()

        # 1. TRT 1 a 24 (dois dígitos testados antes de um dígito para evitar TRT1 casando em TRT15)
        m_trt = re.search(r"\bTRT[\s-]*0?(2[0-4]|1[0-9]|[1-9])\b", clean_upper)
        if m_trt:
            return f"TRT{int(m_trt.group(1))}"

        m_trt_ext = re.search(r"TRIBUNAL\s+REGIONAL\s+DO\s+TRABALHO.*?0?(2[0-4]|1[0-9]|[1-9])(?:\s*A|\s*O|\s*ª|\s*º)?\s+REGIAO", clean_upper)
        if not m_trt_ext:
            m_trt_ext = re.search(r"TRIBUNAL\s+REGIONAL\s+DO\s+TRABALHO.*?0?(2[0-4]|1[0-9]|[1-9])\b", clean_upper)
        if m_trt_ext:
            return f"TRT{int(m_trt_ext.group(1))}"

        # 2. TST
        if re.search(r"\bTST\b|TRIBUNAL\s+SUPERIOR\s+DO\s+TRABALHO", clean_upper):
            return "TST"

        # 3. TRF 1 a 6
        m_trf = re.search(r"\bTRF[\s-]*0?([1-6])\b", clean_upper)
        if m_trf:
            return f"TRF{int(m_trf.group(1))}"

        # 4. Tribunais de Justiça Estaduais (TJSP, TJRJ, etc.)
        m_tj = re.search(r"\bTJ[\s-]*([A-Z]{2})\b", clean_upper)
        if m_tj:
            return f"TJ{m_tj.group(1).upper()}"

        # 5. Tribunais Superiores
        if re.search(r"\bSTJ\b", clean_upper):
            return "STJ"
        if re.search(r"\bSTF\b", clean_upper):
            return "STF"

    # Fallback por desmembramento do número CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO
    if cnj:
        m_cnj = re.search(r"\.\d{4}\.(?P<J>\d)\.(?P<TR>\d{2})\.", str(cnj))
        if m_cnj:
            j_ramo = m_cnj.group("J")
            tr_code = m_cnj.group("TR")
            tr_num = int(tr_code)

            if j_ramo == "5":  # Justiça do Trabalho
                if tr_num == 0:
                    return "TST"
                if 1 <= tr_num <= 24:
                    return f"TRT{tr_num}"
            elif j_ramo == "8":  # Justiça Estadual
                if tr_num in TJ_MAP:
                    return TJ_MAP[tr_num].upper()
            elif j_ramo == "4":  # Justiça Federal
                if 1 <= tr_num <= 6:
                    return f"TRF{tr_num}"

    if raw_str and raw_str.upper() not in {"N/A", "NONE", "NULL", ""}:
        return raw_str.strip().upper()[:50]

    return None

# Termos genéricos que NÃO devem mascarar os motivos substantivos do processo
GENERIC_SUBJECT_TERMS = {
    "reclamacao trabalhista", "direito do trabalho", "obrigacoes", "responsabilidade civil",
    "direito civil", "direito do consumidor", "contratos", "outros", "processo judicial",
    "rescisao do contrato de trabalho", "verbas rescisorias", "outros motivos trabalhistas",
    "reclamacao trabalhista (geral)", "reclamatoria trabalhista", "processo do trabalho",
    "atos processuais", "direito individual do trabalho", "direito processual civil e do trabalho",
    "obrigação / reclamação em apuração inicial", "obrigacao / reclamacao em apuracao inicial",
}

# Mapa léxico determinístico de altíssima precisão para extração em microssegundos (0.01ms)
DETAILED_CAUSE_PATTERNS = [
    # Bancário / Cível / Consumidor
    (7780, "Fraude Bancária / Golpe do PIX / Contestação de Transação", re.compile(r"golpe\s+do\s+pix|fraude|transa[cç][aã]o\s+n[aã]o\s+reconhecida|bloqueio\s+indevido|bloqueio\s+de\s+conta|med\s+pix|invas[aã]o\s+de\s+conta|engenharia\s+social|fortuito\s+interno|s[uú]mula\s*479")),
    (7779, "Negativação Indevida (SPC / Serasa)", re.compile(r"negativa[cç][aã]o\s+indevida|inscri[cç][aã]o\s+indevida|serasa|spc|cadastro\s+de\s+inadimplentes|apontamento\s+desabonador")),
    (9587, "Tarifas Bancárias e Cobrança Indevida", re.compile(r"tarifa\s+banc[aá]ria|cesta\s+de\s+servi[cç]os|pacote\s+de\s+servi[cç]os|cobran[cç]a\s+indevida|falha\s+na\s+presta[cç][aã]o|repeti[cç][aã]o\s+do\s+ind[eé]bito|taxa\s+de\s+manuten[cç][aã]o")),
    (4960, "Revisão de Contrato e Juros Abusivos", re.compile(r"revis[aã]o\s+de\s+contrato|juros\s+abusivos|anatocismo|taxa\s+de\s+juros|tabela\s+price|cl[aá]usula\s+abusiva|capitaliza[cç][aã]o\s+de\s+juros")),
    (997, "Busca e Apreensão em Alienação Fiduciária", re.compile(r"busca\s+e\s+apreens[aã]o|aliena[cç][aã]o\s+fiduci[aá]ria|decreto[\s-]lei\s*911|apreens[aã]o\s+de\s+ve[ií]culo|purga[cç][aã]o\s+da\s+mora")),
    (4970, "Empréstimo Consignado e Reserva de Margem (RMC)", re.compile(r"empr[eé]stimo\s+consignado|reserva\s+de\s+margem|rmc|rcc|desconto\s+em\s+folha|cart[aã]o\s+consignado|margem\s+consign[aá]vel")),
    (7771, "Cartão de Crédito e Anuidade", re.compile(r"cart[aã]o\s+de\s+cr[eé]dito|anuidade|limite\s+do\s+cart[aã]o|fatura\s+do\s+cart[aã]o")),
    (4980, "Superendividamento e Repactuação de Dívidas", re.compile(r"superendividamento|lei\s*14\.?181|repactua[cç][aã]o\s+de\s+d[ií]vidas|m[ií]nimo\s+existencial|renegocia[cç][aã]o")),
    (10433, "Indenização por Danos Morais (Direito do Consumidor)", re.compile(r"dano\s+moral\s+ao\s+consumidor|cdc|c[oó]digo\s+de\s+defesa\s+do\s+consumidor|rela[cç][aã]o\s+de\s+consumo")),

    # Trabalhista Bancário e Geral
    (224, "Direitos da Categoria Bancária (7ª e 8ª Horas / Cargo de Confiança)", re.compile(r"banc[aá]ri[ao]|cargo\s+de\s+confian[cç]a|7[aª]\s+hora|8[aª]\s+hora|s[eé]tima\s+e\s+oitava|enquadramento\s+banc[aá]rio|gratifica[cç][aã]o\s+de\s+fun[cç][aã]o|jornada\s+de\s+seis\s+horas|art\.\s*224")),
    (2550, "Equiparação Salarial e Desvio de Função", re.compile(r"equipara[cç][aã]o\s+salarial|desvio\s+de\s+fun[cç][aã]o|paradigma|diferen[cç]as?\s+salariais?|ac[uú]mulo\s+de\s+fun[cç][aã]o")),
    (2551, "Participação nos Lucros e Resultados (PLR) e Bônus", re.compile(r"\bplr\b|participa[cç][aã]o\s+nos\s+lucros|b[oô]nus|pr[eé]mio\s+produ[cç][aã]o|programa\s+de\s+remunera[cç][aã]o")),
    (2552, "Cobrança de Metas e Estorno de Comissões", re.compile(r"estorno\s+de\s+comiss[oõ]es|cobran[cç]a\s+de\s+metas|metas\s+abusivas|diferen[cç]as?\s+de\s+comiss|comiss[oõ]es\s+por\s+vendas")),
    (1694, "Horas Extras e Intervalo Intrajornada", re.compile(r"horas?\s+extras?|sobrejornada|intrajornada|banco\s+de\s+horas|art\.\s*71|controle\s+de\s+ponto|cart[aã]o\s+de\s+ponto|jornada\s+de\s+trabalho|dsr|sobreaviso")),
    (2546, "Inadimplemento de Verbas Rescisórias e Multa 40%", re.compile(r"verbas\s+rescis[oó]rias|multa\s+do\s+art\.\s*477|multa\s+de\s+40%|aviso\s+pr[eé]vio\s+indenizado|saldo\s+de\s+sal[aá]rio|13[oº]\s+sal[aá]rio|f[eé]rias\s+proporcionais|trct|rescis[aã]o\s+indireta")),
    (2029, "Irregularidade / Ausência de Depósitos de FGTS", re.compile(r"dep[oó]sitos?\s+do\s+fgts|extrato\s+anal[ií]tico\s+do\s+fgts|diferen[cç]as\s+de\s+fgts|fundo\s+de\s+garantia|saque\s+do\s+fgts|guias\s+de\s+fgts")),
    (1855, "Assédio Moral e Danos Morais Trabalhistas", re.compile(r"ass[eé]dio\s+moral|dano\s+moral|danos\s+morais|indeniza[cç][aã]o\s+por\s+dano|humilha[cç][aã]o|tratamento\s+degradante")),
    (1656, "Adicional de Insalubridade (NR-15)", re.compile(r"insalubridade|agente\s+insalubre|nr[\s-]*15|laudo\s+pericial\s+insalub")),
    (1681, "Adicional de Periculosidade (NR-16)", re.compile(r"periculosidade|inflam[aá]veis|energia\s+el[eé]trica|nr[\s-]*16|risco\s+acentuado")),
    (1904, "Responsabilidade Subsidiária / Terceirização (Súmula 331 TST)", re.compile(r"responsabilidade\s+subsidi[aá]ria|s[uú]mula\s*331|tomadora?\s+de\s+servi[cç]os|culpa\s+in\s+vigilando|terceiriza[cç][aã]o|prestadora\s+de\s+servi[cç]os")),
    (1998, "Reconhecimento de Vínculo Empregatício (Pejotização)", re.compile(r"v[ií]nculo\s+de\s+emprego|v[ií]nculo\s+empregat[ií]cio|anota[cç][aã]o\s+da\s+ctps|subordina[cç][aã]o\s+jur[ií]dica|pejotiza[cç][aã]o")),
    (8801, "Acidente de Trabalho / Doença Ocupacional", re.compile(r"acidente\s+de\s+trabalho|doen[cç]a\s+ocupacional|ler\s*\/?\s*dort|nexo\s+causal|estabilidade\s+acident[aá]ria")),
    (1682, "Adicional Noturno", re.compile(r"adicional\s+noturno|jornada\s+noturna|hora\s+noturna")),
    (2555, "Reintegração e Estabilidade Provisória", re.compile(r"reintegra[cç][aã]o|estabilidade\s+provis[oó]ria|estabilidade\s+gestante|cipeir[ao]|acidente\s+de\s+trabalho")),
    (10000, "Execução e Cumprimento de Sentença Trabalhista", re.compile(r"sisbajud|penhora|bloqueio|c[aá]lculos\s+de\s+liquida[cç][aã]o|cumprimento\s+de\s+senten[cç]a|execu[cç][aã]o\s+trabalhista|execu[cç][aã]o\s+de\s+t[ií]tulo")),
]

_CAUSE_FILTERS = [
    (
        code,
        label,
        p,
        tuple({w for part in p.pattern.split("|") for w in re.findall(r"[a-z0-9]{3,}", part)}),
    )
    for code, label, p in DETAILED_CAUSE_PATTERNS
]

_CAUSE_CACHE: Dict[str, list] = {}


def extract_real_cause_fast(
    existing_assuntos: list,
    raw_texts: list,
    classes_pje: Optional[list] = None
) -> list:
    """
    Descobre a causa real do processo em microssegundos:
    1. Analisa termos de alta precisão no teor das publicações (0.01ms).
    2. Utiliza assuntos específicos do DataJud se disponíveis.
    3. Se o texto for conciso e não houver assunto específico, usa a Classe Processual PJe estruturada.
    4. Fallback genérico apenas em último caso.
    """
    full_text = " ".join(str(t) for t in (raw_texts or []) if t)
    matched_causes = []

    if full_text:
        full_text_lower = full_text.lower()
        if full_text_lower in _CAUSE_CACHE:
            matched_causes = [dict(c) for c in _CAUSE_CACHE[full_text_lower]]
        else:
            for code, label, pattern, kws in _CAUSE_FILTERS:
                if (not kws or any(kw in full_text_lower for kw in kws)) and pattern.search(full_text_lower):
                    matched_causes.append({"codigo": code, "nome": label})
                    if len(matched_causes) >= 3:
                        break
            if len(_CAUSE_CACHE) < 2048:
                _CAUSE_CACHE[full_text_lower] = [dict(c) for c in matched_causes]

    # Se achou causas específicas no texto da publicação/sentença, usa elas prioritariamente!
    if matched_causes:
        return matched_causes[:3]

    # Se não achou no texto mas tem assuntos no DataJud, filtra os genéricos se houver mais de um
    if existing_assuntos:
        specific = []
        for a in existing_assuntos:
            nome = a.get("nome", "") if isinstance(a, dict) else str(a)
            if nome.lower() not in GENERIC_SUBJECT_TERMS:
                specific.append(a if isinstance(a, dict) else {"codigo": None, "nome": nome})
        if specific:
            return specific[:2]
        res = []
        for a in existing_assuntos[:2]:
            res.append(a if isinstance(a, dict) else {"codigo": None, "nome": str(a)})
        return res

    # 3. Fallback inteligente na classe processual estruturada do Comunica PJe
    if classes_pje:
        classes_str = " ".join(str(c) for c in classes_pje if c).lower()
        if re.search(r"ordin[aá]rio|atord", classes_str):
            return [{"codigo": 985, "nome": "Ação Trabalhista (Rito Ordinário)"}]
        if re.search(r"sumar[ií]ssimo|atsum", classes_str):
            return [{"codigo": 1125, "nome": "Ação Trabalhista (Rito Sumaríssimo)"}]
        if re.search(r"execu[cç]|cumprimento|cumsen", classes_str):
            return [{"codigo": 10000, "nome": "Execução e Cumprimento de Sentença Trabalhista"}]
        if re.search(r"juizado", classes_str):
            return [{"codigo": 436, "nome": "Juizado Especial Cível / Consumidor"}]
        if re.search(r"procedimento\s+comum|c[ií]vel", classes_str):
            return [{"codigo": 7, "nome": "Procedimento Comum Cível / Consumidor"}]
        if re.search(r"monit[oó]ria", classes_str):
            return [{"codigo": 40, "nome": "Ação Monitória Bancária"}]
        if re.search(r"busca\s+e\s+apreens", classes_str):
            return [{"codigo": 997, "nome": "Busca e Apreensão em Alienação Fiduciária"}]

        # Se houver alguma classe com nome legível
        for c in classes_pje:
            if isinstance(c, str) and len(c.strip()) >= 5:
                return [{"codigo": None, "nome": c.strip()}]

    return [{"codigo": 7644, "nome": "Obrigação / Reclamação em Apuração Inicial"}]


def classify_process_outcome(
    movimentos_codigos: set,
    assuntos_nomes: list,
    texto_publicacoes: str,
    classe_nome: str = ""
) -> str:
    """
    Classifica TODOS os processos em: CONDENACAO, ACORDO, IMPROCEDENTE ou EM_ABERTO.
    Reconhece fases de execução, liquidação e cumprimento de sentença como CONDENACAO.
    """
    txt = (texto_publicacoes or "").lower()
    classe_lower = (classe_nome or "").lower()
    assuntos_concat = " ".join(assuntos_nomes or []).lower()

    # 1. Códigos oficiais TPU do DataJud
    if {219, 221, 11385} & movimentos_codigos:
        return "CONDENACAO"
    if {466, 467} & movimentos_codigos:
        return "ACORDO"
    if 220 in movimentos_codigos:
        return "IMPROCEDENTE"

    # 2. Se a própria Classe Processual ou Assunto já é Execução / Cumprimento de Sentença
    if any(k in classe_lower or k in assuntos_concat for k in [
        "cumprimento de sentença", "cumprimento de sentenca", "execução trabalhista",
        "execucao trabalhista", "execução de título judicial", "execucao de titulo judicial",
        "execução de título extrajudicial", "execucao de titulo extrajudicial",
        "liquidação", "liquidacao"
    ]):
        return "CONDENACAO"

    # 3. Sinais textuais de Acordo no Comunica PJe
    if re.search(
        r"homologo\s+o\s+acordo|homologa[cç][aã]o\s+de\s+acordo|concilia[cç][aã]o\s+homologada|"
        r"cumprimento\s+do\s+acordo|descumprimento\s+do\s+acordo|transa[cç][aã]o\s+homologada",
        txt
    ):
        return "ACORDO"

    # 4. Sinais textuais de Improcedência / Extinção sem ônus
    if re.search(
        r"julgo\s+improcedente|totalmente\s+improcedente|improcedentes\s+os\s+pedidos|"
        r"extinto\s+sem\s+resolu[cç][aã]o\s+do\s+m[eé]rito|arquivem-se\s+os\s+autos\s+por\s+aus[eê]ncia\s+do\s+autor",
        txt
    ):
        return "IMPROCEDENTE"

    # 5. Sinais textuais diretos OU indiretos de Condenação (Sentença, Liquidação, Execução, Penhora, Recurso com preparo)
    if re.search(
        r"procedente\s+em\s+parte|julgo\s+procedente|parcialmente\s+procedente|"
        r"condeno\s+a\s+reclamada|condenar\s+a\s+r[eé]|condena[cç][aã]o|"
        r"cumprimento\s+de\s+senten[cç]a|in[ií]cio\s+da\s+execu[cç][aã]o|execu[cç][aã]o\s+definitiva|"
        r"c[aá]lculos\s+de\s+liquida[cç][aã]o|homologo\s+os\s+c[aá]lculos|apresentar\s+c[aá]lculos|"
        r"penhora|sisbajud|bacenjud|renajud|bloqueio\s+judicial|garantia\s+do\s+ju[ií]zo|"
        r"embargos\s+[aà]\s+execu[cç][aã]o|expe[cç]a-se\s+alvar[aá]|intime-se\s+a\s+executada|"
        r"pagar\s+o\s+valor\s+da\s+condena[cç][aã]o|dep[oó]sito\s+recursal|honor[aá]rios\s+sucumbenciais\s+pela\s+reclamada",
        txt
    ):
        return "CONDENACAO"

    return "EM_ABERTO"


def _page_signature(comm_ids: Set[str]) -> str:
    """Assinatura curta e determinística do conjunto de comunicações de uma página."""
    joined = "\n".join(sorted(comm_ids))
    return hashlib.sha1(joined.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()


def _total_pages_from_payload(payload: Any, itens_por_pagina: int) -> Optional[int]:
    """
    Lê o total de páginas que a própria API informa: totalPages explícito ou
    derivado do campo "count" (total de comunicações) do Comunica PJe.
    """
    if not isinstance(payload, dict):
        return None
    explicit = payload.get("totalPages") or payload.get("total_pages")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    count = payload.get("count")
    if isinstance(count, int) and count > 0 and itens_por_pagina > 0:
        return math.ceil(count / itens_por_pagina)
    return None


VALID_LABOR_COURTS: Set[str] = {"TST"} | {f"TRT{i}" for i in range(1, 25)}

UF_TO_TRTS: Dict[str, List[str]] = {
    "SP": ["TRT2", "TRT15"],
    "RJ": ["TRT1"],
    "MG": ["TRT3"],
    "RS": ["TRT4"],
    "BA": ["TRT5"],
    "PE": ["TRT6"],
    "CE": ["TRT7"],
    "PA": ["TRT8"],
    "AP": ["TRT8"],
    "PR": ["TRT9"],
    "DF": ["TRT10"],
    "TO": ["TRT10"],
    "AM": ["TRT11"],
    "RR": ["TRT11"],
    "SC": ["TRT12"],
    "PB": ["TRT13"],
    "RO": ["TRT14"],
    "AC": ["TRT14"],
    "MA": ["TRT16"],
    "ES": ["TRT17"],
    "GO": ["TRT18"],
    "AL": ["TRT19"],
    "SE": ["TRT20"],
    "RN": ["TRT21"],
    "PI": ["TRT22"],
    "MT": ["TRT23"],
    "MS": ["TRT24"],
}

LABOR_COURTS_DEFAULT_ORDER: List[str] = [
    "TST", "TRT2", "TRT1", "TRT3", "TRT15", "TRT4", "TRT9", "TRT5",
    "TRT6", "TRT12", "TRT18", "TRT7", "TRT10", "TRT8", "TRT17", "TRT11",
    "TRT13", "TRT19", "TRT20", "TRT21", "TRT23", "TRT24", "TRT16", "TRT22", "TRT14",
]


def build_labor_courts_queue(company_uf: Optional[str] = None) -> List[str]:
    """
    Monta a fila ordenada dos 25 tribunais da Justiça do Trabalho (TST + TRT1..TRT24).
    Começa sempre pelo TST, prioriza o(s) TRT(s) da UF da sede da empresa e completa
    com os demais TRTs por volume nacional sem repetições.
    """
    ordered: List[str] = ["TST"]
    uf_clean = str(company_uf or "").strip().upper()
    for trt in UF_TO_TRTS.get(uf_clean, []):
        if trt not in ordered:
            ordered.append(trt)
    for court in LABOR_COURTS_DEFAULT_ORDER:
        if court not in ordered:
            ordered.append(court)
    return ordered


def is_labor_process(numero_processo: Any = None, tribunal: Any = None) -> bool:
    """
    Valida rigorosamente se um processo pertence exclusivamente à Justiça do Trabalho
    (TST ou TRT1..TRT24, segmento CNJ J=5 e TR=00..24), bloqueando TJs e TRFs.
    """
    digits = re.sub(r"\D", "", str(numero_processo or ""))
    if len(digits) == 20:
        if digits[13] != "5":
            return False
        tr_num = int(digits[14:16])
        if tr_num > 24:
            return False

    trib_str = str(tribunal or "").strip()
    if trib_str and trib_str.upper() not in {"N/A", "NONE", "NULL"}:
        norm = normalize_court_code(trib_str, str(numero_processo or "") or None)
        return norm in VALID_LABOR_COURTS

    return len(digits) == 20


def is_tst_process(
    numero_processo: Any = None,
    tribunal: Any = None,
    grau: Any = None,
    is_tst_recurso: bool = False,
    primary_subject: Any = None,
    observation: Any = None,
    classes_pje: Any = None,
    texts: Any = None,
) -> bool:
    """
    Identifica se um processo pertence ao TST (Tribunal Superior do Trabalho)
    pela flag recursal, sigla do tribunal, grau, segmento '.5.00.' do número CNJ
    ou recursos dirigidos à Instância Máxima (Recurso de Revista / AIRR / TST).
    """
    if is_tst_recurso:
        return True
    if str(tribunal or "").strip().upper() == "TST":
        return True
    if "TST" in str(grau or "").upper():
        return True
    cnj_str = str(numero_processo or "").strip()
    if cnj_str:
        if ".5.00." in cnj_str:
            return True
        digits = re.sub(r"\D", "", cnj_str)
        if len(digits) == 20 and digits[13:16] == "500":
            return True

    corpus = []
    if primary_subject:
        corpus.append(str(primary_subject))
    if observation:
        corpus.append(str(observation))
    if isinstance(classes_pje, list):
        corpus.extend(str(c) for c in classes_pje if c)
    elif classes_pje:
        corpus.append(str(classes_pje))
    if isinstance(texts, list):
        corpus.extend(str(t) for t in texts[:5] if t)
    elif texts:
        corpus.append(str(texts))

    search_text = " ".join(corpus).lower()
    if search_text:
        tst_patterns = [
            r"\brecurso\s+de\s+revista\b",
            r"\bairr\b",
            r"\bagravo\s+de\s+instrumento\s+em\s+recurso\s+de\s+revista\b",
            r"\btribunal\s+superior\s+do\s+trabalho\b",
            r"\btst\b",
        ]
        for pat in tst_patterns:
            if re.search(pat, search_text):
                return True

    return False


def _adaptive_rate_limit_pause(resp: Any) -> float:
    """
    Lê headers de rate-limit (X-RateLimit-Remaining / X-RateLimit-Reset) do Comunica PJe.
    Não aplica sleep fixo enquanto houver cota (Remaining > 1); quando Remaining <= 1,
    calcula o delta exato até o reset Unix epoch (ou pausa mínima segura) para evitar 429.
    """
    headers = getattr(resp, "headers", None)
    if not headers or not hasattr(headers, "items"):
        return 0.0

    remaining_val = None
    reset_val = None
    for k, v in headers.items():
        kl = str(k).lower()
        if kl == "x-ratelimit-remaining":
            remaining_val = v
        elif kl == "x-ratelimit-reset":
            reset_val = v

    if remaining_val is None:
        return 0.0

    try:
        remaining = int(str(remaining_val).strip())
    except (ValueError, TypeError):
        return 0.0

    if remaining > 1:
        return 0.0

    pause = 2.5
    if reset_val is not None:
        try:
            reset_num = float(str(reset_val).strip())
            if reset_num > 1_000_000_000:
                delta = reset_num - time.time()
                pause = max(0.5, min(3.2, delta))
            elif 0 < reset_num <= 10:
                pause = max(0.5, min(3.2, reset_num))
        except (ValueError, TypeError):
            pass

    time.sleep(pause)
    return pause


def get_scan_config() -> Dict[str, Any]:
    """
    Retorna a configuração dinâmica de varredura conforme o ambiente (Local vs. Render - US-23).

    O benchmark empírico na API real do Comunica PJe mostrou que requisições sequenciais em
    sessão Keep-Alive atingem ~49 pág/min sem quedas de conexão, enquanto concorrência 3
    causa ReadTimeout/ConnectionReset e cai para ~22.7 pág/min. Por isso, o padrão é
    sequencial (concurrency=1) com pacing adaptativo via headers X-RateLimit-Remaining
    (batch_delay=0.0 sem atraso fixo).

    - Ambiente Local (RENDER ausente ou diferente de 'true'):
        * SCAN_MAX_PAGES = 0 (ilimitado — sem cortes artificiais na varredura local)
        * SCAN_BATCH_DELAY = 0.0 (pacing adaptativo pelos headers de rate limit)
        * SCAN_CONCURRENCY = 1 (Keep-Alive sequencial estável)
        * gc_collect = False
    - Ambiente Render (RENDER='true'):
        * SCAN_MAX_PAGES = 300 (teto seguro até 30.000 comunicações sem estourar 512 MB)
        * SCAN_BATCH_DELAY = 0.0 (pacing adaptativo sem desperdício no time-box de 8s)
        * SCAN_CONCURRENCY = 1 (evita ReadTimeout/Reset e consumo extra de RAM)
        * gc_collect = True (coleta explícita a cada lote para estabilizar RAM ~150 MB)

    Permite override explícito via SCAN_MAX_PAGES, SCAN_BATCH_DELAY e SCAN_CONCURRENCY.
    """
    is_render = os.getenv("RENDER", "").strip().lower() == "true"

    default_max_pages = 300 if is_render else 0
    default_max_pages_per_court = 50 if is_render else 0
    default_page_size = 100
    default_delay = 0.0
    default_concurrency = 1

    raw_max_pages = os.getenv("SCAN_MAX_PAGES")
    if raw_max_pages is not None and raw_max_pages.strip():
        try:
            max_pages = int(raw_max_pages.strip())
        except ValueError:
            max_pages = default_max_pages
    else:
        max_pages = default_max_pages

    raw_max_per_court = os.getenv("SCAN_MAX_PAGES_PER_COURT")
    if raw_max_per_court is not None and raw_max_per_court.strip():
        try:
            max_pages_per_court = max(0, int(raw_max_per_court.strip()))
        except ValueError:
            max_pages_per_court = default_max_pages_per_court
    else:
        max_pages_per_court = default_max_pages_per_court

    raw_delay = os.getenv("SCAN_BATCH_DELAY")
    if raw_delay is not None and raw_delay.strip():
        try:
            batch_delay = float(raw_delay.strip())
        except ValueError:
            batch_delay = default_delay
    else:
        batch_delay = default_delay

    raw_conc = os.getenv("SCAN_CONCURRENCY")
    if raw_conc is not None and raw_conc.strip():
        try:
            concurrency = max(1, int(raw_conc.strip()))
        except ValueError:
            concurrency = default_concurrency
    else:
        concurrency = default_concurrency

    raw_page_size = os.getenv("SCAN_PAGE_SIZE")
    if raw_page_size is not None and raw_page_size.strip():
        try:
            page_size = max(50, min(1000, int(raw_page_size.strip())))
        except ValueError:
            page_size = default_page_size
    else:
        page_size = default_page_size

    return {
        "is_render": is_render,
        "max_pages": max_pages,
        "max_pages_per_court": max_pages_per_court,
        "page_size": page_size,
        "batch_delay": batch_delay,
        "concurrency": concurrency,
        "gc_collect": is_render,
    }


class CNJProcessSearcher:
    """
    Cliente robusto para integração oficial com as APIs públicas do Judiciário:
    1. Comunica PJe (DJEN): Descoberta de publicações e processos por nome da parte.
       - Respeita o limite estrito da API do CNJ de itensPorPagina <= 50.
       - Envia headers obrigatórios com User-Agent para contornar bloqueio do WAF.
    2. DataJud (CNJ): Consulta de metadados, movimentações TPU e segredo de justiça via Elasticsearch.
       - Roteamento dinâmico do alias do tribunal baseado na estrutura CNJ NNNNNNNDDAAAAJTROOOO.
       - Agrupamento em lote via query 'terms' (até 50 processos por POST).
       - Fallback semântico inteligente no texto das publicações para nunca zerar o dashboard.
    """

    COMUNICA_URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"
    DATAJUD_BASE = "https://api-publica.datajud.cnj.jus.br"

    def __init__(self, session: Optional[requests.Session] = None):
        key = os.getenv("DATAJUD_API_KEY", "").strip()
        self.datajud_key = key
        self.session = session or requests.Session()
        # Pool de conexões Keep-Alive compartilhado entre threads do mini-lote paralelo
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.last_scan_truncated = False
        self.last_stop_reason: Optional[str] = None
        
        # Suporte a proxy dedicado isolado (não interfere no pip/build do Render)
        proxy_url = os.getenv("PJE_PROXY_URL") or os.getenv("JUDICIAL_PROXY_URL")
        if proxy_url and proxy_url.strip():
            proxy_clean = proxy_url.strip()
            self.session.proxies = {
                "http": proxy_clean,
                "https": proxy_clean,
            }
            logger.info("CNJProcessSearcher configurado com proxy dedicado PJE_PROXY_URL.")

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        })

    def _request_gov(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        timeout: float = 8.0,
    ) -> requests.Response:
        """Encaminha requisições ao Judiciário com headers pt-BR e fallback anti-geoblocking."""
        return request_gov_br_with_fallback(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json_body=json_body,
            timeout=timeout,
            session=self.session,
        )

    @staticmethod
    def clean_company_name(name: str) -> str:
        """Remove pontuações e sufixos que quebram o match no Comunica PJe."""
        cleaned = re.sub(r"\b(LTDA\.?|S\.?/?\s*A\.?|EIRELI\.?|MEI?\.?|EPP\.?)\b", "", name, flags=re.IGNORECASE)
        cleaned = re.sub(r"[^\w\s]", " ", cleaned)
        cleaned = re.sub(r"\b(LTDA|S\s*A|SA|EIRELI|ME|EPP)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
        return cleaned if len(cleaned) >= 3 else name.strip()

    @staticmethod
    def extract_brand_search_term(raw_input: str, razao_social: str = "", nome_fantasia: str = "") -> str:
        """Extrai o nome raiz/marca da empresa para busca ampla de filiais no Comunica PJe."""
        return extract_brand_search_term(raw_input, razao_social=razao_social, nome_fantasia=nome_fantasia)

    @staticmethod
    def normalize_cnj(raw_number: Any) -> str:
        """Garante exatamente 20 dígitos numéricos sem pontos ou traços."""
        digits = re.sub(r"\D", "", str(raw_number or ""))
        return digits if len(digits) == 20 else ""

    @staticmethod
    def resolve_datajud_alias(cnj_20: str, fallback_sigla: str = "") -> str:
        """Extrai o alias do tribunal no DataJud pelos dígitos 13..15 do número CNJ."""
        if len(cnj_20) == 20:
            j = cnj_20[13]
            tr = int(cnj_20[14:16])
            if j == "5":
                return "api_publica_tst" if tr == 0 else f"api_publica_trt{tr}"
            if j == "4":
                return f"api_publica_trf{tr}"
            if j == "8" and tr in TJ_MAP:
                return f"api_publica_{TJ_MAP[tr]}"
        if fallback_sigla:
            clean_sigla = fallback_sigla.lower().replace("-", "").strip()
            return f"api_publica_{clean_sigla}"
        return "api_publica_trt6"

    def _parse_comunica_items(
        self,
        items: List[Any],
        processes: Dict[str, Any],
        discovered_entities: Optional[Set[str]] = None,
        is_tst_override: bool = False,
        max_records: Optional[int] = None,
    ):
        """
        Extrai números CNJ de 20 dígitos, metadados, CNPJs e snippet compacto em texto plano,
        descartando o HTML bruto `texto` imediatamente para manter o pico de RAM abaixo de 380 MB.
        """
        for item in items:
            if not isinstance(item, dict):
                continue

            if discovered_entities is not None:
                for dest in item.get("destinatarios", []) or []:
                    if isinstance(dest, dict):
                        nome_dest = dest.get("nome") or dest.get("nomeParte")
                        if nome_dest and isinstance(nome_dest, str) and len(nome_dest.strip()) >= 3:
                            discovered_entities.add(nome_dest.strip().upper())
                        cnpj_dest = dest.get("numeroDocumento") or dest.get("cpfCnpj") or dest.get("cnpj")
                        if cnpj_dest and isinstance(cnpj_dest, str) and is_cnpj(cnpj_dest):
                            discovered_entities.add(re.sub(r"\D", "", cnpj_dest))

            raw_num = (
                item.get("numero_processo")
                or item.get("numeroProcesso")
                or item.get("numeroprocessocommascara")
            )
            cnj = self.normalize_cnj(raw_num)
            if not cnj:
                continue

            if max_records and cnj not in processes and len(processes) >= max_records:
                continue

            trib_sigla = "tst" if is_tst_override else (item.get("siglaTribunal", "") or "")
            # Dupla blindagem: descarta qualquer registro fora da Justiça do Trabalho (TST/TRT1..24)
            if not is_labor_process(numero_processo=cnj, tribunal=trib_sigla):
                continue
            is_tst = is_tst_override or is_tst_process(numero_processo=cnj, tribunal=trib_sigla)

            entry = processes.setdefault(cnj, {
                "numeroProcesso": cnj,
                "numero_processo": cnj,
                "siglaTribunal": trib_sigla,
                "orgaoJulgador": item.get("nomeOrgao", ""),
                "dataUltimaPublicacao": item.get("data_disponibilizacao") or item.get("dataDisponibilizacao", ""),
                "publicacoes_texto": [],
                "destinatarios_nomes": [],
                "classes_pje": [],
                "tipos_doc_pje": [],
                "assuntos_inferidos": [],
                "is_tst_recurso": is_tst,
            })
            if is_tst:
                entry["is_tst_recurso"] = True
                if not entry.get("siglaTribunal") or entry["siglaTribunal"] == "N/A":
                    entry["siglaTribunal"] = "tst"

            dest_list = item.get("destinatarios") or []
            if isinstance(dest_list, list):
                for dest in dest_list:
                    if isinstance(dest, dict):
                        nome_dest = dest.get("nome") or dest.get("nomeParte")
                        if nome_dest and isinstance(nome_dest, str) and len(nome_dest.strip()) >= 3:
                            nome_clean = nome_dest.strip().upper()
                            if nome_clean not in entry.setdefault("destinatarios_nomes", []):
                                entry["destinatarios_nomes"].append(nome_clean)
                            if discovered_entities is not None:
                                discovered_entities.add(nome_clean)
                        cnpj_dest = dest.get("numeroDocumento") or dest.get("cpfCnpj") or dest.get("cnpj")
                        if cnpj_dest and isinstance(cnpj_dest, str) and is_cnpj(cnpj_dest):
                            clean_cnpj = re.sub(r"\D", "", cnpj_dest)
                            if clean_cnpj not in entry.setdefault("cnpjs_encontrados", []):
                                entry["cnpjs_encontrados"].append(clean_cnpj)
                            if discovered_entities is not None:
                                discovered_entities.add(clean_cnpj)

            raw_texto = (
                item.get("texto")
                or item.get("teor")
                or item.get("conteudo")
                or item.get("textoPublicacao")
                or ""
            )
            if raw_texto:
                for match in _CNPJ_IN_TEXT_RE.findall(str(raw_texto)):
                    clean_c = re.sub(r"\D", "", match)
                    if len(clean_c) == 14:
                        if clean_c not in entry.setdefault("cnpjs_encontrados", []):
                            entry["cnpjs_encontrados"].append(clean_c)
                        if discovered_entities is not None:
                            discovered_entities.add(clean_c)
                compact_texto = _strip_html_to_text(raw_texto, max_len=MAX_SNIPPET_CHARS)
                if (
                    compact_texto
                    and len(entry["publicacoes_texto"]) < MAX_SNIPPETS_PER_PROCESS
                    and compact_texto not in entry["publicacoes_texto"]
                ):
                    entry["publicacoes_texto"].append(compact_texto)
                del raw_texto

            classe = item.get("nomeClasse") or item.get("nome_classe") or item.get("classe") or ""
            if classe and classe not in entry["classes_pje"]:
                entry["classes_pje"].append(classe)
            cod_classe = item.get("codigoClasse") or item.get("codClasse")
            if cod_classe and cod_classe not in entry.setdefault("codigos_classe", []):
                entry["codigos_classe"].append(cod_classe)

            tipo_doc = item.get("tipoDocumento") or item.get("tipo_documento") or item.get("tipoComunicacao") or ""
            if tipo_doc and tipo_doc not in entry["tipos_doc_pje"]:
                entry["tipos_doc_pje"].append(tipo_doc)

            # Detecção de Recurso de Revista / AIRR / TST no texto ou metadados da publicação
            if not entry.get("is_tst_recurso"):
                if is_tst_process(
                    numero_processo=cnj,
                    tribunal=entry.get("siglaTribunal"),
                    observation=entry.get("orgaoJulgador"),
                    classes_pje=entry.get("classes_pje"),
                    texts=entry.get("publicacoes_texto"),
                ):
                    entry["is_tst_recurso"] = True
                    if not entry.get("siglaTribunal") or entry["siglaTribunal"].lower() in ("n/a", "none"):
                        entry["siglaTribunal"] = "tst"

    def fetch_single_comunica_page(
        self,
        brand_term: str,
        page_num: int = 1,
        timeout: float = 5.0,
        fallback_term: Optional[str] = None,
        sigla_tribunal: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Busca uma única página no Comunica PJe com timeout curto (< 5s),
        normalização genérica do termo de busca e fallback automático se retornar 0 itens.
        Nunca mascara erro HTTP (429, 5xx, timeout) como lista vazia de sucesso.
        """
        primary_term = normalize_search_term_generic(brand_term) or brand_term.strip()
        p = {
            "nomeParte": primary_term,
            "pagina": page_num,
            "itensPorPagina": 50,
        }
        if sigla_tribunal:
            p["siglaTribunal"] = sigla_tribunal
        procs: Dict[str, Any] = {}
        self.last_page_raw_count = 0
        self.last_page_has_more = False
        self.last_page_status_code = 200
        self.last_page_rate_limited = False
        self.last_retry_after = 3
        self.last_effective_query_term = primary_term

        def _do_request(query_val: str):
            req_params = dict(p)
            req_params["nomeParte"] = query_val
            return self._request_gov("GET", self.COMUNICA_URL, params=req_params, timeout=timeout)

        try:
            r = _do_request(primary_term)
            # Tratamento de Rate Limit (429) com espera curta única de 2s sem prender worker
            if r.status_code == 429:
                retry_header = r.headers.get("Retry-After")
                wait_sec = min(3.0, float(retry_header)) if (retry_header and retry_header.isdigit()) else 2.0
                logger.warning("PJe retornou 429 na página %d para '%s'. Tentando espera curta de %.1fs...", page_num, primary_term, wait_sec)
                time.sleep(wait_sec)
                r = _do_request(primary_term)

            self.last_page_status_code = r.status_code
            if r.status_code == 429:
                self.last_page_rate_limited = True
                self.last_retry_after = int(r.headers.get("Retry-After") or 3)
                logger.warning("[SCAN_PAGE] pagina=%d status_http=429 rate_limit_confirmado para '%s'", page_num, primary_term)
                return procs

            if r.status_code == 200:
                py = r.json()
                items = py if isinstance(py, list) else (
                    py.get("items") or py.get("itens") or py.get("content") or []
                )

                # Fallback genérico: se consulta primária retornou 200 OK com 0 itens na página 1,
                # tenta o núcleo da razão social sem sufixos societários ou pontuação
                if len(items) == 0 and page_num == 1:
                    candidate_fallback = fallback_term or extract_corporate_core_name(primary_term)
                    if candidate_fallback and candidate_fallback != primary_term and len(candidate_fallback) >= 3:
                        logger.info(
                            "Consulta primária '%s' retornou 0 itens no PJe. Tentando fallback genérico '%s'...",
                            primary_term,
                            candidate_fallback,
                        )
                        r_fb = _do_request(candidate_fallback)
                        if r_fb.status_code == 200:
                            py_fb = r_fb.json()
                            items_fb = py_fb if isinstance(py_fb, list) else (
                                py_fb.get("items") or py_fb.get("itens") or py_fb.get("content") or []
                            )
                            if items_fb:
                                logger.info(
                                    "Fallback genérico '%s' encontrou %d intimações no PJe!",
                                    candidate_fallback,
                                    len(items_fb),
                                )
                                items = items_fb
                                self.last_effective_query_term = candidate_fallback

                self.last_page_raw_count = len(items)
                self.last_page_has_more = (len(items) == 50)
                self._parse_comunica_items(items, procs)
                logger.info(
                    "[SCAN_PAGE] pagina=%d status_http=%d itens_retornados=%d cnjs_novos=%d total_acumulado=%d motivo_encerramento=%s",
                    page_num,
                    r.status_code,
                    len(items),
                    len(procs),
                    len(procs),
                    "FAST_PATH_SUCESSO" if procs else "ZERO_PROCESSOS_CONFIRMADO",
                )
            else:
                logger.warning("[SCAN_PAGE] pagina=%d status_http=%d erro na API para '%s'", page_num, r.status_code, primary_term)
        except Exception as exc:
            self.last_page_status_code = 0
            logger.warning("[SCAN_PAGE] pagina=%d status_http=0 excecao de conexao (%s): %s", page_num, primary_term, exc)
        return procs

    def paginate_comunica_pje_standard(
        self,
        search_strategy: Dict[str, Any],
        start_page: int = 1,
        max_pages: Optional[int] = None,
        max_pages_per_call: Optional[int] = None,
        on_page_callback=None,
        on_batch_persist=None,
        timeout: float = 6.0,
        known_cnjs: Optional[Set[str]] = None,
        batch_delay: Optional[float] = None,
        itens_por_pagina: int = 100,
        time_budget_seconds: Optional[float] = None,
        prev_page_signature: Optional[str] = None,
        max_records: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Varre sequencialmente o Comunica PJe em fluxo streaming por lote com RAM constante,
        freio anti-loop determinístico em 3 camadas e limite dinâmico por ambiente (US-23).

        Travas determinísticas de encerramento (nenhuma delas depende de "0 CNJs novos"):
        1. Repetição exata: a página tem exatamente as mesmas comunicações de uma página já lida
           (inclusive a última do lote anterior, via prev_page_signature) — loop real da API.
        2. 3 páginas consecutivas sem NENHUMA comunicação inédita. Páginas com 0 CNJs novos, mas
           com intimações novas de processos já vistos, são normais em empresas grandes e seguem.
        3. Fim real: lista vazia com HTTP 200, página curta (< 50 itens) ou pagina >= total de páginas
           informado pela API (totalPages ou count).
        """
        cfg = get_scan_config()
        effective_max_pages = cfg["max_pages"] if max_pages is None else max_pages
        effective_delay = cfg["batch_delay"] if batch_delay is None else batch_delay
        concurrency = max(1, int(cfg.get("concurrency", 2 if cfg.get("is_render") else 3)))
        do_gc = cfg.get("gc_collect", False)

        raw_term = (
            search_strategy.get("effective_query_term")
            or search_strategy.get("comunica_query_term")
            or search_strategy.get("raw_input")
            or ""
        ).strip()
        query_term = normalize_search_term_generic(raw_term) or raw_term

        all_processes: Dict[str, Any] = {}
        discovered_entities: Set[str] = set()
        known_cnjs_set: Set[str] = set(known_cnjs) if known_cnjs is not None else set()
        pages_read = 0
        hit_max_pages = False
        truncated = False
        stop_reason: Optional[str] = None
        anti_loop_triggered = False
        rate_limited = False
        retry_after = 0
        had_http_error = False
        motivo = ""
        # Assinaturas das páginas já lidas; a última do lote anterior vem do cliente para
        # detectar repetição exata também na fronteira entre lotes HTTP.
        seen_page_signatures: Set[str] = {prev_page_signature} if prev_page_signature else set()
        last_page_signature: Optional[str] = prev_page_signature
        seen_comm_ids: Set[str] = set()
        consecutive_no_new_comm_pages = 0
        used_page_size_fallback = False

        def _fetch_single_page(p_num: int):
            req_params = {
                "nomeParte": query_term,
                "pagina": p_num,
                "itensPorPagina": itens_por_pagina,
            }
            try:
                r = self._request_gov("GET", self.COMUNICA_URL, params=req_params, timeout=timeout)
                return p_num, r, None
            except Exception as err:
                return p_num, None, err

        page = start_page
        batch_start_time = time.monotonic()
        while True:
            # Trava de teto configurado por ambiente (SCAN_MAX_PAGES)
            if effective_max_pages > 0 and page > effective_max_pages:
                hit_max_pages = True
                truncated = True
                stop_reason = stop_reason or "MAX_PAGES"
                motivo = "TETO_MAX_PAGES_ATINGIDO"
                break

            # Limite por lote curto (proteção contra timeout HTTP no Render)
            if max_pages_per_call is not None and pages_read >= max_pages_per_call:
                motivo = "LOTE_CURTO_CONCLUIDO"
                break

            # Limite máximo de registros por chamada (quando especificado)
            if max_records is not None and max_records > 0 and len(all_processes) >= max_records:
                truncated = True
                stop_reason = "MAX_RECORDS"
                motivo = "MAX_RECORDS"
                break

            # Limite orçamentário por time-box (proteção ativa contra timeout HTTP no Render Free)
            if time_budget_seconds is not None and time_budget_seconds > 0:
                elapsed = time.monotonic() - batch_start_time
                if elapsed >= time_budget_seconds:
                    truncated = True
                    stop_reason = "TIME_BUDGET"
                    motivo = "LOTE_CURTO_CONCLUIDO"
                    logger.info(
                        "[SCAN_PAGE] Time-box atingido (%.2fs >= %.2fs) após %d páginas lidas na página %d. Encerrando lote com segurança.",
                        elapsed, time_budget_seconds, pages_read, page
                    )
                    break

            # Define quantas páginas buscar em paralelo neste mini-lote respeitando tetos
            chunk_size = concurrency
            if max_pages_per_call is not None:
                chunk_size = min(chunk_size, max(1, max_pages_per_call - pages_read))
            if effective_max_pages > 0:
                chunk_size = min(chunk_size, max(1, effective_max_pages - page + 1))

            chunk_pages = [page + offset for offset in range(chunk_size)]
            if len(chunk_pages) == 1:
                results_by_page = {chunk_pages[0]: _fetch_single_page(chunk_pages[0])}
            else:
                results_by_page = {}
                with ThreadPoolExecutor(max_workers=len(chunk_pages)) as pool:
                    futures = [pool.submit(_fetch_single_page, p_num) for p_num in chunk_pages]
                    for fut in as_completed(futures):
                        p_num, resp_obj, err_obj = fut.result()
                        results_by_page[p_num] = (p_num, resp_obj, err_obj)

            should_break = False
            should_retry_fallback = False

            # Processa respostas estritamente em ordem crescente para nunca pular páginas
            for p_num in chunk_pages:
                if time_budget_seconds is not None and time_budget_seconds > 0 and pages_read > 0:
                    elapsed = time.monotonic() - batch_start_time
                    if elapsed >= time_budget_seconds:
                        page = p_num
                        truncated = True
                        stop_reason = "TIME_BUDGET"
                        motivo = "LOTE_CURTO_CONCLUIDO"
                        logger.info(
                            "[SCAN_PAGE] Time-box atingido (%.2fs >= %.2fs) após %d páginas lidas na página %d. Encerrando lote com segurança.",
                            elapsed, time_budget_seconds, pages_read, page
                        )
                        should_break = True
                        break
                page = p_num
                _, resp, fetch_err = results_by_page[p_num]
                if fetch_err is not None:
                    had_http_error = True
                    motivo = f"EXCECAO_{type(fetch_err).__name__}"
                    logger.error("[SCAN_PAGE] pagina=%d status_http=0 excecao na paginacao do PJe: %s", page, fetch_err)
                    should_break = True
                    break

                if resp.status_code == 429:
                    rate_limited = True
                    retry_hdr = resp.headers.get("Retry-After")
                    retry_after = int(retry_hdr) if (retry_hdr and retry_hdr.isdigit()) else 3
                    motivo = "RATE_LIMITED_429"
                    logger.warning("[SCAN_PAGE] pagina=%d status_http=429 rate_limit na paginacao para '%s'", page, query_term)
                    should_break = True
                    break

                if not resp.ok:
                    if (
                        (resp.status_code in (400, 422) or (resp.status_code == 500 and page > 100))
                        and itens_por_pagina > 50
                        and not used_page_size_fallback
                    ):
                        # Fallback caso a API rejeite ou sobrecarregue (HTTP 500) janela com itensPorPagina=100 em página profunda
                        used_page_size_fallback = True
                        page = (page - 1) * (itens_por_pagina // 50) + 1
                        itens_por_pagina = 50
                        logger.warning(
                            "[SCAN_PAGE] HTTP %d com itensPorPagina>50; ajustando para itensPorPagina=50 na página %d.",
                            resp.status_code, page
                        )
                        should_retry_fallback = True
                        break
                    had_http_error = True
                    motivo = f"HTTP_ERROR_{resp.status_code}"
                    logger.warning("[SCAN_PAGE] pagina=%d status_http=%d erro na paginacao para '%s'", page, resp.status_code, query_term)
                    should_break = True
                    break

                try:
                    payload = resp.json()
                except Exception as exc:
                    had_http_error = True
                    motivo = f"EXCECAO_{type(exc).__name__}"
                    logger.error("[SCAN_PAGE] pagina=%d status_http=0 excecao ao decodificar JSON do PJe: %s", page, exc)
                    should_break = True
                    break

                if isinstance(payload, dict):
                    raw_items = payload.get("items") or payload.get("itens") or payload.get("content") or []
                elif isinstance(payload, list):
                    raw_items = payload
                else:
                    raw_items = []

                # Fim natural da paginação pública
                if not raw_items:
                    motivo = "FIM_NATURAL_LISTA_VAZIA"
                    logger.info("[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=0 cnjs_novos=0 total_acumulado=%d motivo_encerramento=%s", page, len(known_cnjs_set), motivo)
                    should_break = True
                    break

                pages_read += 1

                # Trava Anti-Loop 1: a página atual é idêntica (mesmos IDs de comunicação) a uma página
                # já processada — inclusive a última página do lote anterior. Isso é loop real da API.
                current_comm_ids = {self._communication_identity(it) for it in raw_items if isinstance(it, dict)}
                current_comm_ids.discard("")
                signature = _page_signature(current_comm_ids)
                if current_comm_ids and signature in seen_page_signatures:
                    anti_loop_triggered = True
                    motivo = "REPETICAO_EXATA_IDS_COMUNICACAO"
                    logger.info(
                        "[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=%d cnjs_novos=0 total_acumulado=%d motivo_encerramento=%s",
                        page, len(raw_items), len(known_cnjs_set), motivo
                    )
                    should_break = True
                    break
                seen_page_signatures.add(signature)
                last_page_signature = signature

                # Trava Anti-Loop 2: só conta página "repetida" quando NENHUMA comunicação é inédita.
                # Página com 0 CNJs novos mas com comunicações novas é normal em empresas grandes
                # (várias intimações do mesmo processo) e nunca pode encerrar a varredura.
                new_comm_ids = current_comm_ids - seen_comm_ids
                seen_comm_ids.update(current_comm_ids)
                if current_comm_ids and not new_comm_ids:
                    consecutive_no_new_comm_pages += 1
                    if consecutive_no_new_comm_pages >= 3:
                        anti_loop_triggered = True
                        motivo = "3_PAGINAS_CONSECUTIVAS_SEM_COMUNICACOES_INEDITAS"
                        logger.info(
                            "[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=%d total_acumulado=%d motivo_encerramento=%s",
                            page, len(raw_items), len(known_cnjs_set), motivo
                        )
                        should_break = True
                        break
                else:
                    consecutive_no_new_comm_pages = 0

                page_processes: Dict[str, Any] = {}
                self._parse_comunica_items(raw_items, page_processes, discovered_entities)

                page_cnjs = set(page_processes.keys())
                new_cnjs = page_cnjs - known_cnjs_set
                if not new_cnjs:
                    logger.info(
                        "Página %d sem CNJs novos, mas com %d comunicações inéditas: varredura continua.",
                        page, len(new_comm_ids)
                    )

                # Atualiza conjunto de CNJs conhecidos e mapa em memória
                known_cnjs_set.update(new_cnjs)
                for cnj, data in page_processes.items():
                    if cnj not in all_processes:
                        all_processes[cnj] = data
                    else:
                        for t in data.get("publicacoes_texto", []):
                            if t not in all_processes[cnj].setdefault("publicacoes_texto", []):
                                all_processes[cnj]["publicacoes_texto"].append(t)
                        for c in data.get("classes_pje", []):
                            if c not in all_processes[cnj].setdefault("classes_pje", []):
                                all_processes[cnj]["classes_pje"].append(c)

                # Persistência streaming por lote (upsert imediato sem acúmulo de memória)
                if on_batch_persist:
                    try:
                        on_batch_persist(page, page_processes, len(new_cnjs))
                    except Exception as persist_err:
                        logger.error("Erro no callback on_batch_persist na página %d: %s", page, persist_err)

                # Callback progressivo para contadores da UI/polling
                if on_page_callback:
                    try:
                        on_page_callback(page, len(all_processes), all_processes)
                    except Exception as cb_err:
                        logger.debug("Erro em on_page_callback página %d: %s", page, cb_err)

                # Liberação explícita de memória da página processada (RAM constante)
                del page_processes
                if do_gc:
                    gc.collect()

                # Trava Anti-Loop 3: Critério oficial de encerramento da API do CNJ (< 50 itens brutos)
                if len(raw_items) < 50:
                    motivo = "FIM_NATURAL_MENOS_DE_50_ITENS"
                    logger.info(
                        "[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=%d cnjs_novos=%d total_acumulado=%d motivo_encerramento=%s",
                        page, len(raw_items), len(new_cnjs), len(known_cnjs_set), motivo
                    )
                    should_break = True
                    break

                # Total de páginas informado pela API (totalPages explícito ou derivado de "count").
                # A API do Comunica PJe trunca count em 10000 (janela Elasticsearch): se count >= 10000
                # e a página veio cheia, não encerra na página 100 — continua avançando até fim real.
                raw_count = payload.get("count") if isinstance(payload, dict) else None
                count_is_truncated = isinstance(raw_count, int) and raw_count >= 10000
                total_pages = _total_pages_from_payload(payload, itens_por_pagina)
                if total_pages and page >= total_pages:
                    if not count_is_truncated or len(raw_items) < itens_por_pagina:
                        motivo = "TOTAL_PAGINAS_METADADOS_ATINGIDO"
                        logger.info(
                            "[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=%d cnjs_novos=%d total_acumulado=%d motivo_encerramento=%s",
                            page, len(raw_items), len(new_cnjs), len(known_cnjs_set), motivo
                        )
                        should_break = True
                        break

                if effective_max_pages > 0 and page >= effective_max_pages:
                    hit_max_pages = True
                    motivo = "TETO_MAX_PAGES_ATINGIDO"
                    logger.info(
                        "[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=%d cnjs_novos=%d total_acumulado=%d motivo_encerramento=%s",
                        page, len(raw_items), len(new_cnjs), len(known_cnjs_set), motivo
                    )
                    should_break = True
                    break

                logger.info(
                    "[SCAN_PAGE] pagina=%d status_http=200 itens_retornados=%d cnjs_novos=%d total_acumulado=%d motivo_encerramento=CONTINUANDO",
                    page, len(raw_items), len(new_cnjs), len(known_cnjs_set)
                )
                page += 1

                if effective_delay == 0:
                    _adaptive_rate_limit_pause(resp)

            if should_break:
                break
            if should_retry_fallback:
                continue

            # Diluição no tempo configurável por ambiente (SCAN_BATCH_DELAY)
            if effective_delay > 0:
                time.sleep(effective_delay)

        # Se for busca ampla por marca/grupo (BROAD_BRAND), faz varredura complementar no TST
        # Executa APENAS na primeira página (start_page == 1) para não reconsultar TST em cada lote subsequente
        if (
            start_page == 1
            and (search_strategy.get("is_broad_brand", False) or search_strategy.get("mode") == "BROAD_BRAND")
            and not rate_limited
            and not had_http_error
        ):
            try:
                tst_max_pages = 5
                for tst_page in range(1, tst_max_pages + 1):
                    params_tst = {
                        "nomeParte": query_term,
                        "siglaTribunal": "TST",
                        "pagina": tst_page,
                        "itensPorPagina": 100,
                    }
                    resp_tst = self._request_gov("GET", self.COMUNICA_URL, params=params_tst, timeout=timeout)
                    if not resp_tst.ok:
                        break
                    py_tst = resp_tst.json()
                    tst_items = py_tst if isinstance(py_tst, list) else (
                        py_tst.get("items") or py_tst.get("itens") or []
                    )
                    if not tst_items:
                        break
                    tst_processes: Dict[str, Any] = {}
                    self._parse_comunica_items(tst_items, tst_processes, discovered_entities, is_tst_override=True)
                    tst_new = set(tst_processes.keys()) - known_cnjs_set
                    if tst_new:
                        known_cnjs_set.update(tst_new)
                        for cnj, data in tst_processes.items():
                            if cnj not in all_processes:
                                all_processes[cnj] = data
                            else:
                                for t in data.get("publicacoes_texto", []):
                                    if t not in all_processes[cnj].setdefault("publicacoes_texto", []):
                                        all_processes[cnj]["publicacoes_texto"].append(t)
                        if on_batch_persist:
                            try:
                                on_batch_persist(0, tst_processes, len(tst_new))
                            except Exception as tst_cb_err:
                                logger.debug("Erro em on_batch_persist do TST: %s", tst_cb_err)
                    del tst_processes
                    if do_gc:
                        gc.collect()
                    total_tst_pages = _total_pages_from_payload(py_tst, 100) if isinstance(py_tst, dict) else None
                    if len(tst_items) < 50 or (total_tst_pages and tst_page >= total_tst_pages):
                        break
            except Exception as tst_exc:
                logger.debug("Consulta complementar TST tratada silenciosamente: %s", tst_exc)

        is_completed = (
            not hit_max_pages
            and not rate_limited
            and not had_http_error
            and (
                motivo.startswith("FIM_NATURAL")
                or motivo in (
                    "TOTAL_PAGINAS_METADADOS_ATINGIDO",
                    "REPETICAO_EXATA_IDS_COMUNICACAO",
                    "3_PAGINAS_CONSECUTIVAS_SEM_COMUNICACOES_INEDITAS",
                )
                or anti_loop_triggered
            )
        )
        has_more_pages = (not is_completed) and (not hit_max_pages) and (not rate_limited) and (not had_http_error)
        # Em 429 ou falha transitória do PJe, a próxima chamada retoma exatamente a página que falhou.
        if has_more_pages or rate_limited or had_http_error:
            next_page = page
        else:
            next_page = None
        if had_http_error and not retry_after:
            retry_after = 5

        logger.info(
            "paginate_comunica_pje_standard('%s') finalizada: %d páginas lidas, %d processos únicos (hit_max_pages=%s, anti_loop=%s, rate_limited=%s, is_completed=%s, motivo=%s).",
            query_term, pages_read, len(all_processes), hit_max_pages, anti_loop_triggered, rate_limited, is_completed, motivo
        )

        return {
            "processes": all_processes,
            "discovered_entities": discovered_entities,
            "pages_read": pages_read,
            "total_processes": len(all_processes),
            "hit_max_pages": hit_max_pages,
            "anti_loop_triggered": anti_loop_triggered,
            "rate_limited": rate_limited,
            "had_http_error": had_http_error,
            "retry_after_seconds": retry_after,
            "next_page": next_page,
            "is_completed": is_completed,
            "motivo_encerramento": motivo,
            "last_page_signature": last_page_signature,
            "truncated": truncated or hit_max_pages,
            "stop_reason": stop_reason or motivo,
        }

    def paginate_labor_courts_batch(
        self,
        search_strategy: Dict[str, Any],
        courts_queue: List[str],
        court_index: int = 0,
        start_page: int = 1,
        consecutive_court_errors: int = 0,
        partial_courts: Optional[List[str]] = None,
        layer: int = 1,
        layer2_queue: Optional[List[Dict[str, Any]]] = None,
        layer1_max_pages: int = 5,
        max_pages_per_court: Optional[int] = None,
        on_batch_persist: Optional[Any] = None,
        timeout: float = 10.0,
        known_cnjs: Optional[Set[str]] = None,
        batch_delay: Optional[float] = None,
        itens_por_pagina: int = 100,
        time_budget_seconds: float = 8.0,
        prev_page_signature: Optional[str] = None,
        since_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Varredura em largura em 2 camadas nos 25 tribunais da Justiça do Trabalho (TST + TRT1..24):
        - Camada 1: até `layer1_max_pages` (5) páginas por tribunal, descartando vazios rapidamente
          e entregando cobertura nacional no primeiro minuto.
        - Camada 2: aprofunda a partir da página 6 apenas nos tribunais que ficaram pendentes.
        Usa Keep-Alive, pacing cirúrgico via X-RateLimit-Remaining e circuit-breaker de 3 falhas.
        """
        cfg = get_scan_config()
        effective_max_per_court = (
            max_pages_per_court
            if max_pages_per_court is not None
            else cfg.get("max_pages_per_court", 0)
        )
        effective_delay = cfg["batch_delay"] if batch_delay is None else batch_delay
        do_gc = cfg.get("gc_collect", False)

        raw_term = (
            search_strategy.get("effective_query_term")
            or search_strategy.get("comunica_query_term")
            or search_strategy.get("raw_input")
            or ""
        ).strip()
        query_term = normalize_search_term_generic(raw_term) or raw_term

        total_courts = len(courts_queue)
        cur_layer = 2 if int(layer) == 2 else 1
        pending_l2: List[Dict[str, Any]] = [
            {"court": item["court"], "page": int(item.get("page") or (layer1_max_pages + 1))}
            for item in (layer2_queue or [])
            if isinstance(item, dict) and item.get("court")
        ]
        idx = max(0, int(court_index))
        page = max(1, int(start_page))
        err_streak = max(0, int(consecutive_court_errors))
        partials: List[str] = list(partial_courts or [])
        known_cnjs_set: Set[str] = set(known_cnjs) if known_cnjs is not None else set()
        discovered_entities: Set[str] = set()

        pages_read = 0
        rate_limited = False
        had_http_error = False
        retry_after = 0
        motivo = ""
        current_page_size = itens_por_pagina
        seen_signatures: Set[str] = {prev_page_signature} if prev_page_signature else set()
        last_signature: Optional[str] = prev_page_signature
        seen_comm_ids: Set[str] = set()
        consecutive_no_new_comm = 0

        batch_start = time.time()

        def _advance_court():
            nonlocal idx, page, err_streak, current_page_size
            nonlocal last_signature, consecutive_no_new_comm, cur_layer
            idx += 1
            err_streak = 0
            current_page_size = itens_por_pagina
            seen_signatures.clear()
            last_signature = None
            seen_comm_ids.clear()
            consecutive_no_new_comm = 0
            if cur_layer == 1 and idx >= total_courts and pending_l2:
                cur_layer = 2
                idx = 0
            if cur_layer == 2 and idx < len(pending_l2):
                page = int(pending_l2[idx].get("page") or (layer1_max_pages + 1))
            else:
                page = 1

        while (cur_layer == 1 and idx < total_courts) or (cur_layer == 2 and idx < len(pending_l2)):
            sigla = courts_queue[idx] if cur_layer == 1 else pending_l2[idx]["court"]

            if time_budget_seconds > 0 and pages_read > 0:
                if (time.time() - batch_start) >= time_budget_seconds:
                    motivo = "TIME_BOX_LOTE_ATINGIDO"
                    break

            if effective_max_per_court > 0 and page > effective_max_per_court:
                if sigla not in partials:
                    partials.append(sigla)
                _advance_court()
                continue

            params = {
                "nomeParte": query_term,
                "siglaTribunal": sigla,
                "pagina": page,
                "itensPorPagina": current_page_size,
            }
            if since_date:
                params["dataDisponibilizacaoInicio"] = since_date

            try:
                resp = self._request_gov("GET", self.COMUNICA_URL, params=params, timeout=timeout)
            except Exception as req_exc:
                if current_page_size > 100:
                    next_size = 100
                    factor = current_page_size // next_size
                    page = (page - 1) * max(1, factor) + 1
                    current_page_size = next_size
                    continue
                pages_read += 1
                err_streak += 1
                logger.warning(
                    "[LABOR_SCAN] L%d tribunal=%s pagina=%d erro=%s streak=%d/3",
                    cur_layer, sigla, page, type(req_exc).__name__, err_streak
                )
                if err_streak >= 3:
                    if sigla not in partials:
                        partials.append(sigla)
                    _advance_court()
                    continue
                if time_budget_seconds > 0 and (time.time() - batch_start) >= time_budget_seconds:
                    had_http_error = True
                    retry_after = 3
                    motivo = f"EXCECAO_{type(req_exc).__name__}"
                    break
                continue

            if resp.status_code == 429:
                rate_limited = True
                retry_hdr = resp.headers.get("Retry-After")
                retry_after = int(retry_hdr) if (retry_hdr and retry_hdr.isdigit()) else 3
                motivo = "RATE_LIMITED_429"
                logger.warning("[LABOR_SCAN] L%d tribunal=%s pagina=%d status_http=429", cur_layer, sigla, page)
                break

            if not resp.ok:
                if (
                    (resp.status_code in (400, 422, 500) or resp.status_code == 504)
                    and current_page_size > 50
                ):
                    next_size = 100 if current_page_size > 100 else 50
                    factor = current_page_size // next_size
                    page = (page - 1) * max(1, factor) + 1
                    current_page_size = next_size
                    logger.warning(
                        "[LABOR_SCAN] L%d tribunal=%s HTTP %d; reduzindo para itensPorPagina=%d na pagina %d.",
                        cur_layer, sigla, resp.status_code, current_page_size, page
                    )
                    continue

                pages_read += 1
                err_streak += 1
                logger.warning(
                    "[LABOR_SCAN] L%d tribunal=%s pagina=%d status_http=%d streak=%d/3",
                    cur_layer, sigla, page, resp.status_code, err_streak
                )
                if err_streak >= 3:
                    if sigla not in partials:
                        partials.append(sigla)
                    _advance_court()
                    continue
                if time_budget_seconds > 0 and (time.time() - batch_start) >= time_budget_seconds:
                    had_http_error = True
                    retry_after = 3
                    motivo = f"HTTP_ERROR_{resp.status_code}"
                    break
                continue

            err_streak = 0
            pages_read += 1

            try:
                payload = resp.json()
            except Exception as json_exc:
                had_http_error = True
                retry_after = 3
                motivo = f"EXCECAO_{type(json_exc).__name__}"
                break

            if isinstance(payload, dict):
                raw_items = payload.get("items") or payload.get("itens") or payload.get("content") or []
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raw_items = []

            if not raw_items:
                _advance_court()
                continue

            current_comm_ids = {self._communication_identity(it) for it in raw_items if isinstance(it, dict)}
            current_comm_ids.discard("")
            sig = _page_signature(current_comm_ids)
            if current_comm_ids and sig in seen_signatures:
                _advance_court()
                continue
            seen_signatures.add(sig)
            last_signature = sig

            new_comm_ids = current_comm_ids - seen_comm_ids
            seen_comm_ids.update(current_comm_ids)
            if current_comm_ids and not new_comm_ids:
                consecutive_no_new_comm += 1
                if consecutive_no_new_comm >= 3:
                    _advance_court()
                    continue
            else:
                consecutive_no_new_comm = 0

            page_processes: Dict[str, Any] = {}
            self._parse_comunica_items(
                raw_items,
                page_processes,
                discovered_entities,
                is_tst_override=(sigla == "TST"),
            )
            page_cnjs = set(page_processes.keys())
            new_cnjs = page_cnjs - known_cnjs_set
            known_cnjs_set.update(new_cnjs)

            if on_batch_persist and page_processes:
                try:
                    on_batch_persist(page, page_processes, len(new_cnjs))
                except Exception as persist_err:
                    logger.error("[LABOR_SCAN] Erro em on_batch_persist %s p=%d: %s", sigla, page, persist_err)

            del page_processes
            if do_gc:
                gc.collect()

            raw_count = payload.get("count") if isinstance(payload, dict) else None
            count_is_truncated = isinstance(raw_count, int) and raw_count >= 10000
            total_court_pages = _total_pages_from_payload(payload, current_page_size)
            court_finished = len(raw_items) < 50 or (
                total_court_pages is not None
                and page >= total_court_pages
                and (not count_is_truncated or len(raw_items) < current_page_size)
            )

            if court_finished:
                _advance_court()
            elif effective_max_per_court > 0 and page >= effective_max_per_court:
                if sigla not in partials:
                    partials.append(sigla)
                _advance_court()
            elif cur_layer == 1 and layer1_max_pages > 0 and page >= layer1_max_pages:
                # Camada 1 concluída para este tribunal: agenda o aprofundamento na Camada 2
                pending_l2.append({"court": sigla, "page": page + 1})
                _advance_court()
            else:
                page += 1
                if cur_layer == 2 and idx < len(pending_l2):
                    pending_l2[idx]["page"] = page

            if effective_delay > 0:
                time.sleep(effective_delay)
            else:
                _adaptive_rate_limit_pause(resp)

        all_courts_done = (
            not rate_limited
            and not had_http_error
            and (
                (cur_layer == 1 and idx >= total_courts and not pending_l2)
                or (cur_layer == 2 and idx >= len(pending_l2))
            )
        )
        is_completed = all_courts_done and not partials
        is_partial_done = all_courts_done and bool(partials)
        if not motivo:
            if is_completed:
                motivo = "TODOS_25_TRIBUNAIS_CONCLUIDOS"
            elif is_partial_done:
                motivo = "TRIBUNAIS_CONCLUIDOS_COM_PARCIAIS"
            else:
                motivo = "LOTE_CURTO_CONCLUIDO"

        if cur_layer == 1 and idx < total_courts:
            current_court = courts_queue[idx]
            courts_completed = idx
        elif cur_layer == 2 and idx < len(pending_l2):
            current_court = pending_l2[idx]["court"]
            courts_completed = max(0, total_courts - (len(pending_l2) - idx))
        else:
            current_court = None
            courts_completed = total_courts

        return {
            "layer": cur_layer,
            "layer2_queue": pending_l2,
            "court_index": idx,
            "courts_completed": min(courts_completed, total_courts),
            "total_courts": total_courts,
            "current_court": current_court,
            "next_page": None if all_courts_done else page,
            "consecutive_court_errors": err_streak,
            "partial_courts": partials,
            "is_completed": is_completed,
            "is_partial_done": is_partial_done,
            "has_more": not all_courts_done,
            "rate_limited": rate_limited,
            "had_http_error": had_http_error,
            "retry_after_seconds": retry_after,
            "last_page_signature": last_signature,
            "pages_read": pages_read,
            "total_processes": len(known_cnjs_set),
            "motivo_encerramento": motivo,
        }

    @staticmethod
    def _communication_identity(item: Dict[str, Any]) -> str:
        """
        Identidade estável de uma comunicação do Comunica PJe.
        Usa o ID oficial quando existe; sem ele, combina processo + data + trecho do texto.
        Nunca usa só o número do processo: intimações diferentes do mesmo processo
        precisam ser vistas como inéditas, senão a trava anti-loop corta empresas grandes.
        """
        cid = item.get("id") or item.get("idComunicacao") or item.get("hash")
        if cid:
            return f"id:{cid}"
        numero = item.get("numeroProcesso") or item.get("numero_processo") or item.get("numeroprocessocommascara") or ""
        data = item.get("data_disponibilizacao") or item.get("dataDisponibilizacao") or ""
        texto = str(item.get("texto") or item.get("teor") or "")[:300]
        if not (numero or texto):
            return ""
        digest = hashlib.sha1(f"{numero}|{data}|{texto}".encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()
        return f"h:{digest}"

    def fetch_brasil_api_fast(self, cnpj: str, timeout: float = 3.5) -> Dict[str, Any]:
        """Consulta ultra-rápida de CNPJ na BrasilAPI com timeout restrito de 3.5s."""
        clean = re.sub(r"\D", "", str(cnpj or ""))
        if len(clean) != 14:
            return {}
        try:
            r = self.session.get(f"https://brasilapi.com.br/api/cnpj/v1/{clean}", timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception as exc:
            logger.debug("BrasilAPI rápida tratada para CNPJ %s: %s", clean, exc)
        return {}

    def fetch_comunica_pje(self, company_name: str, max_pages: int = 2) -> Dict[str, Any]:
        """
        Busca rápida no Comunica PJe (DJEN) respeitando o limite estrito de itensPorPagina <= 50.
        Retorna dicionário indexado pelo número CNJ de 20 dígitos.
        """
        processes: Dict[str, Any] = {}
        search_terms = [company_name.strip()]
        cleaned = self.clean_company_name(company_name)
        if cleaned.upper() not in [t.upper() for t in search_terms]:
            search_terms.append(cleaned)
        brand_term = self.extract_brand_search_term(company_name)
        if brand_term and brand_term.upper() not in [t.upper() for t in search_terms]:
            search_terms.append(brand_term)

        for term in search_terms:
            for page in range(1, max_pages + 1):
                params = {
                    "nomeParte": term,
                    "pagina": page,
                    "itensPorPagina": 50,  # NUNCA passar de 50! Limite oficial da API.
                }
                try:
                    resp = self._request_gov("GET", self.COMUNICA_URL, params=params, timeout=20)
                    if resp.status_code != 200:
                        logger.warning("Comunica PJe HTTP %s para termo '%s'", resp.status_code, term)
                        break

                    payload = resp.json()
                    items = payload if isinstance(payload, list) else (
                        payload.get("items") or payload.get("itens") or payload.get("content") or []
                    )
                    if not items:
                        break

                    self._parse_comunica_items(items, processes)
                    if len(items) < 50:
                        break
                except Exception as exc:
                    logger.error("Erro ao consultar Comunica PJe: %s", exc)
                    break

            if processes:
                break

        # Varredura complementar no TST
        try:
            params_tst = {
                "nomeParte": search_terms[0],
                "siglaTribunal": "TST",
                "pagina": 1,
                "itensPorPagina": 50,
            }
            resp_tst = self._request_gov("GET", self.COMUNICA_URL, params=params_tst, timeout=10)
            if resp_tst.ok:
                payload_tst = resp_tst.json()
                tst_items = payload_tst if isinstance(payload_tst, list) else (
                    payload_tst.get("items") or payload_tst.get("itens") or []
                )
                self._parse_comunica_items(tst_items, processes, is_tst_override=True)
        except Exception as exc:
            logger.debug("Consulta ao TST no Comunica PJe tratada silenciosamente: %s", exc)

        return processes

    def fetch_comunica_pje_all_branches(
        self,
        brand_term: str,
        max_pages: int = SYNC_SCAN_MAX_PAGES,
        time_budget_seconds: float = SYNC_SCAN_TIME_BUDGET_SECONDS,
        max_records: int = SYNC_SCAN_MAX_RECORDS,
    ) -> Tuple[Dict[str, Any], Set[str]]:
        """
        Varre até 15 páginas (ou até 1.500 processos / orçamento máximo de 50s) no Comunica PJe
        usando a Marca/Nome Raiz da empresa, descartando HTML bruto no parse para manter RAM < 380 MB.
        """
        processes: Dict[str, Any] = {}
        discovered_entities: Set[str] = set()
        clean_brand = brand_term.strip()
        started_at = time.monotonic()
        truncated = False
        stop_reason: Optional[str] = None

        params_p1 = {
            "nomeParte": clean_brand,
            "pagina": 1,
            "itensPorPagina": 50,
        }
        total_count = None
        has_more = False
        try:
            resp = self._request_gov("GET", self.COMUNICA_URL, params=params_p1, timeout=5.0)
            if resp.ok:
                payload = resp.json()
                if isinstance(payload, dict):
                    total_count = payload.get("count") or payload.get("total")
                    items = payload.get("items") or payload.get("itens") or payload.get("content") or []
                elif isinstance(payload, list):
                    items = payload
                else:
                    items = []
                self._parse_comunica_items(items, processes, discovered_entities, max_records=max_records)
                has_more = len(items) == 50
                del items, payload
        except Exception as exc:
            logger.error("Erro na página 1 do Deep Scan Comunica PJe: %s", exc)

        if total_count and isinstance(total_count, int) and total_count > 0:
            calc_pages = (total_count + 49) // 50
            target_pages = min(max_pages, calc_pages)
            if calc_pages > max_pages:
                truncated = True
                stop_reason = "MAX_PAGES"
        else:
            target_pages = max_pages if has_more else 1

        if target_pages >= 2 and (not max_records or len(processes) < max_records):
            def _fetch_single_page(p_num: int) -> List[Any]:
                if time_budget_seconds and (time.monotonic() - started_at) >= time_budget_seconds:
                    return []
                p = {"nomeParte": clean_brand, "pagina": p_num, "itensPorPagina": 50}
                try:
                    r = self._request_gov("GET", self.COMUNICA_URL, params=p, timeout=5.0)
                    if r.ok:
                        py = r.json()
                        return py if isinstance(py, list) else (
                            py.get("items") or py.get("itens") or py.get("content") or []
                        )
                except Exception as e:
                    logger.debug("Erro ao paginar Comunica PJe página %d: %s", p_num, e)
                return []

            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {executor.submit(_fetch_single_page, p): p for p in range(2, target_pages + 1)}
                for f in as_completed(futures):
                    if time_budget_seconds and (time.monotonic() - started_at) >= time_budget_seconds:
                        truncated = True
                        stop_reason = "TIME_BUDGET"
                        for pending in futures:
                            pending.cancel()
                        break
                    page_items = f.result()
                    if page_items:
                        self._parse_comunica_items(
                            page_items,
                            processes,
                            discovered_entities,
                            max_records=max_records,
                        )
                        del page_items
                    if max_records and len(processes) >= max_records:
                        truncated = True
                        stop_reason = "MAX_RECORDS"
                        for pending in futures:
                            pending.cancel()
                        break

        remaining_budget = (
            time_budget_seconds - (time.monotonic() - started_at)
            if time_budget_seconds
            else 5.0
        )
        if remaining_budget > 2.0 and (not max_records or len(processes) < max_records):
            try:
                params_tst = {
                    "nomeParte": clean_brand,
                    "siglaTribunal": "TST",
                    "pagina": 1,
                    "itensPorPagina": 50,
                }
                resp_tst = self._request_gov("GET", self.COMUNICA_URL, params=params_tst, timeout=min(5.0, remaining_budget))
                if resp_tst.ok:
                    py_tst = resp_tst.json()
                    tst_items = py_tst if isinstance(py_tst, list) else (
                        py_tst.get("items") or py_tst.get("itens") or []
                    )
                    self._parse_comunica_items(
                        tst_items,
                        processes,
                        discovered_entities,
                        is_tst_override=True,
                        max_records=max_records,
                    )
            except Exception as exc:
                logger.debug("Consulta complementar TST tratada silenciosamente: %s", exc)

        gc.collect()
        self.last_scan_truncated = truncated
        self.last_stop_reason = stop_reason
        logger.info(
            "fetch_comunica_pje_all_branches('%s') concluiu em %.2fs: %d processos únicos, %d entidades, truncated=%s (%s).",
            clean_brand, time.monotonic() - started_at, len(processes), len(discovered_entities), truncated, stop_reason,
        )
        return processes, discovered_entities

    def enrich_with_datajud(self, comunica_processes: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Agrupa os números CNJ por tribunal e faz busca em lote (`terms`) no DataJud.
        Combina os metadados do DataJud com os textos já capturados no Comunica PJe.
        """
        by_alias = defaultdict(list)
        for cnj, info in comunica_processes.items():
            alias = self.resolve_datajud_alias(cnj, info.get("siglaTribunal", ""))
            by_alias[alias].append(cnj)

        datajud_results = {}
        headers = {"Content-Type": "application/json"}
        if self.datajud_key:
            headers["Authorization"] = f"APIKey {self.datajud_key}"

        for alias, cnj_list in by_alias.items():
            url = f"{self.DATAJUD_BASE}/{alias}/_search"
            # Lotes de até 50 processos por requisição
            for i in range(0, len(cnj_list), 50):
                batch = cnj_list[i:i + 50]
                body = {
                    "size": len(batch),
                    "_source": ["numeroProcesso", "tribunal", "nivelSigilo", "assuntos", "movimentos.codigo", "grau", "dataAjuizamento", "valorCausa"],
                    "query": {
                        "terms": {
                            "numeroProcesso": batch
                        }
                    }
                }
                try:
                    resp = self._request_gov("POST", url, json_body=body, headers=headers, timeout=6.0)
                    if resp.status_code == 200:
                        hits = resp.json().get("hits", {}).get("hits", [])
                        for hit in hits:
                            src = hit.get("_source", {})
                            num = self.normalize_cnj(src.get("numeroProcesso"))
                            if num:
                                datajud_results[num] = src
                    else:
                        logger.warning("DataJud %s HTTP %s: %s", alias, resp.status_code, resp.text[:200])
                except Exception as exc:
                    logger.error("Erro no DataJud (%s): %s", alias, exc)

        # Consolida Comunica PJe + DataJud
        consolidated = []
        for cnj, com_data in comunica_processes.items():
            dj = datajud_results.get(cnj, {})
            consolidated.append(self._merge_process_data(cnj, com_data, dj))

        return consolidated

    def enrich_top_tribunals_datajud(
        self,
        all_processes: Dict[str, Any],
        max_tribunals: int = 3,
        timeout: float = 6.0,
    ) -> List[Dict[str, Any]]:
        """
        Enriquecimento com Time-Boxing rígido no DataJud:
        1. Agrupa os processos por tribunal (alias).
        2. Seleciona apenas os Top N tribunais com maior volume.
        3. Consulta os Top tribunais em paralelo com timeout estrito de 6s e _source filtrado.
        4. Mescla todos os processos com _merge_process_data_fast em microssegundos.
        """
        by_alias = defaultdict(list)
        for cnj, info in all_processes.items():
            sigla = info.get("siglaTribunal", "") if isinstance(info, dict) else ""
            alias = self.resolve_datajud_alias(cnj, sigla)
            by_alias[alias].append(cnj)

        sorted_tribunals = sorted(by_alias.items(), key=lambda item: len(item[1]), reverse=True)
        top_tribunals = sorted_tribunals[:max_tribunals]

        datajud_results = {}
        headers = {"Content-Type": "application/json"}
        if self.datajud_key:
            headers["Authorization"] = f"APIKey {self.datajud_key}"

        def _fetch_alias_batch(alias: str, cnjs: List[str]) -> List[Dict[str, Any]]:
            url = f"{self.DATAJUD_BASE}/{alias}/_search"
            batch = cnjs[:50]
            body = {
                "size": len(batch),
                "_source": ["numeroProcesso", "tribunal", "nivelSigilo", "assuntos", "movimentos.codigo", "grau", "dataAjuizamento", "valorCausa"],
                "query": {
                    "terms": {
                        "numeroProcesso": batch
                    }
                }
            }
            try:
                resp = self._request_gov("POST", url, json_body=body, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json().get("hits", {}).get("hits", [])
            except Exception as exc:
                logger.debug("DataJud amostragem rápida (%s) tratada: %s", alias, exc)
            return []

        if top_tribunals:
            with ThreadPoolExecutor(max_workers=min(len(top_tribunals), 4)) as pool:
                futures = {
                    pool.submit(_fetch_alias_batch, alias, cnj_list): alias
                    for alias, cnj_list in top_tribunals
                }
                for fut in as_completed(futures):
                    try:
                        hits = fut.result() or []
                        for hit in hits:
                            src = hit.get("_source", {})
                            num = self.normalize_cnj(src.get("numeroProcesso"))
                            if num:
                                datajud_results[num] = src
                    except Exception:
                        pass

        consolidated = [
            self._merge_process_data_fast(cnj, com_data if isinstance(com_data, dict) else {}, datajud_results.get(cnj))
            for cnj, com_data in all_processes.items()
        ]
        return consolidated

    def _merge_process_data_fast(
        self,
        cnj: str,
        com_data: Dict[str, Any],
        dj: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fusão ultra-rápida de processo do Comunica PJe com metadados do DataJud (se disponíveis),
        classificando causa real via extract_real_cause_fast em 0.01ms.
        """
        dj = dj or {}
        raw_texts = com_data.get("publicacoes_texto") or com_data.get("textos_pje") or []
        texto_concat = " ".join(str(t) for t in raw_texts if t).lower()

        # Extração de assuntos existentes do DataJud
        raw_assuntos = dj.get("assuntos", [])
        existing_assuntos = []
        for item in raw_assuntos:
            if isinstance(item, list):
                for sub in item:
                    if isinstance(sub, dict) and sub.get("nome"):
                        existing_assuntos.append({"codigo": sub.get("codigo"), "nome": sub.get("nome")})
            elif isinstance(item, dict) and item.get("nome"):
                existing_assuntos.append({"codigo": item.get("codigo"), "nome": item.get("nome")})

        classes_list = com_data.get("classes_pje", []) or []
        classe_str = " ".join(str(c) for c in classes_list).strip()

        # Classificação instantânea de causa real
        assuntos = extract_real_cause_fast(existing_assuntos, raw_texts, classes_pje=classes_list)
        subjects_names = [a["nome"] for a in assuntos if isinstance(a, dict) and a.get("nome")]
        primary_name = subjects_names[0] if subjects_names else "Obrigação / Reclamação em Apuração Inicial"
        primary_code = assuntos[0].get("codigo") if assuntos and isinstance(assuntos[0], dict) else None

        # Classificação de status/desfecho (DataJud TPU, Classe Processual, Assuntos e Sinais Textuais)
        movimentos = dj.get("movimentos", []) or []
        codigos_mov = {m.get("codigo") for m in movimentos if isinstance(m, dict)}

        status = classify_process_outcome(codigos_mov, subjects_names, texto_concat, classe_str)

        sentence_outcome = "em_aberto"
        sub_outcome = None
        if status == "CONDENACAO":
            sentence_outcome = "condenacoes"
            sub_outcome = "procedencia_parcial" if (221 in codigos_mov or "procedente em parte" in texto_concat or "parcialmente procedente" in texto_concat) else "procedencia_total"
        elif status == "ACORDO":
            sentence_outcome = "acordos"
            sub_outcome = "acordo_homologado"
        elif status == "IMPROCEDENTE":
            sentence_outcome = "improcedentes"
            sub_outcome = "improcedencia"
        else:
            sentence_outcome = "em_aberto"
            sub_outcome = None

        tem_pericia = bool({581, 11024} & codigos_mov) or any(w in texto_concat for w in ["laudo pericial", "perícia", "pericia", "perito"])
        tem_revelia = any(w in texto_concat for w in ["revelia", "confissão ficta", "confissao ficta"])

        classes_concat = " ".join(str(c) for c in com_data.get("classes_pje", [])).lower()
        is_tst = (
            com_data.get("is_tst_recurso", False)
            or (com_data.get("siglaTribunal") or "").lower() == "tst"
            or "tst" in texto_concat
            or "recurso de revista" in texto_concat
            or "airr" in texto_concat
            or "recurso de revista" in classes_concat
        )
        grau_val = "TST (Recursal)" if is_tst else dj.get("grau", "G1")
        sigilo_val = int(dj.get("nivelSigilo", 0))

        # Metadados judiciais: data de distribuição, valor da causa e tribunal canônico
        raw_dist = dj.get("dataAjuizamento") or com_data.get("dataUltimaPublicacao") or com_data.get("data_disponibilizacao")
        data_distribuicao = parse_iso_date(raw_dist, text_fallback=texto_concat)

        raw_val = dj.get("valorCausa")
        valor_causa = parse_currency_value(raw_val, text_fallback=texto_concat)

        raw_trib = dj.get("tribunal") or com_data.get("siglaTribunal") or com_data.get("tribunal")
        canonical_tribunal = normalize_court_code(raw_trib, cnj=cnj)

        resumo_pub = _strip_html_to_text((raw_texts or [""])[0], max_len=500)
        compact_movs = [
            {"codigo": m.get("codigo"), **({"dataHora": m["dataHora"]} if m.get("dataHora") else {})}
            for m in movimentos
            if isinstance(m, dict) and m.get("codigo") is not None
        ]

        return {
            "numeroProcesso": cnj,
            "numero_processo": cnj,
            "tribunal": canonical_tribunal or dj.get("tribunal") or com_data.get("siglaTribunal", "N/A"),
            "data_distribuicao": data_distribuicao,
            "valor_causa": valor_causa,
            "grau": grau_val,
            "is_tst_recurso": is_tst,
            "nivelSigilo": sigilo_val,
            "nivel_sigilo": sigilo_val,
            "dataAjuizamento": dj.get("dataAjuizamento") or com_data.get("dataUltimaPublicacao", ""),
            "dataHoraUltimaAtualizacao": dj.get("dataHoraUltimaAtualizacao") or com_data.get("dataUltimaPublicacao", ""),
            "assuntos": assuntos,
            "subjects": subjects_names,
            "primary_subject": primary_name,
            "primary_subject_code": primary_code,
            "statusDesfecho": status,
            "sentence_outcome": sentence_outcome,
            "sentence_sub_outcome": sub_outcome,
            "has_expert_report": tem_pericia,
            "observation": f"Assunto identificado via publicação PJe: '{primary_name}'." if primary_name else None,
            "sinaisInstrucao": {
                "possuiPericiaOuLaudo": tem_pericia,
                "possuiSinalRevelia": tem_revelia,
                "encontradoNoDatajud": bool(dj),
            },
            # Descarta o campo texto HTML bruto imediatamente após extrair os indicadores/resumo (RAM < 380 MB)
            "publicacoes_texto": [],
            "destinatarios_nomes": com_data.get("destinatarios_nomes", []),
            "cnpjs_encontrados": com_data.get("cnpjs_encontrados", []),
            "classes_pje": classes_list,
            "resumoPublicacao": resumo_pub,
            "raw_metadata": dj if dj else None,
            "movimentos": compact_movs,
        }

    def _merge_process_data(self, cnj: str, com_data: Dict[str, Any], dj: Dict[str, Any]) -> Dict[str, Any]:
        """Mescla dados do Comunica PJe com os metadados do DataJud via pipeline ultra-rápido."""
        return self._merge_process_data_fast(cnj, com_data, dj)

    @staticmethod
    def _infer_subjects_from_text(texto: str) -> List[Dict[str, Any]]:
        """Infere o motivo do processo pelo texto da publicação utilizando extract_real_cause_fast."""
        return extract_real_cause_fast([], [texto])


def detect_subsidiary_liability_in_processes(
    processes: List[Dict[str, Any]],
    evaluated_company: str = "",
    evaluated_cnpj: str = "",
) -> Dict[str, Any]:
    """Re-exportação utilitária de detecção de responsabilidade subsidiária (US-19)."""
    from app.services.ai_analyzer import detect_subsidiary_liability_in_processes as _detect
    return _detect(processes, evaluated_company=evaluated_company, evaluated_cnpj=evaluated_cnpj)
