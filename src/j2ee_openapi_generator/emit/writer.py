"""Deterministic YAML writer for the OpenAPI document (ruamel.yaml)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import SingleQuotedScalarString


def _prepare(doc: Any, key: str | None = None) -> Any:
    if isinstance(doc, dict):
        return {k: _prepare(v, str(k)) for k, v in doc.items()}
    if isinstance(doc, list | tuple):
        return [_prepare(v) for v in doc]
    if key == "openapi" and isinstance(doc, str):
        return SingleQuotedScalarString(doc)
    return doc


def dump_openapi(doc: dict[str, Any]) -> str:
    """Serialize ``doc`` to YAML text; equal input gives byte-identical output."""
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 1_000_000
    yaml.allow_unicode = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.ignore_aliases = lambda *_a: True  # type: ignore[method-assign]
    buf = StringIO()
    yaml.dump(_prepare(doc), buf)
    text = buf.getvalue().replace("\r\n", "\n")
    return text if text.endswith("\n") else text + "\n"


def write_openapi(doc: dict[str, Any], path: Path) -> None:
    """Write ``doc`` to ``path`` (UTF-8, ``\\n`` newlines, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_openapi(doc))
