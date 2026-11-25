from aggregate_helpers import ev, op

from j2ee_openapi_generator.aggregate.dedupe import merge_operations, normalize_path
from j2ee_openapi_generator.llm.schemas import ParamSpec, ResponseSpec, RoleSpec


def test_normalize_path() -> None:
    assert normalize_path("/users/{userId}") == "/users/{}"
    assert normalize_path("/users/{id}") == "/users/{}"
    assert normalize_path("/Users/{a}/Orders/{b}/") == "/users/{}/orders/{}"
    assert normalize_path("/") == "/"
    assert normalize_path("/users/") == "/users"


def test_merge_compatible() -> None:
    a = op(
        "GET",
        "/users/{userId}",
        query_params=[ParamSpec(name="q")],
        evidence=[ev(1)],
        roles=[RoleSpec(name="admin", evidence=[ev(5)])],
        ejb_methods=["a.B#c()"],
    )
    b = op(
        "GET",
        "/users/{id}",
        query_params=[ParamSpec(name="page", type="integer")],
        evidence=[ev(1), ev(2)],
        roles=[RoleSpec(name="admin", evidence=[ev(6)]), RoleSpec(name="user")],
        ejb_methods=["a.B#c()", "a.B#d()"],
        responses=[ResponseSpec(status=200, schema_ref="User"), ResponseSpec(status=404)],
    )
    rows, conflicts = merge_operations([("m:A#x", a), ("m:B#y", b)])
    assert conflicts == []
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "/users/{userId}"
    assert row.packet_ids == ["m:A#x", "m:B#y"]
    o = row.operation
    assert o.path_params[0].name == "userId"
    assert [p.name for p in o.query_params] == ["q", "page"]
    assert [e.line for e in o.evidence] == [1, 2]
    assert [r.name for r in o.roles] == ["admin", "user"]
    assert [e.line for e in o.roles[0].evidence] == [5, 6]
    assert o.ejb_methods == ["a.B#c()", "a.B#d()"]
    assert [r.status for r in o.responses] == [200, 404]


def test_incompatible_param_type_conflict_without_row() -> None:
    a = op("GET", "/orders/{orderId}", param_type="integer")
    b = op("GET", "/orders/{orderId}", param_type="string")
    c = op("GET", "/ok")
    rows, conflicts = merge_operations([("p:A#a", a), ("p:B#b", b), ("p:C#c", c)])
    assert [r.path for r in rows] == ["/ok"]
    assert len(conflicts) == 1
    cf = conflicts[0]
    assert cf.kind == "incompatible_duplicate"
    assert cf.packet_ids == ["p:A#a", "p:B#b"]
    assert len(cf.proposals) == 2
    assert not cf.resolved


def test_incompatible_response_schema() -> None:
    a = op("GET", "/x/{i}", schema="A")
    b = op("GET", "/x/{i}", schema="B")
    rows, conflicts = merge_operations([("p:A#a", a), ("p:B#b", b)])
    assert rows == [] and len(conflicts) == 1


def test_conflict_ids_are_stable() -> None:
    a = op("GET", "/orders/{orderId}", param_type="integer")
    b = op("GET", "/orders/{other}", param_type="string")
    c1 = merge_operations([("p:A#a", a), ("p:B#b", b)])[1][0]
    c2 = merge_operations([("p:A#a", a), ("p:B#b", b)])[1][0]
    assert c1.id == c2.id
    assert c1.id.startswith("c-") and len(c1.id) == 10
    other = merge_operations(
        [
            ("p:A#a", op("GET", "/z/{i}", param_type="integer")),
            ("p:B#b", op("GET", "/z/{i}", param_type="string")),
        ]
    )[1][0]
    assert other.id != c1.id


def test_ejb_path_mismatch_is_advisory() -> None:
    e = ["a.B#find(Long)"]
    a = op("GET", "/things/{id}", ejb=e)
    b = op("GET", "/stuff/{id}", ejb=e)
    c = op("POST", "/stuff", ejb=e)
    rows, conflicts = merge_operations([("p:A#a", a), ("p:B#b", b), ("p:C#c", c)])
    assert len(rows) == 3
    assert [c.kind for c in conflicts] == ["ejb_path_mismatch"]
    assert conflicts[0].packet_ids == ["p:A#a", "p:B#b"]
