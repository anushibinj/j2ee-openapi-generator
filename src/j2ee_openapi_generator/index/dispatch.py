"""Dispatch extraction: which op values a servlet handles and where their code lives.

Supported styles (``DispatchEntry.style``):

* ``if``: ``"x".equals(op)``, ``op.equals("x")``, ``equalsIgnoreCase``, ``OP_X.equals(op)``,
  ``Const.OP_X`` / ``pkg.Const.OP_X``, ``a || b`` conditions, ``op == "x"``, if / else-if chains
  with a final ``else`` reported as ``<default>``.
* ``switch``: string and constant case labels, fallthrough groups (one entry per label, same
  range), ``default:``, arrow rules.
* ``map``: a ``Map<String, X>`` field filled with ``put("x", handler)`` anywhere in the servlet
  or its ancestors (``init()``, static blocks, ...) and read with ``get(op)`` / ``getOrDefault``
  in a method reachable from ``doGet`` / ``doPost`` / ``service``. Handlers may be method
  references, lambdas, anonymous classes or ``new Handler()``.
* ``reflection``: a method that reads the op param and calls ``getMethod`` / ``invoke``; the ops
  are the methods of the servlet (and ancestors up to that base) matching
  ``cfg.dispatch.method_name_patterns``.
* ``default``: servlets (or request methods) with no op dispatch at all, one ``<default>`` entry
  spanning the request method.

The op variable is a local assigned from ``request.getParameter("<name>")`` with the name in
``cfg.dispatch.param_names`` (a literal or a string constant), or a direct ``getParameter`` call
in the comparison. Wrapper calls taking the request first (``Util.getParameter(req, "op")``) and
single character ops (``switch (op.charAt(0)) { case 'n': ... }``) are recognized too.
Dispatch is searched from ``doGet``/``doPost``/``doPut``/``doDelete``/
``service`` (and ``processRequest`` when nothing calls it) through calls to same-class and
superclass methods up to depth 3.

Known gaps: the op passed as a method parameter into a helper that dispatches, dispatch tables
built with ``Map.of`` or loops, ``&&``-negated or ``!equals`` conditions, enhanced switch pattern
matching, reflection through helper methods, and inner classes as servlets.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from tree_sitter import Node, Tree

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.java_symbols import (
    ancestors,
    eval_const_expr,
    find_method,
    lookup_constant,
    node_text,
    parse_tree,
    resolve_type,
    strip_generics,
    unescape_java,
)
from j2ee_openapi_generator.index.models import (
    ClassInfo,
    DispatchEntry,
    JavaIndex,
    MethodInfo,
    ServletRecord,
)

DEFAULT_OP = "<default>"
_HTTP_ORDER = ["GET", "POST", "PUT", "DELETE"]
_ROOTS: list[tuple[str, tuple[str, ...]]] = [
    ("doGet", ("GET",)),
    ("doPost", ("POST",)),
    ("doPut", ("PUT",)),
    ("doDelete", ("DELETE",)),
    ("service", ("GET", "POST")),
]
_TYPE_NODES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "annotation_type_declaration",
    "record_declaration",
}
_WRAPPERS = {"trim", "toLowerCase", "toUpperCase", "intern", "toString", "strip", "charAt"}
_EQUALS = {"equals", "equalsIgnoreCase", "contentEquals"}
_MAP_TYPES = {
    "Map",
    "HashMap",
    "LinkedHashMap",
    "TreeMap",
    "Hashtable",
    "ConcurrentHashMap",
    "ConcurrentMap",
    "SortedMap",
}
_NOT_OPS = {
    "doGet",
    "doPost",
    "doPut",
    "doDelete",
    "doHead",
    "doOptions",
    "doTrace",
    "service",
    "init",
    "destroy",
    "process",
    "processRequest",
    "dispatch",
    "handle",
    "execute",
    "perform",
    "main",
    "equals",
    "hashCode",
    "toString",
}
_MAX_DEPTH = 3


@dataclass
class _Raw:
    op: str
    param: str
    style: str
    owner: ClassInfo
    start: int
    end: int
    constant: str | None = None
    handler: str | None = None
    file: str | None = None  # overrides owner.file (handlers declared elsewhere)


@dataclass
class _FileCtx:
    tree: Tree
    methods: dict[tuple[str, int], Node] = field(default_factory=dict)
    classes: dict[tuple[str, int], Node] = field(default_factory=dict)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _end_line(node: Node) -> int:
    return int(node.end_point[0]) + 1


def _string_value(node: Node) -> str | None:
    if node.type == "character_literal":  # case 'n': / op.charAt(0) == 'n'
        t = node_text(node)
        return unescape_java(t[1:-1]) if len(t) >= 3 else None
    if node.type != "string_literal":
        return None
    t = node_text(node)
    if t.startswith('"""') or len(t) < 2:
        return None
    return unescape_java(t[1:-1])


