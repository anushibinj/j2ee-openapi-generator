from pathlib import Path

import pytest
from aggregate_helpers import op, verified

from j2ee_openapi_generator.aggregate.conflicts import resolve_conflict
from j2ee_openapi_generator.aggregate.decisions import (
    DecisionsError,
    apply_packet_decisions,
    load_decisions,
)
from j2ee_openapi_generator.aggregate.dedupe import merge_operations
from j2ee_openapi_generator.aggregate.models import Decision, DecisionsFile


def _file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "decisions.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_missing_and_empty(tmp_path: Path) -> None:
    assert load_decisions(tmp_path / "nope.yaml").decisions == []
    assert load_decisions(_file(tmp_path, "")).decisions == []


def test_load_example_file() -> None:
    ex = Path(__file__).parents[2] / "review" / "decisions.example.yaml"
    assert len(load_decisions(ex).decisions) == 4


@pytest.mark.parametrize(
    "text, needle",
    [
        ("decisions:\n  - key: a\n    action: drop\n", "'a'"),
        ("decisions:\n  - key: a\n    action: rename_path\n", "new_path"),
        ("decisions:\n  - key: a\n    action: nope\n", "'a'"),
        ("decisions:\n  - key: a\n    action: accept\n    bogus: 1\n", "bogus"),
        (
            "decisions:\n  - key: a\n    action: accept\n  - key: a\n    action: accept\n",
            "duplicate",
        ),
        ("decisions: [\n", "invalid YAML"),
        ("- 1\n", "mapping"),
    ],
)
def test_load_invalid(tmp_path: Path, text: str, needle: str) -> None:
    with pytest.raises(DecisionsError) as ei:
        load_decisions(_file(tmp_path, text))
    assert needle in str(ei.value)


def _d(**kw: object) -> DecisionsFile:
    return DecisionsFile(decisions=[Decision.model_validate(kw)])


def test_accept_rejected_packet() -> None:
    props = {"p:A#a": verified("p:A#a", [op()], status="rejected", reasons=["bad cite"])}
    out, w, used = apply_packet_decisions(props, _d(key="p:A#a", action="accept"))
    assert out["p:A#a"].status == "verified" and w == [] and used == {"p:A#a"}


def test_accept_failed_warns() -> None:
    props = {"p:A#a": verified("p:A#a", [], status="failed")}
    out, w, _ = apply_packet_decisions(props, _d(key="p:A#a", action="accept"))
    assert out["p:A#a"].status == "failed" and w


def test_override() -> None:
    props = {"p:A#a": verified("p:A#a", [op()])}
    out, w, _ = apply_packet_decisions(
        props,
        _d(
            key="p:A#a",
            action="override",
            overrides={"summary": "New", "roles": ["admin"], "bogus": 1},
        ),
    )
    o = out["p:A#a"].operations[0]
    assert o.summary == "New" and o.roles[0].name == "admin"
    assert any("bogus" in x for x in w)


def test_override_invalid_value_warns_and_keeps() -> None:
    props = {"p:A#a": verified("p:A#a", [op()])}
    out, w, _ = apply_packet_decisions(
        props, _d(key="p:A#a", action="override", overrides={"http_method": "TRACE"})
    )
    assert out["p:A#a"].operations[0].http_method == "GET" and w


def test_drop() -> None:
    props = {"p:A#a": verified("p:A#a", [op()])}
    out, _, _ = apply_packet_decisions(props, _d(key="p:A#a", action="drop", reason="BI"))
    assert out["p:A#a"].status == "rejected"
    assert out["p:A#a"].operations == []
    assert "dropped: BI" in out["p:A#a"].reasons


def test_rename_path_renames_params_and_matches_parent() -> None:
    props = {
        "p:A#a~p1": verified("p:A#a~p1", [op("GET", "/orders/{orderId}")]),
        "p:A#a~p2": verified("p:A#a~p2", [op("POST", "/other")]),
    }
    out, w, used = apply_packet_decisions(
        props, _d(key="p:A#a", action="rename_path", new_path="/orders/by-number/{orderNumber}")
    )
    o = out["p:A#a~p1"].operations[0]
    assert o.path == "/orders/by-number/{orderNumber}"
    assert o.path_params[0].name == "orderNumber" and o.path_params[0].legacy_name == "orderId"
    assert used == {"p:A#a"}
    assert w  # the second sub-packet op has no placeholders but the path has one


def test_rename_path_with_match() -> None:
    props = {"p:A#a": verified("p:A#a", [op("GET", "/a"), op("POST", "/b")])}
    out, w, _ = apply_packet_decisions(
        props,
        _d(key="p:A#a", action="override", overrides={"match": "POST /b", "path": "/c"}),
    )
    assert [o.path for o in out["p:A#a"].operations] == ["/a", "/c"]
    assert w == []


def test_conflict_accept_chosen() -> None:
    a, b = op(param_type="integer"), op(param_type="string")
    ops = [("p:A#a", a), ("p:B#b", b)]
    _, conflicts = merge_operations(ops)
    cf = conflicts[0]
    new, w = resolve_conflict(
        cf, Decision(key=cf.id, action="accept", chosen_packet_id="p:B#b"), ops
    )
    assert [p for p, _ in new] == ["p:B#b"] and w == []
    assert merge_operations(new)[1] == []


def test_conflict_rename_and_drop_and_errors() -> None:
    a, b = op(param_type="integer"), op(param_type="string")
    ops = [("p:A#a", a), ("p:B#b", b)]
    cf = merge_operations(ops)[1][0]
    new, _ = resolve_conflict(
        cf,
        Decision(key=cf.id, action="rename_path", chosen_packet_id="p:B#b", new_path="/x/{n}"),
        ops,
    )
    rows, conflicts = merge_operations(new)
    assert conflicts == [] and sorted(r.path for r in rows) == ["/users/{id}", "/x/{n}"]
    new, _ = resolve_conflict(
        cf, Decision(key=cf.id, action="drop", chosen_packet_id="p:A#a", reason="dup"), ops
    )
    assert [p for p, _ in new] == ["p:B#b"]
    same, w = resolve_conflict(cf, Decision(key=cf.id, action="accept"), ops)
    assert same == ops and w
    same, w = resolve_conflict(
        cf, Decision(key=cf.id, action="accept", chosen_packet_id="zzz"), ops
    )
    assert same == ops and w
