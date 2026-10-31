#!/usr/bin/env python3
"""Interactive setup for the Layer B rewrite backend (run via setup.cmd).

Asks for a provider preset (local or online), a model (listed from the
provider's API when possible), the API key (hidden input, never echoed or
logged), and a rewrite profile; tests the connection with one tiny call;
then updates only its own keys in .env, keeping every other line.

Exit codes: 0 saved, 1 aborted/failed, 10 saved and the user wants the
service started now (setup.cmd acts on it).
"""

from __future__ import annotations

import argparse
import getpass
import json
import locale
import os
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import safe_write_text

ROOT = Path(__file__).resolve().parents[2]
EXIT_START_SERVICE = 10


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    backend: str
    base_url: str | None  # None: ask the user
    remote: bool
    needs_key: bool | None  # None: optional
    default_model: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


PRESETS: list[Preset] = [
    Preset(
        "deepseek",
        "DeepSeek",
        "openai-compatible",
        "https://api.deepseek.com",
        True,
        True,
        "deepseek-flash",
        # Reasoning models otherwise spend minutes thinking per rewrite.
        {"WATERMARKS_REWRITE_REASONING_EFFORT": "none"},
    ),
    Preset("openai", "OpenAI", "openai-compatible", "https://api.openai.com", True, True),
    Preset(
        "openrouter", "OpenRouter", "openai-compatible", "https://openrouter.ai/api", True, True
    ),
    Preset("groq", "Groq", "openai-compatible", "https://api.groq.com/openai", True, True),
    Preset("ollama", "Ollama", "ollama", "http://127.0.0.1:11434", False, False, "qwen2.5:7b"),
    Preset("lmstudio", "LM Studio", "openai-compatible", "http://127.0.0.1:1234", False, False),
    Preset("custom", "custom", "openai-compatible", None, True, None),
]


@dataclass(frozen=True)
class Profile:
    key: str
    strategy: str
    temperature: str
    verify: bool  # fidelity check after the rewrite (see WATERMARKS_REWRITE_VERIFY)


PROFILES: list[Profile] = [
    Profile("precise", "paraphrase@0.8", "0.3", True),
    Profile("balanced", "paraphrase@0.8,humanize@0.5", "0.5", True),
    Profile("aggressive", "paraphrase@0.8,mlm@0.2", "0.9", False),
]

# Every key this wizard owns in .env. Keys missing from a result are removed,
# so switching from an online provider to a local one drops the API key.
MANAGED_KEYS = (
    "WATERMARKS_REWRITE_BACKEND",
    "WATERMARKS_REWRITE_MODEL",
    "WATERMARKS_REWRITE_BASE_URL",
    "WATERMARKS_REWRITE_API_KEY",
    "WATERMARKS_REWRITE_ALLOW_REMOTE",
    "WATERMARKS_REWRITE_REASONING_EFFORT",
    "WATERMARKS_REWRITE_TEMPERATURE",
    "WATERMARKS_REWRITE_VERIFY",
    "WATERMARKS_CLEAN_STRATEGY",
)

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "title": "watermarks-remover setup: text rewrite (Layer B) provider",
        "provider": "Choose the provider",
        "local": "local",
        "online": "online",
        "custom": "Other OpenAI-compatible API",
        "base_url": "API base URL (e.g. https://api.example.com)",
        "key": "API key (hidden; Enter keeps the saved key)",
        "key_optional": "API key, if the server needs one (hidden; Enter for none)",
        "key_required": "An API key is required for this provider.",
        "listing": "Fetching the model list...",
        "list_failed": "Could not list models ({err}).",
        "model": "Choose the model (Enter = {default})",
        "model_type": "Model name",
        "profile": "Choose the rewrite profile",
        "precise": "Precise (recommended): one careful paraphrase + fidelity check",
        "balanced": "Balanced: paraphrase + humanize + fidelity check",
        "aggressive": "Aggressive: paraphrase + mlm word swaps (English only, no check)",
        "testing": "Testing the connection...",
        "test_ok": "Connection OK.",
        "test_failed": "Connection failed: {err}",
        "save_anyway": "Save anyway? [y/N]",
        "saved": "Saved to {path}.",
        "start_now": "Start the service now? [Y/n]",
        "aborted": "Setup aborted; nothing was saved.",
        "invalid": "Invalid choice, try again.",
        "origin_note": (
            "Tip: use a provider different from the model that wrote the text "
            "(e.g. not Claude for Claude text)."
        ),
    },
    "pt": {
        "title": "Setup do watermarks-remover: provedor de reescrita de texto (Layer B)",
        "provider": "Escolha o provedor",
        "local": "local",
        "online": "online",
        "custom": "Outra API compatível com OpenAI",
        "base_url": "URL base da API (ex.: https://api.exemplo.com)",
        "key": "Chave da API (oculta; Enter mantém a chave salva)",
        "key_optional": "Chave da API, se o servidor exigir (oculta; Enter para nenhuma)",
        "key_required": "Este provedor exige uma chave de API.",
        "listing": "Buscando a lista de modelos...",
        "list_failed": "Não foi possível listar os modelos ({err}).",
        "model": "Escolha o modelo (Enter = {default})",
        "model_type": "Nome do modelo",
        "profile": "Escolha o perfil de reescrita",
        "precise": "Preciso (recomendado): uma paráfrase cuidadosa + verificação de fidelidade",
        "balanced": "Equilibrado: paráfrase + humanização + verificação de fidelidade",
        "aggressive": "Agressivo: paráfrase + troca de palavras mlm (só inglês, sem verificação)",
        "testing": "Testando a conexão...",
        "test_ok": "Conexão OK.",
        "test_failed": "A conexão falhou: {err}",
        "save_anyway": "Salvar mesmo assim? [s/N]",
        "saved": "Salvo em {path}.",
        "start_now": "Iniciar o serviço agora? [S/n]",
        "aborted": "Setup cancelado; nada foi salvo.",
        "invalid": "Opção inválida, tente de novo.",
        "origin_note": (
            "Dica: use um provedor diferente do modelo que escreveu o texto "
            "(ex.: não use Claude para texto do Claude)."
        ),
    },
}


