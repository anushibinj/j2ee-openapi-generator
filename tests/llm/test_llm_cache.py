from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from j2ee_openapi_generator.llm.cache import Cache, cache_key, normalizer_key, resolver_key

BASE: dict[str, Any] = {
    "packet_hash": "ph",
    "prompt_version": "1",
    "schema_version": "1",
    "style_guide_hash": "sg",
    "model": "m",
    "structured_mode": "json_schema",
    "temperature": 0.0,
    "attempt": 1,
}


def test_cache_key_golden_and_stable() -> None:
    k = cache_key(**BASE)
    assert k == cache_key(**BASE)
    assert len(k) == 64
    assert k == GOLDEN_ANALYZE


def test_cache_key_sensitive_to_every_component() -> None:
    base = cache_key(**BASE)
    changes: dict[str, Any] = {
        "packet_hash": "ph2",
        "prompt_version": "2",
        "schema_version": "2",
        "style_guide_hash": "sg2",
        "model": "m2",
        "structured_mode": "tools",
        "temperature": 0.5,
        "attempt": 2,
    }
    keys = {base}
    for name, val in changes.items():
        k = cache_key(**{**BASE, name: val})
        assert k != base, name
        keys.add(k)
    assert len(keys) == len(changes) + 1


def test_normalizer_and_resolver_keys() -> None:
    n = dict(
        tag="T",
        operations_hash="oh",
        prompt_version="1",
        schema_version="1",
        style_guide_hash="sg",
        model="m",
        structured_mode="json_schema",
        temperature=0.0,
    )
    base = normalizer_key(**n)  # type: ignore[arg-type]
    assert base == GOLDEN_NORMALIZE
    for name, val in {"tag": "U", "operations_hash": "x", "model": "z", "temperature": 1.0}.items():
        assert normalizer_key(**{**n, name: val}) != base  # type: ignore[arg-type]
    r = dict(
        conflicts_hash="ch",
        prompt_version="1",
        schema_version="1",
        style_guide_hash="sg",
        model="m",
        structured_mode="json_schema",
        temperature=0.0,
    )
    rb = resolver_key(**r)  # type: ignore[arg-type]
    assert rb == GOLDEN_RESOLVE
    assert resolver_key(**{**r, "conflicts_hash": "x"}) != rb  # type: ignore[arg-type]
    assert len({base, rb, cache_key(**BASE)}) == 3


def test_cache_roundtrip_deterministic(tmp_path: Path) -> None:
    c = Cache(tmp_path / "cache")
    assert c.get("k") is None
    p = c.put("k", {"b": 1, "a": 2}, "raw", {"x": 1}, usage={"total_tokens": 3})
    first = p.read_bytes()
    rec = c.get("k")
    assert rec == {
        "key": "k",
        "request": {"a": 2, "b": 1},
        "raw": "raw",
        "parsed": {"x": 1},
        "ok": True,
        "errors": [],
        "usage": {"total_tokens": 3},
    }
    c.put("k", {"a": 2, "b": 1}, "raw", {"x": 1}, usage={"total_tokens": 3})
    assert p.read_bytes() == first
    assert list(json.loads(first)) == sorted(json.loads(first))


GOLDEN_ANALYZE = "789e2d6c653d15f8a9c18d27adb80ec4b238d8f9e8e8557eb4279ccb028e75aa"
GOLDEN_NORMALIZE = "1a847c88cda6ea7bfc5e5c582340c77cf3aebb784c312a1774c29c346bd77350"
GOLDEN_RESOLVE = "528906dda87705ae68bbb745348adf6c596260af5c3ef2bd78e3b51a08e199ba"
