"""Tests for the interactive Layer B provider setup (setup.cmd)."""

from __future__ import annotations

from pathlib import Path

import pytest
import server
import setup_wizard as wiz
from rewrite_text import parse_strategy

SECRET = "sk-test-secret-123"  # noqa: S105 (fake test credential)


def _preset(key: str) -> wiz.Preset:
    return next(p for p in wiz.PRESETS if p.key == key)


def _profile(key: str) -> wiz.Profile:
    return next(p for p in wiz.PROFILES if p.key == key)


# --- .env merge ----------------------------------------------------------------


def test_update_env_text_replaces_in_place_and_keeps_the_rest():
    original = (
        "# header comment\n"
        "WATERMARKS_SERVER_API_KEY=\n"
        "# WATERMARKS_REWRITE_MODEL=commented-example\n"
        "WATERMARKS_REWRITE_MODEL=old\n"
        "HF_TOKEN=keep-me\n"
        "WATERMARKS_REWRITE_MODEL=duplicate\n"
        "WATERMARKS_REWRITE_API_KEY=old-key\n"
    )
    out = wiz.update_env_text(
        original,
        {
            "WATERMARKS_REWRITE_MODEL": "new",
            "WATERMARKS_REWRITE_API_KEY": None,
            "WATERMARKS_CLEAN_STRATEGY": "paraphrase@0.8",
        },
    )
    lines = out.splitlines()
    assert lines[:5] == [
        "# header comment",
        "WATERMARKS_SERVER_API_KEY=",
        "# WATERMARKS_REWRITE_MODEL=commented-example",
        "WATERMARKS_REWRITE_MODEL=new",
        "HF_TOKEN=keep-me",
    ]
    assert "duplicate" not in out
    assert "WATERMARKS_REWRITE_API_KEY" not in out
    assert lines[-1] == "WATERMARKS_CLEAN_STRATEGY=paraphrase@0.8"


def test_update_env_text_on_empty_file():
    out = wiz.update_env_text("", {"A": "1", "B": None})
    assert out.splitlines()[-1] == "A=1"
    assert "B=" not in out


def test_read_env_parses_active_lines_and_quotes(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text('# c\nA=1\nB="two words"\n\nbroken line\n', encoding="utf-8")
    assert wiz.read_env(env) == {"A": "1", "B": "two words"}


# --- presets and profiles --------------------------------------------------------


def test_online_preset_updates_allow_remote_and_key():
    updates = wiz.build_env_updates(
        _preset("deepseek"), "https://api.deepseek.com", "m", SECRET, _profile("precise")
    )
    assert updates["WATERMARKS_REWRITE_BACKEND"] == "openai-compatible"
    assert updates["WATERMARKS_REWRITE_API_KEY"] == SECRET
    assert updates["WATERMARKS_REWRITE_ALLOW_REMOTE"] == "1"
    assert updates["WATERMARKS_REWRITE_REASONING_EFFORT"] == "none"
    assert updates["WATERMARKS_CLEAN_STRATEGY"] == "paraphrase@0.8"


def test_local_preset_drops_online_only_keys():
    updates = wiz.build_env_updates(
        _preset("ollama"), "http://127.0.0.1:11434", "qwen2.5:7b", None, _profile("precise")
    )
    assert updates["WATERMARKS_REWRITE_BACKEND"] == "ollama"
    for key in (
        "WATERMARKS_REWRITE_API_KEY",
        "WATERMARKS_REWRITE_ALLOW_REMOTE",
        "WATERMARKS_REWRITE_REASONING_EFFORT",
    ):
        assert updates[key] is None


def test_every_profile_strategy_is_valid():
    for profile in wiz.PROFILES:
        # The server accepts every profile strategy, including "none".
        assert server._validate_strategy(profile.strategy) == profile.strategy
        if profile.strategy != server.NO_REWRITE:
            parse_strategy(profile.strategy)
    # Precision first: the default profile never runs the English-only mlm step.
    assert "mlm" not in wiz.PROFILES[0].strategy
    assert wiz.PROFILES[0].key == "precise"


def test_preserve_profile_turns_the_rewrite_off():
    profile = _profile("preserve")
    updates = wiz.build_env_updates(
        _preset("deepseek"), "https://api.deepseek.com", "deepseek-flash", SECRET, profile
    )
    assert updates["WATERMARKS_CLEAN_STRATEGY"] == "none"
    assert updates["WATERMARKS_REWRITE_VERIFY"] is None
    assert updates["WATERMARKS_WITH_MLM"] is None
    # The provider stays configured for per-request rewrites.
    assert updates["WATERMARKS_REWRITE_MODEL"] == "deepseek-flash"


@pytest.mark.parametrize("lang", sorted(wiz.MESSAGES))
def test_every_profile_has_a_label(lang):
    for profile in wiz.PROFILES:
        assert wiz.MESSAGES[lang][profile.key]


def test_presets_cover_local_and_online():
    assert {p.remote for p in wiz.PRESETS} == {True, False}
    assert all(p.base_url is None or p.base_url.startswith("http") for p in wiz.PRESETS)


# --- interactive flow -------------------------------------------------------------


def _scripted(answers: list[str]):
    it = iter(answers)
    return lambda _prompt="": next(it)


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, object] = {}

    def fake_list(backend, base_url, api_key):
        calls["list"] = (backend, base_url, api_key)
        return ["deepseek-flash", "deepseek-v4-pro"]

    def fake_test(backend, base_url, model, api_key, extra):
        calls["test"] = (backend, base_url, model, api_key)

    monkeypatch.setattr(wiz, "list_models", fake_list)
    monkeypatch.setattr(wiz, "test_connection", fake_test)
    return calls


