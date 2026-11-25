"""Merge duplicate operations and detect conflicts. Never picks a winner automatically."""

from __future__ import annotations

import re
from collections import defaultdict

from j2ee_openapi_generator.aggregate.models import ConflictRow, OperationRow
from j2ee_openapi_generator.ids import sha256_text
from j2ee_openapi_generator.llm.schemas import (
    Evidence,
    FieldSpec,
    Operation,
    ParamSpec,
    RequestBody,
    ResponseSpec,
    RoleSpec,
)

_PLACEHOLDER = re.compile(r"\{[^}/]*\}")


def normalize_path(path: str) -> str:
    """Dedupe key of a path: positional placeholders, lowercase literals, no trailing slash."""
    stripped = path.strip()
    if len(stripped) > 1:
        stripped = stripped.rstrip("/") or "/"
    parts: list[str] = []
    last = 0
    for m in _PLACEHOLDER.finditer(stripped):
        parts.append(stripped[last : m.start()].lower())
        parts.append("{}")
        last = m.end()
    parts.append(stripped[last:].lower())
    return "".join(parts)


def conflict_id(kind: str, method: str, subject: str) -> str:
    """Stable conflict id: ``c-`` plus 8 hex chars of sha256 over ``kind|METHOD|subject``."""
    return "c-" + sha256_text(f"{kind}|{method.upper()}|{subject}")[:8]


class _Incompatible(Exception):
    pass


def _uniq(items: list[str], more: list[str]) -> list[str]:
    out = list(items)
    for i in more:
        if i not in out:
            out.append(i)
    return out


def _evidence(a: list[Evidence], b: list[Evidence]) -> list[Evidence]:
    out = list(a)
    seen = {(e.file, e.line, e.snippet, e.claim) for e in a}
    for e in b:
        k = (e.file, e.line, e.snippet, e.claim)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _merge_param(a: ParamSpec, b: ParamSpec, where: str) -> ParamSpec:
    if a.type != b.type:
        raise _Incompatible(f"{where} '{a.name}' type {a.type} vs {b.type}")
    if a.format and b.format and a.format != b.format:
        raise _Incompatible(f"{where} '{a.name}' format {a.format} vs {b.format}")
    return a.model_copy(
        update={
            "format": a.format or b.format,
            "required": a.required or b.required,
            "description": a.description or b.description,
            "enum": _uniq(a.enum, b.enum),
            "default": a.default if a.default is not None else b.default,
            "legacy_name": a.legacy_name or b.legacy_name,
            "evidence": _evidence(a.evidence, b.evidence),
        }
    )


def _merge_path_params(a: list[ParamSpec], b: list[ParamSpec]) -> list[ParamSpec]:
    out: list[ParamSpec] = []
    for i in range(max(len(a), len(b))):
        if i < len(a) and i < len(b):
            out.append(_merge_param(a[i], b[i], "path param"))
        else:
            out.append((a if i < len(a) else b)[i])
    return out


def _merge_query_params(a: list[ParamSpec], b: list[ParamSpec]) -> list[ParamSpec]:
    out = list(a)
    index = {p.name: i for i, p in enumerate(out)}
    for p in b:
        if p.name in index:
            out[index[p.name]] = _merge_param(out[index[p.name]], p, "query param")
        else:
            index[p.name] = len(out)
            out.append(p)
    return out


def _merge_fields(a: list[FieldSpec], b: list[FieldSpec]) -> list[FieldSpec]:
    names = {f.name for f in a}
    return list(a) + [f for f in b if f.name not in names]


def _merge_body(a: RequestBody | None, b: RequestBody | None) -> RequestBody | None:
    if a is None or b is None:
        return a or b
    if a.schema_ref != b.schema_ref:
        raise _Incompatible(f"request body schema {a.schema_ref} vs {b.schema_ref}")
    return a.model_copy(
        update={
            "fields": _merge_fields(a.fields, b.fields),
            "required": _uniq(a.required, b.required),
        }
    )


def _merge_responses(a: list[ResponseSpec], b: list[ResponseSpec]) -> list[ResponseSpec]:
    by_status: dict[int, ResponseSpec] = {r.status: r for r in a}
    for r in b:
        cur = by_status.get(r.status)
        if cur is None:
            by_status[r.status] = r
            continue
        if cur.schema_ref != r.schema_ref:
            raise _Incompatible(f"response {r.status} schema {cur.schema_ref} vs {r.schema_ref}")
        by_status[r.status] = cur.model_copy(
            update={
                "description": cur.description or r.description,
                "content_type": cur.content_type or r.content_type,
                "fields": _merge_fields(cur.fields, r.fields),
                "evidence": _evidence(cur.evidence, r.evidence),
            }
        )
    return [by_status[s] for s in sorted(by_status)]


