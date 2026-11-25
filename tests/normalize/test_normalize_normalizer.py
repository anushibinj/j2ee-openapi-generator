from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from j2ee_openapi_generator.aggregate.models import OperationRow
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.llm.base import StructuredResult
from j2ee_openapi_generator.llm.cache import CacheMiss
from j2ee_openapi_generator.llm.fake import FakeLLM
from j2ee_openapi_generator.llm.schemas import (
    EndpointProposal,
    OperationRename,
    RenameMap,
    TagDescription,
)
from j2ee_openapi_generator.normalize.normalizer import (
    build_listing,
    fallback_operation_id,
    fatal_errors,
    normalize_sync,
    run_normalize,
    validate_rename_map,
)
from j2ee_openapi_generator.workspace import Workspace

LLM_DIR = Path(__file__).parents[1] / "fixtures" / "llm"


def load_rows() -> list[OperationRow]:
    rows: dict[tuple[str, str], OperationRow] = {}
    for p in sorted(LLM_DIR.glob("*.json")):
        if p.name.startswith("_") or p.name.endswith(".repair.json"):
            continue
        prop = EndpointProposal.model_validate_json(p.read_text())
        for op in prop.operations:
            rows[(op.http_method, op.path)] = OperationRow(
                method=op.http_method, path=op.path, operation=op, packet_ids=[prop.packet_id]
            )
    return list(rows.values())


class Store:
    def __init__(self, rows: list[OperationRow]) -> None:
        self.rows = rows
        self.descs: dict[str, str] = {}

    def operations(self) -> list[OperationRow]:
        return [r.model_copy(deep=True) for r in self.rows]

    def set_operation_texts(
        self,
        method: str,
        path: str,
        operation_id: str | None,
        summary: str | None,
        description: str | None,
    ) -> None:
        for r in self.rows:
            if (r.method, r.path) == (method, path):
                r.operation_id = operation_id
                r.description = description
                if summary:
                    r.operation = r.operation.model_copy(update={"summary": summary})

    def set_tag_description(self, tag: str, description: str) -> None:
        self.descs[tag] = description

    def tag_descriptions(self) -> dict[str, str]:
        return dict(self.descs)

    def tags(self) -> list[str]:
        return sorted({r.operation.tag for r in self.rows})


def make_cfg(tmp_path: Path, conc: int = 4) -> tuple[Config, Workspace]:
    cfg = Config(repo_root=tmp_path, work_dir=tmp_path / "w")
    cfg.llm.max_concurrency = conc
    ws = Workspace(cfg)
    ws.ensure()
    return cfg, ws


def mkrow(method: str, path: str, tag: str = "T") -> OperationRow:
    base = load_rows()[0]
    op = base.operation.model_copy(
        update={"http_method": method, "path": path, "tag": tag, "summary": "S"}
    )
    return OperationRow(method=method, path=path, operation=op, packet_ids=["p"])


# ---------------------------------------------------------------- listing
def test_listing_format_sorted() -> None:
    rows = [mkrow("POST", "/b"), mkrow("GET", "/b"), mkrow("GET", "/a"), mkrow("GET", "/z", "X")]
    lines = build_listing("T", rows).split("\n")
    assert [ln.split(" | ")[0] for ln in lines] == ["GET /a", "GET /b", "POST /b"]
    assert lines[0].split(" | ")[1:3] == ["-", "S"]


# ---------------------------------------------------------------- validation
def _rm(*ids: tuple[str, str], **kw: object) -> RenameMap:
    return RenameMap(
        tag="T",
        operations=[OperationRename(key=k, operation_id=i, **kw) for k, i in ids],  # type: ignore[arg-type]
    )


ROWS = [mkrow("GET", "/a"), mkrow("GET", "/b")]


def check(rm: RenameMap, existing: set[str] | None = None) -> list[str]:
    return fatal_errors(validate_rename_map(rm, ROWS, existing or set())[1])


def test_valid() -> None:
    assert check(_rm(("GET /a", "listA"), ("GET /b", "getB"))) == []


@pytest.mark.parametrize("bad", ["List", "list_a", "1a", "a", "list-a", ""])
def test_bad_id(bad: str) -> None:
    assert check(_rm(("GET /a", bad)))


def test_duplicates() -> None:
    assert check(_rm(("GET /a", "listA"), ("GET /b", "listA")))
    assert check(_rm(("GET /a", "listA")), {"listA"})


def test_unknown_key_is_warning() -> None:
    cleaned, msgs = validate_rename_map(_rm(("GET /a", "listA"), ("GET /q", "getQ")), ROWS, set())
    assert fatal_errors(msgs) == [] and msgs
    assert [o.key for o in cleaned.operations] == ["GET /a"]


def test_em_dash_and_lengths() -> None:
    rm = _rm(("GET /a", "listA"), summary="A — b", description="x — y")
    cleaned, msgs = validate_rename_map(rm, ROWS, set())
    assert not fatal_errors(msgs)
    assert cleaned.operations[0].summary == "A ,  b"
    assert "—" not in (cleaned.operations[0].description or "")
    assert check(_rm(("GET /a", "listA"), summary="x" * 121))
    assert check(_rm(("GET /a", "listA"), summary="a\nb"))
    assert check(_rm(("GET /a", "listA"), summary="  "))
    assert check(_rm(("GET /a", "listA"), description="x" * 1001))


