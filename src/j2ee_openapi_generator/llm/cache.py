"""Content-addressed cache of LLM calls (one JSON file per key, deterministic bytes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from j2ee_openapi_generator.ids import sha256_obj


class CacheMiss(Exception):
    """Raised by callers running offline when a cache entry is absent."""


def cache_key(
    packet_hash: str,
    prompt_version: str,
    schema_version: str,
    style_guide_hash: str,
    model: str,
    structured_mode: str,
    temperature: float,
    attempt: int,
) -> str:
    return sha256_obj(
        {
            "kind": "analyze",
            "packet_hash": packet_hash,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "style_guide_hash": style_guide_hash,
            "model": model,
            "structured_mode": structured_mode,
            "temperature": temperature,
            "attempt": attempt,
        }
    )


def normalizer_key(
    tag: str,
    operations_hash: str,
    prompt_version: str,
    schema_version: str,
    style_guide_hash: str,
    model: str,
    structured_mode: str,
    temperature: float,
) -> str:
    return sha256_obj(
        {
            "kind": "normalize",
            "tag": tag,
            "operations_hash": operations_hash,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "style_guide_hash": style_guide_hash,
            "model": model,
            "structured_mode": structured_mode,
            "temperature": temperature,
        }
    )


def resolver_key(
    conflicts_hash: str,
    prompt_version: str,
    schema_version: str,
    style_guide_hash: str,
    model: str,
    structured_mode: str,
    temperature: float,
) -> str:
    return sha256_obj(
        {
            "kind": "resolve",
            "conflicts_hash": conflicts_hash,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "style_guide_hash": style_guide_hash,
            "model": model,
            "structured_mode": structured_mode,
            "temperature": temperature,
        }
    )


class Cache:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def put(
        self,
        key: str,
        request: dict[str, Any],
        raw: str,
        parsed: dict[str, Any] | None,
        ok: bool = True,
        errors: list[str] | None = None,
        usage: dict[str, int] | None = None,
    ) -> Path:
        record = {
            "key": key,
            "request": request,
            "raw": raw,
            "parsed": parsed,
            "ok": ok,
            "errors": list(errors or []),
            "usage": dict(usage or {}),
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(key)
        p.write_text(
            json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return p