def _call_name(n: Node) -> str:
    nm = n.child_by_field_name("name")
    return node_text(nm) if nm is not None else ""


def _call_args(n: Node) -> list[Node]:
    a = n.child_by_field_name("arguments")
    if a is None:
        return []
    return [c for c in a.named_children if "comment" not in c.type]


def _inner(node: Node) -> Node:
    while node.type == "parenthesized_expression" and node.named_children:
        node = node.named_children[0]
    return node


class _Extractor:
    """State for the dispatch analysis of one servlet."""

    def __init__(
        self,
        cfg: Config,
        java: JavaIndex,
        read_source: Callable[[str], str],
        files: dict[str, _FileCtx],
        servlet: ClassInfo,
    ) -> None:
        self.cfg = cfg
        self.java = java
        self.read_source = read_source
        self.files = files
        self.servlet = servlet
        self.analysis: dict[tuple[str, str, int], list[_Raw]] = {}
        self.lineage = [servlet, *ancestors(java, servlet.fqcn)]

    # ------------------------------------------------------------------ file access
    def file(self, path: str) -> _FileCtx | None:
        if path in self.files:
            return self.files[path]
        try:
            src = self.read_source(path)
        except (OSError, KeyError, FileNotFoundError):
            return None
        tree = parse_tree(src)
        ctx = _FileCtx(tree=tree)
        for n in _walk(tree.root_node):
            if n.type == "method_declaration":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    ctx.methods[(node_text(nm), _line(n))] = n
            elif n.type in _TYPE_NODES:
                nm = n.child_by_field_name("name")
                if nm is not None:
                    ctx.classes[(node_text(nm), _line(n))] = n
        self.files[path] = ctx
        return ctx

    def method_node(self, owner: ClassInfo, m: MethodInfo) -> Node | None:
        f = self.file(owner.file)
        if f is None:
            return None
        return f.methods.get((m.name, m.start_line))

    def class_node(self, c: ClassInfo) -> Node | None:
        f = self.file(c.file)
        if f is None:
            return None
        return f.classes.get((c.simple_name, c.start_line))

    # ------------------------------------------------------------------ op detection
    def _getparam_name(self, call: Node, cls: ClassInfo) -> str | None:
        if call.type != "method_invocation" or _call_name(call) != "getParameter":
            return None
        # ``req.getParameter("op")`` and wrapper forms such as
        # ``UtilRequest.getParameter(req, "op")``: any argument naming a dispatch param counts.
        for arg in _call_args(call):
            val, _ = self._value(arg, cls)
            if val is not None and val in self.cfg.dispatch.param_names:
                return val
        return None

    def _value(self, node: Node, cls: ClassInfo) -> tuple[str | None, str | None]:
        """(string value, constant name as written) of a literal / constant expression."""
        node = _inner(node)
        s = _string_value(node)
        if s is not None:
            return s, None
        if node.type in ("identifier", "field_access", "scoped_identifier"):
            ref = re.sub(r"\s+", "", node_text(node))
            return lookup_constant(self.java, cls, ref), ref
        if node.type == "binary_expression":
            v = _eval(self.java, cls, node_text(node))
            return v, None
        return None, None

    def op_vars(self, mnode: Node, cls: ClassInfo) -> dict[str, str]:
        out: dict[str, str] = {}
        for n in _walk(mnode):
            target: Node | None = None
            value: Node | None = None
            if n.type == "variable_declarator":
                target = n.child_by_field_name("name")
                value = n.child_by_field_name("value")
            elif n.type == "assignment_expression":
                target = n.child_by_field_name("left")
                value = n.child_by_field_name("right")
            if target is None or value is None or target.type != "identifier":
                continue
            for sub in _walk(value):
                p = self._getparam_name(sub, cls)
                if p is not None:
                    out.setdefault(node_text(target), p)
                    break
        return out

    def op_param(self, node: Node, opvars: dict[str, str], cls: ClassInfo) -> str | None:
        node = _inner(node)
        if node.type == "identifier":
            return opvars.get(node_text(node))
        if node.type == "method_invocation":
            direct = self._getparam_name(node, cls)
            if direct is not None:
                return direct
            obj = node.child_by_field_name("object")
            if obj is not None and _call_name(node) in _WRAPPERS:
                return self.op_param(obj, opvars, cls)
        return None

    # ------------------------------------------------------------------ if chains
    def comparisons(
        self, cond: Node, opvars: dict[str, str], cls: ClassInfo
    ) -> list[tuple[str, str, str | None]]:
        cond = _inner(cond)
        if cond.type == "binary_expression":
            opn = cond.child_by_field_name("operator")
            operator = node_text(opn) if opn is not None else ""
            left = cond.child_by_field_name("left")
            right = cond.child_by_field_name("right")
            if left is None or right is None:
                return []
            if operator in ("||", "&&"):
                return self.comparisons(left, opvars, cls) + self.comparisons(right, opvars, cls)
            if operator == "==":
                return self._pair(left, right, opvars, cls)
            return []
        if cond.type == "method_invocation" and _call_name(cond) in _EQUALS:
            obj = cond.child_by_field_name("object")
            args = _call_args(cond)
            if obj is not None and len(args) == 1:
                return self._pair(obj, args[0], opvars, cls)
            if obj is not None and node_text(obj) == "Objects" and len(args) == 2:
                return self._pair(args[0], args[1], opvars, cls)
        return []

    def _pair(
        self, a: Node, b: Node, opvars: dict[str, str], cls: ClassInfo
    ) -> list[tuple[str, str, str | None]]:
        for op_side, val_side in ((a, b), (b, a)):
            param = self.op_param(op_side, opvars, cls)
            if param is None:
                continue
            val, const = self._value(val_side, cls)
            if val is not None:
                return [(val, param, const)]
        return []

    def _if_ranges(self, node: Node) -> tuple[int, int]:
        cons = node.child_by_field_name("consequence")
        start = _line(node)
        if cons is None:
            return start, _end_line(node)
        end = _end_line(cons)
        alt = node.child_by_field_name("alternative")
        if cons.type == "block" and alt is not None:
            else_line = next((_line(c) for c in node.children if c.type == "else"), _line(alt))
            if else_line == end:
                stmts = [c for c in cons.named_children]
                end = _end_line(stmts[-1]) if stmts else _line(cons)
        return start, end

    def scan_ifs(self, mnode: Node, opvars: dict[str, str], owner: ClassInfo) -> list[_Raw]:
        out: list[_Raw] = []
        consumed: set[int] = set()

        def visit(n: Node) -> None:
            if n.type == "if_statement" and n.id not in consumed:
                chain: list[Node] = []
                cur: Node | None = n
                final_else: Node | None = None
                while cur is not None and cur.type == "if_statement":
                    chain.append(cur)
                    consumed.add(cur.id)
                    alt = cur.child_by_field_name("alternative")
                    if alt is not None and alt.type != "if_statement":
                        final_else = alt
                    cur = alt if alt is not None and alt.type == "if_statement" else None
                matched = False
                last_param = ""
                for ifn in chain:
                    cond = ifn.child_by_field_name("condition")
                    comps = self.comparisons(cond, opvars, owner) if cond is not None else []
                    if comps:
                        matched = True
                        s, e = self._if_ranges(ifn)
                        for val, param, const in comps:
                            last_param = param
                            out.append(_Raw(val, param, "if", owner, s, e, const))
                    else:
                        cons = ifn.child_by_field_name("consequence")
                        if cons is not None:
                            visit(cons)
                if matched and final_else is not None:
                    else_line = next(
                        (
                            _line(c)
                            for c in chain[-1].children
                            if c.type == "else" and _line(c) <= _line(final_else)
                        ),
                        _line(final_else),
                    )
                    out.append(
                        _Raw(DEFAULT_OP, last_param, "if", owner, else_line, _end_line(final_else))
                    )
                elif final_else is not None:
                    visit(final_else)
                return
            for ch in n.children:
                visit(ch)

        visit(mnode)
        return out

    # ------------------------------------------------------------------ switch
    def scan_switches(self, mnode: Node, opvars: dict[str, str], owner: ClassInfo) -> list[_Raw]:
        out: list[_Raw] = []
        for n in _walk(mnode):
            if n.type not in ("switch_expression", "switch_statement"):
                continue
            cond = n.child_by_field_name("condition")
            body = n.child_by_field_name("body")
            if cond is None or body is None:
                continue
            param = self.op_param(cond, opvars, owner)
            if param is None:
                continue
            pending: list[tuple[str, str | None]] = []
            pending_start = 0
            for grp in body.named_children:
                if grp.type not in ("switch_block_statement_group", "switch_rule"):
                    continue
                labels = [c for c in grp.children if c.type == "switch_label"]
                has_body = any(
                    c.is_named and c.type != "switch_label" and "comment" not in c.type
                    for c in grp.children
                )
                if not pending:
                    pending_start = _line(grp)
                for lab in labels:
                    if node_text(lab).lstrip().startswith("default"):
                        pending.append((DEFAULT_OP, None))
                        continue
                    for v in lab.named_children:
                        val, const = self._value(v, owner)
                        if val is not None:
                            pending.append((val, const))
                if has_body:
                    for val, const in pending:
                        out.append(
                            _Raw(val, param, "switch", owner, pending_start, _end_line(grp), const)
                        )
                    pending = []
            for val, const in pending:
                out.append(_Raw(val, param, "switch", owner, pending_start, _end_line(body), const))
        return out

    # ------------------------------------------------------------------ map style
    def _map_names(self) -> set[str]:
        names: set[str] = set()
        for c in self.lineage:
            for f in c.fields:
                if strip_generics(f.type).rsplit(".", 1)[-1] in _MAP_TYPES:
                    names.add(f.name)
        return names

    def scan_map(self, mnode: Node, opvars: dict[str, str], owner: ClassInfo) -> list[_Raw]:
        names = self._map_names()
        if not names:
            return []
        used: str | None = None
        param = ""
        default_node: Node | None = None
        for n in _walk(mnode):
            if n.type != "method_invocation" or _call_name(n) not in ("get", "getOrDefault"):
                continue
            obj = n.child_by_field_name("object")
            args = _call_args(n)
            if obj is None or not args:
                continue
            oname = node_text(obj).removeprefix("this.")
            if oname not in names:
                continue
            p = self.op_param(args[0], opvars, owner)
            if p is None:
                continue
            used, param = oname, p
            if _call_name(n) == "getOrDefault" and len(args) == 2:
                default_node = args[1]
            break
        if used is None:
            return []
        out: list[_Raw] = []
        for c in self.lineage:
            cnode = self.class_node(c)
            if cnode is None:
                continue
            for n in _walk(cnode):
                if n.type != "method_invocation" or _call_name(n) != "put":
                    continue
                obj = n.child_by_field_name("object")
                args = _call_args(n)
                if obj is None or len(args) != 2 or node_text(obj).removeprefix("this.") != used:
                    continue
                val, const = self._value(args[0], c)
                if val is None:
                    continue
                s, e, f, h = self.resolve_handler(args[1], c)
                out.append(_Raw(val, param, "map", c, s, e, const, h, f))
        if default_node is not None:
            s, e, f, h = self.resolve_handler(default_node, owner)
            out.append(_Raw(DEFAULT_OP, param, "map", owner, s, e, None, h, f))
        return out

    def _pick_handler_method(self, c: ClassInfo) -> MethodInfo | None:
        reqresp = [m for m in c.methods if _looks_like_handler(m)]
        if reqresp:
            return reqresp[0]
        for want in ("handle", "execute", "process", "run", "apply"):
            for m in c.methods:
                if m.name == want:
                    return m
        non_private = [m for m in c.methods if "private" not in m.modifiers]
        return non_private[0] if non_private else (c.methods[0] if c.methods else None)

    def resolve_handler(
        self, v: Node, ctx_cls: ClassInfo
    ) -> tuple[int, int, str | None, str | None]:
        """(start, end, file or None, handler method name) for a handler expression."""
        v = _inner(v)
        fallback = (_line(v), _end_line(v), None, None)
        if v.type == "method_reference":
            names = [c for c in v.children if c.type == "identifier"]
            if not names:
                return fallback
            mname = node_text(names[-1])
            target = self._lookup_in(v.children[0], mname, ctx_cls)
            if target is None:
                return fallback
            return target[1].start_line, target[1].end_line, target[0].file, mname
        if v.type == "lambda_expression":
            body = v.child_by_field_name("body")
            if body is None:
                return fallback
            if body.type == "block":
                return _line(body), _end_line(body), None, None
            call = _inner(body)
            if call.type == "method_invocation":
                obj = call.child_by_field_name("object")
                if obj is None or node_text(obj) == "this":
                    target = self._lookup_in(None, _call_name(call), ctx_cls)
                    if target is not None:
                        m = target[1]
                        return m.start_line, m.end_line, target[0].file, m.name
            return _line(body), _end_line(body), None, None
        if v.type == "object_creation_expression":
            cb = next((c for c in v.children if c.type == "class_body"), None)
            if cb is not None:
                meths = [c for c in cb.children if c.type == "method_declaration"]
                if not meths:
                    return fallback
                best = next((m for m in meths if len(_params_of(m)) == 2), meths[0])
                nm = best.child_by_field_name("name")
                return _line(best), _end_line(best), None, node_text(nm) if nm else None
            tnode = v.child_by_field_name("type")
            if tnode is not None:
                fq = resolve_type(self.java, ctx_cls, node_text(tnode))
                hc = self.java.classes.get(fq) if fq else None
                if hc is not None:
                    hm = self._pick_handler_method(hc)
                    if hm is not None:
                        return hm.start_line, hm.end_line, hc.file, hm.name
            return fallback
        return fallback

    def _lookup_in(
        self, qualifier: Node | None, name: str, ctx_cls: ClassInfo
    ) -> tuple[ClassInfo, MethodInfo] | None:
        if qualifier is not None and node_text(qualifier) not in ("this", "super"):
            fq = resolve_type(self.java, ctx_cls, node_text(qualifier))
            if fq is not None and fq in self.java.classes:
                return find_method(self.java, fq, name)
        return find_method(self.java, self.servlet.fqcn, name)

    # ------------------------------------------------------------------ reflection
    def scan_reflection(self, mnode: Node, opvars: dict[str, str], owner: ClassInfo) -> list[_Raw]:
        reads_op = bool(opvars)
        public_only = False
        invokes = False
        param = next(iter(opvars.values()), "")
        for n in _walk(mnode):
            if n.type != "method_invocation":
                continue
            nm = _call_name(n)
            if nm in ("getMethod", "getDeclaredMethod", "invoke"):
                invokes = True
            if nm == "getMethod":
                public_only = True
            p = self._getparam_name(n, owner)
            if p is not None and not reads_op:
                reads_op = True
                param = p
        if not (reads_op and invokes):
            return []
        mm_name = _method_name_of(mnode)
        out: list[_Raw] = []
        seen_methods: set[str] = set()
        chain: list[ClassInfo] = []
        for c in self.lineage:
            chain.append(c)
            if c.fqcn == owner.fqcn:
                break
        patterns = [_pattern_regex(p) for p in self.cfg.dispatch.method_name_patterns]
        for c in chain:
            for m in c.methods:
                if m.name in seen_methods:
                    continue
                seen_methods.add(m.name)
                if m.name in _NOT_OPS or m.name == mm_name:
                    continue
                if "private" in m.modifiers or "static" in m.modifiers:
                    continue
                if public_only and "public" not in m.modifiers:
                    continue
                if not _looks_like_handler(m):
                    continue
                op = _match_patterns(patterns, m.name)
                if op is None:
                    continue
                out.append(
                    _Raw(op, param, "reflection", c, m.start_line, m.end_line, None, m.name, c.file)
                )
        return out

    # ------------------------------------------------------------------ analysis
    def analyze(self, owner: ClassInfo, m: MethodInfo) -> list[_Raw]:
        key = (owner.fqcn, m.name, m.start_line)
        if key in self.analysis:
            return self.analysis[key]
        self.analysis[key] = []
        mnode = self.method_node(owner, m)
        raws: list[_Raw] = []
        if mnode is not None:
            opvars = self.op_vars(mnode, owner)
            raws = self.scan_ifs(mnode, opvars, owner) + self.scan_switches(mnode, opvars, owner)
            if not raws:
                raws = self.scan_map(mnode, opvars, owner)
            if not raws:
                raws = self.scan_reflection(mnode, opvars, owner)
        self.analysis[key] = raws
        return raws

    def calls(self, owner: ClassInfo, m: MethodInfo) -> list[tuple[ClassInfo, MethodInfo]]:
        mnode = self.method_node(owner, m)
        if mnode is None:
            return []
        out: list[tuple[ClassInfo, MethodInfo]] = []
        for n in _walk(mnode):
            if n.type != "method_invocation":
                continue
            obj = n.child_by_field_name("object")
            otext = node_text(obj) if obj is not None else ""
            if otext not in ("", "this", "super"):
                continue
            name = _call_name(n)
            argc = len(_call_args(n))
            pool = self.lineage
            if otext == "super":
                idx = next((i for i, c in enumerate(self.lineage) if c.fqcn == owner.fqcn), -1)
                pool = self.lineage[idx + 1 :]
            found: tuple[ClassInfo, MethodInfo] | None = None
            for c in pool:
                for cand in c.methods:
                    if cand.name == name and len(cand.params) == argc:
                        found = (c, cand)
                        break
                if found:
                    break
            if found is None:
                for c in pool:
                    hit = next((x for x in c.methods if x.name == name), None)
                    if hit is not None:
                        found = (c, hit)
                        break
            if found is not None and found not in out:
                out.append(found)
        return out

    def find_root(self, name: str) -> tuple[ClassInfo, MethodInfo] | None:
        for c in self.lineage:
            for m in c.methods:
                if m.name == name:
                    return c, m
        return None

    def run(self, module: str) -> list[DispatchEntry]:
        dispatch_http: dict[tuple[str, str, int], set[str]] = {}
        dispatch_order: list[tuple[ClassInfo, MethodInfo]] = []
        reached: set[tuple[str, str, int]] = set()
        roots: list[tuple[ClassInfo, MethodInfo, tuple[str, ...]]] = []
        for name, http in _ROOTS:
            root_hit = self.find_root(name)
            if root_hit is not None:
                roots.append((root_hit[0], root_hit[1], http))

        def explore(owner: ClassInfo, m: MethodInfo, http: tuple[str, ...]) -> bool:
            found_any = False
            seen: set[tuple[str, str, int]] = set()
            frontier = [(owner, m)]
            for _depth in range(_MAX_DEPTH + 1):
                nxt: list[tuple[ClassInfo, MethodInfo]] = []
                for o, mm in frontier:
                    k = (o.fqcn, mm.name, mm.start_line)
                    if k in seen:
                        continue
                    seen.add(k)
                    reached.add(k)
                    if self.analyze(o, mm):
                        if k not in dispatch_http:
                            dispatch_order.append((o, mm))
                        dispatch_http.setdefault(k, set()).update(http)
                        found_any = True
                    else:
                        nxt.extend(self.calls(o, mm))
                frontier = nxt
            return found_any

        no_dispatch: list[tuple[ClassInfo, MethodInfo, tuple[str, ...]]] = []
        for owner, m, http in roots:
            if not explore(owner, m, http):
                no_dispatch.append((owner, m, http))
        pr = self.find_root("processRequest")
        if pr is not None and (pr[0].fqcn, pr[1].name, pr[1].start_line) not in reached:
            if not explore(pr[0], pr[1], ("GET", "POST")):
                no_dispatch.append((pr[0], pr[1], ("GET", "POST")))

        entries: dict[tuple[str, tuple[str, ...]], DispatchEntry] = {}

        def add(e: DispatchEntry) -> None:
            key = (e.op, tuple(e.http_methods))
            entries.setdefault(key, e)

        for owner, m in dispatch_order:
            http_set_d = dispatch_http[(owner.fqcn, m.name, m.start_line)]
            http_list = [h for h in _HTTP_ORDER if h in http_set_d]
            for r in self.analyze(owner, m):
                add(
                    DispatchEntry(
                        module=module,
                        servlet_fqcn=self.servlet.fqcn,
                        op=r.op,
                        param_name=r.param,
                        style=r.style,
                        file=r.file or r.owner.file,
                        start_line=r.start,
                        end_line=r.end,
                        http_methods=http_list,
                        constant=r.constant,
                        handler_method=r.handler,
                    )
                )
        if not any(op == DEFAULT_OP for op, _methods in entries) and no_dispatch:
            # entries without any op dispatch: one entry spanning the largest request method
            best = max(no_dispatch, key=lambda t: t[1].end_line - t[1].start_line)
            http_set = {h for _, _, hs in no_dispatch for h in hs}
            add(
                DispatchEntry(
                    module=module,
                    servlet_fqcn=self.servlet.fqcn,
                    op=DEFAULT_OP,
                    param_name="",
                    style="default",
                    file=best[0].file,
                    start_line=best[1].start_line,
                    end_line=best[1].end_line,
                    http_methods=[h for h in _HTTP_ORDER if h in http_set],
                )
            )
        return [entries[k] for k in sorted(entries)]