def test_run_deepseek_end_to_end(tmp_path: Path, fake_provider):
    env = tmp_path / ".env"
    env.write_text("HF_TOKEN=keep-me\n", encoding="utf-8")
    printed: list[str] = []
    # provider 1 (DeepSeek), Enter = default model, Enter = precise, n = do not start
    rc = wiz.run(
        env,
        ask=_scripted(["1", "", "", "n"]),
        ask_secret=_scripted([SECRET]),
        out=printed.append,
        lang="en",
    )
    assert rc == 0
    values = wiz.read_env(env)
    assert values["HF_TOKEN"] == "keep-me"  # noqa: S105
    assert values["WATERMARKS_REWRITE_MODEL"] == "deepseek-flash"
    assert values["WATERMARKS_REWRITE_API_KEY"] == SECRET
    assert values["WATERMARKS_CLEAN_STRATEGY"] == "paraphrase@0.8"
    assert fake_provider["test"] == (
        "openai-compatible",
        "https://api.deepseek.com",
        "deepseek-flash",
        SECRET,
    )
    assert not any(SECRET in line for line in printed)


def test_run_enter_keeps_saved_key_and_can_start_service(tmp_path: Path, fake_provider):
    env = tmp_path / ".env"
    env.write_text(
        f"WATERMARKS_REWRITE_BASE_URL=https://api.deepseek.com\nWATERMARKS_REWRITE_API_KEY={SECRET}\n",
        encoding="utf-8",
    )
    # model 2 by number, profile 2, Enter = start the service
    rc = wiz.run(
        env, ask=_scripted(["1", "2", "2", ""]), ask_secret=_scripted([""]), out=print, lang="pt"
    )
    assert rc == wiz.EXIT_START_SERVICE
    values = wiz.read_env(env)
    assert values["WATERMARKS_REWRITE_API_KEY"] == SECRET
    assert values["WATERMARKS_REWRITE_MODEL"] == "deepseek-v4-pro"
    assert values["WATERMARKS_CLEAN_STRATEGY"] == "paraphrase@0.8,humanize@0.5"


def test_run_requires_a_key_for_online_providers(tmp_path: Path, fake_provider):
    printed: list[str] = []
    wiz.run(
        tmp_path / ".env",
        ask=_scripted(["1", "", "", "n"]),
        ask_secret=_scripted(["", SECRET]),
        out=printed.append,
        lang="en",
    )
    assert wiz.MESSAGES["en"]["key_required"] in printed


