from pathlib import Path

from aggregate_helpers import op

from j2ee_openapi_generator.aggregate.models import ConflictRow, CoverageRow, OperationRow
from j2ee_openapi_generator.aggregate.store import Store
from j2ee_openapi_generator.index.models import EntityRecord


def _row(method: str, path: str, tag: str = "Users") -> OperationRow:
    return OperationRow(
        method=method, path=path, operation=op(method, path, tag=tag), packet_ids=["p:A#a"]
    )


def test_round_trip_and_ordering(tmp_path: Path) -> None:
    s = Store(tmp_path / "s.db")
    s.put_operations(
        [
            _row("DELETE", "/users/{id}"),
            _row("POST", "/users"),
            _row("GET", "/users/{id}"),
            _row("GET", "/users"),
            _row("PATCH", "/users/{id}"),
            _row("PUT", "/users/{id}"),
            _row("GET", "/a", tag="Other"),
        ]
    )
    got = [(r.method, r.path) for r in s.operations()]
    assert got == [
        ("GET", "/a"),
        ("GET", "/users"),
        ("POST", "/users"),
        ("GET", "/users/{id}"),
        ("PUT", "/users/{id}"),
        ("PATCH", "/users/{id}"),
        ("DELETE", "/users/{id}"),
    ]
    assert s.tags() == ["Other", "Users"]
    s.close()


def test_set_texts_and_tags() -> None:
    s = Store(":memory:")
    s.put_operations([_row("GET", "/users/{id}")])
    s.set_operation_texts("get", "/users/{userId}", "getUser", "Read a user", "Longer text")
    r = s.operations()[0]
    assert (r.operation_id, r.description, r.operation.summary) == (
        "getUser",
        "Longer text",
        "Read a user",
    )
    s.set_operation_texts("GET", "/nope", "x", "y", "z")  # ignored
    s.set_operation_texts("GET", "/users/{id}", None, None, None)
    assert s.operations()[0].operation_id == "getUser"
    s.set_tag_description("Users", "d1")
    s.set_tag_description("A", "d0")
    assert list(s.tag_descriptions().items()) == [("A", "d0"), ("Users", "d1")]


def test_snapshot_and_reset() -> None:
    s = Store(":memory:")
    s.put_operations([_row("GET", "/users")])
    s.put_conflicts(
        [ConflictRow(id="c-1", kind="incompatible_duplicate", summary="x", packet_ids=["a"])]
    )
    s.put_coverage(
        [CoverageRow(entry_id="b", status="mapped"), CoverageRow(entry_id="a", status="failed")]
    )
    s.put_questions({"Users": ["z?", "a?"]})
    s.put_lookups(["countries", "a"])
    ent = EntityRecord(fqcn="a.B", simple_name="B", kind="dto", file="f", start_line=1, end_line=2)
    snap = s.snapshot([ent], {"error.b": "B", "error.a": "A"})
    assert [c.entry_id for c in snap.coverage] == ["a", "b"]
    assert snap.open_questions == {"Users": ["a?", "z?"]}
    assert snap.lookup_types == ["a", "countries"]
    assert list(snap.error_codes) == ["error.a", "error.b"]
    assert snap.entities == [ent] and len(snap.conflicts) == 1
    s.reset()
    snap = s.snapshot([], {})
    assert snap.operations == [] and snap.conflicts == [] and snap.coverage == []
