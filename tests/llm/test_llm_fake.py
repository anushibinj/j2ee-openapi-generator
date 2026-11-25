from __future__ import annotations

from pathlib import Path

from j2ee_openapi_generator.llm.fake import FakeLLM
from j2ee_openapi_generator.llm.schemas import EndpointProposal, RenameMap, ResolverOutput

FIX = Path(__file__).parents[1] / "fixtures" / "llm"


async def analyze(f: FakeLLM, item: str) -> object:
    return await f.structured(
        purpose="analyze", item_id=item, messages=[], response_model=EndpointProposal
    )


async def test_analyze_and_default_op() -> None:
    f = FakeLLM(FIX)
    r = await analyze(f, "sales:OrderServlet#list")
    assert r.ok and r.parsed["packet_id"] == "sales:OrderServlet#list"  # type: ignore[attr-defined]
    r2 = await analyze(f, "accounts:UserServlet#<default>")
    assert r2.ok  # type: ignore[attr-defined]


async def test_every_canned_proposal_validates() -> None:
    f = FakeLLM(FIX)
    n = 0
    for p in sorted(FIX.glob("*.json")):
        if p.name.startswith("_") or p.name.endswith(".repair.json"):
            continue
        r = await analyze(f, EndpointProposal.model_validate_json(p.read_text()).packet_id)
        assert r.ok, p.name  # type: ignore[attr-defined]
        n += 1
    assert n == 21


async def test_repair_file_on_second_attempt() -> None:
    f = FakeLLM(FIX)
    first = await analyze(f, "sales:LookupServlet#statuses")
    second = await analyze(f, "sales:LookupServlet#statuses@2")
    assert first.ok and second.ok  # type: ignore[attr-defined]
    assert first.raw != second.raw  # type: ignore[attr-defined]
    assert second.raw == (FIX / "sales--LookupServlet__statuses.repair.json").read_text()  # type: ignore[attr-defined]
    # no repair file: attempt 2 falls back to the normal file
    a = await analyze(f, "sales:OrderServlet#list")
    b = await analyze(f, "sales:OrderServlet#list@2")
    assert a.raw == b.raw  # type: ignore[attr-defined]


async def test_normalize_and_resolve() -> None:
    f = FakeLLM(FIX)
    n = await f.structured(
        purpose="normalize", item_id="Sales Orders", messages=[], response_model=RenameMap
    )
    assert n.ok and n.parsed["tag"] == "Sales Orders"
    r = await f.structured(
        purpose="resolve", item_id="all", messages=[], response_model=ResolverOutput
    )
    assert r.ok and r.parsed["suggestions"][0]["conflict_id"] == "GET /orders/{}"


async def test_missing_and_malformed(tmp_path: Path) -> None:
    f = FakeLLM(tmp_path)
    r = await analyze(f, "a:B#c")
    assert not r.ok and "no canned response" in r.errors[0]  # type: ignore[attr-defined]
    (tmp_path / "a--B__c.json").write_text('{"packet_id": 1}')
    r = await analyze(f, "a:B#c")
    assert not r.ok and r.errors  # type: ignore[attr-defined]
    (tmp_path / "a--B__d.json").write_text("not json")
    assert not (await analyze(f, "a:B#d")).ok  # type: ignore[attr-defined]
