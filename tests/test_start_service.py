"""Static checks for the one-command local launcher (start-service.cmd)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "service" / "scripts" / "start_service.ps1"
REQUIREMENTS = ROOT / "service" / "requirements-local.txt"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.parametrize(
    ("cmd", "script"),
    [
        ("start-service.cmd", "start_service.ps1"),
        ("start-docker.cmd", "start_docker.ps1"),
        ("setup.cmd", "setup_wizard.py"),
    ],
)
def test_cmd_runs_its_script(cmd, script):
    text = (ROOT / cmd).read_text(encoding="utf-8")
    assert rf"%~dp0service\scripts\{script}" in text
    assert (ROOT / "service" / "scripts" / script).is_file()


def test_local_requirements_are_exactly_pinned():
    lines = [
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines, "no requirements listed"
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][\w.+]*", line), line
    # The launcher installs torch from the CPU index by reading this exact pin.
    assert any(line.startswith("torch==") for line in lines)


def test_default_strategy_needs_what_the_launcher_installs():
    # The launcher exists so the default strategy runs locally; if a step is
    # added that it does not provision, this test should force an update.
    strategy = (ROOT / "config" / "clean_strategy.json").read_text(encoding="utf-8")
    tactics = set(re.findall(r"([a-z_]+)@", strategy))
    assert tactics <= {"paraphrase", "mlm"}, tactics


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell not installed")
@pytest.mark.parametrize("script", ["start_service.ps1", "start_docker.ps1", "launcher_common.ps1"])
def test_launcher_scripts_parse(script):
    path = ROOT / "service" / "scripts" / script
    probe = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$null, [ref]$errors); "
        "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    res = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_launcher_prefetches_the_model_the_mlm_step_loads():
    import rewrite_text

    assert f"snapshot_download('{rewrite_text._MLM_MODEL}'" in PS1.read_text(encoding="utf-8")
