"""Docker one-command setup: compose wiring, image defaults, host-loopback URLs."""

from __future__ import annotations

import re
from pathlib import Path

import common
import pytest
import rewrite_text
import setup_wizard as wiz
from server import _apply_layer_b

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S)", COMPOSE, re.M | re.S)
    assert match, name
    return match.group(1)


def test_core_receives_every_setting_the_wizard_writes():
    core = _service_block("wr-core")
    for key in wiz.MANAGED_KEYS:
        if key == "WATERMARKS_WITH_MLM":
            # A build-time choice, passed as the WITH_MLM build arg.
            assert "WITH_MLM: ${WATERMARKS_WITH_MLM:-0}" in core
        else:
            assert f"{key}: ${{{key}:-" in core, key


def test_core_defaults_favor_precision():
    core = _service_block("wr-core")
    assert "WATERMARKS_CLEAN_STRATEGY: ${WATERMARKS_CLEAN_STRATEGY:-paraphrase@0.8}" in core
    assert "WATERMARKS_REWRITE_VERIFY: ${WATERMARKS_REWRITE_VERIFY:-1}" in core
    assert "host.docker.internal:host-gateway" in core


def test_setup_service_writes_the_repo_env():
    setup = _service_block("wr-setup")
    assert "setup_wizard.py" in setup
    assert "/workspace/.env" in setup
    assert "- .:/workspace" in setup
    # Same image tag as wr-core, so it must build with the same args.
    assert "WITH_MLM: ${WATERMARKS_WITH_MLM:-0}" in setup


def test_image_defaults():
    assert "ARG WITH_MLM=0" in DOCKERFILE
    assert "WATERMARKS_IN_DOCKER=1" in DOCKERFILE
    assert "WATERMARKS_CLEAN_STRATEGY=paraphrase@0.8" in DOCKERFILE
    assert "COPY requirements-local.txt" in DOCKERFILE
    ignore = (ROOT / "service" / ".dockerignore").read_text(encoding="utf-8")
    assert "!requirements-local.txt" in ignore


# --- host-loopback URLs ----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434", ("http://host.docker.internal:11434", True)),
        ("http://localhost:1234/", ("http://host.docker.internal:1234/", True)),
        ("https://api.deepseek.com", ("https://api.deepseek.com", False)),
    ],
)
def test_docker_host_url_in_container(monkeypatch, url, expected):
    monkeypatch.setenv("WATERMARKS_IN_DOCKER", "1")
    assert common.docker_host_url(url) == expected


def test_docker_host_url_is_a_noop_on_the_host(monkeypatch):
    monkeypatch.delenv("WATERMARKS_IN_DOCKER", raising=False)
    assert common.docker_host_url("http://127.0.0.1:11434") == ("http://127.0.0.1:11434", False)


def test_server_reaches_host_ollama_without_allow_remote(monkeypatch):
    monkeypatch.setenv("WATERMARKS_IN_DOCKER", "1")
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "ollama")
    monkeypatch.setenv("WATERMARKS_REWRITE_MODEL", "m")
    monkeypatch.setenv("WATERMARKS_REWRITE_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.delenv("WATERMARKS_REWRITE_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("WATERMARKS_REWRITE_VERIFY", raising=False)
    seen = {}

    def fake_apply(text, steps, **kwargs):
        seen["base_url"] = kwargs["base_url"]
        return "out", {"steps": []}

    monkeypatch.setattr(rewrite_text, "apply_strategy", fake_apply)
    _apply_layer_b("x", "paraphrase@0.8", {})
    assert seen["base_url"] == "http://host.docker.internal:11434"


def test_server_still_denies_real_remote_without_allow_remote(monkeypatch):
    monkeypatch.setenv("WATERMARKS_IN_DOCKER", "1")
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "ollama")
    monkeypatch.setenv("WATERMARKS_REWRITE_MODEL", "m")
    monkeypatch.setenv("WATERMARKS_REWRITE_BASE_URL", "http://10.0.0.5:11434")
    monkeypatch.delenv("WATERMARKS_REWRITE_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError, match="ALLOW_REMOTE"):
        _apply_layer_b("x", "paraphrase@0.8", {})


def test_wizard_probes_through_the_docker_host_but_saves_loopback(monkeypatch):
    monkeypatch.setenv("WATERMARKS_IN_DOCKER", "1")
    seen = {}

    def fake_get_json(url, api_key, timeout=15):
        seen["url"] = url
        return {"models": [{"name": "qwen2.5:7b"}]}

    monkeypatch.setattr(wiz, "_get_json", fake_get_json)
    assert wiz.list_models("ollama", "http://127.0.0.1:11434", None) == ["qwen2.5:7b"]
    assert seen["url"] == "http://host.docker.internal:11434/api/tags"
    updates = wiz.build_env_updates(
        wiz.PRESETS[4], "http://127.0.0.1:11434", "qwen2.5:7b", None, wiz.PROFILES[0]
    )
    assert updates["WATERMARKS_REWRITE_BASE_URL"] == "http://127.0.0.1:11434"


def test_aggressive_profile_requests_the_mlm_image():
    by_key = {p.key: p for p in wiz.PROFILES}
    for key, expected in (("precise", None), ("aggressive", "1")):
        updates = wiz.build_env_updates(wiz.PRESETS[0], "https://x.test", "m", "k", by_key[key])
        assert updates["WATERMARKS_WITH_MLM"] == expected
