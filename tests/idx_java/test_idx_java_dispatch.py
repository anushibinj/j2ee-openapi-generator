# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.dispatch import extract_dispatch
from j2ee_openapi_generator.index.java_symbols import (
    build_java_index,
    parse_java_file,
    resolve_index,
)
from j2ee_openapi_generator.index.models import DispatchEntry, JavaIndex, ServletRecord

APP = Path(__file__).parents[1] / "fixtures" / "legacy-app"

HEAD = """package t;
import javax.servlet.http.*;
"""


def run_snippets(
    sources: dict[str, str], servlet: str = "t.S", cfg: Config | None = None
) -> list[DispatchEntry]:
    classes = {}
    for path, src in sources.items():
        for ci in parse_java_file(path, src):
            classes[ci.fqcn] = ci
    idx = JavaIndex(classes=dict(sorted(classes.items())))
    resolve_index(idx)
    cfg = cfg or Config(repo_root=Path("."))
    rec = ServletRecord(module="m", name="S", class_fqcn=servlet, url_patterns=["/s"])
    return extract_dispatch(cfg, idx, [rec], lambda p: sources[p])


def by_op(entries: list[DispatchEntry]) -> dict[str, DispatchEntry]:
    return {e.op: e for e in entries}


def servlet(body: str, extends: str = "HttpServlet", extra: str = "") -> dict[str, str]:
    return {"t/S.java": f"{HEAD}{extra}public class S extends {extends} {{\n{body}\n}}\n"}


# ----------------------------------------------------------------- fixture contract
def fixture_entries() -> dict[str, DispatchEntry]:
    from j2ee_openapi_generator.index.models import FileRecord

    cfg = Config(repo_root=APP)
    inv = [
        FileRecord(path=p.relative_to(APP).as_posix(), sha256="", size=0, kind="java")
        for p in sorted(APP.rglob("*.java"))
        if "/test/" not in p.as_posix()
    ]
    idx = build_java_index(cfg, inv)
    recs = [
        ServletRecord(module=m, name=n, class_fqcn=f"com.acme.{m}.web.{n}", url_patterns=["/" + n])
        for m, n in [
            ("accounts", "UserServlet"),
            ("accounts", "ProfileServlet"),
            ("sales", "OrderServlet"),
            ("sales", "ReportServlet"),
            ("sales", "LookupServlet"),
        ]
    ]
    out = extract_dispatch(cfg, idx, recs, lambda p: (APP / p).read_text(encoding="utf-8"))
    ids = [f"{e.module}:{e.servlet_simple_name}#{e.op}" for e in out]
    assert ids == sorted(ids)
    return dict(zip(ids, out, strict=True))


def test_fixture_dispatch_contract() -> None:
    e = fixture_entries()
    expected = {
        "accounts:UserServlet#list": (42, 49),
        "accounts:UserServlet#read": (50, 58),
        "accounts:UserServlet#edit": (59, 63),
        "accounts:UserServlet#new": (64, 67),
        "accounts:UserServlet#save": (68, 69),
        "accounts:UserServlet#delete": (70, 77),
        "accounts:UserServlet#<default>": (78, 80),
        "accounts:ProfileServlet#view": (41, 43),
        "accounts:ProfileServlet#update": (44, 57),
        "sales:OrderServlet#list": (55, 65),
        "sales:OrderServlet#view": (66, 75),
        "sales:OrderServlet#detail": (76, 81),
        "sales:OrderServlet#step1": (82, 94),
        "sales:OrderServlet#step2": (95, 109),
        "sales:OrderServlet#confirm": (110, 120),
        "sales:OrderServlet#export": (121, 132),
        "sales:OrderServlet#<default>": (133, 134),
        "sales:ReportServlet#summary": (21, 27),
        "sales:ReportServlet#daily": (29, 37),
        "sales:LookupServlet#countries": (39, 43),
        "sales:LookupServlet#statuses": (45, 48),
    }
    assert set(e) == set(expected)
    for k, (s, en) in expected.items():
        assert (e[k].start_line, e[k].end_line) == (s, en), k
    assert e["accounts:UserServlet#list"].style == "if"
    assert e["accounts:UserServlet#list"].http_methods == ["GET", "POST"]
    assert e["accounts:UserServlet#list"].file.endswith("web/UserServlet.java")
    assert e["accounts:ProfileServlet#view"].constant == "OP_VIEW"
    assert e["sales:OrderServlet#export"].style == "switch"
    assert e["sales:OrderServlet#export"].constant == "OP_EXPORT"
    assert e["sales:ReportServlet#daily"].style == "reflection"
    assert e["sales:ReportServlet#daily"].handler_method == "doDaily"
    assert e["sales:LookupServlet#statuses"].style == "map"
    assert e["sales:LookupServlet#statuses"].handler_method == "writeStatuses"
    assert e["sales:LookupServlet#statuses"].http_methods == ["GET"]
    assert all(x.param_name == "op" for x in e.values())


