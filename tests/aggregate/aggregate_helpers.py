"""Shared helpers for aggregate tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from j2ee_openapi_generator.aggregate.models import VerifiedProposal
from j2ee_openapi_generator.llm.schemas import (
    EndpointProposal,
    Evidence,
    LegacySource,
    Operation,
    ParamSpec,
    ResponseSpec,
)
from j2ee_openapi_generator.packets.models import EntryPointMeta, MappingHints, WorkPacket

LLM_DIR = Path(__file__).parents[1] / "fixtures" / "llm"


def op(
    method: str = "GET",
    path: str = "/users/{id}",
    *,
    param_type: str = "integer",
    schema: str = "User",
    ejb: list[str] | None = None,
    **extra: Any,
) -> Operation:
    data: dict[str, Any] = {
        "http_method": method,
        "path": path,
        "summary": "s",
        "resource": "users",
        "tag": "Users",
        "path_params": [ParamSpec(name=n, type=param_type) for n in _names(path)],
        "responses": [ResponseSpec(status=200, schema_ref=schema)],
        "legacy_source": LegacySource(module="m", servlet="S", op="o"),
        "ejb_methods": ejb or [],
    }
    data.update(extra)
    return Operation.model_validate(data)


def _names(path: str) -> list[str]:
    import re

    return re.findall(r"\{([^}]*)\}", path)


def ev(line: int = 1, claim: str = "c") -> Evidence:
    return Evidence(claim=claim, file="F.java", line=line, snippet="x")


def verified(pid: str, ops: list[Operation], **kw: Any) -> VerifiedProposal:
    return VerifiedProposal(
        packet_id=pid, status=kw.pop("status", "verified"), operations=ops, **kw
    )


def packet(pid: str, usage: int = 0) -> WorkPacket:
    module, rest = pid.split(":", 1)
    servlet, _, opn = rest.partition("#")
    base, _, _ = pid.partition("~")
    return WorkPacket(
        id=pid,
        parent_id=base if "~" in pid else None,
        meta=EntryPointMeta(
            id=base,
            module=module,
            servlet_fqcn=f"x.{servlet}",
            op=opn.split("~")[0],
            param_name="op",
            style="if",
            http_methods=["GET"],
            url_patterns=["/x"],
        ),
        hints=MappingHints(http_method="GET", shape="item"),
        usage_count=usage,
    )


def canned_proposals() -> list[EndpointProposal]:
    """All canned analyzer outputs; the bad-citation statuses file is replaced by its repair."""
    out: list[EndpointProposal] = []
    for f in sorted(LLM_DIR.glob("*.json")):
        if f.name.startswith("_") or f.name == "sales--LookupServlet__statuses.json":
            continue
        out.append(EndpointProposal.model_validate(json.loads(f.read_text(encoding="utf-8"))))
    return out


def to_verified(p: EndpointProposal) -> VerifiedProposal:
    return VerifiedProposal(
        packet_id=p.packet_id,
        status="no_api_needed" if p.no_api_needed else "verified",
        operations=p.operations,
        no_api_needed=p.no_api_needed,
        open_questions=p.open_questions,
        lookups_needed=[lk.type for lk in p.lookups_needed],
        confidence=p.confidence,
    )
