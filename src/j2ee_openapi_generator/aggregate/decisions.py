"""Human review decisions (``review/decisions.yaml``) applied to verified proposals."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from j2ee_openapi_generator.aggregate.models import Decision, DecisionsFile, VerifiedProposal
from j2ee_openapi_generator.llm.schemas import Operation, RoleSpec

_PLACEHOLDER = re.compile(r"\{([^}/]*)\}")

#: Operation fields a human may override (``match`` selects one operation, see below).
OVERRIDABLE = frozenset(
    {
        "summary",
        "path",
        "http_method",
        "tag",
        "resource",
        "idempotent",
        "roles",
        "hint_override_reason",
    }
)


class DecisionsError(ValueError):
    """The decisions file is invalid. The message names the offending entry key."""


def load_decisions(path: Path) -> DecisionsFile:
    """Load and validate a decisions file. A missing or empty file yields no decisions."""
    if not path.is_file():
        return DecisionsFile()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DecisionsError(f"{path}: invalid YAML: {exc}") from exc
    if raw is None:
        return DecisionsFile()
    if not isinstance(raw, dict):
        raise DecisionsError(f"{path}: top level must be a mapping with a 'decisions' list")
    extra = sorted(set(raw) - {"version", "decisions"})
    if extra:
        raise DecisionsError(f"{path}: unknown top-level keys: {', '.join(map(str, extra))}")
    entries = raw.get("decisions") or []
    if not isinstance(entries, list):
        raise DecisionsError(f"{path}: 'decisions' must be a list")
    version = raw.get("version", 1)
    if version != 1:
        raise DecisionsError(f"{path}: unsupported version {version!r}")
    parsed: list[Decision] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        label = (
            f"entry {i + 1} (key {entry['key']!r})"
            if isinstance(entry, dict) and "key" in entry
            else f"entry {i + 1}"
        )
        if not isinstance(entry, dict):
            raise DecisionsError(f"{path}: {label} must be a mapping")
        try:
            d = Decision.model_validate(entry)
        except ValidationError as exc:
            msgs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise DecisionsError(f"{path}: {label}: {msgs}") from exc
        problem = _check_decision(d)
        if problem:
            raise DecisionsError(f"{path}: {label}: {problem}")
        if d.key in seen:
            raise DecisionsError(f"{path}: {label}: duplicate key")
        seen.add(d.key)
        parsed.append(d)
    return DecisionsFile(version=1, decisions=parsed)


def _check_decision(d: Decision) -> str | None:
    if d.action == "drop" and not (d.reason and d.reason.strip()):
        return "a drop decision requires a reason"
    if d.action == "rename_path":
        if not d.new_path or not d.new_path.startswith("/"):
            return "rename_path requires new_path starting with '/'"
    if d.action == "override" and not d.overrides:
        return "override requires a non-empty overrides mapping"
    return None


def is_conflict_key(key: str) -> bool:
    return key.startswith("c-")


def parent_id(packet_id: str) -> str:
    return packet_id.split("~", 1)[0]


def _matches(key: str, packet_id: str) -> bool:
    return key == packet_id or key == parent_id(packet_id)


def set_operation_path(op: Operation, new_path: str, warnings: list[str]) -> Operation:
    """New path for ``op``; path parameter names follow the new placeholders positionally."""
    names = _PLACEHOLDER.findall(new_path)
    params = list(op.path_params)
    if names and len(names) == len(params):
        params = [
            p.model_copy(update={"name": n, "legacy_name": p.legacy_name or p.name})
            if p.name != n
            else p
            for p, n in zip(params, names, strict=True)
        ]
    elif len(names) != len(params):
        warnings.append(
            f"path {new_path} has {len(names)} placeholders but the operation has "
            f"{len(params)} path params; params left unchanged"
        )
    return op.model_copy(update={"path": new_path, "path_params": params})


def _selected(op: Operation, match: str | None) -> bool:
    if not match:
        return True
    method, _, path = match.partition(" ")
    from j2ee_openapi_generator.aggregate.dedupe import normalize_path

    return op.http_method == method.upper() and normalize_path(op.path) == normalize_path(path)


def apply_overrides(
    op: Operation, overrides: Mapping[str, object], warnings: list[str]
) -> Operation:
    """Apply field overrides, re-validating the model. Bad keys or values warn, never crash."""
    updates: dict[str, Any] = {}
    for k, v in overrides.items():
        if k == "match":
            continue
        if k not in OVERRIDABLE:
            warnings.append(f"override key '{k}' is not allowed and was ignored")
            continue
        if k == "roles" and isinstance(v, list):
            v = [RoleSpec(name=r).model_dump() if isinstance(r, str) else r for r in v]
        updates[k] = v
    new_path = updates.pop("path", None)
    try:
        data = {**op.model_dump(), **updates}
        result = Operation.model_validate(data)
    except ValidationError as exc:
        warnings.append(f"override rejected: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})")
        return op
    if isinstance(new_path, str):
        result = set_operation_path(result, new_path, warnings)
    return result


def apply_packet_decisions(
    proposals: Mapping[str, VerifiedProposal], decisions: DecisionsFile
) -> tuple[dict[str, VerifiedProposal], list[str], set[str]]:
    """Apply packet-keyed decisions.

    Returns the new proposals, warnings and the set of decision keys that were consumed
    (matched a packet). Conflict-keyed decisions are left for :mod:`conflicts`.
    """
    out = {k: v.model_copy(deep=True) for k, v in proposals.items()}
    warnings: list[str] = []
    used: set[str] = set()
    for d in decisions.decisions:
        if is_conflict_key(d.key):
            continue
        targets = sorted(pid for pid in out if _matches(d.key, pid))
        if not targets:
            continue
        used.add(d.key)
        for pid in targets:
            out[pid] = _apply_one(out[pid], d, warnings)
    return out, warnings, used


def _apply_one(p: VerifiedProposal, d: Decision, warnings: list[str]) -> VerifiedProposal:
    if d.action == "drop":
        return p.model_copy(
            update={
                "status": "rejected",
                "operations": [],
                "reasons": [*p.reasons, f"dropped: {d.reason}"],
            }
        )
    if p.status == "failed":
        warnings.append(f"{d.key}: cannot {d.action} failed packet {p.packet_id}")
        return p
    if p.status == "rejected":
        if not p.operations:
            warnings.append(f"{d.key}: rejected packet {p.packet_id} has no operations to accept")
            return p
        p = p.model_copy(
            update={
                "status": "verified",
                "reasons": [*p.reasons, f"accepted by decision: {d.reason or d.action}"],
            }
        )
    if d.action == "accept":
        if p.status == "no_api_needed" or not p.operations:
            warnings.append(f"{d.key}: nothing to accept for {p.packet_id}")
        return p
    if p.status == "no_api_needed" or not p.operations:
        warnings.append(f"{d.key}: {p.packet_id} has no operations to {d.action}")
        return p
    match = d.overrides.get("match") if d.action == "override" else None
    ops: list[Operation] = []
    hit = False
    for op in p.operations:
        if not _selected(op, match if isinstance(match, str) else None):
            ops.append(op)
            continue
        hit = True
        if d.action == "override":
            ops.append(apply_overrides(op, d.overrides, warnings))
        else:
            assert d.new_path is not None
            ops.append(set_operation_path(op, d.new_path, warnings))
    if not hit:
        warnings.append(f"{d.key}: no operation of {p.packet_id} matched {match!r}")
    return p.model_copy(update={"operations": ops})