def test_fixture_dispatch_excludes_test_dir_literal() -> None:
    assert not any("hidden" in k for k in fixture_entries())


# ----------------------------------------------------------------- if style
def test_if_variants_and_http_methods() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        if ("a".equals(op)) {
            one();
        } else if (op.equalsIgnoreCase("b") || "c".equals(op)) {
            two();
        } else if ("d".equals(op))
            three();
        else {
            four();
        }
    }
    """
    e = by_op(run_snippets(servlet(body)))
    assert set(e) == {"a", "b", "c", "d", "<default>"}
    assert (e["a"].start_line, e["a"].end_line) == (7, 8)
    assert (e["b"].start_line, e["b"].end_line) == (9, 10) == (e["c"].start_line, e["c"].end_line)
    assert (e["d"].start_line, e["d"].end_line) == (11, 12)
    assert (e["<default>"].start_line, e["<default>"].end_line) == (13, 15)
    assert all(x.http_methods == ["GET"] and x.style == "if" for x in e.values())


def test_do_post_only_and_own_dispatch_beats_helper() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) { show(); }
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) {
        String action = req.getParameter("action");
        if ("save".equals(action)) { save(); }
    }
    """
    out = run_snippets(servlet(body))
    e = by_op(out)
    assert e["save"].http_methods == ["POST"] and e["save"].param_name == "action"
    # doGet has no dispatch: a default entry represents it
    assert e["<default>"].style == "default" and e["<default>"].http_methods == ["GET"]
    assert (e["<default>"].start_line, e["<default>"].end_line) == (5, 5)


def test_same_op_in_get_and_post_keeps_both_dispatch_entries() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        if ("update".equals(op)) { showForm(); }
    }
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        if ("update".equals(op)) { save(); }
    }
    """
    entries = [e for e in run_snippets(servlet(body)) if e.op == "update"]

    assert len(entries) == 2
    assert {tuple(e.http_methods) for e in entries} == {("GET",), ("POST",)}
    assert len({(e.start_line, e.end_line) for e in entries}) == 2


def test_direct_get_parameter_in_condition_and_unknown_param_ignored() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        if ("x".equals(req.getParameter("cmd"))) { a(); }
        String other = req.getParameter("unrelated");
        if ("y".equals(other)) { b(); }
    }
    """
    e = by_op(run_snippets(servlet(body)))
    assert set(e) == {"x"} and e["x"].param_name == "cmd"


def test_constants_from_other_class_and_qualified() -> None:
    srcs = servlet(
        """
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter(Ops.PARAM);
        if (Ops.LIST.equals(op)) { a(); }
        else if (op.equals(t.Ops.SAVE)) { b(); }
        else if (LOCAL.equals(op)) { c(); }
    }
""",
        extra="import static t.Ops.LOCAL;\n",
    )
    srcs["t/Ops.java"] = (
        'package t; public class Ops { public static final String PARAM = "op";'
        ' public static final String LIST = "list"; public static final String SAVE = "sa" + "ve";'
        ' public static final String LOCAL = "loc"; }'
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"list", "save", "loc"}
    assert e["list"].constant == "Ops.LIST"
    assert e["save"].constant == "t.Ops.SAVE"
    assert e["loc"].constant == "LOCAL"