def test_run_failed_test_and_declined_save_leaves_env_untouched(
    tmp_path: Path, fake_provider, monkeypatch: pytest.MonkeyPatch
):
    def boom(*_a):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(wiz, "test_connection", boom)
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    with pytest.raises(wiz.Aborted):
        wiz.run(env, ask=_scripted(["1", "", "", "n"]), ask_secret=_scripted([SECRET]), lang="en")
    assert env.read_text(encoding="utf-8") == "A=1\n"


def test_run_falls_back_to_typed_model_when_listing_fails(
    tmp_path: Path, fake_provider, monkeypatch: pytest.MonkeyPatch
):
    def no_list(*_a):
        raise OSError("connection refused")

    monkeypatch.setattr(wiz, "list_models", no_list)
    env = tmp_path / ".env"
    # provider 5 (Ollama, no key), Enter = preset default model, profile 1, no start
    rc = wiz.run(env, ask=_scripted(["5", "", "1", "n"]), ask_secret=_scripted([]), lang="en")
    assert rc == 0
    values = wiz.read_env(env)
    assert values["WATERMARKS_REWRITE_BACKEND"] == "ollama"
    assert values["WATERMARKS_REWRITE_MODEL"] == "qwen2.5:7b"
    assert "WATERMARKS_REWRITE_API_KEY" not in values


def test_run_custom_provider_asks_for_base_url(tmp_path: Path, fake_provider):
    env = tmp_path / ".env"
    custom = str(len(wiz.PRESETS))
    rc = wiz.run(
        env,
        ask=_scripted([custom, "https://llm.example.test/", "", "", "n"]),
        ask_secret=_scripted([""]),
        lang="en",
    )
    assert rc == 0
    values = wiz.read_env(env)
    assert values["WATERMARKS_REWRITE_BASE_URL"] == "https://llm.example.test"
    assert values["WATERMARKS_REWRITE_ALLOW_REMOTE"] == "1"


def test_detect_lang(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WATERMARKS_LANG", "pt")
    assert wiz.detect_lang() == "pt"
    monkeypatch.setenv("WATERMARKS_LANG", "en")
    assert wiz.detect_lang() == "en"


def test_messages_have_the_same_keys_in_every_language():
    keys = [set(m) for m in wiz.MESSAGES.values()]
    assert all(k == keys[0] for k in keys)


# --- server picks up the strategy -----------------------------------------------


def test_server_strategy_env_wins_over_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "clean_strategy.json"
    cfg.write_text('{"default_strategy": "paraphrase@0.8,mlm@0.2"}', encoding="utf-8")
    monkeypatch.setenv("WATERMARKS_CLEAN_STRATEGY", "paraphrase@0.8")
    assert server._resolve_default_strategy(cfg) == "paraphrase@0.8"
    monkeypatch.delenv("WATERMARKS_CLEAN_STRATEGY")
    assert server._resolve_default_strategy(cfg) == "paraphrase@0.8,mlm@0.2"


def test_server_rejects_invalid_strategy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WATERMARKS_CLEAN_STRATEGY", "nonsense@2")
    with pytest.raises(SystemExit):
        server._resolve_default_strategy(tmp_path / "missing.json")


# --- real HTTP against a loopback fake provider ----------------------------------


@pytest.fixture
def fake_api():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: dict[str, str | None] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            if self.path == "/v1/models":
                body = {"data": [{"id": "b-model"}, {"id": "a-model"}]}
            elif self.path == "/api/tags":
                body = {"models": [{"name": "qwen2.5:7b"}]}
            else:
                self.send_error(404)
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", seen
    httpd.shutdown()
    httpd.server_close()


def test_list_models_openai_format_sends_bearer(fake_api):
    base, seen = fake_api
    assert wiz.list_models("openai-compatible", base + "/", SECRET) == ["a-model", "b-model"]
    assert seen["auth"] == f"Bearer {SECRET}"


def test_list_models_ollama_format_without_key(fake_api):
    base, seen = fake_api
    assert wiz.list_models("ollama", base, None) == ["qwen2.5:7b"]
    assert seen["auth"] is None


def test_get_json_refuses_non_http():
    with pytest.raises(ValueError):
        wiz._get_json("file:///etc/passwd", None)