def detect_lang() -> str:
    """'pt' for Portuguese locales (or WATERMARKS_LANG=pt), else 'en'."""
    forced = os.environ.get("WATERMARKS_LANG", "").lower()
    if forced in MESSAGES:
        return forced
    name = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
    return "pt" if name.startswith(("pt", "portuguese")) else "en"


# --- .env --------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """Active KEY=VALUE pairs of a dotenv file (comments and blanks skipped)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def update_env_text(text: str, updates: dict[str, str | None]) -> str:
    """Set/remove keys in dotenv *text*, keeping comments and other lines.

    The first active line of a key is replaced in place, later duplicates are
    dropped, a None value removes the key, and new keys are appended at the end.
    """
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in updates and not stripped.startswith("#"):
            if key not in seen and updates[key] is not None:
                out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
        out.append(line)
    missing = [k for k, v in updates.items() if v is not None and k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# Layer B rewrite provider (written by setup.cmd)")
        out.extend(f"{k}={updates[k]}" for k in missing)
    return "\n".join(out) + "\n"


def build_env_updates(
    preset: Preset, base_url: str, model: str, api_key: str | None, profile: Profile
) -> dict[str, str | None]:
    updates: dict[str, str | None] = dict.fromkeys(MANAGED_KEYS)
    updates.update(
        {
            "WATERMARKS_REWRITE_BACKEND": preset.backend,
            "WATERMARKS_REWRITE_MODEL": model,
            "WATERMARKS_REWRITE_BASE_URL": base_url,
            "WATERMARKS_REWRITE_TEMPERATURE": profile.temperature,
            "WATERMARKS_CLEAN_STRATEGY": profile.strategy,
            "WATERMARKS_REWRITE_VERIFY": "1" if profile.verify else None,
        }
    )
    if api_key:
        updates["WATERMARKS_REWRITE_API_KEY"] = api_key
    if not _is_loopback(base_url):
        updates["WATERMARKS_REWRITE_ALLOW_REMOTE"] = "1"
    updates.update(preset.extra)
    return updates


def _is_loopback(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


# --- provider calls ----------------------------------------------------------


def _get_json(url: str, api_key: str | None, timeout: float = 15) -> Any:
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) URL: {url}")
    from rewrite_text import _NoRedirect

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (scheme checked)
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_models(backend: str, base_url: str, api_key: str | None) -> list[str]:
    base = base_url.rstrip("/")
    if backend == "ollama":
        data = _get_json(f"{base}/api/tags", None)
        return sorted(m["name"] for m in data.get("models", []))
    data = _get_json(f"{base}/v1/models", api_key)
    return sorted(m["id"] for m in data.get("data", []))


def test_connection(
    backend: str, base_url: str, model: str, api_key: str | None, extra: dict[str, str]
) -> None:
    """One tiny generation through the same code path /clean uses; raises on failure."""
    from rewrite_text import _generate_once

    _generate_once(
        backend,
        base_url,
        model,
        api_key,
        "Reply with the single word: OK",
        60,
        0.0,
        extra.get("WATERMARKS_REWRITE_REASONING_EFFORT"),
    )


# --- interactive flow --------------------------------------------------------


class Aborted(Exception):
    pass


def _choose(
    ask: Callable[[str], str], out: Callable[[str], None], msg: dict, prompt: str, n: int
) -> int:
    while True:
        answer = ask(f"{prompt} [1-{n}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= n:
            return int(answer) - 1
        out(msg["invalid"])


def run(
    env_path: Path,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
    lang: str | None = None,
    start_prompt: bool = True,
) -> int:
    msg = MESSAGES[lang or detect_lang()]
    current = read_env(env_path)
    out(msg["title"])
    out("")

    out(msg["provider"] + ":")
    for i, p in enumerate(PRESETS, 1):
        if p.key == "custom":
            out(f"  {i}. {msg['custom']}")
        else:
            out(f"  {i}. {p.label} ({msg['online'] if p.remote else msg['local']})")
    preset = PRESETS[_choose(ask, out, msg, msg["provider"], len(PRESETS))]
    out(msg["origin_note"])

    base_url = preset.base_url
    while not base_url:
        base_url = ask(f"{msg['base_url']}: ").strip().rstrip("/")

    api_key: str | None = None
    if preset.needs_key is not False:
        same_provider = current.get("WATERMARKS_REWRITE_BASE_URL") == base_url
        saved = current.get("WATERMARKS_REWRITE_API_KEY") if same_provider else None
        prompt = msg["key"] if preset.needs_key else msg["key_optional"]
        while True:
            typed = ask_secret(f"{prompt}: ").strip()
            api_key = typed or saved
            if api_key or not preset.needs_key:
                break
            out(msg["key_required"])

    out(msg["listing"])
    try:
        models = list_models(preset.backend, base_url, api_key)
    except Exception as e:  # any network/auth/format error: fall back to typing
        out(msg["list_failed"].format(err=_short(e)))
        models = []
    if models:
        default = preset.default_model if preset.default_model in models else models[0]
        for i, m in enumerate(models, 1):
            out(f"  {i}. {m}{'  *' if m == default else ''}")
        while True:
            answer = ask(msg["model"].format(default=default) + ": ").strip()
            if not answer:
                model = default
                break
            if answer.isdigit() and 1 <= int(answer) <= len(models):
                model = models[int(answer) - 1]
                break
            if answer in models:
                model = answer
                break
            out(msg["invalid"])
    else:
        model = ""
        while not model:
            hint = f" [{preset.default_model}]" if preset.default_model else ""
            model = ask(f"{msg['model_type']}{hint}: ").strip() or (preset.default_model or "")

    out(msg["profile"] + ":")
    for i, prof in enumerate(PROFILES, 1):
        out(f"  {i}. {msg[prof.key]}")
    answer = ask(f"{msg['profile']} [1-{len(PROFILES)}, Enter = 1]: ").strip()
    while answer and not (answer.isdigit() and 1 <= int(answer) <= len(PROFILES)):
        out(msg["invalid"])
        answer = ask(f"{msg['profile']} [1-{len(PROFILES)}, Enter = 1]: ").strip()
    profile = PROFILES[int(answer) - 1 if answer else 0]

    out(msg["testing"])
    try:
        test_connection(preset.backend, base_url, model, api_key, preset.extra)
        out(msg["test_ok"])
    except Exception as e:  # report and let the user decide
        out(msg["test_failed"].format(err=_short(e)))
        if ask(msg["save_anyway"] + " ").strip().lower() not in ("y", "s", "yes", "sim"):
            raise Aborted from e

    original = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    updates = build_env_updates(preset, base_url, model, api_key, profile)
    safe_write_text(env_path, update_env_text(original, updates))
    out(msg["saved"].format(path=env_path))

    if start_prompt and ask(msg["start_now"] + " ").strip().lower() in ("", "y", "s", "yes", "sim"):
        return EXIT_START_SERVICE
    return 0


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", type=Path, default=ROOT / ".env")
    p.add_argument(
        "--no-start-prompt",
        action="store_true",
        help="Do not offer to start the service (used by start-service.cmd)",
    )
    args = p.parse_args()
    lang = detect_lang()
    try:
        return run(args.env_file, lang=lang, start_prompt=not args.no_start_prompt)
    except (Aborted, EOFError, KeyboardInterrupt):
        print()
        print(MESSAGES[lang]["aborted"])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