# ---------------------------------------------------------------------- helpers
def _eval(java: JavaIndex, cls: ClassInfo, text: str) -> str | None:
    return eval_const_expr(text, lambda n: lookup_constant(java, cls, n))


def _params_of(method_node: Node) -> list[Node]:
    p = method_node.child_by_field_name("parameters")
    return [c for c in p.named_children] if p is not None else []


def _method_name_of(mnode: Node) -> str:
    nm = mnode.child_by_field_name("name")
    return node_text(nm) if nm is not None else ""


def _looks_like_handler(m: MethodInfo) -> bool:
    if len(m.params) != 2:
        return False
    t0 = strip_generics(m.params[0][0]).rsplit(".", 1)[-1]
    t1 = strip_generics(m.params[1][0]).rsplit(".", 1)[-1]
    return t0.endswith("Request") and t1.endswith("Response")


def _pattern_regex(pattern: str) -> tuple[re.Pattern[str], bool]:
    """Regex for a name pattern; second item True when ``{Op}`` (capitalized) is used."""
    cap = "{Op}" in pattern
    esc = re.escape(pattern).replace(r"\{Op\}", r"(?P<op>[A-Z][A-Za-z0-9_]*)")
    esc = esc.replace(r"\{op\}", r"(?P<op>[A-Za-z_][A-Za-z0-9_]*)")
    return re.compile(esc), cap