def test_service_and_helper_chain_depth_and_super_call() -> None:
    srcs = servlet(
        """
    protected void service(HttpServletRequest req, HttpServletResponse resp) { a(req, resp); }
    private void a(HttpServletRequest req, HttpServletResponse resp) { b(req, resp); }
    private void b(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        if ("go".equals(op)) { x(); }
    }
""",
    )
    e = by_op(run_snippets(srcs))
    assert e["go"].http_methods == ["GET", "POST"]
    # inherited dispatch through super.doGet
    srcs2 = servlet(
        "    protected void doGet(HttpServletRequest q, HttpServletResponse r) { super.doGet(q, r); }",
        extends="Base",
    )
    srcs2["t/Base.java"] = (
        HEAD
        + """public class Base extends HttpServlet {
    protected void doGet(HttpServletRequest q, HttpServletResponse r) {
        String op = q.getParameter("op");
        if ("inh".equals(op)) { y(); }
    }
}
"""
    )
    e2 = by_op(run_snippets(srcs2))
    assert e2["inh"].file == "t/Base.java" and e2["inh"].http_methods == ["GET"]


def test_too_deep_helper_chain_not_followed() -> None:
    srcs = servlet(
        """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) { a(req, resp); }
    private void a(HttpServletRequest req, HttpServletResponse resp) { b(req, resp); }
    private void b(HttpServletRequest req, HttpServletResponse resp) { c(req, resp); }
    private void c(HttpServletRequest req, HttpServletResponse resp) { d(req, resp); }
    private void d(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        if ("deep".equals(op)) { x(); }
    }
"""
    )
    e = by_op(run_snippets(servlet_fixup(srcs)))
    assert "deep" not in e


def servlet_fixup(s: dict[str, str]) -> dict[str, str]:
    return s


def test_no_op_variable_gives_default_style() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        resp.getWriter().write("hi");
    }
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) {
        doGet(req, resp);
    }
    """
    out = run_snippets(servlet(body))
    assert len(out) == 1
    e = out[0]
    assert e.op == "<default>" and e.style == "default"
    assert e.http_methods == ["GET", "POST"]
    assert (e.start_line, e.end_line) == (5, 7)


def test_servlet_without_request_methods_yields_nothing() -> None:
    assert run_snippets(servlet("    void helper() {}")) == []


# ----------------------------------------------------------------- switch
def test_switch_fallthrough_default_and_constants() -> None:
    srcs = servlet(
        """
    static final String OP_C = "c";
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        switch (op.toLowerCase()) {
        case "a":
        case "b":
            one();
            break;
        case OP_C: {
            two();
            break;
        }
        case "d", "e":
            three();
            break;
        default:
            four();
        }
    }
"""
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"a", "b", "c", "d", "e", "<default>"}
    assert (e["a"].start_line, e["a"].end_line) == (9, 12) == (e["b"].start_line, e["b"].end_line)
    assert (e["c"].start_line, e["c"].end_line) == (13, 16)
    assert e["c"].constant == "OP_C"
    assert (e["d"].start_line, e["d"].end_line) == (17, 19) == (e["e"].start_line, e["e"].end_line)
    assert (e["<default>"].start_line, e["<default>"].end_line) == (20, 21)
    assert all(x.style == "switch" for x in e.values())


def test_switch_arrow_and_direct_condition() -> None:
    srcs = servlet(
        """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        switch (req.getParameter("op")) {
            case "p" -> one();
            case "q" -> {
                two();
            }
            default -> three();
        }
    }
