from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from emit_report_support import config, snapshot

from j2ee_openapi_generator.emit.builder import build_openapi
from j2ee_openapi_generator.emit.writer import write_openapi
from j2ee_openapi_generator.report import validate as v

RULES = Path(__file__).parents[2] / "rules" / ".spectral.yaml"


@pytest.fixture(autouse=True)
def _no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never reach pnpm/oasdiff (network) unless a test patches them in."""
    monkeypatch.setattr(v.shutil, "which", lambda _n: None)


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    p = tmp_path / "openapi.yml"
    write_openapi(build_openapi(snapshot(), config()), p)
    return p


def fake_run(returncode: int, out: str, calls: list[list[str]]) -> Any:
    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, out, "")

    return run


def by_tool(results: list[v.ValidationResult]) -> dict[str, v.ValidationResult]:
    return {r.tool: r for r in results}


def test_spec_validator_ok_both_versions(tmp_path: Path) -> None:
    for version in ("3.1.0", "3.0.3"):
        p = tmp_path / f"{version}.yml"
        write_openapi(build_openapi(snapshot(), config(version)), p)
        res = by_tool(v.validate_openapi(p))["openapi-spec-validator"]
        assert res.ok and not res.skipped and version in res.output


def test_spec_validator_fails_on_invalid(tmp_path: Path) -> None:
    p = tmp_path / "bad.yml"
    p.write_text("openapi: '3.1.0'\ninfo: {title: t}\npaths: {}\n", encoding="utf-8")
    res = by_tool(v.validate_openapi(p))["openapi-spec-validator"]
    assert not res.ok and res.output


def test_all_skipped_without_tools(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v.shutil, "which", lambda _n: None)
    res = by_tool(v.validate_openapi(spec, baseline=spec))
    assert res["openapi-spec-validator"].ok
    assert res["spectral"].skipped and "pnpm" in res["spectral"].output
    assert res["oasdiff"].skipped and "oasdiff" in res["oasdiff"].output


def test_oasdiff_skipped_without_baseline(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(v.subprocess, "run", fake_run(0, "", []))
    assert by_tool(v.validate_openapi(spec))["oasdiff"].skipped


def test_spectral_and_oasdiff_commands(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(v.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(v.subprocess, "run", fake_run(0, "ok", calls))
    base = spec.parent / "base.yml"
    base.write_text(spec.read_text(encoding="utf-8"), encoding="utf-8")
    res = by_tool(v.validate_openapi(spec, baseline=base, rules=RULES))
    assert res["spectral"].ok and not res["spectral"].skipped
    assert res["oasdiff"].ok and not res["oasdiff"].skipped
    assert calls[0] == [
        "pnpm",
        "dlx",
        "@stoplight/spectral-cli",
        "lint",
        str(spec),
        "-r",
        str(RULES),
    ]
    assert calls[1] == ["oasdiff", "breaking", str(base), str(spec), "--fail-on", "ERR"]


def test_tool_failure_reported(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(v.subprocess, "run", fake_run(1, "2 errors", []))
    res = by_tool(v.validate_openapi(spec, baseline=spec, rules=RULES))
    assert not res["spectral"].ok and not res["spectral"].skipped
    assert not res["oasdiff"].ok and res["oasdiff"].output == "2 errors"


def test_spectral_network_failure_is_nonfatal(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(v.subprocess, "run", fake_run(1, "ERR_PNPM_META_FETCH getaddrinfo", []))
    res = by_tool(v.validate_openapi(spec, rules=RULES))["spectral"]
    assert res.skipped and res.ok and "getaddrinfo" in res.output


def test_timeout_is_skipped(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **_kw: Any) -> None:
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(v.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(v.subprocess, "run", boom)
    res = by_tool(v.validate_openapi(spec, rules=RULES))["spectral"]
    assert res.skipped and "timed out" in res.output


def test_spectral_ruleset_is_valid_yaml() -> None:
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    assert rules["extends"] == ["spectral:oas"]
    for name, rule in rules["rules"].items():
        if isinstance(rule, dict):
            assert {"given", "then"} <= set(rule), name
            assert rule["severity"] in ("error", "warn", "info", "hint"), name
    assert rules["rules"]["operation-operationId"] == "error"
    assert rules["rules"]["operation-operationId-unique"] == "error"
    assert {
        "oapi-path-prefix",
        "oapi-path-kebab-case",
        "oapi-path-no-verbs",
        "oapi-property-camel-case",
        "oapi-operation-has-4xx",
        "oapi-operation-has-5xx",
        "oapi-error-problem-json",
    } <= set(rules["rules"])
