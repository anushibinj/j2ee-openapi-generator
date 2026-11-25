from __future__ import annotations

import pytest

from j2ee_openapi_generator.index.models import (
    BranchFacts,
    DispatchEntry,
    DomainIndex,
    EjbMethod,
    EjbRecord,
    EntityRecord,
    IndexBundle,
    ModuleRecord,
    WebIndex,
)
from j2ee_openapi_generator.packets.hints import (
    compute_hints,
    kebab,
    pick_primary_entity,
    plural_kebab,
    resource_candidates,
)


def entry(op: str, facts: BranchFacts | None = None, servlet: str = "UserServlet") -> DispatchEntry:
    return DispatchEntry(
        module="m",
        servlet_fqcn=f"p.{servlet}",
        op=op,
        param_name="op",
        style="if",
        file="p/S.java",
        start_line=1,
        end_line=2,
        facts=facts or BranchFacts(),
    )


def hint(
    op: str,
    *,
    has_id: bool = False,
    form_only: bool = False,
    binary: bool = False,
    multipart: bool = False,
    entity: str | None = "User",
    facts: BranchFacts | None = None,
) -> tuple[str, str]:
    h = compute_hints(entry(op, facts), "UserServlet", entity, has_id, form_only, binary, multipart)
    return h.http_method, h.shape


@pytest.mark.parametrize("op", ["list", "search", "find", "query", "all", "listUsers", "find_all"])
def test_list_verbs(op: str) -> None:
    assert hint(op) == ("GET", "collection")


@pytest.mark.parametrize("op", ["read", "view", "get", "show", "detail", "load", "getUser"])
def test_read_verbs(op: str) -> None:
    assert hint(op) == ("GET", "item")


@pytest.mark.parametrize("op", ["add", "create", "new", "insert", "addUser"])
def test_create_verbs(op: str) -> None:
    assert hint(op) == ("POST", "collection")


def test_save_depends_on_id() -> None:
    assert hint("save") == ("POST", "collection")
    h = compute_hints(entry("save"), "UserServlet", "User", True, False, False, False)
    assert (h.http_method, h.shape) == ("PUT", "item")
    assert "PATCH" in h.reason


@pytest.mark.parametrize("op", ["update", "modify"])
def test_update_verbs(op: str) -> None:
    h = compute_hints(entry(op), "UserServlet", None, True, False, False, False)
    assert (h.http_method, h.shape) == ("PUT", "item")
    assert "PATCH" in h.reason


@pytest.mark.parametrize("op", ["delete", "remove", "deleteUser"])
def test_delete_verbs(op: str) -> None:
    assert hint(op) == ("DELETE", "item")


@pytest.mark.parametrize("op", ["export", "download", "print"])
def test_binary_verbs(op: str) -> None:
    assert hint(op) == ("GET", "binary")


@pytest.mark.parametrize("op", ["upload", "import"])
def test_multipart_verbs(op: str) -> None:
    assert hint(op) == ("POST", "multipart")


def test_edit_and_new() -> None:
    assert hint("edit", form_only=True) == ("GET", "item")
    assert hint("edit", form_only=False) == ("PUT", "item")
    assert hint("new", form_only=True) == ("NONE", "no_api_needed")
    assert hint("new", form_only=False) == ("POST", "collection")


def test_unknown_op_is_action() -> None:
    h = compute_hints(entry("confirmOrder"), "OrderServlet", "Order", False, False, False, False)
    assert (h.http_method, h.shape, h.verb_kebab) == ("POST", "action", "confirm-order")
    assert "{id}/confirm-order" in h.reason
    assert hint("step1")[1] == "action"
    assert (
        compute_hints(entry("step1"), "S", None, False, False, False, False).verb_kebab == "step1"
    )


def test_unknown_op_flags() -> None:
    assert hint("zap", binary=True) == ("GET", "binary")
    assert hint("zap", multipart=True) == ("POST", "multipart")


def test_default_op() -> None:
    assert hint("<default>") == ("NONE", "no_api_needed")
    facts = BranchFacts(ejb_calls=["p.Svc#run()"])
    assert hint("<default>", facts=facts)[1] == "action"


def test_name_forms() -> None:
    assert kebab("UserAccount") == "user-account"
    assert kebab("HTTPServer") == "http-server"
    assert kebab("order_line") == "order-line"
    assert kebab("step1") == "step1"
    assert plural_kebab("UserAccount") == "user-accounts"
    assert plural_kebab("Category") == "categories"
    assert plural_kebab("Address") == "addresses"
    assert plural_kebab("") == ""


def test_resource_candidates() -> None:
    assert resource_candidates("UserServlet", "User") == ["users"]
    assert resource_candidates("OrderController", "OrderLine") == ["orders", "order-lines"]
    assert resource_candidates("CustomerAction", None) == ["customers"]
    assert resource_candidates("ReportServlet", "OrderSummaryDto") == ["reports", "order-summaries"]
    assert resource_candidates("Plain", None) == ["plains"]


def _bundle() -> IndexBundle:
    def ent(name: str, kind: str = "entity") -> EntityRecord:
        return EntityRecord(
            fqcn=f"m.{name}",
            simple_name=name,
            kind=kind,  # type: ignore[arg-type]
            file=f"m/{name}.java",
            start_line=1,
            end_line=2,
        )

    ejb = EjbRecord(
        bean_fqcn="m.SvcBean",
        kind="stateless",
        interfaces=["m.SvcLocal"],
        impl_file="m/SvcBean.java",
        methods=[
            EjbMethod(
                name="a", signature="a(Order)", return_type="List<Order>", start_line=1, end_line=2
            ),
            EjbMethod(name="b", signature="b(Long)", return_type="Order", start_line=1, end_line=2),
            EjbMethod(
                name="c", signature="c(String)", return_type="Customer", start_line=1, end_line=2
            ),
        ],
    )
    return IndexBundle(
        web=WebIndex(
            modules=[ModuleRecord(name="m", web_xml="m/web.xml", root_dir="m", context_root="/m")]
        ),
        domain=DomainIndex(
            ejbs=[ejb], entities=[ent("Order"), ent("Customer"), ent("Status", "enum")]
        ),
    )


def test_pick_primary_entity() -> None:
    b = _bundle()
    facts = BranchFacts(
        ejb_calls=["m.SvcLocal#a(Order)", "m.SvcLocal#b(Long)", "m.SvcLocal#c(String)"]
    )
    assert pick_primary_entity(entry("x", facts), b) == "Order"
    # no EJB calls: servlet noun match
    assert pick_primary_entity(entry("x", servlet="OrderServlet"), b) == "Order"
    assert pick_primary_entity(entry("x", servlet="CustomerServlet"), b) == "Customer"
    assert pick_primary_entity(entry("x", servlet="WidgetServlet"), b) is None