"""
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"p", "q", "<default>"}
    assert (e["p"].start_line, e["p"].end_line) == (7, 7)
    assert (e["q"].start_line, e["q"].end_line) == (8, 10)
    assert e["<default>"].start_line == 11


# ----------------------------------------------------------------- map
def test_map_style_lambda_anonymous_class_and_handler_class() -> None:
    srcs = servlet(
        """
    interface H { void handle(HttpServletRequest q, HttpServletResponse r); }
    static class ListH implements H {
        public void handle(HttpServletRequest q, HttpServletResponse r) {
            list();
        }
    }
    private static final java.util.Map<String, H> TABLE = new java.util.HashMap<>();
    static {
        TABLE.put("lam", (q, r) -> {
            a();
        });
        TABLE.put("anon", new H() {
            public void handle(HttpServletRequest q, HttpServletResponse r) {
                b();
            }
        });
        TABLE.put("cls", new ListH());
        TABLE.put("ref", S::viaRef);
        TABLE.put("call", (q, r) -> viaCall(q, r));
    }
    static void viaRef(HttpServletRequest q, HttpServletResponse r) {
        c();
    }
    void viaCall(HttpServletRequest q, HttpServletResponse r) {
        d();
    }
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) {
        H h = TABLE.getOrDefault(req.getParameter("op"), this::fallback);
        h.handle(req, resp);
    }
    void fallback(HttpServletRequest q, HttpServletResponse r) {
        e();
    }
"""
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"lam", "anon", "cls", "ref", "call", "<default>"}
    assert all(x.style == "map" and x.http_methods == ["POST"] for x in e.values())
    assert (e["lam"].start_line, e["lam"].end_line) == (13, 15)
    assert e["anon"].handler_method == "handle" and (e["anon"].start_line, e["anon"].end_line) == (
        17,
        19,
    )
    assert e["cls"].handler_method == "handle" and (e["cls"].start_line, e["cls"].end_line) == (
        7,
        9,
    )
    assert e["ref"].handler_method == "viaRef" and (e["ref"].start_line, e["ref"].end_line) == (
        25,
        27,
    )
    assert e["call"].handler_method == "viaCall" and (e["call"].start_line, e["call"].end_line) == (
        28,
        30,
    )
    assert e["<default>"].handler_method == "fallback"


def test_map_put_in_init_with_constant_keys() -> None:
    srcs = servlet(
        """
    static final String K = "kk";
    java.util.Map<String, Runnable> m = new java.util.HashMap<String, Runnable>();
    public void init() {
        m.put(K, this::run1);
        this.m.put("plain", this::run1);
    }
    void run1() {
        x();
    }
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        Runnable r = m.get(op);
    }
"""
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"kk", "plain"}
    assert e["kk"].constant == "K" and e["kk"].handler_method == "run1"


def test_map_without_lookup_is_not_dispatch() -> None:
    srcs = servlet(
        """
    java.util.Map<String, Runnable> m = new java.util.HashMap<>();
    public void init() { m.put("a", () -> {}); }
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) { foo(); }
"""
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"<default>"} and e["<default>"].style == "default"


# ----------------------------------------------------------------- reflection
def test_reflection_patterns_and_filters() -> None:
    srcs = servlet(
        """
    public void doReport(HttpServletRequest q, HttpServletResponse r) {
        a();
    }
    public void handleExport(HttpServletRequest q, HttpServletResponse r) { b(); }
    public void plain(HttpServletRequest q, HttpServletResponse r) { c(); }
    private void doHidden(HttpServletRequest q, HttpServletResponse r) { d(); }
    public void doBadArgs(String x) { e(); }
    public void doOverride(HttpServletRequest q, HttpServletResponse r) { f(); }
""",
        extends="Base",
    )
    srcs["t/Base.java"] = (
        HEAD
        + """public abstract class Base extends HttpServlet {
    protected void doGet(HttpServletRequest q, HttpServletResponse r) { run(q, r); }
    protected void doPost(HttpServletRequest q, HttpServletResponse r) { run(q, r); }
    private void run(HttpServletRequest q, HttpServletResponse r) throws Exception {
        String op = q.getParameter("op");
        String name = "do" + cap(op);
        getClass().getMethod(name, HttpServletRequest.class, HttpServletResponse.class).invoke(this, q, r);
    }
    public void doOverride(HttpServletRequest q, HttpServletResponse r) { g(); }
    public void doInherited(HttpServletRequest q, HttpServletResponse r) { h(); }
    static String cap(String s) { return s; }
}
"""
    )
    e = by_op(run_snippets(srcs))
    # getMethod => public only; {op} pattern lets plain match too
    assert set(e) == {"report", "export", "plain", "override", "inherited"}
    assert e["report"].handler_method == "doReport" and e["report"].style == "reflection"
    assert e["report"].file == "t/S.java" and e["inherited"].file == "t/Base.java"
    assert e["override"].file == "t/S.java"  # nearest declaration wins
    assert e["report"].http_methods == ["GET", "POST"]
    assert (e["report"].start_line, e["report"].end_line) == (5, 7)


def test_reflection_custom_patterns() -> None:
    cfg = Config(repo_root=Path("."))
    cfg.dispatch.method_name_patterns = ["do{Op}"]
    srcs = servlet(
        """
    public void doOne(HttpServletRequest q, HttpServletResponse r) { a(); }
    public void other(HttpServletRequest q, HttpServletResponse r) { b(); }
    protected void doGet(HttpServletRequest q, HttpServletResponse r) throws Exception {
        String op = q.getParameter("op");
        getClass().getDeclaredMethod("do" + op, HttpServletRequest.class, HttpServletResponse.class).invoke(this, q, r);
    }
