#!/usr/bin/env python3
"""
RadarJus - Revisor Automático de PR com Gemini Flash via Descoberta Dinâmica de Modelos.

Este script lê as regras do RadarJus (.github/REVIEW_RULES.md), extrai o diff
apenas de arquivos de código relevantes, aplica truncamento econômico de tokens
(máx 12.000 caracteres) e consulta a API REST do Gemini utilizando descoberta
dinâmica de modelos Flash suportados pela chave de API.
"""

import fnmatch
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_DIFF_CHARS = 12000
OUTPUT_COMMENT_FILE = "ai_review_comment.md"
REVIEW_RULES_FILE = ".github/REVIEW_RULES.md"
COMMENT_HEADER_TAG = "<!-- radarjus-ai-reviewer -->"

FALLBACK_FLASH_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash-8b",
]

# Filtros de inclusão de arquivos de código
CODE_PATTERNS = [
    "app/**/*.py",
    "app/*.py",
    "frontend/src/**/*.js",
    "frontend/src/**/*.jsx",
    "frontend/src/**/*.css",
    "frontend/api/**/*.js",
    "tests/**/*.py",
    "tests/*.py",
]

# Arquivos explicitamente ignorados na revisão
IGNORE_PATTERNS = [
    "*package-lock.json",
    "*.min.js",
    "*.min.css",
    "*.pdf",
    "*.docx",
    "*.xlsx",
    "*.csv",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.svg",
    "*.ico",
    "*.gif",
    "PROOF_OF_AUTHORSHIP.sha256",
    "dist/*",
    "build/*",
]


def match_any(path: str, patterns: list[str]) -> bool:
    """Verifica se o caminho corresponde a algum dos padrões glob."""
    norm_path = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(norm_path, pattern):
            return True
        if pattern.startswith("app/") and norm_path.startswith("app/") and norm_path.endswith(".py"):
            return True
        if (
            pattern.startswith("frontend/src/")
            and norm_path.startswith("frontend/src/")
            and norm_path.endswith((".js", ".jsx", ".css"))
        ):
            return True
        if pattern.startswith("frontend/api/") and norm_path.startswith("frontend/api/") and norm_path.endswith(".js"):
            return True
        if pattern.startswith("tests/") and norm_path.startswith("tests/") and norm_path.endswith(".py"):
            return True
    return False


