# ruff: noqa: E501
from __future__ import annotations

import time
from pathlib import Path

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.java_symbols import (
    ancestors,
    build_java_index,
    find_method,
    lookup_constant,
    parse_java_file,
    resolve_index,
    resolve_type,
)
from j2ee_openapi_generator.index.models import FileRecord, JavaIndex

APP = Path(__file__).parents[1] / "fixtures" / "legacy-app"


def make_index(sources: dict[str, str]) -> JavaIndex:
    classes = {}
    for path, src in sources.items():
        for ci in parse_java_file(path, src):
            classes[ci.fqcn] = ci
    idx = JavaIndex(classes=dict(sorted(classes.items())))
    resolve_index(idx)
    return idx


def fixture_inventory() -> list[FileRecord]:
    return [
        FileRecord(path=p.relative_to(APP).as_posix(), sha256="", size=0, kind="java")
        for p in sorted(APP.rglob("*.java"))
        if "/test/" not in p.as_posix()
    ]


def test_nested_classes_annotations_and_members() -> None:
    src = """package a.b;
import java.util.List;
import javax.persistence.*;

@Entity
@Table(name = "T")
public class Outer extends Base implements Runnable, java.io.Serializable {
    @Column(nullable = false,
            length = 80)
    private String name = "x";
    protected static final String A = "p" + "q";
    static final String B = A + "-r";
    List<Map<String, Integer>> multi, other;
    public void run() throws java.io.IOException, IllegalStateException {}
    int add(int a, String... rest) { return a; }
    public static class Inner { String z; }
    enum Color { RED, GREEN; int f() { return 1; } }
    interface Cb { void call(String s); }
}
"""
    classes = {c.fqcn: c for c in parse_java_file("a/b/Outer.java", src)}
    assert set(classes) == {
        "a.b.Outer",
        "a.b.Outer.Inner",
        "a.b.Outer.Color",
        "a.b.Outer.Cb",
    }
    o = classes["a.b.Outer"]
    assert o.package == "a.b" and o.simple_name == "Outer" and o.kind == "class"
    assert o.annotations == ["Entity", 'Table(name = "T")']
    assert o.superclass == "Base"
    assert o.interfaces == ["Runnable", "java.io.Serializable"]
    f = {x.name: x for x in o.fields}
    assert f["name"].annotations == ["Column(nullable = false, length = 80)"]
    assert f["name"].initializer == '"x"'
    assert f["name"].line == 10
    assert f["multi"].type == "List<Map<String, Integer>>" and f["other"].type == f["multi"].type
    assert o.constants == {"A": "pq", "B": "pq-r"}
    m = {x.name: x for x in o.methods}
    assert m["run"].throws == ["java.io.IOException", "IllegalStateException"]
    assert m["add"].params == [("int", "a"), ("String...", "rest")]
    assert m["run"].start_line == 14 and m["run"].end_line == 14
    assert classes["a.b.Outer.Color"].kind == "enum"
    assert classes["a.b.Outer.Color"].enum_constants == ["RED", "GREEN"]
    assert classes["a.b.Outer.Cb"].kind == "interface"
    assert o.imports == ["java.util.List", "javax.persistence.*"]


def test_annotation_type_and_interface_constants() -> None:
    src = """package p;
public @interface Mark { String value() default "x"; }
interface K { String OP = "go"; }
"""
    cl = {c.fqcn: c for c in parse_java_file("p/K.java", src)}
    assert cl["p.Mark"].kind == "annotation"
    assert cl["p.K"].constants == {"OP": "go"}


def test_resolution_imports_package_wildcard() -> None:
    idx = make_index(
        {
            "x/Base.java": 'package x; public class Base { static final String K = "k"; }',
            "x/Child.java": "package x; import y.*; import z.Explicit;\n"
            "public class Child extends Base implements Wild, Explicit {"
            " Wild w; Explicit e; Base b; java.util.List<Base> l; String s; Nope n; }",
            "y/Wild.java": "package y; public interface Wild {}",
        }
    )
    c = idx.classes["x.Child"]
    assert c.superclass == "x.Base"
    assert c.interfaces == ["y.Wild", "z.Explicit"]
    rt = {f.name: f.resolved_type for f in c.fields}
    assert rt == {
        "w": "y.Wild",
        "e": "z.Explicit",
        "b": "x.Base",
        "l": "java.util.List",
        "s": "java.lang.String",
        "n": None,
    }
    assert resolve_type(idx, c, "Base[]") == "x.Base"
    assert resolve_type(idx, c, "Map<String,Base>") is None