def _merge_roles(a: list[RoleSpec], b: list[RoleSpec]) -> list[RoleSpec]:
    out = list(a)
    index = {r.name: i for i, r in enumerate(out)}
    for r in b:
        if r.name in index:
            cur = out[index[r.name]]
            out[index[r.name]] = cur.model_copy(
                update={"evidence": _evidence(cur.evidence, r.evidence)}
            )
        else:
            index[r.name] = len(out)
            out.append(r)
    return out


def merge_pair(a: Operation, b: Operation) -> Operation:
    """Merge two compatible operations; raises ``ValueError`` when incompatible."""
    try:
        return _merge_pair(a, b)
    except _Incompatible as exc:
        raise ValueError(str(exc)) from exc


def _merge_pair(a: Operation, b: Operation) -> Operation:
    src = a.legacy_source.model_copy(
        update={
            "jsps": _uniq(a.legacy_source.jsps, b.legacy_source.jsps),
            "http_methods": _uniq(a.legacy_source.http_methods, b.legacy_source.http_methods),
        }
    )
    return a.model_copy(
        update={
            "path_params": _merge_path_params(a.path_params, b.path_params),
            "query_params": _merge_query_params(a.query_params, b.query_params),
            "request_body": _merge_body(a.request_body, b.request_body),
            "responses": _merge_responses(a.responses, b.responses),
            "roles": _merge_roles(a.roles, b.roles),
            "idempotent": a.idempotent or b.idempotent,
            "legacy_source": src,
            "ejb_methods": _uniq(a.ejb_methods, b.ejb_methods),
            "evidence": _evidence(a.evidence, b.evidence),
            "hint_override_reason": a.hint_override_reason or b.hint_override_reason,
        }
    )


def operation_key(op: Operation) -> tuple[str, str]:
    return op.http_method, normalize_path(op.path)


def merge_operations(
    ops: list[tuple[str, Operation]],
) -> tuple[list[OperationRow], list[ConflictRow]]:
    """Group by (method, normalized path) and merge compatible duplicates.

    ``ops`` are ``(packet_id, operation)`` pairs sorted by packet id. Incompatible groups
    produce an ``incompatible_duplicate`` conflict and no row. Rows sharing an EJB method and
    HTTP method under different paths produce an advisory ``ejb_path_mismatch`` conflict.
    """
    groups: dict[tuple[str, str], list[tuple[str, Operation]]] = defaultdict(list)
    for pid, op in ops:
        groups[operation_key(op)].append((pid, op))

    rows: list[OperationRow] = []
    conflicts: list[ConflictRow] = []
    for key in sorted(groups):
        members = groups[key]
        method, norm = key
        pids = sorted({p for p, _ in members})
        merged = members[0][1]
        reason: str | None = None
        for _, other in members[1:]:
            try:
                merged = _merge_pair(merged, other)
            except _Incompatible as exc:
                reason = str(exc)
                break
        if reason is not None:
            conflicts.append(
                ConflictRow(
                    id=conflict_id("incompatible_duplicate", method, norm),
                    kind="incompatible_duplicate",
                    summary=f"{method} {norm}: incompatible duplicate from "
                    f"{', '.join(pids)} ({reason})",
                    packet_ids=pids,
                    proposals=[
                        {
                            "packet_id": p,
                            "key": f"{method} {norm}",
                            "operation": o.model_dump(mode="json"),
                        }
                        for p, o in members
                    ],
                )
            )
            continue
        rows.append(
            OperationRow(
                method=method,
                path=merged.path.strip().rstrip("/") or "/",
                operation=merged,
                packet_ids=pids,
            )
        )

    conflicts.extend(_ejb_mismatches(rows))
    conflicts.sort(key=lambda c: (c.kind, c.id))
    return rows, conflicts


def _ejb_mismatches(rows: list[OperationRow]) -> list[ConflictRow]:
    by_ejb: dict[tuple[str, str], list[OperationRow]] = defaultdict(list)
    for row in rows:
        for ejb in row.operation.ejb_methods:
            by_ejb[(row.method, ejb)].append(row)
    out: list[ConflictRow] = []
    for (method, ejb), members in sorted(by_ejb.items()):
        norms = sorted({normalize_path(r.path) for r in members})
        if len(norms) < 2:
            continue
        pids = sorted({p for r in members for p in r.packet_ids})
        out.append(
            ConflictRow(
                id=conflict_id("ejb_path_mismatch", method, ejb),
                kind="ejb_path_mismatch",
                summary=f"{method} {ejb} is exposed under different paths: {', '.join(norms)}",
                packet_ids=pids,
                proposals=[
                    {
                        "packet_id": p,
                        "key": f"{method} {normalize_path(r.path)}",
                        "ejb_method": ejb,
                        "operation": r.operation.model_dump(mode="json"),
                    }
                    for r in sorted(members, key=lambda r: normalize_path(r.path))
                    for p in r.packet_ids
                ],
            )
        )
    return out
