"""Java symbol index built with tree-sitter.

``parse_java_file`` turns one source file into ``ClassInfo`` records (nested classes become
separate records named ``pkg.Outer.Inner``). ``build_java_index`` parses all inventoried java
files (optionally in a process pool) and then runs a resolution pass that fills
``superclass``, ``interfaces``, ``FieldInfo.resolved_type`` and string constants that refer to
other classes.

Resolution order for a simple type name: nested classes of the class and its enclosing classes,
explicit imports (returned even when the class is not in the index), same package (index only),
wildcard imports (index only), a small ``java.lang`` set. Dotted names are resolved by their
first segment, or taken as written when they look like a fully qualified name.

What it deliberately misses: no type inference (``var``, expression types), no generics bounds
or type arguments (only the raw type is resolved), no static imports of members beyond string
constants, no overload resolution (``find_method`` returns the first method with that name),
no lambdas or anonymous classes as symbols, no member classes inherited from superclasses, no
constants computed by anything other than string/number literals joined with ``+``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser, Tree

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.models import (
    ClassInfo,
    FieldInfo,
    FileRecord,
    JavaIndex,
    MethodInfo,
)

_LANGUAGE: Language | None = None
_PARSER: Parser | None = None

_TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "annotation_type_declaration": "annotation",
    "record_declaration": "class",
}
_JAVA_LANG = frozenset(
    "Object String Integer Long Short Byte Double Float Boolean Character Number Math "
    "StringBuilder StringBuffer Exception RuntimeException Throwable Error Class Enum "
    "Iterable Comparable Runnable Thread System Void CharSequence Override Deprecated "
    "SuppressWarnings FunctionalInterface IllegalArgumentException IllegalStateException "
    "NumberFormatException NullPointerException".split()
)
_WS = re.compile(r"\s+")


def get_parser() -> Parser:
    """Process-local cached tree-sitter parser for Java."""
    global _LANGUAGE, _PARSER
    if _PARSER is None:
        _LANGUAGE = Language(tree_sitter_java.language())
        _PARSER = Parser(_LANGUAGE)
    return _PARSER


def parse_tree(source: str) -> Tree:
    """Parse Java source. The tree holds byte offsets of the UTF-8 encoding of ``source``."""
    return get_parser().parse(source.encode("utf-8"))


def node_text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text is not None else ""


def collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def strip_generics(type_text: str) -> str:
    """``Map<String, List<X>>[]`` -> ``Map``; ``a.B<C>.D`` keeps dotted prefix before ``<``."""
    out: list[str] = []
    depth = 0
    for ch in type_text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    res = "".join(out).replace("[]", "").replace("...", "")
    return collapse(res)


# --------------------------------------------------------------------------- constants
_TOKEN = re.compile(
    r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>\d+[lL]?)|(?P<id>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)|(?P<op>[+()]))'
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "'": "'", "\\": "\\"}


def unescape_java(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if (
                nxt == "u"
                and i + 5 < len(body) + 0
                and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2 : i + 6])
            ):
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def eval_const_expr(text: str, lookup: Callable[[str], str | None]) -> str | None:
    """Evaluate ``"a" + OTHER + "b"`` style expressions. ``None`` if anything is unresolvable."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            return None
        kind = m.lastgroup or ""
        tokens.append((kind, m.group(kind)))
        pos = m.end()
        if text[pos:].strip() == "":
            break
    idx = 0

    def operand() -> str | None:
        nonlocal idx
        if idx >= len(tokens):
            return None
        kind, val = tokens[idx]
        idx += 1
        if kind == "str":
            return unescape_java(val[1:-1])
        if kind == "num":
            return val.rstrip("lL")
        if kind == "id":
            return lookup(re.sub(r"\s+", "", val))
        if kind == "op" and val == "(":
            inner = expr()
            if idx < len(tokens) and tokens[idx] == ("op", ")"):
                idx += 1
                return inner
            return None
        return None

    def expr() -> str | None:
        nonlocal idx
        left = operand()
        while left is not None and idx < len(tokens) and tokens[idx] == ("op", "+"):
            idx += 1
            right = operand()
            if right is None:
                return None
            left += right
        return left

    result = expr()
    if idx != len(tokens):
        return None
    return result


# --------------------------------------------------------------------------- parsing
def _modifiers_and_annotations(node: Node | None) -> tuple[list[str], list[str]]:
    mods: list[str] = []
    anns: list[str] = []
    if node is None:
        return mods, anns
    for ch in node.children:
        if ch.type in ("annotation", "marker_annotation"):
            anns.append(collapse(node_text(ch)).lstrip("@"))
        else:
            mods.append(node_text(ch))
    return mods, anns


