from __future__ import annotations

import pytest
from javabuild_helper import build_index, jsp_files, read_source

from j2ee_openapi_generator.index.branch_facts import compute_branch_facts
from j2ee_openapi_generator.index.ejb import build_ejb_registry
from j2ee_openapi_generator.index.models import BranchFacts, DispatchEntry, EjbRecord, JavaIndex

JAR = ["accounts/src/main/resources/META-INF/ejb-jar.xml"]
USER = "accounts/src/main/java/com/acme/accounts/web/UserServlet.java"
ORDER = "sales/src/main/java/com/acme/sales/web/OrderServlet.java"
UML = "com.acme.accounts.ejb.UserManagerLocal"
OML = "com.acme.sales.ejb.OrderManagerLocal"


@pytest.fixture(scope="module")
def java() -> JavaIndex:
    return build_index()


@pytest.fixture(scope="module")
def ejbs(java: JavaIndex) -> list[EjbRecord]:
    return build_ejb_registry(java, read_source, JAR)


def facts(
    java: JavaIndex, ejbs: list[EjbRecord], module: str, fqcn: str, file: str, lo: int, hi: int
) -> BranchFacts:
    entry = DispatchEntry(
        module=module,
        servlet_fqcn=fqcn,
        op="x",
        param_name="op",
        style="if",
        file=file,
        start_line=lo,
        end_line=hi,
    )
    return compute_branch_facts(entry, java, read_source, ejbs, module, jsp_files())


def user(java: JavaIndex, ejbs: list[EjbRecord], lo: int, hi: int) -> BranchFacts:
    return facts(java, ejbs, "accounts", "com.acme.accounts.web.UserServlet", USER, lo, hi)


def order(java: JavaIndex, ejbs: list[EjbRecord], lo: int, hi: int) -> BranchFacts:
    return facts(java, ejbs, "sales", "com.acme.sales.web.OrderServlet", ORDER, lo, hi)


