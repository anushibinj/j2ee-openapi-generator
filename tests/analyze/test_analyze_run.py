from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from j2ee_openapi_generator.analyze.analyzer import (
    analysis_summary,
    analyze_sync,
    build_messages,
    load_proposals,
    render_packet_text,
    run_analyze,
)
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import id_to_filename
from j2ee_openapi_generator.llm.base import StructuredResult
from j2ee_openapi_generator.llm.cache import CacheMiss
from j2ee_openapi_generator.llm.fake import FakeLLM
from j2ee_openapi_generator.packets.models import (
    EntryPointMeta,
    MappingHints,
    SourceChunk,
    WorkPacket,
)
from j2ee_openapi_generator.workspace import Workspace, read_json, write_json

LLM_DIR = Path(__file__).parents[1] / "fixtures" / "llm"
IDS = [
    "accounts:UserServlet#list",
    "accounts:UserServlet#read",
    "sales:LookupServlet#statuses",
]


def make_packet(pid: str, parent: str | None = None) -> WorkPacket:
    module, rest = pid.split(":", 1)
    servlet, op = rest.split("#", 1)
    return WorkPacket(
        id=pid,
        parent_id=parent,
        meta=EntryPointMeta(
            id=pid,
            module=module,
            servlet_fqcn=f"com.acme.{module}.{servlet}",
            op=op,
            param_name="op",
            style="if_chain",
            http_methods=["GET"],
            url_patterns=["/x"],
        ),
        chunks=[
            SourceChunk(
                file="a/B.java",
                start_line=41,
                end_line=42,
                role="branch",
                text="=== FILE a/B.java (lines 41-42) ===\n0041| int x = 1;\n0042| return;\n",
            )
        ],
        hints=MappingHints(http_method="GET", shape="collection"),
        packet_hash=f"hash-{pid}",
        usage_count=3,
    )


def setup(tmp_path: Path, ids: list[str] = IDS, max_conc: int = 4) -> tuple[Config, Workspace]:
    style = tmp_path / "style.md"
    style.write_text("STYLE-GUIDE-MARKER\n", encoding="utf-8")
    cfg = Config(
        repo_root=tmp_path,
        work_dir=tmp_path / "work",
        out_dir=tmp_path / "out",
        review_dir=tmp_path / "review",
        style_guide=style,
    )
    cfg.llm.max_concurrency = max_conc
    ws = Workspace(cfg)
    ws.ensure()
    for pid in ids:
        write_json(
            ws.packets_dir / f"{id_to_filename(pid)}.json", make_packet(pid).model_dump(mode="json")
        )
    return cfg, ws


class Recorder:
    """Client double wrapping FakeLLM; records calls and in-flight concurrency."""

    def __init__(self, inner: FakeLLM) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    model_name = "fake"
    structured_mode = "json_schema"
    temperature = 0.0

    async def structured(self, **kw: Any) -> StructuredResult:
        self.calls.append(kw["item_id"])
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        for _ in range(5):
            await asyncio.sleep(0)
        try:
            return await self.inner.structured(**kw)
        finally:
            self.in_flight -= 1


class Forbidden:
    model_name = "fake"
    structured_mode = "json_schema"
    temperature = 0.0

    async def structured(self, **kw: Any) -> StructuredResult:
        raise AssertionError("client must not be called")


async def test_miss_then_hit(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path)
    rec = Recorder(FakeLLM(LLM_DIR))
    recs = await run_analyze(cfg, rec, ws)
    assert [r.packet_id for r in recs] == sorted(IDS)
    assert all(r.ok and r.proposal for r in recs)
    assert len(rec.calls) == 3
    again = await run_analyze(cfg, Forbidden(), ws)
    assert [r.model_dump() for r in again] == [r.model_dump() for r in recs]
    loaded = load_proposals(ws)
    assert sorted(loaded) == sorted(IDS)
    assert analysis_summary(recs) == {"total": 3, "ok": 3, "failed": 0}


async def test_offline_lists_all_misses(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path)
    with pytest.raises(CacheMiss) as ei:
        await run_analyze(cfg, Forbidden(), ws, offline=True)
    for pid in IDS:
        assert pid in str(ei.value)
    assert not list(ws.proposals_dir.glob("*.json"))


async def test_offline_hit(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path)
    await run_analyze(cfg, FakeLLM(LLM_DIR), ws)
    recs = await run_analyze(cfg, Forbidden(), ws, offline=True)
    assert len(recs) == 3


async def test_force_bypasses_cache(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path)
    await run_analyze(cfg, FakeLLM(LLM_DIR), ws)
    rec = Recorder(FakeLLM(LLM_DIR))
    await run_analyze(cfg, rec, ws, force=True)
    assert len(rec.calls) == 3


