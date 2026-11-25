from pathlib import Path

from j2ee_openapi_generator.index.jsp import mask_jsp, parse_js_calls, parse_jsp

APP = Path(__file__).parents[1] / "fixtures" / "legacy-app"
ACC = APP / "accounts/src/main/webapp"
SAL = APP / "sales/src/main/webapp"
OPS = ["op"]


def _nl(s: str) -> list[int]:
    return [i for i, c in enumerate(s) if c == "\n"]


def test_mask_keeps_newlines_and_length() -> None:
    src = (
        'a <% String s = "%\\" > x"; %> b\n'
        "<%= foo(\n  'x') %>\n"
        "<%! int n;\n int m; %>\n"
        "<%-- c <a href='x'> --%> <%-- multi\nline --%>\nend"
    )
    out = mask_jsp(src)
    assert len(out) == len(src)
    assert _nl(out) == _nl(src)
    assert "href" not in out and "foo" not in out and "int" not in out
    assert out.rstrip().endswith("end")


def test_mask_single_line_and_nested_quotes() -> None:
    src = '<p><% if (a.equals("b")) { %>x<% } %></p>'
    out = mask_jsp(src)
    assert out.replace(" ", "") == "<p>x</p>"
    assert len(out) == len(src)


def test_user_list() -> None:
    f = parse_jsp("accounts/userList.jsp", (ACC / "userList.jsp").read_text(), "accounts", OPS)
    assert f.includes == ["/WEB-INF/jspf/header.jspf"]
    assert len(f.forms) == 1  # comment forms are masked
    form = f.forms[0]
    assert (form.line, form.action, form.method, form.op, form.op_param) == (
        23, "UserServlet", "GET", "list", "op",
    )  # fmt: skip
    by = {i.name: i for i in form.inputs}
    assert by["name"].maxlength == 80
    assert by["size"].options == ["10", "20", "50"]
    assert by["size"].value == "20"
    assert f.form_defaults["size"] == "20" and f.form_defaults["page"] == "0"
    assert "name" not in f.form_defaults  # EL default skipped
    links = {(link.line, link.url) for link in f.links}
    assert links == {
        (41, "UserServlet?op=read&id=${u.id}"),
        (44, "UserServlet?op=edit&id=${u.id}"),
        (50, "UserServlet?op=new"),
    }
    assert f.links[0].query == {"op": "read", "id": "${u.id}"}
    assert f.foreach == [("users", "u")]
    assert {"u.id", "u.name", "u.email", "param.name"} <= set(f.el_refs)
    assert "user.list.title" in f.message_keys and "error.user.inuse" in f.message_keys
    assert f.includes == ["/WEB-INF/jspf/header.jspf"]
    [call] = f.js_calls
    assert (call.line, call.kind, call.method, call.url, call.params) == (
        58, "ajax", "POST", "UserServlet?op=delete", ["id", "op"],
    )  # fmt: skip


def test_user_edit() -> None:
    f = parse_jsp("accounts/userEdit.jsp", (ACC / "userEdit.jsp").read_text(), "accounts", OPS)
    [form] = f.forms
    assert (form.line, form.method, form.op) == (9, "POST", "save")
    by = {i.name: i for i in form.inputs}
    assert by["id"].type == "hidden" and by["id"].line == 11
    assert by["version"].value == "${user.version}"
    assert by["name"].required and by["name"].maxlength == 80
    assert by["email"].pattern == "[^@]+@[^@]+" and by["email"].required
    assert by["status"].type == "select" and by["status"].options == ["${s}"]
    assert by["birthDate"].type == "date"
    assert f.foreach == [("statuses", "s")]
    assert "error" in f.el_refs and "user.status" in f.el_refs
    assert "user.name" in f.message_keys


def test_user_view() -> None:
    f = parse_jsp("accounts/userView.jsp", (ACC / "userView.jsp").read_text(), "accounts", OPS)
    assert [(link.line, link.query["op"]) for link in f.links] == [(16, "edit"), (17, "list")]
    [form] = f.forms
    assert (form.line, form.action, form.op, form.method) == (19, "profile", "update", "POST")
    sel = next(i for i in form.inputs if i.name == "locale")
    assert sel.options == ["en", "fr"]
    assert "user.name" in f.el_refs and ("user.phones", "p") in f.foreach


def test_header_jspf() -> None:
    f = parse_jsp(
        "accounts/WEB-INF/jspf/header.jspf",
        (ACC / "WEB-INF/jspf/header.jspf").read_text(),
        "accounts",
        OPS,
    )
    assert {"menu.users", "menu.profile"} <= set(f.message_keys)
    assert {link.url for link in f.links} >= {"UserServlet?op=list", "profile?op=view"}


