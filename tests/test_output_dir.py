"""Default cleaner output goes to the repo-root output/ folder."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import common
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "service" / "scripts"
# Built from its code point so this file stays free of raw invisible Unicode.
ZWSP = chr(0x200B)
GIT = shutil.which("git")


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATERMARKS_OUTPUT_DIR", raising=False)


def test_checkout_uses_repo_output_dir(no_env, tmp_path: Path):
    src = tmp_path / "draft.md"
    assert common.cleaned_path(src) == ROOT / "output" / "draft.cleaned.md"
    assert common.cleaned_path(src, ".rewritten") == ROOT / "output" / "draft.rewritten.md"


def test_env_override_wins_and_is_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    out = tmp_path / "custom" / "out"
    monkeypatch.setenv("WATERMARKS_OUTPUT_DIR", str(out))
    assert common.cleaned_path(tmp_path / "a.png") == out / "a.cleaned.png"
    assert out.is_dir()


def test_falls_back_next_to_input_outside_checkout(
    no_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # Installed skill copies and the Docker image (/app/scripts) have no
    # output/ folder two levels above the scripts.
    monkeypatch.setattr(common, "__file__", str(tmp_path / "app" / "scripts" / "common.py"))
    assert common.default_output_dir() is None
    src = tmp_path / "in" / "notes.docx"
    assert common.cleaned_path(src) == src.with_name("notes.cleaned.docx")


def test_detects_output_folder_of_any_checkout(
    no_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / "output").mkdir()
    monkeypatch.setattr(common, "__file__", str(tmp_path / "service" / "scripts" / "common.py"))
    assert common.default_output_dir() == tmp_path / "output"


def _run_clean_text(*args: str, env_out: Path) -> None:
    env = {**os.environ, "WATERMARKS_OUTPUT_DIR": str(env_out)}
    subprocess.run(
        [sys.executable, str(SCRIPTS / "clean_text.py"), *args],
        env=env,
        capture_output=True,
        check=True,
    )


def test_clean_text_cli_writes_into_output_dir(tmp_path: Path):
    src = tmp_path / "draft.md"
    src.write_text(f"Hello{ZWSP} world\n", encoding="utf-8")
    out = tmp_path / "output"
    _run_clean_text(str(src), env_out=out)
    assert (out / "draft.cleaned.md").read_text(encoding="utf-8") == "Hello world\n"
    assert not (tmp_path / "draft.cleaned.md").exists()


def test_explicit_output_flag_still_wins(tmp_path: Path):
    src = tmp_path / "draft.md"
    src.write_text(f"Hello{ZWSP} world\n", encoding="utf-8")
    dest = tmp_path / "chosen.md"
    _run_clean_text(str(src), "-o", str(dest), env_out=tmp_path / "output")
    assert dest.read_text(encoding="utf-8") == "Hello world\n"
    assert not (tmp_path / "output").exists()


@pytest.mark.skipif(
    GIT is None or not (ROOT / ".git").exists(), reason="needs git and a git checkout"
)
def test_gitignore_keeps_folder_but_ignores_contents():
    def ignored(path: str) -> bool:
        res = subprocess.run(
            [GIT, "check-ignore", "-q", "--no-index", path],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        return res.returncode == 0

    assert ignored("output/draft.cleaned.md")
    assert ignored("output/nested/file.png")
    assert not ignored("output/.gitkeep")
