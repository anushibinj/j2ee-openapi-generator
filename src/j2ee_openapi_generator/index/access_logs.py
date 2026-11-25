"""Access-log hit counts per (servlet url pattern, op value)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, unquote

from j2ee_openapi_generator.index.models import AccessCount, ModuleRecord, WebIndex
from j2ee_openapi_generator.index.webxml import resolve_with_pattern

# Matches the request part of Apache common/combined and JBoss/Undertow lines.
_REQ = re.compile(r'"(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) (\S+)(?: HTTP/[\d.]+)?"')


def _split_module(web: WebIndex, path: str) -> tuple[ModuleRecord, str] | None:
    """Find the module by longest context root and return the path below it."""
    best: tuple[int, ModuleRecord, str] | None = None
    for m in web.modules:
        ctx = m.context_root.rstrip("/")
        if ctx == "":
            cand = (0, m, path)
        elif path == ctx:
            cand = (len(ctx), m, "/")
        elif path.startswith(ctx + "/"):
            cand = (len(ctx), m, path[len(ctx) :])
        else:
            continue
        if best is None or cand[0] > best[0]:
            best = cand
    return None if best is None else (best[1], best[2])


def _hit(line: str, web: WebIndex, dispatch_params: Sequence[str]) -> tuple[str, str | None] | None:
    m = _REQ.search(line)
    if not m:
        return None
    uri = m.group(1)
    raw_path, _, query = uri.partition("?")
    raw_path = raw_path.split(";", 1)[0]
    if not raw_path.startswith("/"):
        return None
    sp = _split_module(web, unquote(raw_path))
    if sp is None:
        return None
    module, rest = sp
    hit = resolve_with_pattern(web, module.name, rest)
    if hit is None:
        return None
    q = parse_qs(query.split("#", 1)[0], keep_blank_values=False)
    op: str | None = None
    for name in dispatch_params:
        if q.get(name):
            op = q[name][0]
            break
    return hit[1], op


def load_access_counts(
    paths: list[Path], web: WebIndex, dispatch_params: list[str]
) -> list[AccessCount]:
    counts: dict[tuple[str, str | None], int] = {}
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                key = _hit(line, web, dispatch_params)
            except ValueError:
                continue
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
    return [
        AccessCount(url_pattern=k[0], op=k[1], count=v)
        for k, v in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
    ]


def usage_for(counts: Sequence[AccessCount], url_patterns: Sequence[str], op: str | None) -> int:
    """Total hits for any of the patterns; restricted to ``op`` when given, else all ops."""
    pats = set(url_patterns)
    return sum(c.count for c in counts if c.url_pattern in pats and (op is None or c.op == op))
