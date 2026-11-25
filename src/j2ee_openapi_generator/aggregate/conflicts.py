"""Conflict resolution by decision and LLM resolution suggestions (never auto-applied)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from j2ee_openapi_generator.aggregate.decisions import apply_overrides, set_operation_path
from j2ee_openapi_generator.aggregate.dedupe import normalize_path
from j2ee_openapi_generator.aggregate.models import ConflictRow, Decision
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import sha256_obj, sha256_text
from j2ee_openapi_generator.llm.base import LLMClient
from j2ee_openapi_generator.llm.cache import Cache, CacheMiss, resolver_key
from j2ee_openapi_generator.llm.schemas import (
    SCHEMA_VERSION,
    Operation,
    ResolverOutput,
)
from j2ee_openapi_generator.prompts import PROMPT_VERSION, render_prompt

HEADER = "# Suggestions only. Copy accepted entries into decisions.yaml.\n"


class ResolverError(RuntimeError):
    """The resolver call failed."""


def _involves(conflict: ConflictRow, packet_id: str, op: Operation) -> bool:
    """Is (packet, op) one of the proposals that make up ``conflict``?"""
    for prop in conflict.proposals:
        if prop.get("packet_id") != packet_id:
            continue
        key = str(prop.get("key", ""))
        method, _, norm = key.partition(" ")
        if op.http_method == method and normalize_path(op.path) == norm:
            return True
    return False


def resolve_conflict(
    conflict: ConflictRow,
    decision: Decision,
    ops: list[tuple[str, Operation]],
) -> tuple[list[tuple[str, Operation]], list[str]]:
    """Apply a conflict-keyed decision to the ``(packet_id, operation)`` list.

    - ``accept``: the chosen packet's conflicting operations stay, the others are dropped.
    - ``override``: like accept, and the chosen operations get ``overrides`` applied.
    - ``rename_path``: the chosen packet's conflicting operations move to ``new_path``.
    - ``drop``: the chosen packet's conflicting operations are removed (all when no choice).
    Returns the new list and warnings. The caller re-merges the result.
    """
    warnings: list[str] = []
    chosen = decision.chosen_packet_id
    if chosen is not None and chosen not in conflict.packet_ids:
        return ops, [f"{decision.key}: chosen_packet_id {chosen} is not part of the conflict"]
    if chosen is None and decision.action != "drop":
        return ops, [f"{decision.key}: {decision.action} needs chosen_packet_id"]
    out: list[tuple[str, Operation]] = []
    for pid, op in ops:
        if not _involves(conflict, pid, op):
            out.append((pid, op))
            continue
        is_chosen = pid == chosen
        if decision.action == "drop":
            if chosen is None or is_chosen:
                continue
            out.append((pid, op))
        elif not is_chosen:
            if decision.action == "rename_path":
                out.append((pid, op))
            continue
        elif decision.action == "accept":
            out.append((pid, op))
        elif decision.action == "override":
            out.append((pid, apply_overrides(op, decision.overrides, warnings)))
        else:
            assert decision.new_path is not None
            out.append((pid, set_operation_path(op, decision.new_path, warnings)))
    return out, warnings


def write_suggestions(path: Path, suggestions: ResolverOutput) -> None:
    """Write ``review/suggestions.yaml`` (decisions-shaped, sorted, deterministic)."""
    entries: list[dict[str, Any]] = []
    for s in sorted(suggestions.suggestions, key=lambda s: (s.conflict_id, s.action)):
        e: dict[str, Any] = {"key": s.conflict_id, "action": s.action}
        if s.chosen_packet_id:
            e["chosen_packet_id"] = s.chosen_packet_id
        if s.new_path:
            e["new_path"] = s.new_path
        e["reason"] = s.rationale
        entries.append(e)
    body = yaml.safe_dump(
        {"version": 1, "decisions": entries},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + body, encoding="utf-8")


def _conflict_payload(conflicts: list[ConflictRow]) -> list[dict[str, Any]]:
    return [
        {
            "conflict_id": c.id,
            "kind": c.kind,
            "summary": c.summary,
            "packet_ids": c.packet_ids,
            "proposals": c.proposals,
        }
        for c in sorted(conflicts, key=lambda c: c.id)
    ]


async def suggest_resolutions(
    cfg: Config,
    client: LLMClient,
    conflicts: list[ConflictRow],
    cache: Cache,
    offline: bool,
    force: bool,
) -> ResolverOutput:
    """Ask the resolver for suggestions. Results are suggestions only and never applied."""
    if not conflicts:
        return ResolverOutput()
    guide_path = cfg.style_guide_path
    guide = guide_path.read_text(encoding="utf-8") if guide_path.is_file() else ""
    payload = _conflict_payload(conflicts)
    key = resolver_key(
        sha256_obj(payload),
        PROMPT_VERSION,
        SCHEMA_VERSION,
        sha256_text(guide),
        client.model_name,
        client.structured_mode,
        client.temperature,
    )
    if not force:
        hit = cache.get(key)
        if hit is not None and hit.get("ok") and isinstance(hit.get("parsed"), dict):
            try:
                return ResolverOutput.model_validate(hit["parsed"])
            except ValidationError:
                pass
    if offline:
        raise CacheMiss(f"resolver cache entry {key[:12]} missing and offline mode is on")
    prompt = render_prompt(
        "resolver",
        style_guide=guide,
        conflicts=json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        schema=json.dumps(ResolverOutput.model_json_schema(), indent=2, sort_keys=True),
    )
    res = await client.structured(
        purpose="resolve",
        item_id="_resolver",
        messages=[{"role": "user", "content": prompt}],
        response_model=ResolverOutput,
    )
    cache.put(key, res.request, res.raw, res.parsed, ok=res.ok, errors=res.errors, usage=res.usage)
    if not res.ok or res.parsed is None:
        raise ResolverError("; ".join(res.errors) or "resolver call failed")
    return ResolverOutput.model_validate(res.parsed)
