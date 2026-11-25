from __future__ import annotations

import os
from pathlib import Path

import yaml
from emit_report_support import config, snapshot
from openapi_spec_validator import validate

from j2ee_openapi_generator.emit.builder import build_openapi
from j2ee_openapi_generator.emit.writer import dump_openapi, write_openapi

GOLDEN = Path(__file__).parent / "golden" / "openapi.yml"


def test_dump_is_deterministic_and_has_trailing_newline() -> None:
    a = dump_openapi(build_openapi(snapshot(), config()))
    b = dump_openapi(build_openapi(snapshot(), config()))
    assert a == b and a.endswith("\n") and not a.endswith("\n\n")
    assert "\r" not in a


def test_quoting_and_no_aliases() -> None:
    shared = {"type": "string"}
    text = dump_openapi({"openapi": "3.1.0", "a": {"200": shared, "b": shared}, "v": "1.0"})
    assert "openapi: '3.1.0'" in text
    assert "'200':" in text
    assert "&" not in text and "*id" not in text
    assert yaml.safe_load(text)["a"]["200"] == shared


def test_write_openapi_bytes_match_dump(tmp_path: Path) -> None:
    doc = build_openapi(snapshot(), config())
    target = tmp_path / "nested" / "openapi.yml"
    write_openapi(doc, target)
    assert target.read_bytes() == dump_openapi(doc).encode("utf-8")
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == yaml.safe_load(dump_openapi(doc))


def test_unicode_preserved() -> None:
    assert "Zürich" in dump_openapi({"city": "Zürich"})


def test_golden_byte_for_byte() -> None:
    text = dump_openapi(build_openapi(snapshot(), config("3.1.0")))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_bytes(text.encode("utf-8"))
    assert GOLDEN.read_bytes() == text.encode("utf-8")


def test_valid_in_both_versions() -> None:
    for version in ("3.1.0", "3.0.3"):
        text = dump_openapi(build_openapi(snapshot(), config(version)))
        doc = yaml.safe_load(text)
        assert doc["openapi"] == version
        validate(doc)