def get_git_diff_and_files() -> tuple[list[str], str]:
    """Obtém a lista de arquivos alterados e o diff consolidado."""
    base_ref = os.getenv("GITHUB_BASE_REF", "main")

    diff_targets = [
        f"origin/{base_ref}...HEAD",
        f"origin/{base_ref}...",
        "HEAD~1",
        "HEAD",
    ]

    all_files: list[str] = []
    chosen_target = None

    for target in diff_targets:
        try:
            res = subprocess.run(
                ["git", "diff", "--name-only", target],
                capture_output=True,
                text=True,
                check=True,
            )
            raw_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if raw_files:
                all_files = raw_files
                chosen_target = target
                break
        except subprocess.CalledProcessError:
            continue

    if not chosen_target:
        # Fallback para working directory diff se nada no histórico
        try:
            res = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True,
                text=True,
                check=True,
            )
            all_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            chosen_target = "HEAD"
        except (subprocess.CalledProcessError, OSError):
            all_files = []

    # Filtrar apenas arquivos de código permitidos e não ignorados
    filtered_files: list[str] = []
    for f in all_files:
        if match_any(f, IGNORE_PATTERNS):
            continue
        if match_any(f, CODE_PATTERNS):
            filtered_files.append(f)

    if not filtered_files:
        return [], ""

    diff_cmd = ["git", "diff", chosen_target, "--"] + filtered_files
    try:
        diff_res = subprocess.run(
            diff_cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        diff_text = diff_res.stdout
    except subprocess.CalledProcessError as e:
        diff_text = f"Erro ao coletar git diff: {e}"

    return filtered_files, diff_text


def load_review_rules() -> str:
    """Lê o arquivo de regras de revisão do repositório."""
    rules_path = Path(REVIEW_RULES_FILE)
    if rules_path.is_file():
        try:
            return rules_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            return f"# Regras do RadarJus\nErro ao ler {REVIEW_RULES_FILE}: {e}"
    return (
        "# Invariantes do RadarJus\n"
        "- 100% dos processos analisados (sem [:50]).\n"
        "- Regra de Bloqueio com 0 condenações (0.0%).\n"
        "- LGPD: sem nomes/CPFs de pessoas físicas em banco/logs.\n"
        "- Render 512 MB: sem dependências pesadas e requisições com timeout.\n"
        "- Frontend: sourcemap: false e responsivo mobile.\n"
    )


def discover_available_flash_models(api_key: str) -> list[str]:
    """Consulta ListModels na API do Gemini e retorna modelos Flash elegíveis ordenados por relevância."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    req = urllib.request.Request(
        url,
        headers={"x-goog-api-key": api_key},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("models", [])

            scored_flash: list[tuple[int, str]] = []
            other_eligible: list[str] = []

            for m in models:
                raw_name = m.get("name", "")
                model_id = raw_name.replace("models/", "").strip()
                methods = m.get("supportedGenerationMethods", [])

                if "generateContent" not in methods:
                    continue

                lower_id = model_id.lower()
                # Ignorar modelos não textuais ou pesados
                if any(bad in lower_id for bad in ["embedding", "embed", "tts", "image", "imagen", "audio", "aqa"]):
                    continue

                if "flash" in lower_id:
                    score = 0
                    if "lite" in lower_id:
                        score += 10
                    if "2.5" in lower_id:
                        score += 5
                    elif "2.0" in lower_id:
                        score += 4
                    elif "1.5" in lower_id:
                        score += 2
                    scored_flash.append((score, model_id))
                elif "gemini" in lower_id:
                    other_eligible.append(model_id)

            scored_flash.sort(key=lambda x: x[0], reverse=True)
            results = [item[1] for item in scored_flash]
            if not results:
                results = other_eligible

            if results:
                print(f"[INFO] Modelos descobertos automaticamente: {results[:5]}")
                return results
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Falha ao consultar ListModels ({e}). Utilizando lista padrão de fallback.")

    return FALLBACK_FLASH_MODELS


def call_gemini_api(prompt: str, api_key: str) -> tuple[str, str]:
    """Chama a API REST do Gemini testando modelos disponíveis e retorna (texto_revisao, modelo_utilizado)."""
    env_model = os.getenv("GEMINI_MODEL", "").strip()

    if env_model:
        models_to_try = [env_model]
        print(f"[INFO] Usando modelo sobrescrito por GEMINI_MODEL: {env_model}")
    else:
        discovered = discover_available_flash_models(api_key)
        models_to_try = discovered if discovered else FALLBACK_FLASH_MODELS

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048,
        },
    }
    json_data = json.dumps(payload).encode("utf-8")

    last_error = None
    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=json_data,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        try:
            print(f"[INFO] Solicitando revisão ao modelo: {model}...")
            with urllib.request.urlopen(req, timeout=45) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                candidates = resp_json.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"].strip(), model
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code} ({model}): {err_body}"
            print(f"[WARN] Modelo {model} retornou erro {e.code}: {err_body[:200]}")
            if e.code in (404, 429, 503):
                # Tenta automaticamente o próximo modelo disponível
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = str(e)
            print(f"[WARN] Erro de rede com modelo {model}: {e}")
            continue

    raise RuntimeError(f"Falha ao chamar a API do Gemini: {last_error}")


def main() -> int:
    """Execução principal do revisor de PR."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        print("[INFO] GEMINI_API_KEY não configurada nos Secrets. Pulando revisão de IA limpa.")
        content = (
            f"{COMMENT_HEADER_TAG}\n"
            "### 🤖 RadarJus AI Code Reviewer\n\n"
            "> ℹ️ *A variável de ambiente `GEMINI_API_KEY` não está configurada nos segredos deste repositório. "
            "A etapa de revisão automatizada com IA foi concluída com sucesso sem bloqueios.*\n"
        )
        Path(OUTPUT_COMMENT_FILE).write_text(content, encoding="utf-8")
        return 0

    rules_content = load_review_rules()
    changed_files, diff_text = get_git_diff_and_files()

    if not changed_files or not diff_text.strip():
        print("[INFO] Nenhum arquivo de código modificado elegível para revisão.")
        content = (
            f"{COMMENT_HEADER_TAG}\n"
            "### 🤖 RadarJus AI Code Reviewer\n\n"
            "✅ **Nenhum arquivo de código-fonte modificado** neste PR (alterações restritas a documentação, ativos ou configs). "
            "Nenhuma violação das regras de negócio foi detectada.\n"
        )
        Path(OUTPUT_COMMENT_FILE).write_text(content, encoding="utf-8")
        return 0

    total_chars = len(diff_text)
    truncated_msg = ""
    if total_chars > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n\n[... Diff truncado em 12.000 caracteres para economia de tokens ...]"
        truncated_msg = f" (truncado de {total_chars} para {MAX_DIFF_CHARS} caracteres)"

    # Estimativa simples de tokens (1 token ~ 4 caracteres)
    estimated_input_tokens = (len(rules_content) + len(diff_text)) // 4

    files_list_str = "\n".join(f"- `{f}`" for f in changed_files)

    prompt = f"""Você é o Revisor Sênior de Código e Arquiteto de Software do RadarJus.
Sua missão é realizar um Code Review cirúrgico, rigoroso e conciso no seguinte Pull Request, avaliando EXCLUSIVAMENTE conformidade com as regras e integridade do código.

=== REGRAS DE REVISÃO E INVARIANTES DO RADARJUS ===
{rules_content}

=== ARQUIVOS DE CÓDIGO ANALISADOS ({len(changed_files)}) ===
{files_list_str}

=== DIFF DO CÓDIGO{truncated_msg} ===
{diff_text}

=== INSTRUÇÕES DE FORMATAÇÃO ===
Responda em Português do Brasil de forma concisa e direta, estritamente estruturada nestes 3 blocos em markdown:

### 🚨 1. Bloqueadores (Segurança & Invariantes)
(Aponte violações críticas: slices [:50], risco 0% não bloqueado, vazamento LGPD de pessoa física, bypass de sourcemap, consultas sem tenant_id, hardcode de empresas. Se não houver nenhum problema, diga claramente: "Nenhum bloqueador detectado. Invariantes preservadas.")

### ⚡ 2. Alertas de Performance (Render Free 512 MB)
(Aponte riscos de estouro de memória: loops infinitos, chamadas HTTP sem timeout, imports pesados desnecessários, vazamento em cache. Se estiver otimizado, confirme que o consumo está adequado.)

### 💡 3. Sugestões de Melhoria
(Sugestões pontuais de tipagem, clareza ou testes adicionais recomendados.)
"""

    print(f"[INFO] Consultando Gemini para {len(changed_files)} arquivos ({total_chars} chars)...")
    try:
        review_markdown, used_model = call_gemini_api(prompt, api_key)
        final_comment = (
            f"{COMMENT_HEADER_TAG}\n"
            "## 🤖 RadarJus AI Code Reviewer\n\n"
            f"{review_markdown}\n\n"
            f"---\n*Revisão automática gerada com `{used_model}` | ~{estimated_input_tokens} tokens processados | {len(changed_files)} arquivo(s) inspecionado(s)*\n"
        )
        Path(OUTPUT_COMMENT_FILE).write_text(final_comment, encoding="utf-8")
        print(f"[SUCCESS] Revisão salva em {OUTPUT_COMMENT_FILE} com modelo {used_model}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Erro ao chamar Gemini: {e}")
        error_comment = (
            f"{COMMENT_HEADER_TAG}\n"
            "### 🤖 RadarJus AI Code Reviewer\n\n"
            f"⚠️ *Não foi possível completar a revisão via Gemini devido a uma falha na chamada de API: {e}*\n"
            "O pipeline prosseguirá normalmente sem bloquear o pull request.\n"
        )
        Path(OUTPUT_COMMENT_FILE).write_text(error_comment, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