def _child(node: Node, type_name: str) -> Node | None:
    for ch in node.children:
        if ch.type == type_name:
            return ch
    return None


def _type_names(node: Node) -> list[str]:
    """Collect type names from a ``type_list`` / ``extends_interfaces`` style node."""
    out: list[str] = []
    for ch in node.children:
        if ch.type == "type_list":
            out.extend(_type_names(ch))
        elif ch.is_named and ch.type not in ("comment", "line_comment", "block_comment"):
            out.append(strip_generics(node_text(ch)))
    return out


def _parse_method(node: Node) -> MethodInfo | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    mods_node = _child(node, "modifiers")
    mods, anns = _modifiers_and_annotations(mods_node)
    type_node = node.child_by_field_name("type")
    params: list[tuple[str, str]] = []
    params_node = node.child_by_field_name("parameters")
    if params_node is not None:
        for p in params_node.children:
            if p.type == "formal_parameter":
                t = p.child_by_field_name("type")
                n = p.child_by_field_name("name")
                if t is not None and n is not None:
                    params.append((collapse(node_text(t)), node_text(n)))
            elif p.type == "spread_parameter":
                tn = [c for c in p.children if c.is_named and c.type != "modifiers"]
                if len(tn) >= 2:
                    nm = tn[-1]
                    nm_text = node_text(nm.child_by_field_name("name") or nm)
                    params.append((collapse(node_text(tn[0])) + "...", nm_text))
    throws: list[str] = []
    for ch in node.children:
        if ch.type == "throws":
            throws.extend(_type_names(ch))
    return MethodInfo(
        name=node_text(name_node),
        return_type=collapse(node_text(type_node)) if type_node is not None else "",
        params=params,
        annotations=anns,
        modifiers=mods,
        throws=throws,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _parse_fields(node: Node, is_interface: bool) -> list[FieldInfo]:
    mods_node = _child(node, "modifiers")
    mods, anns = _modifiers_and_annotations(mods_node)
    if is_interface:
        for m in ("public", "static", "final"):
            if m not in mods:
                mods.append(m)
    type_node = node.child_by_field_name("type")
    type_text = collapse(node_text(type_node)) if type_node is not None else ""
    out: list[FieldInfo] = []
    for ch in node.children:
        if ch.type != "variable_declarator":
            continue
        name_node = ch.child_by_field_name("name")
        if name_node is None:
            continue
        dims = ch.child_by_field_name("dimensions")
        value = ch.child_by_field_name("value")
        out.append(
            FieldInfo(
                name=node_text(name_node),
                type=type_text + (collapse(node_text(dims)) if dims is not None else ""),
                annotations=list(anns),
                line=name_node.start_point[0] + 1,
                modifiers=list(mods),
                initializer=collapse(node_text(value)) if value is not None else None,
            )
        )
    return out


def _local_constants(fields: list[FieldInfo]) -> dict[str, str]:
    """Static final String constants evaluated within one class (fixed point)."""
    cands = {
        f.name: f.initializer
        for f in fields
        if f.initializer is not None
        and "static" in f.modifiers
        and "final" in f.modifiers
        and f.type == "String"
    }
    consts: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, init in cands.items():
            if name in consts:
                continue
            val = eval_const_expr(init, consts.get)
            if val is not None:
                consts[name] = val
                changed = True
    return dict(sorted(consts.items()))


def _parse_type_decl(
    node: Node,
    *,
    rel_path: str,
    package: str,
    imports: list[str],
    outer: str | None,
    out: list[ClassInfo],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    simple = node_text(name_node)
    fqcn = (f"{outer}." if outer else (f"{package}." if package else "")) + simple
    kind = _TYPE_DECLS[node.type]
    mods, anns = _modifiers_and_annotations(_child(node, "modifiers"))
    del mods
    superclass: str | None = None
    interfaces: list[str] = []
    sc = node.child_by_field_name("superclass")
    if sc is not None:
        names = _type_names(sc)
        superclass = names[0] if names else None
    for ch in node.children:
        if ch.type in ("super_interfaces", "extends_interfaces"):
            interfaces.extend(_type_names(ch))
        elif ch.type == "interfaces":
            interfaces.extend(_type_names(ch))
    info = ClassInfo(
        fqcn=fqcn,
        simple_name=simple,
        package=package,
        file=rel_path,
        kind=kind,
        imports=list(imports),
        superclass=superclass,
        interfaces=interfaces,
        annotations=anns,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )
    out.append(info)
    body = node.child_by_field_name("body")
    if body is None:
        return
    members: list[Node] = list(body.children)
    for ch in list(members):
        if ch.type == "enum_body_declarations":
            members.extend(ch.children)
    for m in members:
        if m.type == "enum_constant":
            n = m.child_by_field_name("name")
            if n is not None:
                info.enum_constants.append(node_text(n))
        elif m.type == "field_declaration" or m.type == "constant_declaration":
            info.fields.extend(_parse_fields(m, kind == "interface"))
        elif m.type in ("method_declaration", "annotation_type_element_declaration"):
            mi = _parse_method(m)
            if mi is not None:
                info.methods.append(mi)
        elif m.type in _TYPE_DECLS:
            _parse_type_decl(
                m, rel_path=rel_path, package=package, imports=imports, outer=fqcn, out=out
            )
    info.constants = _local_constants(info.fields)


def parse_java_file(rel_path: str, source: str) -> list[ClassInfo]:
    """Parse one Java source file into class symbols (outer first, nested after, in order)."""
    tree = parse_tree(source)
    root = tree.root_node
    package = ""
    imports: list[str] = []
    decls: list[Node] = []
    for ch in root.children:
        if ch.type == "package_declaration":
            for sub in ch.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    package = node_text(sub)
        elif ch.type == "import_declaration":
            text = collapse(node_text(ch)).removeprefix("import ").rstrip(";").strip()
            text = re.sub(r"\s*\.\s*", ".", text)
            if text.startswith("static "):
                text = "static " + text[7:].strip()
            imports.append(text)
        elif ch.type in _TYPE_DECLS:
            decls.append(ch)
    out: list[ClassInfo] = []
    for d in decls:
        _parse_type_decl(
            d, rel_path=rel_path, package=package, imports=imports, outer=None, out=out
        )
    return out


def _parse_path(args: tuple[str, str]) -> list[ClassInfo]:
    rel_path, abs_path = args
    try:
        source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_java_file(rel_path, source)


def build_java_index(cfg: Config, inventory: list[FileRecord], workers: int = 1) -> JavaIndex:
    """Parse every ``kind == 'java'`` file and resolve names. Deterministic for any ``workers``."""
    root = cfg.repo_path
    jobs = sorted((r.path, str(root / r.path)) for r in inventory if r.kind == "java")
    results: list[list[ClassInfo]]
    if workers <= 1 or len(jobs) < 2:
        results = [_parse_path(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(
                pool.map(_parse_path, jobs, chunksize=max(1, len(jobs) // (workers * 4)))
            )
    classes: dict[str, ClassInfo] = {}
    for infos in results:
        for ci in infos:
            classes.setdefault(ci.fqcn, ci)
    index = JavaIndex(classes=dict(sorted(classes.items())))
    resolve_index(index)
    return index


# --------------------------------------------------------------------------- resolution
def _enclosing_fqcns(fqcn: str, index: JavaIndex) -> Iterator[str]:
    """``p.A.B`` yields ``p.A.B``, ``p.A`` (those present in the index)."""
    cur = fqcn
    while cur:
        if cur in index.classes:
            yield cur
        if "." not in cur:
            break
        cur = cur.rsplit(".", 1)[0]


def resolve_type(index: JavaIndex, cls: ClassInfo, type_name: str) -> str | None:
    """Resolve a type as written inside ``cls`` to an FQCN (raw type; generics/arrays dropped)."""
    name = strip_generics(type_name).strip()
    if not name:
        return None
    if "." in name:
        head, tail = name.split(".", 1)
        head_fq = _resolve_simple(index, cls, head)
        if head_fq is not None:
            cand = f"{head_fq}.{tail}"
            return cand
        if name in index.classes:
            return name
        if head[:1].islower():
            return name
        return None
    return _resolve_simple(index, cls, name)


def _resolve_simple(index: JavaIndex, cls: ClassInfo, name: str) -> str | None:
    for enc in _enclosing_fqcns(cls.fqcn, index):
        cand = f"{enc}.{name}"
        if cand in index.classes:
            return cand
        if enc.rsplit(".", 1)[-1] == name:
            return enc
    for imp in cls.imports:
        if imp.startswith("static ") or imp.endswith(".*"):
            continue
        if imp.rsplit(".", 1)[-1] == name:
            return imp
    if cls.package:
        cand = f"{cls.package}.{name}"
    else:
        cand = name
    if cand in index.classes:
        return cand
    for imp in cls.imports:
        if imp.endswith(".*") and not imp.startswith("static "):
            cand = f"{imp[:-2]}.{name}"
            if cand in index.classes:
                return cand
    if name in _JAVA_LANG:
        return f"java.lang.{name}"
    return None


def resolve_index(index: JavaIndex) -> None:
    """Fill superclass / interfaces / field resolved types / cross-class constants in place."""
    for cls in index.classes.values():
        if cls.superclass:
            cls.superclass = resolve_type(index, cls, cls.superclass) or cls.superclass
        cls.interfaces = [resolve_type(index, cls, i) or i for i in cls.interfaces]
        for f in cls.fields:
            f.resolved_type = resolve_type(index, cls, f.type)
    for cls in index.classes.values():
        for f in cls.fields:
            if (
                f.name not in cls.constants
                and f.initializer is not None
                and "static" in f.modifiers
                and "final" in f.modifiers
                and f.type == "String"
            ):
                val = eval_const_expr(
                    f.initializer,
                    lambda n, c=cls: lookup_constant(index, c, n),  # type: ignore[misc]
                )
                if val is not None:
                    cls.constants[f.name] = val
        cls.constants = dict(sorted(cls.constants.items()))


def ancestors(index: JavaIndex, fqcn: str) -> list[ClassInfo]:
    """Superclasses and interfaces present in the index, nearest first, cycle safe."""
    start = index.classes.get(fqcn)
    if start is None:
        return []
    seen = {fqcn}
    out: list[ClassInfo] = []
    queue: list[ClassInfo] = [start]
    while queue:
        cur = queue.pop(0)
        parents = ([cur.superclass] if cur.superclass else []) + list(cur.interfaces)
        for p in parents:
            if p in seen:
                continue
            seen.add(p)
            pc = index.classes.get(p)
            if pc is not None:
                out.append(pc)
                queue.append(pc)
    return out


def find_method(index: JavaIndex, fqcn: str, name: str) -> tuple[ClassInfo, MethodInfo] | None:
    """First method called ``name`` in the class, then its ancestors (nearest first)."""
    cls = index.classes.get(fqcn)
    if cls is None:
        return None
    for c in [cls, *ancestors(index, fqcn)]:
        for m in c.methods:
            if m.name == name:
                return c, m
    return None


def lookup_constant(index: JavaIndex, cls: ClassInfo, name_or_qualified: str) -> str | None:
    """Resolve ``OP``, ``Const.OP`` or ``pkg.Const.OP`` to a string constant value."""
    return _lookup_constant(index, cls, name_or_qualified, frozenset())


def _constant_in(
    index: JavaIndex, holder: ClassInfo, name: str, seen: frozenset[str]
) -> str | None:
    key = f"{holder.fqcn}#{name}"
    if key in seen:
        return None
    for c in [holder, *ancestors(index, holder.fqcn)]:
        if name in c.constants:
            return c.constants[name]
    seen = seen | {key}
    for c in [holder, *ancestors(index, holder.fqcn)]:
        for f in c.fields:
            if (
                f.name == name
                and f.initializer
                and "static" in f.modifiers
                and "final" in f.modifiers
            ):
                return eval_const_expr(
                    f.initializer,
                    lambda n, cc=c: _lookup_constant(index, cc, n, seen),  # type: ignore[misc]
                )
    return None


def _lookup_constant(
    index: JavaIndex, cls: ClassInfo, ref: str, seen: frozenset[str]
) -> str | None:
    ref = ref.strip()
    if "." in ref:
        qual, name = ref.rsplit(".", 1)
        fq = resolve_type(index, cls, qual)
        holder = index.classes.get(fq) if fq else None
        if holder is None:
            return None
        return _constant_in(index, holder, name, seen)
    for enc in _enclosing_fqcns(cls.fqcn, index):
        v = _constant_in(index, index.classes[enc], ref, seen)
        if v is not None:
            return v
    for imp in cls.imports:
        if not imp.startswith("static "):
            continue
        target = imp[7:]
        if target.endswith(".*"):
            holder = index.classes.get(target[:-2])
        elif target.rsplit(".", 1)[-1] == ref:
            holder = index.classes.get(target.rsplit(".", 1)[0])
        else:
            continue
        if holder is not None:
            v = _constant_in(index, holder, ref, seen)
            if v is not None:
                return v
    return None
