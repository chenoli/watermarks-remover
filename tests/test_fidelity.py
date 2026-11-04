"""Tests for the Layer B fidelity guarantees: prompts and the verify-and-retry loop."""

from __future__ import annotations

import json

import pytest
import rewrite_text
import setup_wizard as wiz
from rewrite_text import (
    PROMPTS,
    apply_strategy_verified,
    build_prompt,
    parse_verdict,
    verify_fidelity,
)
from server import _apply_layer_b

PASS = json.dumps(
    {
        "same_meaning": True,
        "facts_preserved": True,
        "same_languages": True,
        "grammar_ok": True,
        "natural_ok": True,
        "issues": [],
    }
)
FAIL = json.dumps(
    {
        "same_meaning": True,
        "facts_preserved": True,
        "same_languages": False,
        "grammar_ok": True,
        "natural_ok": True,
        "issues": ["EN section translated to Portuguese"],
    }
)

LLM_KWARGS = {
    "backend": "openai-compatible",
    "model": "m",
    "base_url": "https://api.example.test",
    "api_key": "k",
}


# --- prompts --------------------------------------------------------------------


@pytest.mark.parametrize("tactic", ["paraphrase", "humanize", "level", "chunk_unit"])
def test_prose_prompts_demand_language_meaning_and_grammar(tactic):
    prompt = PROMPTS[tactic]
    clause = prompt.index("Never translate")
    assert clause < prompt.index("{TEXT}")
    for phrase in ("its own language", "meaning exactly the same", "grammatically correct"):
        assert phrase in prompt


def test_intensity_clause_repeats_the_fidelity_rule_after_the_text():
    prompt = build_prompt("paraphrase", "HELLO", rewrite_level=0.8)
    assert prompt.rindex("Never translate") > prompt.index("HELLO")


@pytest.mark.parametrize("tactic", ["paraphrase", "level", "chunk_unit"])
def test_churn_prompts_demand_natural_idiomatic_wording(tactic):
    prompt = PROMPTS[tactic]
    assert prompt.index("native speaker") < prompt.index("{TEXT}")
    for phrase in ("idioms", "register", '"pra" stays "pra"', "over synonym swaps"):
        assert phrase in prompt


def test_humanize_keeps_register_and_idioms():
    prompt = PROMPTS["humanize"]
    assert "register and voice" in prompt
    assert "idioms and set phrases" in prompt


def test_intensity_clause_forbids_unnatural_words_at_high_levels():
    prompt = build_prompt("paraphrase", "HELLO", rewrite_level=0.9)
    tail = prompt[prompt.index("HELLO") :]
    assert "never justifies an unnatural word" in tail


def test_verify_prompt_asks_for_natural_ok_with_examples():
    from rewrite_text import FIDELITY_CHECKS, VERIFY_PROMPT

    assert "natural_ok" in FIDELITY_CHECKS
    assert '"natural_ok": true|false' in VERIFY_PROMPT
    assert "to be just" in VERIFY_PROMPT


# --- verdict parsing --------------------------------------------------------------


def test_parse_verdict_pass():
    assert parse_verdict(PASS)["passed"] is True


def test_parse_verdict_fenced_and_surrounded_by_text():
    raw = f"Here is my verdict:\n```json\n{FAIL}\n```\nThanks."
    verdict = parse_verdict(raw)
    assert verdict["passed"] is False
    assert verdict["same_languages"] is False
    assert verdict["issues"] == ["EN section translated to Portuguese"]


@pytest.mark.parametrize("raw", ["not json at all", "{broken", "[]", ""])
def test_parse_verdict_garbage_fails_closed(raw):
    verdict = parse_verdict(raw)
    assert verdict["passed"] is False
    assert verdict["issues"]


def test_parse_verdict_unnatural_rewrite_fails():
    data = json.loads(PASS)
    data["natural_ok"] = False
    data["issues"] = ['"Be truthful" is not idiomatic for "Be honest"']
    verdict = parse_verdict(json.dumps(data))
    assert verdict["passed"] is False
    assert verdict["natural_ok"] is False
    assert verdict["issues"]


def test_parse_verdict_missing_natural_ok_fails_closed():
    data = json.loads(PASS)
    del data["natural_ok"]
    assert parse_verdict(json.dumps(data))["passed"] is False


def test_parse_verdict_requires_real_booleans():
    data = json.loads(PASS)
    data["grammar_ok"] = "true"  # a string is not a pass
    assert parse_verdict(json.dumps(data))["passed"] is False


def test_verify_fidelity_uses_temperature_zero_and_both_texts(monkeypatch):
    seen = {}

    def fake_gen(backend, base_url, model, api_key, prompt, timeout, temperature, effort):
        seen.update(prompt=prompt, temperature=temperature, effort=effort)
        return PASS

    monkeypatch.setattr(rewrite_text, "_generate_once", fake_gen)
    verdict = verify_fidelity("ORIG {TEXT}", "NEW", reasoning_effort="none", **LLM_KWARGS)
    assert verdict["passed"] is True
    assert seen["temperature"] == 0.0
    assert seen["effort"] == "none"
    assert "ORIG {TEXT}" in seen["prompt"]
    assert "NEW" in seen["prompt"]