def test_ancestors_cycle_safe_and_find_method() -> None:
    idx = make_index(
        {
            "a/A.java": "package a; class A extends B { void own() {} }",
            "a/B.java": "package a; class B extends A implements I { void up() {} }",
            "a/I.java": "package a; interface I { void iface(); }",
        }
    )
    assert [c.fqcn for c in ancestors(idx, "a.A")] == ["a.B", "a.I"]
    hit = find_method(idx, "a.A", "up")
    assert hit is not None and hit[0].fqcn == "a.B"
    hit = find_method(idx, "a.A", "iface")
    assert hit is not None and hit[0].fqcn == "a.I"
    assert find_method(idx, "a.A", "missing") is None
    assert find_method(idx, "no.Such", "x") is None


def test_lookup_constant_forms() -> None:
    idx = make_index(
        {
            "k/Const.java": 'package k; public class Const { public static final String OP_LIST = "list";'
            ' public static final String PAIR = OP_LIST + "2"; public static final String FWD = Other.X + "!"; }',
            "k/Other.java": 'package k; class Other { static final String X = "ex"; }',
            "k/Base.java": 'package k; class Base { static final String INH = "inherited"; }',
            "k/S.java": "package k; import static k.Const.OP_LIST; class S extends Base {}",
            "m/T.java": "package m; import k.Const; class T { static final String L = Const.OP_LIST; }",
        }
    )
    s = idx.classes["k.S"]
    t = idx.classes["m.T"]
    assert idx.classes["k.Const"].constants["PAIR"] == "list2"
    assert idx.classes["k.Const"].constants["FWD"] == "ex!"
    assert lookup_constant(idx, t, "Const.OP_LIST") == "list"
    assert lookup_constant(idx, t, "k.Const.OP_LIST") == "list"
    assert lookup_constant(idx, t, "L") == "list"
    assert lookup_constant(idx, s, "INH") == "inherited"
    assert lookup_constant(idx, s, "OP_LIST") == "list"  # static import
    assert lookup_constant(idx, s, "Nope") is None
    assert lookup_constant(idx, t, "Const.Nope") is None


def test_fixture_index_and_worker_equivalence() -> None:
    cfg = Config(repo_root=APP)
    inv = fixture_inventory()
    one = build_java_index(cfg, inv, workers=1)
    four = build_java_index(cfg, inv, workers=4)
    assert one.model_dump_json() == four.model_dump_json()
    assert list(one.classes) == sorted(one.classes)
    user = one.classes["com.acme.accounts.model.User"]
    name = next(f for f in user.fields if f.name == "name")
    assert "Column(nullable = false, length = 80)" in name.annotations
    assert (
        one.classes["com.acme.sales.web.ReportServlet"].superclass
        == "com.acme.sales.web.BaseServlet"
    )
    assert one.classes["com.acme.accounts.model.UserStatus"].enum_constants == [
        "ACTIVE",
        "SUSPENDED",
        "CLOSED",
    ]
    ops = one.classes["com.acme.sales.web.OrderServlet"].constants
    assert ops["OP_LIST"] == "list" and ops["OP_EXPORT"] == "export" and len(ops) == 7
    assert "com.acme.sales.web.LookupServlet.Handler" in one.classes


def test_parse_speed_sanity() -> None:
    body = "\n".join(
        f'    public int m{i}(String a, int b) {{ if (a.equals("x{i}")) {{ return b; }} return {i}; }}'
        for i in range(200)
    )
    src = "package p;\n" + "\n".join(f"class C{i} {{\n{body}\n}}" for i in range(25))
    start = time.perf_counter()
    out = parse_java_file("p/C.java", src)
    assert len(out) == 25 and len(out[0].methods) == 200
    assert time.perf_counter() - start < 5.0