async def test_concurrency_bound(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path, max_conc=2)
    rec = Recorder(FakeLLM(LLM_DIR))
    await run_analyze(cfg, rec, ws)
    assert rec.max_in_flight == 2


async def test_failure_record(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path, ids=["accounts:UserServlet#list", "nope:Missing#x"])
    recs = await run_analyze(cfg, FakeLLM(LLM_DIR), ws)
    by = {r.packet_id: r for r in recs}
    assert by["accounts:UserServlet#list"].ok
    bad = by["nope:Missing#x"]
    assert not bad.ok and bad.proposal is None and bad.errors
    assert analysis_summary(recs)["failed"] == 1
    assert load_proposals(ws)["nope:Missing#x"].ok is False


async def test_repair_attempt(tmp_path: Path) -> None:
    pid = "sales:LookupServlet#statuses"
    cfg, ws = setup(tmp_path, ids=[pid])
    first = (await run_analyze(cfg, FakeLLM(LLM_DIR), ws))[0]
    rec = Recorder(FakeLLM(LLM_DIR))
    second = (
        await run_analyze(cfg, rec, ws, attempt=2, feedback={pid: ["bad citation line 9999"]})
    )[0]
    assert rec.calls == [f"{pid}@2"]
    assert second.attempt == 2 and second.ok
    assert second.cache_key != first.cache_key
    assert second.proposal != first.proposal
    stem = id_to_filename(pid)
    assert (ws.proposals_dir / f"{stem}.attempt1.json").is_file()
    assert read_json(ws.proposals_dir / f"{stem}.attempt1.json")["attempt"] == 1
    assert read_json(ws.proposals_dir / f"{stem}.json")["attempt"] == 2
    assert list(load_proposals(ws)) == [pid]


async def test_feedback_in_prompt(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path, ids=["sales:LookupServlet#statuses"])
    seen: list[str] = []

    class Spy(Recorder):
        async def structured(self, **kw: Any) -> StructuredResult:
            seen.append(kw["messages"][0]["content"])
            return await super().structured(**kw)

    await run_analyze(
        cfg,
        Spy(FakeLLM(LLM_DIR)),
        ws,
        attempt=2,
        feedback={"sales:LookupServlet#statuses": ["reason one"]},
    )
    assert "Previous attempt was rejected for these reasons, fix them" in seen[0]
    assert "- reason one" in seen[0]


async def test_only_filter(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path)
    rec = Recorder(FakeLLM(LLM_DIR))
    recs = await run_analyze(cfg, rec, ws, only="accounts:UserServlet#read")
    assert [r.packet_id for r in recs] == ["accounts:UserServlet#read"]
    recs = await run_analyze(cfg, rec, ws, only_ids={"accounts:UserServlet#list"})
    assert [r.packet_id for r in recs] == ["accounts:UserServlet#list"]


async def test_only_matches_subpackets(tmp_path: Path) -> None:
    ids = ["accounts:UserServlet#list~p1", "accounts:UserServlet#list~p2", "x:Y#z"]
    cfg, ws = setup(tmp_path, ids=[])
    for pid in ids:
        parent = pid.split("~")[0] if "~" in pid else None
        write_json(
            ws.packets_dir / f"{id_to_filename(pid)}.json",
            make_packet(pid, parent).model_dump(mode="json"),
        )
    recs = await run_analyze(cfg, FakeLLM(LLM_DIR), ws, only="accounts:UserServlet#list")
    assert [r.packet_id for r in recs] == ids[:2]


def test_sync_wrapper_deterministic_bytes(tmp_path: Path) -> None:
    cfg, ws = setup(tmp_path)
    analyze_sync(cfg, FakeLLM(LLM_DIR), ws)
    snap = {p.name: p.read_bytes() for p in ws.proposals_dir.glob("*.json")}
    analyze_sync(cfg, Forbidden(), ws, offline=True)
    assert snap == {p.name: p.read_bytes() for p in ws.proposals_dir.glob("*.json")}


def test_prompt_text() -> None:
    pkt = make_packet("accounts:UserServlet#list")
    text = render_packet_text(pkt)
    assert "0041| int x = 1;" in text and "=== FILE a/B.java" in text
    assert text == render_packet_text(pkt)
    msgs = build_messages(pkt, "MY-STYLE", ["why"])
    assert msgs[0]["role"] == "system" and "MY-STYLE" in msgs[0]["content"]
    assert "0042| return;" in msgs[0]["content"]
    assert "- why" in msgs[0]["content"]
    assert msgs[1]["content"] == "Analyze packet accounts:UserServlet#list. Return only the JSON."
    assert "Previous attempt" not in build_messages(pkt, "S", None)[0]["content"]
