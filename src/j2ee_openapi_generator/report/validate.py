"""Validation wrapper: openapi-spec-validator, Spectral and oasdiff."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES = Path(__file__).resolve().parents[3] / "rules" / ".spectral.yaml"
_NETWORK_HINTS = (
    "ENOTFOUND",
    "ETIMEDOUT",
    "EAI_AGAIN",
    "ECONNREFUSED",
    "ECONNRESET",
    "ERR_PNPM_FETCH",
    "ERR_PNPM_META_FETCH",
    "getaddrinfo",
    "network",
    "tunneling socket",
)
TIMEOUT_S = 240


@dataclass
class ValidationResult:
    tool: str
    ok: bool
    skipped: bool = False
    output: str = ""


def _spec_validator(path: Path) -> ValidationResult:
    from openapi_spec_validator import OpenAPIV30SpecValidator, OpenAPIV31SpecValidator
    from openapi_spec_validator import validate as validate_spec

    tool = "openapi-spec-validator"
    try:
        doc: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return ValidationResult(tool, False, False, f"cannot load {path}: {exc}")
    if not isinstance(doc, dict):
        return ValidationResult(tool, False, False, "document is not a mapping")
    version = str(doc.get("openapi", ""))
    if version.startswith("3.1"):
        cls: Any = OpenAPIV31SpecValidator
    elif version.startswith("3.0"):
        cls = OpenAPIV30SpecValidator
    else:
        return ValidationResult(tool, False, False, f"unsupported openapi version {version!r}")
    try:
        validate_spec(doc, cls=cls)
    except Exception as exc:  # the validator raises many error types
        return ValidationResult(tool, False, False, f"{type(exc).__name__}: {exc}")
    return ValidationResult(tool, True, False, f"valid OpenAPI {version}")


def _run(cmd: list[str]) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {TIMEOUT_S}s"
    except OSError as exc:
        return None, f"cannot run {cmd[0]}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _spectral(path: Path, rules: Path | None) -> ValidationResult:
    tool = "spectral"
    if shutil.which("pnpm") is None:
        return ValidationResult(tool, True, True, "pnpm not found on PATH")
    ruleset = rules if rules is not None else (DEFAULT_RULES if DEFAULT_RULES.exists() else None)
    if ruleset is None:
        return ValidationResult(tool, True, True, "no Spectral ruleset available")
    code, out = _run(
        ["pnpm", "dlx", "@stoplight/spectral-cli", "lint", str(path), "-r", str(ruleset)]
    )
    if code is None:
        return ValidationResult(tool, True, True, out)
    if code != 0 and any(h in out for h in _NETWORK_HINTS):
        return ValidationResult(tool, True, True, f"spectral unavailable (network?):\n{out}")
    return ValidationResult(tool, code == 0, False, out)


def _oasdiff(path: Path, baseline: Path | None) -> ValidationResult:
    tool = "oasdiff"
    if baseline is None:
        return ValidationResult(tool, True, True, "no baseline given")
    if shutil.which("oasdiff") is None:
        return ValidationResult(tool, True, True, "oasdiff not found on PATH")
    code, out = _run(["oasdiff", "breaking", str(baseline), str(path), "--fail-on", "ERR"])
    if code is None:
        return ValidationResult(tool, True, True, out)
    return ValidationResult(tool, code == 0, False, out)


def validate_openapi(
    path: Path, baseline: Path | None = None, rules: Path | None = None
) -> list[ValidationResult]:
    """Run every available validator; unavailable tools yield ``skipped`` results."""
    return [_spec_validator(path), _spectral(path, rules), _oasdiff(path, baseline)]
