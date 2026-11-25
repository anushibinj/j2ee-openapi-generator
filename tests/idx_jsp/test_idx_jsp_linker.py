from j2ee_openapi_generator.index.jsp import parse_jsp
from j2ee_openapi_generator.index.linker import jsps_for_entry, link_references
from j2ee_openapi_generator.index.models import (
    FormFact,
    FormInput,
    JsCall,
    JspFacts,
    LinkFact,
    ModuleRecord,
    ServletRecord,
    WebIndex,
)

CTX = "${pageContext.request.contextPath}"
U = "com.acme.accounts.web.UserServlet"
P = "com.acme.accounts.web.ProfileServlet"
O = "com.acme.sales.web.OrderServlet"  # noqa: E741

WEB = WebIndex(
    modules=[
        ModuleRecord(
            name="accounts", web_xml="accounts/WEB-INF/web.xml", root_dir="accounts",
            context_root="/accounts",
        ),
        ModuleRecord(
            name="sales", web_xml="sales/WEB-INF/web.xml", root_dir="sales", context_root="/sales"
        ),
    ],
    servlets=[
        ServletRecord(module="accounts", name="U", class_fqcn=U, url_patterns=["/UserServlet"]),
        ServletRecord(
            module="accounts", name="P", class_fqcn=P, url_patterns=["/profile"],
            source="annotation",
        ),
        ServletRecord(module="sales", name="O", class_fqcn=O, url_patterns=["/OrderServlet"]),
        ServletRecord(
            module="sales", name="R", class_fqcn="com.acme.sales.web.Rest",
            url_patterns=["/api/*", "*.do"],
        ),
    ],
)  # fmt: skip


def _jsp(file: str, **kw: object) -> JspFacts:
    return JspFacts(file=file, module=file.split("/")[0], **kw)  # type: ignore[arg-type]


def test_form_link_js_relative() -> None:
    jsp = _jsp(
        "accounts/src/main/webapp/users/userList.jsp",
        forms=[
            FormFact(
                file="f", line=3, action="../UserServlet", method="POST", op="save", op_param="op",
                inputs=[FormInput(name="op"), FormInput(name="id"), FormInput(name="name")],
            )
        ],
        links=[LinkFact(file="f", line=5, url="UserServlet?op=read&id=${u.id};jsessionid=AB")],
        js_calls=[
            JsCall(
                file="f", line=9, kind="post", url="/UserServlet?op=delete", method="POST",
                params=["id", "op"],
            )
        ],
    )  # fmt: skip
    refs = link_references([jsp], WEB, ["op"])
    got = [(r.kind, r.line, r.op, r.http_method, r.params) for r in refs]
    assert got == [
        ("js", 9, "delete", "POST", ["id"]),
        ("link", 5, "read", "GET", ["id"]),
        ("form", 3, "save", "POST", ["id", "name"]),
    ]
    assert {r.servlet_fqcn for r in refs} == {U}
    assert jsps_for_entry(refs, U, "save") == [jsp.file]
    assert jsps_for_entry(refs, U, "nope") == []


def test_context_path_absolute_and_unknown() -> None:
    jsp = _jsp(
        "accounts/src/main/webapp/a.jsp",
        links=[
            LinkFact(file="f", line=1, url=CTX + "/UserServlet?op=list"),
            LinkFact(file="f", line=2, url="/accounts/profile?op=view"),
            LinkFact(file="f", line=3, url="/sales/OrderServlet?op=step1"),
            LinkFact(file="f", line=4, url="/UserServlet"),
            LinkFact(file="f", line=5, url="Unknown?op=x"),
            LinkFact(file="f", line=6, url="http://example.com/UserServlet?op=x"),
            LinkFact(file="f", line=7, url="css/site.css"),
            LinkFact(file="f", line=8, url="${u.url}"),
            LinkFact(file="f", line=9, url="<c:url value='/UserServlet'/>"),
        ],
    )  # fmt: skip
    refs = link_references([jsp], WEB, ["op"])
    got = sorted((r.line, r.module, r.servlet_fqcn.rsplit(".", 1)[1], r.op) for r in refs)
    assert got == [
        (1, "accounts", "UserServlet", "list"),
        (2, "accounts", "ProfileServlet", "view"),
        (3, "sales", "OrderServlet", "step1"),
        (4, "accounts", "UserServlet", None),
        (9, "accounts", "UserServlet", None),
    ]


def test_prefix_extension_matching_and_sorting() -> None:
    jsp = _jsp(
        "sales/src/main/webapp/x.jsp",
        links=[
            LinkFact(file="f", line=1, url="api/v1/thing?op=a"),
            LinkFact(file="f", line=2, url="save.do?op=b"),
            LinkFact(file="f", line=3, url="OrderServlet?op=z"),
            LinkFact(file="f", line=4, url="OrderServlet?op=a"),
        ],
    )
    refs = link_references([jsp], WEB, ["op"])
    assert [(r.servlet_fqcn.rsplit(".", 1)[1], r.op) for r in refs] == [
        ("OrderServlet", "a"),
        ("OrderServlet", "z"),
        ("Rest", "a"),
        ("Rest", "b"),
    ]


def test_fixture_style_jsp_end_to_end() -> None:
    src = (
        '<form action="OrderServlet" method="post">\n'
        '<input type="hidden" name="op" value="step1"/><input name="customerId"/></form>\n'
        "<script>fetch('LookupServlet?op=countries');</script>"
    )
    facts = parse_jsp("sales/src/main/webapp/wizardStep1.jsp", src, "sales", ["op"])
    refs = link_references([facts], WEB, ["op"])
    assert [(r.kind, r.op, r.params) for r in refs] == [("form", "step1", ["customerId"])]