def _match_patterns(patterns: list[tuple[re.Pattern[str], bool]], name: str) -> str | None:
    for rx, cap in patterns:
        m = rx.fullmatch(name)
        if m is None or "op" not in m.groupdict():
            continue
        op = m.group("op")
        return op[0].lower() + op[1:] if cap else op
    return None


def extract_dispatch(
    cfg: Config,
    java: JavaIndex,
    servlets: list[ServletRecord],
    read_source: Callable[[str], str],
) -> list[DispatchEntry]:
    """Dispatch entries of every registered servlet, sorted by servlet, op and method."""
    files: dict[str, _FileCtx] = {}
    result: dict[tuple[str, str, str, tuple[str, ...]], DispatchEntry] = {}
    for rec in servlets:
        cls = java.classes.get(rec.class_fqcn)
        if cls is None:
            continue
        ex = _Extractor(cfg, java, read_source, files, cls)
        for e in ex.run(rec.module):
            key = (
                e.module,
                e.servlet_fqcn,
                e.op,
                tuple(e.http_methods),
            )
            result.setdefault(key, e)
    return sorted(
        result.values(),
        key=lambda e: (
            e.module,
            e.servlet_simple_name,
            e.op,
            tuple(e.http_methods),
            e.file,
            e.start_line,
            e.end_line,
        ),
    )
