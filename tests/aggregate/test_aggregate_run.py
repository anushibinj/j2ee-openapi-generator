import asyncio
from pathlib import Path

import yaml
from aggregate_helpers import LLM_DIR, canned_proposals, packet, to_verified

from j2ee_openapi_generator.aggregate.conflicts import suggest_resolutions, write_suggestions
from j2ee_openapi_generator.aggregate.run import derive_error_codes, run_aggregate
from j2ee_openapi_generator.config import load_config
from j2ee_openapi_generator.index.models import IndexBundle, PropertiesBundle
from j2ee_openapi_generator.llm.cache import Cache
from j2ee_openapi_generator.llm.fake import FakeLLM
from j2ee_openapi_generator.workspace import Workspace, write_json

CONFIG = Path(__file__).parents[1] / "fixtures" / "oapi.yaml"
CONFLICT_KEY = "GET /orders/{}"


def _setup(tmp_path: Path, decisions: str | None = None, extra_packets: bool = True) -> Workspace:
    cfg = load_config(CONFIG)
    cfg.work_dir = tmp_path / "work"
    cfg.review_dir = tmp_path / "review"
    ws = Workspace(cfg)
    ws.ensure()
    for p in canned_proposals():
        v = to_verified(p)
        write_json(
            ws.verified_dir / f"{v.packet_id.replace('#', '__').replace(':', '--')}.json",
            v.model_dump(mode="json"),
        )
        pk = packet(p.packet_id, usage=3)
        write_json(
            ws.packets_dir / f"{pk.id.replace('#', '__').replace(':', '--')}.json",
            pk.model_dump(mode="json"),
        )
    if decisions is not None:
        ws.review_dir.mkdir(parents=True, exist_ok=True)
        (ws.review_dir / "decisions.yaml").write_text(decisions, encoding="utf-8")
    return ws


def test_conflict_and_coverage(tmp_path: Path) -> None:
    ws = _setup(tmp_path)
    store, warnings = run_aggregate(ws.cfg, ws, IndexBundle())
    assert warnings == []
    ops = store.operations()
    assert len(ops) == 16  # 18 minus the conflicting pair
    assert not any(r.path == "/orders/{orderId}" for r in ops)
    conflicts = store.conflicts()
    inc = [c for c in conflicts if c.kind == "incompatible_duplicate"]
    assert len(inc) == 1
    assert inc[0].packet_ids == ["sales:OrderServlet#detail", "sales:OrderServlet#view"]
    cov = {c.entry_id: c for c in store.coverage()}
    assert len(cov) == 21
    assert cov["sales:OrderServlet#view"].status == "conflict"
    assert cov["sales:OrderServlet#detail"].status == "conflict"
    assert cov["accounts:UserServlet#edit"].status == "no_api_needed"
    assert cov["accounts:UserServlet#save"].status == "mapped"
    assert cov["accounts:UserServlet#save"].operations == ["POST /users", "PUT /users/{userId}"]
    assert cov["accounts:UserServlet#list"].usage_count == 3
    assert {c.status for c in cov.values()} <= {"mapped", "no_api_needed", "conflict"}
    snap = store.snapshot([], {})
    assert snap.lookup_types == ["countries", "statuses"]
    assert snap.open_questions["Sales Orders"]
    store.close()


def test_rename_path_decision_resolves(tmp_path: Path) -> None:
    ws = _setup(
        tmp_path,
        "decisions:\n"
        "  - key: 'sales:OrderServlet#detail'\n"
        "    action: rename_path\n"
        "    new_path: /orders/by-number/{orderNumber}\n",
    )
    store, warnings = run_aggregate(ws.cfg, ws, IndexBundle())
    assert warnings == []
    paths = [(r.method, r.path) for r in store.operations()]
    assert len(paths) == 18
    assert ("GET", "/orders/by-number/{orderNumber}") in paths
    assert ("GET", "/orders/{orderId}") in paths
    assert not [c for c in store.conflicts() if c.kind == "incompatible_duplicate"]
    cov = {c.entry_id: c for c in store.coverage()}
    assert cov["sales:OrderServlet#detail"].status == "mapped"
    assert len(store.decisions()) == 1
    store.close()


def test_conflict_id_accept_decision_and_unknown_keys(tmp_path: Path) -> None:
    ws = _setup(tmp_path)
    store, _ = run_aggregate(ws.cfg, ws, IndexBundle())
    cid = next(c.id for c in store.conflicts() if c.kind == "incompatible_duplicate")
    store.close()
    ws = _setup(
        tmp_path,
        f"decisions:\n  - key: {cid}\n    action: accept\n"
        "    chosen_packet_id: 'sales:OrderServlet#view'\n"
        "  - key: nope:X#y\n    action: accept\n  - key: c-deadbeef\n    action: accept\n"
        "    chosen_packet_id: a\n",
    )
    store, warnings = run_aggregate(ws.cfg, ws, IndexBundle())
    assert len(store.operations()) == 17
    cov = {c.entry_id: c for c in store.coverage()}
    assert cov["sales:OrderServlet#view"].status == "mapped"
    assert cov["sales:OrderServlet#detail"].status == "rejected"
    assert any("nope:X#y" in w for w in warnings)
    assert any("c-deadbeef" in w for w in warnings)
    assert any(c.id == cid and c.resolved for c in store.conflicts())
    store.close()