"""
    )
    e = by_op(run_snippets(srcs, cfg=cfg))
    assert set(e) == {"one"}


# ----------------------------------------------------------------- misc
def test_dedupe_and_sorting_and_unknown_servlet() -> None:
    srcs = servlet(
        """
    protected void doGet(HttpServletRequest q, HttpServletResponse r) {
        String op = q.getParameter("op");
        if ("b".equals(op)) { a(); }
        if ("a".equals(op)) { a(); }
        if ("b".equals(op)) { dup(); }
    }
"""
    )
    out = run_snippets(srcs)
    assert [e.op for e in out] == ["a", "b"]
    assert by_op(out)["b"].start_line == 7
    assert run_snippets(srcs, servlet="t.Missing") == []


def test_non_ascii_source_line_numbers() -> None:
    srcs = servlet(
        """
    // café über 中文
    protected void doGet(HttpServletRequest q, HttpServletResponse r) {
        String op = q.getParameter("op");
        if ("é".equals(op)) {
            a();
        }
    }
"""
    )
    e = by_op(run_snippets(srcs))
    assert set(e) == {"é"} and (e["é"].start_line, e["é"].end_line) == (8, 10)


# ----------------------------------------------------------------- wrapper getParameter / charAt
def test_wrapper_get_parameter_and_char_switch() -> None:
    """``Util.getParameter(req, "op")`` + ``switch (op.charAt(0)) { case 'n': ... }``."""
    body = """
    public void doPost(HttpServletRequest req, HttpServletResponse resp) {
        String szOperation = UtilRequest.getParameter(req, "op");
        if (szOperation != null) {
            switch (szOperation.charAt(0)) {
            case 'n':
                add(req);
                show(req);
                break;
            case 'u':
                update(req);
                break;
            case 'g':
            case 'd':
                groups(req);
                break;
            default:
                unknown();
            }
        } else {
        }
    }
    """
    e = by_op(run_snippets(servlet(body)))
    assert {"n", "u", "g", "d", "<default>"} <= set(e)
    assert e["n"].style == "switch" and e["n"].param_name == "op"
    assert (e["g"].start_line, e["g"].end_line) == (e["d"].start_line, e["d"].end_line)
    assert e["n"].http_methods == ["POST"]


def test_wrapper_get_parameter_inline_and_if_char_compare() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        if (UtilRequest.getParameter(req, "action").charAt(0) == 'x') { a(); }
        else if ('y' == UtilRequest.getParameter(req, "action").charAt(0)) { b(); }
    }
    """
    e = by_op(run_snippets(servlet(body)))
    assert {"x", "y"} <= set(e) and e["x"].param_name == "action"


def test_wrapper_get_parameter_unknown_param_still_ignored() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String other = UtilRequest.getParameter(req, "unrelated");
        if ("y".equals(other)) { b(); }
    }
    """
    e = by_op(run_snippets(servlet(body)))
    assert set(e) == {"<default>"} and e["<default>"].style == "default"


def test_char_escape_label() -> None:
    body = """
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String op = req.getParameter("op");
        switch (op.charAt(0)) {
        case '\\t': a(); break;
        case 'q': b(); break;
        }
    }
    """
    e = by_op(run_snippets(servlet(body)))
    assert {"\t", "q"} <= set(e)
