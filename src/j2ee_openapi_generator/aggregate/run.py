"""Aggregate stage driver: verified proposals + decisions -> merged operations in SQLite."""

from __future__ import annotations

import asyncio
import re

from pydantic import ValidationError

from j2ee_openapi_generator.aggregate.conflicts import (
    ResolverError,
    resolve_conflict,
    suggest_resolutions,
    write_suggestions,
)
from j2ee_openapi_generator.aggregate.decisions import (
    apply_packet_decisions,
    is_conflict_key,
    load_decisions,
    parent_id,
)
from j2ee_openapi_generator.aggregate.dedupe import merge_operations
from j2ee_openapi_generator.aggregate.models import (
    ConflictRow,
    CoverageRow,
    OperationRow,
    VerifiedProposal,
)
from j2ee_openapi_generator.aggregate.store import Store
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.models import IndexBundle
from j2ee_openapi_generator.llm.base import LLMClient
from j2ee_openapi_generator.llm.cache import Cache, CacheMiss
from j2ee_openapi_generator.llm.schemas import Operation
from j2ee_openapi_generator.packets.models import WorkPacket
from j2ee_openapi_generator.workspace import Workspace, read_json

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_MAX_CONFLICT_ROUNDS = 5


def derive_error_codes(bundle: IndexBundle) -> dict[str, str]:
    """Properties keys ``error.*`` (or ``*.error.*``) -> stable upper snake codes, sorted."""
    codes: dict[str, str] = {}
    for b in bundle.properties.bundles:
        for key in b.entries:
            if key.startswith("error."):
                rest = key[len("error.") :]
            elif ".error." in key:
                rest = key.replace(".error.", ".", 1)
            else:
                continue
            code = _NON_ALNUM.sub("_", _CAMEL.sub("_", rest)).strip("_").upper()
            if code:
                codes[key] = code
    return dict(sorted(codes.items()))


def _load_packets(ws: Workspace, warnings: list[str]) -> dict[str, WorkPacket]:
    out: dict[str, WorkPacket] = {}
    if ws.packets_dir.is_dir():
        for f in sorted(ws.packets_dir.glob("*.json")):
            try:
                p = WorkPacket.model_validate(read_json(f))
            except (ValueError, ValidationError) as exc:
                warnings.append(f"skipped unreadable packet {f.name}: {exc}")
                continue
            out[p.id] = p
    return out


def _load_verified(ws: Workspace, warnings: list[str]) -> dict[str, VerifiedProposal]:
    out: dict[str, VerifiedProposal] = {}
    if ws.verified_dir.is_dir():
        for f in sorted(ws.verified_dir.glob("*.json")):
            try:
                v = VerifiedProposal.model_validate(read_json(f))
            except (ValueError, ValidationError) as exc:
                warnings.append(f"skipped unreadable verified proposal {f.name}: {exc}")
                continue
            out[v.packet_id] = v
    return out


def _collect_ops(proposals: dict[str, VerifiedProposal]) -> list[tuple[str, Operation]]:
    ops: list[tuple[str, Operation]] = []
    for pid in sorted(proposals):
        p = proposals[pid]
        if p.status == "verified":
            ops.extend((pid, op) for op in p.operations)
    return ops


def _lookup_type(path: str) -> str | None:
    parts = [s for s in path.split("/") if s]
    if len(parts) >= 2 and parts[0] == "lookups" and not parts[1].startswith("{"):
        return parts[1]
    return None


def build_store(
    cfg: Config,
    ws: Workspace,
    bundle: IndexBundle,
    warnings: list[str],
) -> tuple[Store, list[ConflictRow]]:
    """Deterministic part of aggregation. Returns the filled store and unresolved conflicts."""
    packets = _load_packets(ws, warnings)
    proposals = _load_verified(ws, warnings)
    decisions = load_decisions(ws.review_dir / "decisions.yaml")

    proposals, w, used = apply_packet_decisions(proposals, decisions)
    warnings.extend(w)

    ops = _collect_ops(proposals)
    rows, conflicts = merge_operations(ops)
    resolved: dict[str, ConflictRow] = {}
    conflict_decisions = {d.key: d for d in decisions.decisions if is_conflict_key(d.key)}
    for _ in range(_MAX_CONFLICT_ROUNDS):
        todo = [c for c in conflicts if c.id in conflict_decisions and c.id not in resolved]
        if not todo:
            break
        for c in todo:
            ops, w = resolve_conflict(c, conflict_decisions[c.id], ops)
            warnings.extend(w)
            used.add(c.id)
            resolved[c.id] = c.model_copy(update={"resolved": True})
        rows, conflicts = merge_operations(ops)
    for key in sorted(conflict_decisions):
        if key not in used:
            warnings.append(f"decision {key}: no such conflict (stale decision?)")
    for d in decisions.decisions:
        if d.key not in used and not is_conflict_key(d.key):
            warnings.append(f"decision {d.key}: no such packet (unknown key)")

    all_conflicts = sorted(
        [*conflicts, *(c for k, c in resolved.items() if k not in {x.id for x in conflicts})],
        key=lambda c: (c.kind, c.id),
    )

    if resolved:
        kept = {pid for pid, _ in ops}
        for pid, p in list(proposals.items()):
            if p.status == "verified" and p.operations and pid not in kept:
                note = f"dropped by conflict resolution ({', '.join(sorted(resolved))})"
                proposals[pid] = p.model_copy(
                    update={"status": "rejected", "reasons": [*p.reasons, note]}
                )

    coverage = _coverage(packets, proposals, rows, conflicts)
    questions = _questions(proposals)
    lookups = {
        t for p in proposals.values() if p.status == "verified" for t in p.lookups_needed
    } | {t for r in rows if (t := _lookup_type(r.path))}

    if ws.db_path.exists() and str(ws.db_path) != ":memory:":
        ws.db_path.unlink()
    store = Store(ws.db_path)
    store.reset()
    store.put_operations(rows)
    store.put_conflicts(all_conflicts)
    store.put_decisions(decisions.decisions)
    store.put_coverage(coverage)
    store.put_questions(questions)
    store.put_lookups(sorted(lookups))
    return store, [c for c in conflicts if c.id not in resolved]


