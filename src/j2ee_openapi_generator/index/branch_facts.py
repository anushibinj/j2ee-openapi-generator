"""Mechanical facts of one dispatch branch (request params, session, forwards, EJB calls ...).

The Java source of the entry file is parsed with tree-sitter and the nodes whose first line is
inside the branch range are inspected. Same-class helper methods are followed up to depth 2.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from j2ee_openapi_generator.index.ejb import (
    class_chain,
    resolve_class,
    split_top,
    strip_generics,
    unquote,
)
from j2ee_openapi_generator.index.models import (
    BranchFacts,
    ClassInfo,
    DispatchEntry,
    EjbRecord,
    JavaIndex,
)

_LANG = Language(tree_sitter_java.language())
_MAX_HELPER_DEPTH = 2

STATUS_CONSTANTS: dict[str, int] = {
    "SC_CONTINUE": 100,
    "SC_SWITCHING_PROTOCOLS": 101,
    "SC_OK": 200,
    "SC_CREATED": 201,
    "SC_ACCEPTED": 202,
    "SC_NON_AUTHORITATIVE_INFORMATION": 203,
    "SC_NO_CONTENT": 204,
    "SC_RESET_CONTENT": 205,
    "SC_PARTIAL_CONTENT": 206,
    "SC_MULTIPLE_CHOICES": 300,
    "SC_MOVED_PERMANENTLY": 301,
    "SC_MOVED_TEMPORARILY": 302,
    "SC_FOUND": 302,
    "SC_SEE_OTHER": 303,
    "SC_NOT_MODIFIED": 304,
    "SC_USE_PROXY": 305,
    "SC_TEMPORARY_REDIRECT": 307,
    "SC_BAD_REQUEST": 400,
    "SC_UNAUTHORIZED": 401,
    "SC_PAYMENT_REQUIRED": 402,
    "SC_FORBIDDEN": 403,
    "SC_NOT_FOUND": 404,
    "SC_METHOD_NOT_ALLOWED": 405,
    "SC_NOT_ACCEPTABLE": 406,
    "SC_PROXY_AUTHENTICATION_REQUIRED": 407,
    "SC_REQUEST_TIMEOUT": 408,
    "SC_CONFLICT": 409,
    "SC_GONE": 410,
    "SC_LENGTH_REQUIRED": 411,
    "SC_PRECONDITION_FAILED": 412,
    "SC_REQUEST_ENTITY_TOO_LARGE": 413,
    "SC_REQUEST_URI_TOO_LONG": 414,
    "SC_UNSUPPORTED_MEDIA_TYPE": 415,
    "SC_REQUESTED_RANGE_NOT_SATISFIABLE": 416,
    "SC_EXPECTATION_FAILED": 417,
    "SC_INTERNAL_SERVER_ERROR": 500,
    "SC_NOT_IMPLEMENTED": 501,
    "SC_BAD_GATEWAY": 502,
    "SC_SERVICE_UNAVAILABLE": 503,
    "SC_GATEWAY_TIMEOUT": 504,
    "SC_HTTP_VERSION_NOT_SUPPORTED": 505,
}


# ------------------------------------------------------------------ small tree helpers
def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _txt(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8")


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _end_line(node: Node) -> int:
    return int(node.end_point[0]) + 1


def _args(node: Node) -> list[Node]:
    a = node.child_by_field_name("arguments")
    return list(a.named_children) if a is not None else []


@dataclass
class _Env:
    types: dict[str, str] = field(default_factory=dict)
    strs: dict[str, list[str]] = field(default_factory=dict)
    ejb_fields: set[str] = field(default_factory=set)


@dataclass
class _Acc:
    params: dict[str, int] = field(default_factory=dict)
    multi: set[str] = field(default_factory=set)
    param_map: bool = False
    parts: set[str] = field(default_factory=set)
    session_reads: set[str] = field(default_factory=set)
    session_writes: set[str] = field(default_factory=set)
    attrs: set[str] = field(default_factory=set)
    forwards: set[str] = field(default_factory=set)
    includes: set[str] = field(default_factory=set)
    redirects: set[str] = field(default_factory=set)
    status: dict[int, int] = field(default_factory=dict)
    writes: bool = False
    ctypes: set[str] = field(default_factory=set)
    cdisp: set[str] = field(default_factory=set)
    thrown: set[str] = field(default_factory=set)
    caught: set[str] = field(default_factory=set)
    ejb: set[str] = field(default_factory=set)
    helpers: set[str] = field(default_factory=set)
    ranges: set[tuple[str, int, int]] = field(default_factory=set)


class _Analyzer:
    def __init__(
        self,
        entry: DispatchEntry,
        java: JavaIndex,
        read_source: Callable[[str], str],
        ejbs: list[EjbRecord],
        module_root: str,
        jsp_files: set[str],
    ) -> None:
        self.entry = entry
        self.java = java
        self.read_source = read_source
        self.ejbs = ejbs
        self.module_root = module_root.strip("/") if module_root not in ("", ".") else ""
        self.jsp_files = jsp_files
        self.parser = Parser(_LANG)
        self.trees: dict[str, Node] = {}
        self.acc = _Acc()
        self.visited: set[tuple[str, int]] = set()
        self.ejb_by_type: dict[str, EjbRecord] = {}
        for r in ejbs:
            self.ejb_by_type.setdefault(r.bean_fqcn, r)
            for i in r.interfaces:
                self.ejb_by_type.setdefault(i, r)
        self.lo = entry.start_line
        self.hi = entry.end_line

    # ------------------------------------------------------------ parsing
    def tree(self, file: str) -> Node:
        if file not in self.trees:
            self.trees[file] = self.parser.parse(self.read_source(file).encode("utf-8")).root_node
        return self.trees[file]

    def type_decl(self, file: str, simple: str) -> Node | None:
        for n in _walk(self.tree(file)):
            if n.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                if _txt(n.child_by_field_name("name")) == simple:
                    return n
        return None

    def class_for_file(self) -> ClassInfo | None:
        e = self.entry
        if e.servlet_fqcn in self.java.classes and self.java.classes[e.servlet_fqcn].file == e.file:
            return self.java.classes[e.servlet_fqcn]
        cands = sorted(
            (c for c in self.java.classes.values() if c.file == e.file), key=lambda c: c.fqcn
        )
        if cands:
            return cands[0]
        return self.java.classes.get(e.servlet_fqcn)

    # ------------------------------------------------------------ constants / strings
    def const_in(self, ci: ClassInfo, name: str) -> str | None:
        for c in class_chain(self.java, ci):
            if name in c.constants:
                return (
                    unquote(c.constants[name])
                    if c.constants[name].startswith('"')
                    else c.constants[name]
                )
            for f in c.fields:
                if f.name == name and "static" in f.modifiers and f.initializer:
                    ini = f.initializer.strip()
                    if ini.startswith('"') and ini.endswith('"'):
                        return unquote(ini)
        return None

    def const_value(
        self, name: str, ci: ClassInfo | None, qualifier: str | None = None
    ) -> str | None:
        if ci is None:
            return None
        if qualifier:
            holder = resolve_class(self.java, ci, qualifier)
            if holder is not None:
                return self.const_in(holder, name)
            return None
        v = self.const_in(ci, name)
        if v is not None:
            return v
        for imp in ci.imports:
            t = imp.strip().removeprefix("import ").removeprefix("static ").rstrip(";").strip()
            if t.endswith("." + name):
                holder = resolve_class(self.java, ci, t[: -len(name) - 1])
                if holder is not None:
                    v = self.const_in(holder, name)
                    if v is not None:
                        return v
            elif t.endswith(".*"):
                holder = resolve_class(self.java, ci, t[:-2])
                if holder is not None:
                    v = self.const_in(holder, name)
                    if v is not None:
                        return v
        return None

    def strs(
        self, node: Node | None, env: _Env, ci: ClassInfo | None, partial: bool = False
    ) -> list[str]:
        if node is None:
            return []
        t = node.type
        if t == "string_literal":
            return [unquote(_txt(node))]
        if t == "parenthesized_expression" and node.named_children:
            return self.strs(node.named_children[0], env, ci, partial)
        if t == "ternary_expression":
            out: list[str] = []
            for k in ("consequence", "alternative"):
                out.extend(self.strs(node.child_by_field_name(k), env, ci, partial))
            return sorted(set(out))
        if t == "binary_expression" and _txt(node.child_by_field_name("operator")) == "+":
            lefts = self.strs(node.child_by_field_name("left"), env, ci, partial) or (
                [f"{{{_txt(node.child_by_field_name('left'))}}}"] if partial else []
            )
            rights = self.strs(node.child_by_field_name("right"), env, ci, partial) or (
                [f"{{{_txt(node.child_by_field_name('right'))}}}"] if partial else []
            )
            if not lefts or not rights:
                return []
            return sorted({a + b for a in lefts[:4] for b in rights[:4]})
        if t == "identifier":
            name = _txt(node)
            if name in env.strs:
                return env.strs[name]
            v = self.const_value(name, ci)
            if v is not None:
                return [v]
        if t == "field_access":
            v = self.const_value(
                _txt(node.child_by_field_name("field")),
                ci,
                _txt(node.child_by_field_name("object")),
            )
            if v is not None:
                return [v]
        if t == "method_invocation":
            name = _txt(node.child_by_field_name("name"))
            if name == "getContextPath" and partial:
                return [""]
            if name in ("encodeRedirectURL", "encodeURL", "encodeRedirectUrl", "trim") and _args(
                node
            ):
                return self.strs(_args(node)[0], env, ci, partial)
        if partial:
            return [f"{{{_txt(node)}}}"]
        return []

    def ints(self, node: Node | None) -> list[int]:
        if node is None:
            return []
        t = node.type
        if t == "decimal_integer_literal":
            try:
                return [int(_txt(node).replace("_", ""))]
            except ValueError:
                return []
        if t == "parenthesized_expression" and node.named_children:
            return self.ints(node.named_children[0])
        if t == "ternary_expression":
            return self.ints(node.child_by_field_name("consequence")) + self.ints(
                node.child_by_field_name("alternative")
            )
        name = ""
        if t == "identifier":
            name = _txt(node)
        elif t == "field_access":
            name = _txt(node.child_by_field_name("field"))
        if name in STATUS_CONSTANTS:
            return [STATUS_CONSTANTS[name]]
        return []

    # ------------------------------------------------------------ environments
    def method_env(self, method: Node, chain: list[ClassInfo], ci: ClassInfo | None) -> _Env:
        env = _Env()
        for c in reversed(chain):
            for f in c.fields:
                env.types[f.name] = f.type
                if any(a.split("(")[0].strip().rsplit(".", 1)[-1] == "EJB" for a in f.annotations):
                    env.ejb_fields.add(f.name)
        for n in _walk(method):
            t = n.type
            if t in ("formal_parameter", "spread_parameter"):
                ty, nm = n.child_by_field_name("type"), n.child_by_field_name("name")
                if ty is not None and nm is not None:
                    env.types[_txt(nm)] = _txt(ty)
            elif t == "enhanced_for_statement":
                ty, nm = n.child_by_field_name("type"), n.child_by_field_name("name")
                if ty is not None and nm is not None:
                    env.types[_txt(nm)] = _txt(ty)
            elif t == "catch_formal_parameter":
                nm = n.child_by_field_name("name")
                ct = next((c for c in n.children if c.type == "catch_type"), None)
                if nm is not None and ct is not None and ct.named_children:
                    env.types[_txt(nm)] = _txt(ct.named_children[0])
            elif t == "local_variable_declaration":
                ty = n.child_by_field_name("type")
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    nm, val = d.child_by_field_name("name"), d.child_by_field_name("value")
                    if nm is None:
                        continue
                    declared = _txt(ty)
                    if declared == "var" and val is not None:
                        v = val
                        if v.type == "cast_expression":
                            declared = _txt(v.child_by_field_name("type"))
                        elif v.type == "object_creation_expression":
                            declared = _txt(v.child_by_field_name("type"))
                    env.types[_txt(nm)] = declared
                    if val is not None:
                        self.bind_value(_txt(nm), val, env, ci)
            elif t == "assignment_expression":
                left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
                if left is not None and left.type == "identifier" and right is not None:
                    self.bind_value(_txt(left), right, env, ci)
        return env

    def bind_value(self, name: str, val: Node, env: _Env, ci: ClassInfo | None) -> None:
        if val.type == "method_invocation" and _txt(val.child_by_field_name("name")) in (
            "getRequestDispatcher",
            "getNamedDispatcher",
        ):
            a = _args(val)
            if a:
                found = self.strs(a[0], env, ci)
                if found:
                    env.strs[name] = found
            return
        found = self.strs(val, env, ci)
        if found:
            env.strs[name] = sorted(set(env.strs.get(name, []) + found))

    # ------------------------------------------------------------ jsp paths
    def resolve_jsp(self, raw: str) -> str:
        path = raw.split("?", 1)[0].split("#", 1)[0]
        if not path:
            return raw
        if not path.startswith("/"):
            path = "/" + path
        base = f"{self.module_root}/src/main/webapp" if self.module_root else "src/main/webapp"
        cand = posixpath.normpath(base + path)
        if cand in self.jsp_files:
            return cand
        hits = sorted(f for f in self.jsp_files if f.endswith(path))
        if hits:
            pref = [h for h in hits if self.module_root and h.startswith(self.module_root + "/")]
            return (pref or hits)[0]
        return raw

    # ------------------------------------------------------------ EJB
    def type_of(self, node: Node | None, env: _Env, ci: ClassInfo | None) -> str | None:
        if node is None:
            return None
        t = node.type
        if t == "identifier":
            return env.types.get(_txt(node))
        if t == "field_access":
            if _txt(node.child_by_field_name("object")) == "this":
                return env.types.get(_txt(node.child_by_field_name("field")))
            return None
        if t == "parenthesized_expression" and node.named_children:
            return self.type_of(node.named_children[0], env, ci)
        if t == "cast_expression":
            return _txt(node.child_by_field_name("type"))
        if t == "method_invocation" and _txt(node.child_by_field_name("name")) == "lookup":
            a = _args(node)
            if a:
                m = re.fullmatch(r"([\w.]+)\s*\.class", _txt(a[0]).strip())
                if m:
                    return m.group(1)
                if a[0].type == "string_literal":
                    return self.jndi_type(unquote(_txt(a[0])))
        return None

    def jndi_type(self, name: str) -> str | None:
        if "!" in name:
            return name.rsplit("!", 1)[1]
        last = name.rstrip("/").rsplit("/", 1)[-1]
        for r in self.ejbs:
            if r.bean_fqcn.rsplit(".", 1)[-1] == last and r.interfaces:
                return r.interfaces[0]
        return None

    def ejb_call(self, node: Node, env: _Env, ci: ClassInfo | None) -> None:
        obj = node.child_by_field_name("object")
        ty = self.type_of(obj, env, ci)
        if ty is None:
            return
        target = resolve_class(self.java, ci, ty)
        fq = target.fqcn if target is not None else strip_generics(ty)
        rec = self.ejb_by_type.get(fq)
        is_ejb_field = (
            obj is not None
            and obj.type in ("identifier", "field_access")
            and (_txt(obj).removeprefix("this.") in env.ejb_fields)
        )
        if rec is None and not (is_ejb_field and target is not None):
            return
        name = _txt(node.child_by_field_name("name"))
        args = _args(node)
        sig = self.method_sig(name, args, rec, target, env, ci)
        self.acc.ejb.add(f"{fq}#{sig}")

    def method_sig(
        self,
        name: str,
        args: list[Node],
        rec: EjbRecord | None,
        target: ClassInfo | None,
        env: _Env,
        ci: ClassInfo | None,
    ) -> str:
        cands: list[tuple[str, list[str]]] = []
        if rec is not None:
            for m in rec.methods:
                if m.name == name:
                    inner = m.signature[m.signature.index("(") + 1 : m.signature.rindex(")")]
                    cands.append((m.signature, [p for p in split_top(inner) if p]))
        if not cands and target is not None:
            for mi in target.methods:
                if mi.name == name:
                    cands.append((mi.signature, [t for t, _ in mi.params]))
        same = [c for c in cands if len(c[1]) == len(args)]
        if not same:
            return f"{name}(?)"
        if len(same) > 1:
            guesses = [self.guess_type(a, env) for a in args]
            for c in same:
                if all(
                    g is None or strip_generics(p) == g for g, p in zip(guesses, c[1], strict=True)
                ):
                    return c[0]
        return same[0][0]

    def guess_type(self, node: Node, env: _Env) -> str | None:
        t = node.type
        if t == "string_literal":
            return "String"
        if t == "decimal_integer_literal":
            return "int"
        if t == "true" or t == "false":
            return "boolean"
        if t == "identifier":
            ty = env.types.get(_txt(node))
            return strip_generics(ty) if ty else None
        if t == "object_creation_expression":
            return strip_generics(_txt(node.child_by_field_name("type")))
        return None

    # ------------------------------------------------------------ scanning
    def in_range(self, node: Node, lo: int, hi: int) -> bool:
        return lo <= _line(node) <= hi

    def scan(self, method: Node, ci: ClassInfo | None, lo: int, hi: int, depth: int) -> None:
        chain = class_chain(self.java, ci) if ci is not None else []
        env = self.method_env(method, chain, ci)
        for n in _walk(method):
            if not self.in_range(n, lo, hi):
                continue
            t = n.type
            if t == "method_invocation":
                self.on_invocation(n, env, ci, depth)
            elif t == "method_reference":
                self.on_method_ref(n, ci, depth)
            elif t == "throw_statement":
                self.on_throw(n, env)
            elif t == "catch_clause":
                for ct in _walk(n):
                    if ct.type == "catch_type":
                        for c in ct.named_children:
                            self.acc.caught.add(strip_generics(_txt(c)))
                        break

    def on_throw(self, n: Node, env: _Env) -> None:
        for c in n.named_children:
            if c.type == "object_creation_expression":
                self.acc.thrown.add(strip_generics(_txt(c.child_by_field_name("type"))))
            elif c.type == "identifier" and _txt(c) in env.types:
                self.acc.thrown.add(strip_generics(env.types[_txt(c)]))

    def is_session(self, obj: Node | None, env: _Env) -> bool:
        if obj is None:
            return False
        if obj.type == "method_invocation":
            return _txt(obj.child_by_field_name("name")) == "getSession"
        if obj.type == "identifier":
            ty = env.types.get(_txt(obj), "")
            return "HttpSession" in ty or "session" in _txt(obj).lower()
        if obj.type == "field_access":
            return "session" in _txt(obj.child_by_field_name("field")).lower()
        return False

    def on_invocation(self, n: Node, env: _Env, ci: ClassInfo | None, depth: int) -> None:
        name = _txt(n.child_by_field_name("name"))
        obj = n.child_by_field_name("object")
        args = _args(n)
        a = self.acc
        line = _line(n)
        first = args[0] if args else None
        if name == "getParameter" and first is not None:
            for v in self.strs(first, env, ci):
                a.params[v] = min(a.params.get(v, line), line)
        elif name == "getParameterValues" and first is not None:
            a.multi.update(self.strs(first, env, ci))
        elif name == "getParameterMap":
            a.param_map = True
        elif name == "getPart" and first is not None:
            a.parts.update(self.strs(first, env, ci))
        elif name == "getParts":
            a.parts.add("*")
        elif name in ("setAttribute", "getAttribute", "removeAttribute") and first is not None:
            names = self.strs(first, env, ci)
            if self.is_session(obj, env):
                if name == "getAttribute":
                    a.session_reads.update(names)
                else:
                    a.session_writes.update(names)
            elif name == "setAttribute":
                low = _txt(obj).lower()
                if "context" not in low and "application" not in low:
                    a.attrs.update(names)
        elif name in ("forward", "include") and obj is not None:
            raw: list[str] = []
            if obj.type == "method_invocation" and _txt(obj.child_by_field_name("name")) in (
                "getRequestDispatcher",
                "getNamedDispatcher",
            ):
                oa = _args(obj)
                raw = self.strs(oa[0], env, ci) if oa else []
            elif obj.type == "identifier":
                raw = env.strs.get(_txt(obj), [])
            target = a.forwards if name == "forward" else a.includes
            for r in raw:
                target.add(self.resolve_jsp(r))
        elif name == "sendRedirect" and first is not None:
            a.redirects.update(self.strs(first, env, ci, partial=True))
        elif name in ("sendError", "setStatus") and first is not None:
            for code in self.ints(first):
                a.status[code] = min(a.status.get(code, line), line)
        elif name in ("getWriter", "getOutputStream"):
            a.writes = True
        elif name == "setContentType" and first is not None:
            a.ctypes.update(self.strs(first, env, ci))
        elif name in ("setHeader", "addHeader") and len(args) >= 2:
            hdr = {h.lower() for h in self.strs(args[0], env, ci)}
            vals = self.strs(args[1], env, ci)
            if "content-disposition" in hdr:
                a.cdisp.update(vals)
            if "content-type" in hdr:
                a.ctypes.update(vals)
        self.ejb_call(n, env, ci)
        if depth < _MAX_HELPER_DEPTH and (obj is None or _txt(obj) == "this"):
            self.follow(name, len(args), ci, depth)

    def on_method_ref(self, n: Node, ci: ClassInfo | None, depth: int) -> None:
        kids = n.named_children
        if len(kids) == 2 and kids[0].type == "this" and kids[1].type == "identifier":
            if depth < _MAX_HELPER_DEPTH:
                self.follow(_txt(kids[1]), None, ci, depth)

    def follow(self, name: str, argc: int | None, ci: ClassInfo | None, depth: int) -> None:
        if ci is None:
            return
        for c in class_chain(self.java, ci):
            if not any(m.name == name for m in c.methods) or not c.file:
                continue
            decl = self.type_decl(c.file, c.simple_name)
            if decl is None:
                continue
            body = decl.child_by_field_name("body")
            if body is None:
                continue
            found = [
                m
                for m in body.named_children
                if m.type == "method_declaration" and _txt(m.child_by_field_name("name")) == name
            ]
            if argc is not None:
                same = [m for m in found if len(self.param_nodes(m)) == argc or self.is_varargs(m)]
                found = same or found
            for m in found:
                s, e = _line(m), _end_line(m)
                key = (c.file, s)
                if key in self.visited:
                    continue
                if c.file == self.entry.file and s <= self.hi and e >= self.lo:
                    continue
                self.visited.add(key)
                self.acc.helpers.add(name)
                self.acc.ranges.add((name, s, e))
                self.scan(m, c, s, e, depth + 1)
            if found:
                return

    @staticmethod
    def param_nodes(m: Node) -> list[Node]:
        p = m.child_by_field_name("parameters")
        return [c for c in p.named_children] if p is not None else []

    def is_varargs(self, m: Node) -> bool:
        return any(c.type == "spread_parameter" for c in self.param_nodes(m))

    # ------------------------------------------------------------ driver
    def run(self) -> BranchFacts:
        e = self.entry
        root = self.tree(e.file)
        for m in _walk(root):
            if m.type not in ("method_declaration", "constructor_declaration"):
                continue
            if _line(m) > self.hi or _end_line(m) < self.lo:
                continue
            ci = self.enclosing_class(m)
            self.visited.add((e.file, _line(m)))
            self.scan(m, ci, self.lo, self.hi, 0)
        a = self.acc
        return BranchFacts(
            params=sorted(a.params),
            param_lines=dict(sorted(a.params.items())),
            multi_params=sorted(a.multi),
            reads_param_map=a.param_map,
            multipart_parts=sorted(a.parts),
            session_reads=sorted(a.session_reads),
            session_writes=sorted(a.session_writes),
            attributes_set=sorted(a.attrs),
            forwards=sorted(a.forwards),
            includes=sorted(a.includes),
            redirects=sorted(a.redirects),
            status_codes=sorted(a.status),
            status_lines={str(k): v for k, v in sorted(a.status.items())},
            writes_response=a.writes,
            content_types=sorted(a.ctypes),
            content_disposition=sorted(a.cdisp),
            exceptions_thrown=sorted(a.thrown),
            exceptions_caught=sorted(a.caught),
            ejb_calls=sorted(a.ejb),
            helper_calls=sorted(a.helpers),
            helper_ranges=sorted(a.ranges),
        )

    def enclosing_class(self, m: Node) -> ClassInfo | None:
        cur: Node | None = m.parent
        while cur is not None:
            if cur.type in ("class_declaration", "enum_declaration", "interface_declaration"):
                simple = _txt(cur.child_by_field_name("name"))
                for c in sorted(self.java.classes.values(), key=lambda c: c.fqcn):
                    if c.file == self.entry.file and c.simple_name == simple:
                        return c
                break
            cur = cur.parent
        return self.class_for_file()


def compute_branch_facts(
    entry: DispatchEntry,
    java: JavaIndex,
    read_source: Callable[[str], str],
    ejbs: list[EjbRecord],
    module_root: str,
    jsp_files: set[str],
) -> BranchFacts:
    """Facts of the code in ``entry.start_line..entry.end_line`` plus followed helpers."""
    return _Analyzer(entry, java, read_source, ejbs, module_root, jsp_files).run()