def test_tag_description_rules() -> None:
    ok = RenameMap(tag="T", tag_descriptions=[TagDescription(tag="T", description="d")])
    assert not fatal_errors(validate_rename_map(ok, ROWS, set())[1])
    wrong = RenameMap(tag="T", tag_descriptions=[TagDescription(tag="U", description="d")])
    assert fatal_errors(validate_rename_map(wrong, ROWS, set())[1])
    long = RenameMap(tag="T", tag_descriptions=[TagDescription(tag="T", description="d" * 501)])
    assert fatal_errors(validate_rename_map(long, ROWS, set())[1])


def test_structural_fields_rejected_by_schema() -> None:
    with pytest.raises(ValueError):
        RenameMap.model_validate(
            {"tag": "T", "operations": [{"key": "GET /a", "operation_id": "x1", "path": "/z"}]}
        )


# ---------------------------------------------------------------- fallback ids
@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/users", "listUsers"),
        ("GET", "/users/{userId}", "getUser"),
        ("POST", "/users", "createUser"),
        ("PUT", "/users/{userId}", "replaceUser"),
        ("PATCH", "/users/{userId}", "updateUser"),
        ("DELETE", "/users/{userId}", "deleteUser"),
        ("POST", "/orders/{orderId}/submit", "submitOrder"),
        ("GET", "/lookups/countries", "lookupCountries"),
        ("GET", "/orders/export", "exportOrders"),
        ("POST", "/orders/drafts", "createOrderDraft"),
        ("GET", "/orders/by-number/{orderNumber}", "getOrderByNumber"),
        ("GET", "/categories", "listCategories"),
        ("GET", "/", "listResource"),
    ],
)
def test_fallback_shapes(method: str, path: str, expected: str) -> None:
    assert fallback_operation_id(method, path, set()) == expected


def test_fallback_unique() -> None:
    taken: set[str] = set()
    ids = [fallback_operation_id("GET", "/users", taken) for _ in range(3)]
    assert ids == ["listUsers", "listUsers2", "listUsers3"]
    assert taken == set(ids)


# ---------------------------------------------------------------- run
def test_run_with_fixtures(tmp_path: Path) -> None:
    cfg, ws = make_cfg(tmp_path)
    store = Store(load_rows())
    rep = normalize_sync(cfg, FakeLLM(LLM_DIR), ws, store)
    ids = [r.operation_id for r in store.rows]
    assert all(ids) and len(set(ids)) == len(ids)
    assert {t.tag: t.status for t in rep.tags}  # all tags reported
    assert all(t.status == "ok" for t in rep.tags), rep.errors
    assert "listOrders" in ids and "createOrderDraft" in ids
    assert store.descs["Sales Orders"]


def test_cache_offline_force(tmp_path: Path) -> None:
    cfg, ws = make_cfg(tmp_path)
    with pytest.raises(CacheMiss):
        normalize_sync(cfg, FakeLLM(LLM_DIR), ws, Store(load_rows()), offline=True)
    normalize_sync(cfg, FakeLLM(LLM_DIR), ws, Store(load_rows()))

    class Boom(FakeLLM):
        async def structured(self, **kw: object) -> StructuredResult:  # type: ignore[override]
            raise AssertionError("called")

    st = Store(load_rows())
    rep = normalize_sync(cfg, Boom(LLM_DIR), ws, st, offline=True)
    assert all(t.cached for t in rep.tags)
    rep = normalize_sync(cfg, Boom(LLM_DIR), ws, Store(load_rows()), force=True)
    assert all(t.status == "failed" for t in rep.tags)  # forced call raised, fell back


def test_concurrency_bound(tmp_path: Path) -> None:
    cfg, ws = make_cfg(tmp_path, conc=2)
    state = {"cur": 0, "max": 0}

    class Slow(FakeLLM):
        async def structured(self, **kw):  # type: ignore[no-untyped-def]
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await asyncio.sleep(0.02)
            try:
                return await super().structured(**kw)
            finally:
                state["cur"] -= 1

    normalize_sync(cfg, Slow(LLM_DIR), ws, Store(load_rows()))
    assert state["max"] == 2


def test_deterministic(tmp_path: Path) -> None:
    outs = []
    for i in range(2):
        cfg, ws = make_cfg(tmp_path / str(i), conc=1 + i * 3)
        st = Store(load_rows())
        normalize_sync(cfg, FakeLLM(LLM_DIR), ws, st)
        outs.append(json.dumps([(r.method, r.path, r.operation_id) for r in st.rows]))
    assert outs[0] == outs[1]


def test_bad_map_falls_back(tmp_path: Path) -> None:
    cfg, ws = make_cfg(tmp_path)
    bad = tmp_path / "fake"
    (bad / "_normalizer").mkdir(parents=True)
    for p in (LLM_DIR / "_normalizer").glob("*.json"):
        (bad / "_normalizer" / p.name).write_text(p.read_text())
    m = json.loads((bad / "_normalizer" / "Sales_Orders.json").read_text())
    for o in m["operations"]:
        o["operation_id"] = "Bad_Id"
    (bad / "_normalizer" / "Sales_Orders.json").write_text(json.dumps(m))
    st = Store(load_rows())
    rep = asyncio.run(run_normalize(cfg, FakeLLM(bad), ws, st))
    by = {t.tag: t for t in rep.tags}
    assert by["Sales Orders"].status == "fallback" and by["Sales Orders"].errors
    assert by["Sales Lookups"].status == "ok"
    ids = [r.operation_id for r in st.rows]
    assert all(ids) and len(set(ids)) == len(ids)