def _first_tag(p: VerifiedProposal) -> str:
    return p.operations[0].tag if p.operations else "General"


def _questions(proposals: dict[str, VerifiedProposal]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pid in sorted(proposals):
        p = proposals[pid]
        if p.status not in ("verified", "no_api_needed") or not p.open_questions:
            continue
        bucket = out.setdefault(_first_tag(p), [])
        for q in p.open_questions:
            if q not in bucket:
                bucket.append(q)
    return {t: sorted(q) for t, q in sorted(out.items())}


def _coverage(
    packets: dict[str, WorkPacket],
    proposals: dict[str, VerifiedProposal],
    rows: list[OperationRow],
    conflicts: list[ConflictRow],
) -> list[CoverageRow]:
    members: dict[str, set[str]] = {}
    for pid, pk in packets.items():
        members.setdefault(pk.parent_id or parent_id(pid), set()).add(pid)
    for pid in proposals:
        members.setdefault(parent_id(pid), set()).add(pid)

    out: list[CoverageRow] = []
    for entry in sorted(members):
        subs = sorted(members[entry])
        ops = sorted({f"{r.method} {r.path}" for r in rows if any(s in r.packet_ids for s in subs)})
        c_hits = [
            c
            for c in conflicts
            if c.kind == "incompatible_duplicate" and any(s in c.packet_ids for s in subs)
        ]
        props = [proposals[s] for s in subs if s in proposals]
        reasons: list[str] = []

        def add(r: str, reasons: list[str] = reasons) -> None:
            if r and r not in reasons:
                reasons.append(r)

        for s in subs:
            if s not in proposals:
                add(f"{s}: no verified proposal")
        for p in props:
            for r in p.reasons:
                add(r)
            if p.no_api_needed:
                add(p.no_api_needed)
        statuses = {p.status for p in props}
        if ops:
            status = "mapped"
        elif c_hits:
            status = "conflict"
            for c in c_hits:
                add(f"{c.id}: {c.summary}")
        elif "failed" in statuses or any(s not in proposals for s in subs):
            status = "failed"
        elif "rejected" in statuses:
            status = "rejected"
        else:
            status = "no_api_needed"
            if not props or not any(p.no_api_needed for p in props):
                add("no operations proposed")
        verified_conf = [p.confidence for p in props if p.status == "verified"]
        conf = min(verified_conf) if verified_conf else 0.0
        if not verified_conf and props:
            conf = min(p.confidence for p in props)
        usage = max((packets[s].usage_count for s in subs if s in packets), default=0)
        out.append(
            CoverageRow(
                entry_id=entry,
                status=status,
                operations=ops,
                usage_count=usage,
                confidence=conf,
                reasons=reasons,
            )
        )
    return out


async def run_aggregate_async(
    cfg: Config,
    ws: Workspace,
    bundle: IndexBundle,
    *,
    client: LLMClient | None = None,
    resolve_with_llm: bool = False,
    offline: bool = False,
    force: bool = False,
) -> tuple[Store, list[str]]:
    """Aggregate; returns the opened (rebuilt) store and warnings."""
    warnings: list[str] = []
    store, open_conflicts = build_store(cfg, ws, bundle, warnings)
    store.set_meta("schema", "1")
    if resolve_with_llm and open_conflicts:
        if client is None:
            warnings.append("resolve_with_llm requested but no LLM client given; skipped")
        else:
            try:
                out = await suggest_resolutions(
                    cfg, client, open_conflicts, Cache(ws.cache_dir), offline, force
                )
                write_suggestions(ws.review_dir / "suggestions.yaml", out)
            except (CacheMiss, ResolverError) as exc:
                warnings.append(f"resolver suggestions unavailable: {exc}")
    return store, warnings


def run_aggregate(
    cfg: Config,
    ws: Workspace,
    bundle: IndexBundle,
    *,
    client: LLMClient | None = None,
    resolve_with_llm: bool = False,
    offline: bool = False,
    force: bool = False,
) -> tuple[Store, list[str]]:
    """Sync wrapper of :func:`run_aggregate_async`."""
    return asyncio.run(
        run_aggregate_async(
            cfg,
            ws,
            bundle,
            client=client,
            resolve_with_llm=resolve_with_llm,
            offline=offline,
            force=force,
        )
    )


__all__ = [
    "build_store",
    "derive_error_codes",
    "run_aggregate",
    "run_aggregate_async",
]