def test_drop_and_failed_coverage(tmp_path: Path) -> None:
    ws = _setup(
        tmp_path,
        "decisions:\n  - key: 'sales:ReportServlet#daily'\n    action: drop\n    reason: BI\n",
    )
    # add a failed proposal and a packet without a proposal
    from j2ee_openapi_generator.aggregate.models import VerifiedProposal

    bad = VerifiedProposal(packet_id="sales:X#y", status="failed", reasons=["boom"])
    write_json(ws.verified_dir / "sales--X__y.json", bad.model_dump(mode="json"))
    pk = packet("sales:X#z")
    write_json(ws.packets_dir / "sales--X__z.json", pk.model_dump(mode="json"))
    # sub packets collapse onto the parent entry
    for n in (1, 2):
        sp = packet(f"sales:S#big~p{n}")
        write_json(ws.packets_dir / f"s{n}.json", sp.model_dump(mode="json"))
    store, _ = run_aggregate(ws.cfg, ws, IndexBundle())
    cov = {c.entry_id: c for c in store.coverage()}
    assert cov["sales:ReportServlet#daily"].status == "rejected"
    assert "dropped: BI" in cov["sales:ReportServlet#daily"].reasons
    assert cov["sales:X#y"].status == "failed"
    assert cov["sales:X#z"].status == "failed"
    assert cov["sales:S#big"].status == "failed"
    assert "sales:S#big~p1" not in cov
    store.close()


def test_idempotent_rerun(tmp_path: Path) -> None:
    ws = _setup(tmp_path)
    s1, _ = run_aggregate(ws.cfg, ws, IndexBundle())
    snap1 = s1.snapshot([], {}).model_dump_json()
    s1.close()
    s2, _ = run_aggregate(ws.cfg, ws, IndexBundle())
    assert s2.snapshot([], {}).model_dump_json() == snap1
    s2.close()


def test_derive_error_codes() -> None:
    b = IndexBundle()
    b.properties.bundles.append(
        PropertiesBundle(
            file="m.properties",
            base_name="m",
            entries={
                "error.user.inuse": "x",
                "error.user.inUse": "y",
                "mod.error.bad": "z",
                "label.name": "n",
            },
        )
    )
    codes = derive_error_codes(b)
    assert codes == {
        "error.user.inUse": "USER_IN_USE",
        "error.user.inuse": "USER_INUSE",
        "mod.error.bad": "MOD_BAD",
    }
    assert list(codes) == sorted(codes)


def test_resolver_suggestions_not_applied(tmp_path: Path) -> None:
    ws = _setup(tmp_path)
    client = FakeLLM(LLM_DIR)
    store, warnings = run_aggregate(ws.cfg, ws, IndexBundle(), client=client, resolve_with_llm=True)
    assert warnings == []
    text = (ws.review_dir / "suggestions.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Suggestions only. Copy accepted entries into decisions.yaml.\n")
    data = yaml.safe_load(text)
    assert data["decisions"][0]["action"] == "rename_path"
    assert data["decisions"][0]["new_path"] == "/orders/by-number/{orderNumber}"
    # not applied: the conflict is still open
    assert len(store.operations()) == 16
    assert not (ws.review_dir / "decisions.yaml").exists()
    # cached: offline re-run works, and the cache holds the resolver call
    store.close()
    again, w2 = run_aggregate(
        ws.cfg, ws, IndexBundle(), client=client, resolve_with_llm=True, offline=True
    )
    assert w2 == []
    again.close()


def test_resolver_offline_cache_miss_warns(tmp_path: Path) -> None:
    ws = _setup(tmp_path)
    store, warnings = run_aggregate(
        ws.cfg, ws, IndexBundle(), client=FakeLLM(LLM_DIR), resolve_with_llm=True, offline=True
    )
    assert any("resolver suggestions unavailable" in w for w in warnings)
    assert not (ws.review_dir / "suggestions.yaml").exists()
    store.close()


def test_write_suggestions_sorted(tmp_path: Path) -> None:
    from j2ee_openapi_generator.llm.schemas import ConflictSuggestion, ResolverOutput

    out = ResolverOutput(
        suggestions=[
            ConflictSuggestion(conflict_id="c-2", action="drop", rationale="r2"),
            ConflictSuggestion(
                conflict_id="c-1", action="accept", chosen_packet_id="p", rationale="r1"
            ),
        ]
    )
    p = tmp_path / "x" / "suggestions.yaml"
    write_suggestions(p, out)
    first = p.read_text(encoding="utf-8")
    write_suggestions(p, out)
    assert first == p.read_text(encoding="utf-8")
    assert [d["key"] for d in yaml.safe_load(first)["decisions"]] == ["c-1", "c-2"]


def test_suggest_no_conflicts(tmp_path: Path) -> None:
    ws = _setup(tmp_path)
    out = asyncio.run(
        suggest_resolutions(ws.cfg, FakeLLM(LLM_DIR), [], Cache(ws.cache_dir), False, False)
    )
    assert out.suggestions == []
