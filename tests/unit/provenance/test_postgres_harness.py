"""Offline contracts for the opt-in PostgreSQL harness."""

import importlib.util
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tests/integration/postgres"


def test_compose_is_localhost_only_and_requires_an_explicit_password():
    text = (HARNESS / "compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(text)
    service = compose["services"]["postgres"]
    assert service["image"] == "postgres:17.11-bookworm"
    assert service["ports"] == ["127.0.0.1:${VCP_TEST_PG_PORT:-55432}:5432"]
    assert service["environment"]["POSTGRES_PASSWORD"] == (
        "${VCP_TEST_PG_PASSWORD:?set VCP_TEST_PG_PASSWORD}"
    )
    assert service["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U postgres -d postgres"]
    assert service["volumes"] == ["pgdata:/var/lib/postgresql/data"]
    assert compose["volumes"] == {"pgdata": None}


def test_runner_uses_temporary_credentials_waits_and_always_cleans_up():
    text = (HARNESS / "run.ps1").read_text(encoding="utf-8")
    for required in (
        "PGSERVICEFILE",
        "PGPASSFILE",
        "VCP_TEST_PG_SERVICE",
        "GetRandomFileName",
        "--wait",
        "--wait-timeout",
        "finally",
        "down",
        "--volumes",
        "Remove-Item",
        "--frozen",
        '"-m" "postgres"',
        "tests/integration/provenance",
    ):
        assert required in text
    assert "Write-Host $env:VCP_TEST_PG_PASSWORD" not in text
    assert "docker compose config" not in text
    assert "--showlocals" not in text


def test_ci_is_separate_masks_random_password_and_uses_the_local_runner():
    text = (ROOT / ".github/workflows/postgres-provenance.yml").read_text(encoding="utf-8")
    assert "::add-mask::" in text
    assert "openssl rand -hex 32" in text
    assert "tests/integration/postgres/run.ps1" in text
    assert "shell: pwsh" in text
    assert "services:" not in text


@pytest.fixture
def harness_module():
    spec = importlib.util.spec_from_file_location(
        "postgres_integration_fixtures", ROOT / "tests/integration/provenance/conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("missing", ["service", "servicefile", "passfile", "driver"])
def test_unconfigured_fixture_skips_without_connecting(
    harness_module, monkeypatch, tmp_path, missing
):
    for key in ("VCP_TEST_PG_SERVICE", "PGSERVICEFILE", "PGPASSFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PGSERVICE", "ambient-service-must-not-opt-in")
    if missing != "service":
        monkeypatch.setenv("VCP_TEST_PG_SERVICE", "test-service")
    for kind in ("servicefile", "passfile"):
        path = tmp_path / kind
        if missing != kind:
            path.touch()
        monkeypatch.setenv("PG" + kind.upper(), str(path))

    def load(name):
        if missing == "driver":
            raise ImportError("missing optional driver")
        pytest.fail("an unconfigured fixture tried to load a driver")

    monkeypatch.setattr(harness_module, "import_module", load)
    with pytest.raises(pytest.skip.Exception) as skipped:
        harness_module.configured_driver()
    assert str(skipped.value) == "PostgreSQL integration service is not configured"


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell"])
@pytest.mark.parametrize(
    "failure,expected", [("none", 0), ("up", 1), ("tests", 13), ("down", 1), ("generated", 0)]
)
def test_runner_cleanup_and_exit_status_with_mock_commands(tmp_path, failure, expected, shell_name):
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip("PowerShell is unavailable for runner contract execution")
    log = tmp_path / "events.jsonl"
    secret = secrets.token_hex(32)
    env = {
        # Let each PowerShell version resolve its own built-in modules. The test
        # host may itself inherit a different edition's PSModulePath.
        **{key: value for key, value in os.environ.items() if key.lower() != "psmodulepath"},
        "HARNESS_RUNNER": str(HARNESS / "run.ps1"),
        "HARNESS_LOG": str(log),
        "HARNESS_FAILURE": failure,
        "VCP_TEST_PG_PASSWORD": secret,
    }
    if failure == "generated":
        env.pop("VCP_TEST_PG_PASSWORD")
    script = r"""
function docker {
    @{command="docker"; args=@($args); credentials=(Split-Path $env:PGPASSFILE)} |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $env:HARNESS_LOG
    if (($args -contains "up" -and $env:HARNESS_FAILURE -eq "up") -or
        ($args -contains "down" -and $env:HARNESS_FAILURE -eq "down")) {
        $global:LASTEXITCODE = 9
    } else { $global:LASTEXITCODE = 0 }
}
function uv {
    @{command="uv"; args=@($args); serviceExists=(Test-Path $env:PGSERVICEFILE);
        passExists=(Test-Path $env:PGPASSFILE);
        passwordPresent=([bool]$env:VCP_TEST_PG_PASSWORD)} |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $env:HARNESS_LOG
    if ($env:HARNESS_FAILURE -eq "tests") { $global:LASTEXITCODE = 13 }
    else { $global:LASTEXITCODE = 0 }
}
& $env:HARNESS_RUNNER
exit $LASTEXITCODE
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert secret not in result.stdout + result.stderr
    assert result.returncode == expected, result.stdout + result.stderr
    events = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()]
    docker = [event for event in events if event["command"] == "docker"]
    assert len(docker) == 2
    assert "up" in docker[0]["args"] and "--wait" in docker[0]["args"]
    assert "down" in docker[1]["args"] and "--volumes" in docker[1]["args"]
    assert not Path(docker[0]["credentials"]).exists()
    invocations = [event for event in events if event["command"] == "uv"]
    assert len(invocations) == (0 if failure == "up" else 1)
    for event in invocations:
        assert event["serviceExists"] and event["passExists"] and event["passwordPresent"]
        assert event["args"][event["args"].index("-m") + 1] == "postgres"
    assert secret not in log.read_text(encoding="utf-8-sig")
