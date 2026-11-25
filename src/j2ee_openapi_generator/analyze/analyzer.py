"""Analyze stage: send each work packet to the LLM and store one ProposalRecord per packet."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from pydantic import ValidationError

from j2ee_openapi_generator.aggregate.models import ProposalRecord
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import id_to_filename, sha256_text
from j2ee_openapi_generator.llm.base import LLMClient, StructuredResult
from j2ee_openapi_generator.llm.cache import Cache, CacheMiss, cache_key
from j2ee_openapi_generator.llm.client import strict_schema
from j2ee_openapi_generator.llm.schemas import SCHEMA_VERSION, EndpointProposal
from j2ee_openapi_generator.packets.models import WorkPacket
from j2ee_openapi_generator.prompts import PROMPT_VERSION, render_prompt
from j2ee_openapi_generator.workspace import Workspace, read_json, write_json

FEEDBACK_HEADING = "Previous attempt was rejected for these reasons, fix them:"


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)


def render_packet_text(packet: WorkPacket) -> str:
    """Deterministic prompt text for one packet."""
    parts: list[str] = [
        "## Entry point",
        _json(packet.meta.model_dump(mode="json")),
        "## Mapping hints",
        _json(packet.hints.model_dump(mode="json")),
        "## Properties",
        _json(packet.properties),
        "## Usage count",
        str(packet.usage_count),
        "## Source",
    ]
    parts.extend(chunk.text.rstrip("\n") for chunk in packet.chunks)
    return "\n".join(parts) + "\n"


def _feedback_text(feedback: list[str] | None) -> str:
    if not feedback:
        return ""
    return FEEDBACK_HEADING + "\n" + "\n".join(f"- {r}" for r in feedback)


def build_messages(
    packet: WorkPacket, style_guide: str, feedback: list[str] | None = None
) -> list[dict[str, str]]:
    schema = json.dumps(strict_schema(EndpointProposal), sort_keys=True, separators=(",", ":"))
    system = render_prompt(
        "analyzer",
        style_guide=style_guide,
        schema=schema,
        packet=render_packet_text(packet),
        feedback=_feedback_text(feedback),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Analyze packet {packet.id}. Return only the JSON."},
    ]


def load_packets(ws: Workspace) -> list[WorkPacket]:
    if not ws.packets_dir.is_dir():
        return []
    packets = [
        WorkPacket.model_validate(read_json(p)) for p in sorted(ws.packets_dir.glob("*.json"))
    ]
    return sorted(packets, key=lambda p: p.id)


def _selected(
    packets: list[WorkPacket], only: str | None, only_ids: set[str] | None
) -> list[WorkPacket]:
    out = packets
    if only is not None:
        out = [
            p for p in out if p.id == only or p.parent_id == only or p.id.startswith(only + "~p")
        ]
    if only_ids is not None:
        out = [p for p in out if p.id in only_ids]
    return out


def _record_path(ws: Workspace, packet_id: str) -> Any:
    return ws.proposals_dir / f"{id_to_filename(packet_id)}.json"


def _to_record(packet: WorkPacket, attempt: int, key: str, res: StructuredResult) -> ProposalRecord:
    if not res.ok or res.parsed is None:
        return ProposalRecord(
            packet_id=packet.id,
            attempt=attempt,
            ok=False,
            errors=list(res.errors) or ["LLM call failed"],
            cache_key=key,
        )
    try:
        proposal = EndpointProposal.model_validate(res.parsed)
    except ValidationError as exc:
        return ProposalRecord(
            packet_id=packet.id,
            attempt=attempt,
            ok=False,
            errors=[f"invalid proposal: {exc}"],
            cache_key=key,
        )
    proposal.packet_id = packet.id
    return ProposalRecord(
        packet_id=packet.id, attempt=attempt, ok=True, proposal=proposal, cache_key=key
    )


def _result_from_cache(entry: dict[str, Any]) -> StructuredResult:
    return StructuredResult(
        ok=bool(entry.get("ok", False)),
        parsed=entry.get("parsed"),
        raw=str(entry.get("raw", "")),
        errors=list(entry.get("errors", [])),
        usage=dict(entry.get("usage", {})),
        request=dict(entry.get("request", {})),
    )


async def run_analyze(
    cfg: Config,
    client: LLMClient,
    ws: Workspace,
    *,
    only: str | None = None,
    offline: bool = False,
    force: bool = False,
    attempt: int = 1,
    feedback: dict[str, list[str]] | None = None,
    only_ids: set[str] | None = None,
) -> list[ProposalRecord]:
    ws.ensure()
    packets = _selected(load_packets(ws), only, only_ids)
    style_guide = cfg.style_guide_path.read_text(encoding="utf-8")
    style_hash = sha256_text(style_guide)
    cache = Cache(ws.cache_dir)

    keys = {
        p.id: cache_key(
            p.packet_hash,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            style_hash,
            client.model_name,
            client.structured_mode,
            client.temperature,
            attempt,
        )
        for p in packets
    }
    entries: dict[str, dict[str, Any] | None] = {
        p.id: (None if force else cache.get(keys[p.id])) for p in packets
    }
    # A cached failure is retried online; offline it is replayed as a failure.
    for pid, entry in entries.items():
        if entry is not None and not entry.get("ok", False) and not offline:
            entries[pid] = None

    if offline:
        misses = [p.id for p in packets if entries[p.id] is None]
        if misses:
            raise CacheMiss(
                f"offline analyze: {len(misses)} packet(s) not in cache: " + ", ".join(misses)
            )

    sem = asyncio.Semaphore(max(1, cfg.llm.max_concurrency))

    async def one(packet: WorkPacket) -> ProposalRecord:
        key = keys[packet.id]
        entry = entries[packet.id]
        if entry is not None:
            return _to_record(packet, attempt, key, _result_from_cache(entry))
        messages = build_messages(packet, style_guide, (feedback or {}).get(packet.id))
        item_id = packet.id if attempt == 1 else f"{packet.id}@{attempt}"
        async with sem:
            try:
                res = await client.structured(
                    purpose="analyze",
                    item_id=item_id,
                    messages=messages,
                    response_model=EndpointProposal,
                )
            except Exception as exc:  # noqa: BLE001 - one packet must not abort the run
                res = StructuredResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
        cache.put(
            key,
            request=res.request or {"messages": messages},
            raw=res.raw,
            parsed=res.parsed,
            ok=res.ok,
            errors=res.errors,
            usage=res.usage,
        )
        return _to_record(packet, attempt, key, res)

    records = list(await asyncio.gather(*(one(p) for p in packets)))
    records.sort(key=lambda r: r.packet_id)

    for rec in records:
        path = _record_path(ws, rec.packet_id)
        if attempt >= 2 and path.is_file():
            try:
                prev = read_json(path)
            except ValueError:
                prev = {}
            if prev.get("attempt", 1) == 1:
                shutil.copyfile(path, path.with_name(path.stem + ".attempt1.json"))
        write_json(path, rec.model_dump(mode="json"))
    return records


def analyze_sync(
    cfg: Config,
    client: LLMClient,
    ws: Workspace,
    *,
    only: str | None = None,
    offline: bool = False,
    force: bool = False,
    attempt: int = 1,
    feedback: dict[str, list[str]] | None = None,
    only_ids: set[str] | None = None,
) -> list[ProposalRecord]:
    return asyncio.run(
        run_analyze(
            cfg,
            client,
            ws,
            only=only,
            offline=offline,
            force=force,
            attempt=attempt,
            feedback=feedback,
            only_ids=only_ids,
        )
    )


def load_proposals(ws: Workspace) -> dict[str, ProposalRecord]:
    out: dict[str, ProposalRecord] = {}
    if not ws.proposals_dir.is_dir():
        return out
    for p in sorted(ws.proposals_dir.glob("*.json")):
        if p.name.endswith(".attempt1.json"):
            continue
        rec = ProposalRecord.model_validate(read_json(p))
        out[rec.packet_id] = rec
    return dict(sorted(out.items()))


def analysis_summary(records: list[ProposalRecord]) -> dict[str, int]:
    ok = sum(1 for r in records if r.ok)
    return {"total": len(records), "ok": ok, "failed": len(records) - ok}
