"""Stable ID and hashing helpers. All hashing in the tool goes through here."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8 friendly."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj: Any) -> str:
    """sha256 of the canonical JSON of ``obj``."""
    return sha256_text(canonical_json(obj))


def entry_point_id(
    module: str, servlet_simple_name: str, op: str, discriminator: str | None = None
) -> str:
    """Stable entry point ID, optionally disambiguated by request method."""
    base = f"{module}:{servlet_simple_name}#{op}"
    return f"{base}@{discriminator}" if discriminator else base


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def id_to_filename(entry_id: str) -> str:
    """Filesystem-safe, reversible-enough file stem for an ID (``:``/``#`` mapped)."""
    return _SAFE.sub("_", entry_id.replace("#", "__").replace(":", "--")).strip("_")


def split_entry_point_id(entry_id: str) -> tuple[str, str, str]:
    """Return (module, servlet, operation identifier), including any method discriminator."""
    mod, rest = entry_id.split(":", 1)
    servlet, op = rest.split("#", 1)
    return mod, servlet, op


def stable_sorted(items: list[Any], key: Any = None) -> list[Any]:
    """Sort helper kept here so all stages use one rule."""
    return sorted(items, key=key)