def test_user_list(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    f = user(java, ejbs, 42, 49)
    assert f.params == ["name", "page", "size"]
    assert f.param_lines == {"name": 45, "page": 43, "size": 44}
    assert f.attributes_set == ["total", "users"]
    assert f.forwards == ["accounts/src/main/webapp/userList.jsp"]
    assert f.ejb_calls == [f"{UML}#countUsers(String)", f"{UML}#findUsers(String,int,int)"]
    assert f.helper_calls == ["parseInt"]
    assert f.session_reads == [] and f.session_writes == []


def test_user_read(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    f = user(java, ejbs, 50, 58)
    assert f.params == ["id"] and f.param_lines["id"] == 51
    assert f.status_codes == [404] and f.status_lines == {"404": 54}
    assert f.forwards == ["accounts/src/main/webapp/userView.jsp"]
    assert f.ejb_calls == [f"{UML}#findUser(Long)"]
    assert f.attributes_set == ["user"]


def test_user_edit_new(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    e = user(java, ejbs, 59, 63)
    assert e.params == ["id"] and e.forwards == ["accounts/src/main/webapp/userEdit.jsp"]
    assert e.attributes_set == ["statuses", "user"]
    n = user(java, ejbs, 64, 67)
    assert n.params == [] and n.forwards == ["accounts/src/main/webapp/userEdit.jsp"]
    assert n.ejb_calls == []


def test_user_save_follows_helper(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    f = user(java, ejbs, 68, 69)
    assert f.params == ["birthDate", "email", "id", "name", "status", "version"]
    assert f.param_lines["id"] == 85 and f.param_lines["version"] == 90
    assert f.helper_calls == ["saveUser"]
    assert f.helper_ranges == [("saveUser", 83, 117)]
    assert f.forwards == ["accounts/src/main/webapp/userEdit.jsp"]
    assert f.status_codes == [400, 409]
    assert f.status_lines == {"400": 97, "409": 113}
    assert f.exceptions_caught == ["OptimisticLockException"]
    assert f.attributes_set == ["error"]
    assert f.redirects == ["/UserServlet?op=read&id={user.getId()}"]
    assert set(f.ejb_calls) == {
        f"{UML}#create(User)",
        f"{UML}#findUser(Long)",
        f"{UML}#update(User,Long)",
    }


def test_user_delete_and_default(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    d = user(java, ejbs, 70, 77)
    assert d.params == ["id"]
    assert d.status_codes == [204, 409] and d.status_lines == {"204": 74, "409": 76}
    assert d.exceptions_caught == ["UserInUseException"]
    assert d.ejb_calls == [f"{UML}#delete(Long)"]
    df = user(java, ejbs, 78, 80)
    assert df.redirects == ["/UserServlet?op=list"]
    assert df.params == []


def test_depth_limit(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    # entry in doGet (line 30) -> process (depth 1) -> saveUser/parseInt (depth 2)
    f = user(java, ejbs, 30, 30)
    assert "process" in f.helper_calls and "saveUser" in f.helper_calls
    assert "name" in f.params


def test_profile_update(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    f = facts(
        java,
        ejbs,
        "accounts",
        "com.acme.accounts.web.ProfileServlet",
        "accounts/src/main/java/com/acme/accounts/web/ProfileServlet.java",
        44,
        57,
    )
    assert f.params == ["displayName", "email", "locale"]
    assert f.status_codes == [400]
    assert f.redirects == ["/profile?op=view"]
    assert f.ejb_calls == [f"{UML}#update(User,Long)"]


def test_order_list_and_constants(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    f = order(java, ejbs, 55, 65)
    assert f.params == ["customerId", "page", "size", "status"]
    assert f.param_lines["page"] == 56
    assert f.forwards == ["sales/src/main/webapp/orderList.jsp"]
    assert set(f.ejb_calls) == {
        f"{OML}#findOrders(String,Long,int,int)",
        f"{OML}#countOrders(String,Long)",
    }


def test_order_wizard_session(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    s1 = order(java, ejbs, 82, 94)
    assert s1.params == ["customerId", "notes"]
    assert s1.session_writes == ["orderDraft"] and s1.session_reads == []
    assert s1.forwards == [
        "sales/src/main/webapp/wizardStep1.jsp",
        "sales/src/main/webapp/wizardStep2.jsp",
    ]
    s2 = order(java, ejbs, 95, 109)
    assert s2.session_reads == ["orderDraft"] and s2.session_writes == ["orderDraft"]
    assert s2.params == ["quantity", "sku", "unitPrice"]
    assert s2.status_codes == [400]
    c = order(java, ejbs, 110, 120)
    assert c.session_reads == ["orderDraft"] and c.session_writes == ["orderDraft"]
    assert c.status_lines == {"400": 113}
    assert c.ejb_calls == [f"{OML}#createFromDraft(OrderDraft)"]
    assert c.redirects == ["/OrderServlet?op=view&id={order.getId()}"]


def test_order_export_csv(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    f = order(java, ejbs, 121, 132)
    assert f.params == ["status"]
    assert f.content_types == ["text/csv"]
    assert f.content_disposition == ['attachment; filename="orders.csv"']
    assert f.writes_response is True


def test_order_view_detail_default(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    v = order(java, ejbs, 66, 75)
    assert v.status_codes == [404] and v.params == ["id"]
    d = order(java, ejbs, 76, 81)
    assert d.params == ["orderNumber"] and d.ejb_calls == [f"{OML}#findSummary(String)"]
    df = order(java, ejbs, 133, 134)
    assert df.redirects == ["/OrderServlet?op=list"]


def test_reflection_methods(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    rs = "sales/src/main/java/com/acme/sales/web/ReportServlet.java"
    s = facts(java, ejbs, "sales", "com.acme.sales.web.ReportServlet", rs, 21, 27)
    assert s.params == ["from", "to"] and s.forwards == ["sales/src/main/webapp/report.jsp"]
    assert s.ejb_calls == [f"{OML}#summarize(String,String)"]
    d = facts(java, ejbs, "sales", "com.acme.sales.web.ReportServlet", rs, 29, 36)
    assert d.params == ["day"] and d.content_types == ["application/json"] and d.writes_response


def test_deterministic(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    assert user(java, ejbs, 68, 69) == user(java, ejbs, 68, 69)


def test_wrapper_get_parameter_names_are_collected() -> None:
    """``Util.getParameter(req, "x")`` records ``x``, not the request variable."""
    from j2ee_openapi_generator.index.java_symbols import parse_java_file, resolve_index

    src = """package t;
import javax.servlet.http.*;
public class W extends HttpServlet {
    void a(HttpServletRequest req) {
        String key = UtilRequest.getParameter(req, "LicenseValue");
        String one = req.getParameter("plain");
        String[] many = UtilRequest.getParameterValues(req, "ids");
    }
}
"""
    classes = {c.fqcn: c for c in parse_java_file("t/W.java", src)}
    idx = JavaIndex(classes=dict(sorted(classes.items())))
    resolve_index(idx)
    entry = DispatchEntry(
        module="m",
        servlet_fqcn="t.W",
        op="x",
        param_name="op",
        style="if",
        file="t/W.java",
        start_line=4,
        end_line=8,
    )
    f = compute_branch_facts(entry, idx, lambda p: src, [], "m", set())
    assert f.params == ["LicenseValue", "plain"]
    assert f.multi_params == ["ids"]