def test_users_js() -> None:
    calls = parse_js_calls("js/users.js", (ACC / "js/users.js").read_text())
    got = [(c.line, c.kind, c.method, c.url, c.params) for c in calls]
    assert got == [
        (4, "xhr", "GET", "UserServlet?op=list&page=", ["op", "page"]),
        (10, "post", "POST", "UserServlet?op=delete", ["id", "op"]),
        (16, "get", "GET", "UserServlet?op=read&id=", ["id", "op"]),
    ]


def test_orders_js() -> None:
    calls = parse_js_calls("js/orders.js", (SAL / "js/orders.js").read_text())
    assert [(c.line, c.kind, c.url) for c in calls] == [
        (3, "get", "LookupServlet?op=statuses"),
        (8, "xhr", "OrderServlet?op=export&status="),
    ]


def test_order_list() -> None:
    f = parse_jsp("sales/orderList.jsp", (SAL / "orderList.jsp").read_text(), "sales", OPS)
    [form] = f.forms
    assert (form.line, form.action, form.method, form.op) == (9, "OrderServlet", "GET", "list")
    assert {"status", "customerId", "page", "size"} <= {i.name for i in form.inputs}
    assert [(link.line, link.query.get("op")) for link in f.links][:4] == [
        (26, "view"), (30, "detail"), (34, "step1"), (35, "export"),
    ]  # fmt: skip


def test_wizards_and_report() -> None:
    w1 = parse_jsp("sales/wizardStep1.jsp", (SAL / "wizardStep1.jsp").read_text(), "sales", OPS)
    [form] = w1.forms
    assert (form.line, form.method, form.op) == (7, "POST", "step1")
    by = {i.name: i for i in form.inputs}
    assert by["customerId"].required
    assert by["notes"].type == "textarea" and by["notes"].maxlength == 500
    [call] = w1.js_calls
    assert (call.line, call.kind, call.url) == (19, "fetch", "LookupServlet?op=countries")

    w2 = parse_jsp("sales/wizardStep2.jsp", (SAL / "wizardStep2.jsp").read_text(), "sales", OPS)
    assert [(f.line, f.op) for f in w2.forms] == [(12, "step2"), (25, "confirm")]
    sku = next(i for i in w2.forms[0].inputs if i.name == "sku")
    assert sku.required and sku.maxlength == 20 and sku.pattern == "[A-Z0-9-]{3,20}"
    assert w2.foreach == [("sessionScope.orderDraft.lines", "l")]

    rep = parse_jsp("sales/report.jsp", (SAL / "report.jsp").read_text(), "sales", OPS)
    [form] = rep.forms
    assert (form.line, form.op) == (7, "summary")
    [call] = rep.js_calls
    assert (call.line, call.kind, call.url, call.params) == (
        19, "get", "ReportServlet?op=daily&day=2024-01-31", ["day", "op"],
    )  # fmt: skip


def test_js_variants_and_comments() -> None:
    js = """// $.get('Fake?op=no')
/* fetch('Fake2') */
fetch('A', {method: 'post'});
jQuery.ajax({url: "B?x=1", method: 'DELETE', data: {a: 1, 'b': 2}});
$.get('C', {q: 1});
var s = 'http://x'; $.post('D?op=1', {op: 'two'});
"""
    calls = parse_js_calls("x.js", js)
    got = [(c.line, c.kind, c.method, c.url, c.params) for c in calls]
    assert got == [
        (3, "fetch", "POST", "A", []),
        (4, "ajax", "DELETE", "B?x=1", ["a", "b", "x"]),
        (5, "get", "GET", "C", ["q"]),
        (6, "post", "POST", "D?op=1", ["op"]),
    ]


def test_broken_html_and_c_url() -> None:
    src = (
        "<div><form action=\"<c:url value='/OrderServlet'/>\" method=post>\n"
        '<input name=a value=1><input type="submit" name="go">\n'
        "<a href=\"<c:url value='/X'/>\">x</a>\n"
        '<c:url value="/Y" var="y"><c:param name="op" value="k"/></c:url>'
    )
    f = parse_jsp("a.jsp", src, None, OPS)
    assert f.forms[0].action == "/OrderServlet" and f.forms[0].method == "POST"
    assert [i.name for i in f.forms[0].inputs] == ["a"]
    assert [(link.url, link.query) for link in f.links] == [
        ("/X", {}),
        ("/Y", {"op": "k"}),
    ]