# --- verify-and-retry loop --------------------------------------------------------


def _fake_backend(monkeypatch, verdicts: list[str]):
    """Rewrites echo an attempt counter; verdicts are returned in order."""
    calls = {"rewrite_inputs": [], "verdicts": iter(verdicts), "n": 0}

    def fake_gen(backend, base_url, model, api_key, prompt, timeout, temperature, effort):
        if prompt.startswith("You check a rewrite"):
            return next(calls["verdicts"])
        calls["n"] += 1
        calls["rewrite_inputs"].append(prompt.split("---\n", 1)[1].split("\n\n")[0])
        return f"rewrite {calls['n']}"

    monkeypatch.setattr(rewrite_text, "_generate_once", fake_gen)
    return calls


def test_verified_returns_first_passing_rewrite(monkeypatch):
    _fake_backend(monkeypatch, [PASS])
    out, stats = apply_strategy_verified("original", [("paraphrase", 0.8)], **LLM_KWARGS)
    assert out == "rewrite 1"
    assert stats["verification"]["passed"] is True
    assert stats["verification"]["attempts"] == 1


def test_verified_retries_from_the_original_text(monkeypatch):
    calls = _fake_backend(monkeypatch, [FAIL, PASS])
    out, stats = apply_strategy_verified("original", [("paraphrase", 0.8)], **LLM_KWARGS)
    assert out == "rewrite 2"
    assert stats["verification"]["attempts"] == 2
    assert stats["verification"]["verdicts"][0]["passed"] is False
    # The retry rewrites the original, not the rejected attempt.
    assert calls["rewrite_inputs"] == ["original", "original"]


def test_verified_raises_when_no_attempt_passes(monkeypatch):
    _fake_backend(monkeypatch, [FAIL, FAIL, FAIL])
    with pytest.raises(RuntimeError, match="fidelity check failed after 3 attempt"):
        apply_strategy_verified("original", [("paraphrase", 0.8)], retries=2, **LLM_KWARGS)


def test_verified_skips_mlm_only_strategies(monkeypatch):
    monkeypatch.setattr(rewrite_text, "_mlm_infill", lambda text, level: text + " edited")
    out, stats = apply_strategy_verified("original", [("mlm", 0.2)], **LLM_KWARGS)
    assert out == "original edited"
    assert stats["verification"]["passed"] is None


# --- server wiring ----------------------------------------------------------------


@pytest.fixture
def deepseek_env(monkeypatch):
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "openai-compatible")
    monkeypatch.setenv("WATERMARKS_REWRITE_MODEL", "m")
    monkeypatch.setenv("WATERMARKS_REWRITE_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("WATERMARKS_REWRITE_API_KEY", "k")
    monkeypatch.setenv("WATERMARKS_REWRITE_ALLOW_REMOTE", "1")


def test_server_verify_env_runs_the_check(monkeypatch, deepseek_env):
    monkeypatch.setenv("WATERMARKS_REWRITE_VERIFY", "1")
    _fake_backend(monkeypatch, [FAIL, PASS])
    out, stats = _apply_layer_b("original", "paraphrase@0.8", {})
    assert out == "rewrite 2"
    assert stats["verification"]["attempts"] == 2


def test_server_verify_failure_is_a_client_error(monkeypatch, deepseek_env):
    monkeypatch.setenv("WATERMARKS_REWRITE_VERIFY", "1")
    monkeypatch.setenv("WATERMARKS_REWRITE_VERIFY_RETRIES", "0")
    _fake_backend(monkeypatch, [FAIL])
    with pytest.raises(ValueError, match="fidelity check failed after 1 attempt"):
        _apply_layer_b("original", "paraphrase@0.8", {})


def test_server_without_verify_env_skips_the_check(monkeypatch, deepseek_env):
    monkeypatch.delenv("WATERMARKS_REWRITE_VERIFY", raising=False)
    _fake_backend(monkeypatch, [])  # any verify call would exhaust the iterator
    out, stats = _apply_layer_b("original", "paraphrase@0.8", {})
    assert out == "rewrite 1"
    assert "verification" not in stats


# --- setup profiles ---------------------------------------------------------------


def test_profiles_turn_the_check_on_except_aggressive():
    by_key = {p.key: p for p in wiz.PROFILES}
    preset = wiz.PRESETS[0]
    for key, expected in (("precise", "1"), ("balanced", "1"), ("aggressive", None)):
        updates = wiz.build_env_updates(preset, "https://x.test", "m", "k", by_key[key])
        assert updates["WATERMARKS_REWRITE_VERIFY"] == expected
