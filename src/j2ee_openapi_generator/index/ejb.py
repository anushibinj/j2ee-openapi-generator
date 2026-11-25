"""EJB registry built from the Java index and ejb-jar.xml descriptors.

This module also hosts a few small type and annotation helpers shared with
``entities`` and ``branch_facts``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from lxml import etree

from j2ee_openapi_generator.index.models import ClassInfo, EjbMethod, EjbRecord, JavaIndex, MethodInfo

SESSION_KINDS: dict[str, str] = {
    "Stateless": "stateless",
    "Stateful": "stateful",
    "Singleton": "singleton",
    "MessageDriven": "message-driven",
}
_XML_KINDS: dict[str, str] = {
    "stateless": "stateless",
    "stateful": "stateful",
    "singleton": "singleton",
}
_LIFECYCLE = {"PostConstruct", "PreDestroy", "PostActivate", "PrePassivate", "Timeout"}
_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_CLASS_RE = re.compile(r"([\w.]+)\s*\.class")


# ------------------------------------------------------------------ helpers
def ann_name(ann: str) -> str:
    """Simple name of an annotation signature (no ``@``, no package, no args)."""
    head = ann.strip().lstrip("@").split("(", 1)[0].strip()
    return head.rsplit(".", 1)[-1]


def ann_args(ann: str) -> str:
    """Raw argument text of an annotation signature, empty when none."""
    if "(" not in ann:
        return ""
    inner = ann.split("(", 1)[1]
    inner = inner.rsplit(")", 1)[0]
    return inner.strip()


def find_ann(anns: list[str], name: str) -> str | None:
    for a in anns:
        if ann_name(a) == name:
            return a
    return None


def split_top(text: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` outside of (), {}, <>, [] and string literals."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            cur.append(ch)
        elif ch in "({<[":
            depth += 1
            cur.append(ch)
        elif ch in ")}>]":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail or parts:
        parts.append(tail)
    return parts


def parse_kwargs(args: str) -> dict[str, str]:
    """``a = 1, b = "x"`` -> {a: "1", b: '"x"'}; a lone value is stored as ``value``."""
    out: dict[str, str] = {}
    for part in split_top(args):
        if not part:
            continue
        m = re.match(r"^(\w+)\s*=\s*(.*)$", part, re.S)
        if m:
            out[m.group(1)] = m.group(2).strip()
        else:
            out["value"] = part
    return out


def unquote(text: str) -> str:
    m = _STR_RE.fullmatch(text.strip())
    if not m:
        return text.strip()
    return re.sub(r"\\(.)", lambda mm: {"n": "\n", "t": "\t"}.get(mm.group(1), mm.group(1)), m.group(1))


def string_literals(text: str) -> list[str]:
    return [unquote(f'"{m}"') for m in _STR_RE.findall(text)]


def strip_generics(type_text: str) -> str:
    t = type_text.strip()
    if "<" in t:
        t = t[: t.index("<")]
    while t.endswith("[]"):
        t = t[:-2]
    return t.strip()


def generic_args(type_text: str) -> list[str]:
    t = type_text.strip()
    if "<" not in t or not t.endswith(">"):
        return []
    inner = t[t.index("<") + 1 : -1]
    out: list[str] = []
    for part in split_top(inner):
        part = re.sub(r"^\?\s*(extends|super)\s+", "", part).strip()
        if part and part != "?":
            out.append(part)
    return out


def elem_type_of(type_text: str) -> str | None:
    """Element (or map value) type of a collection/array/generic type as written."""
    t = type_text.strip()
    if t.endswith("[]"):
        return t[:-2].strip()
    args = generic_args(t)
    return args[-1] if args else None


def all_type_names(type_text: str) -> list[str]:
    """Every raw type name mentioned in a possibly generic type text."""
    names: list[str] = []
    base = strip_generics(type_text)
    if base:
        names.append(base)
    for a in generic_args(type_text):
        names.extend(all_type_names(a))
    return names


def _simple_index(java: JavaIndex) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for fq, ci in java.classes.items():
        idx.setdefault(ci.simple_name, []).append(fq)
    return idx


def resolve_class(java: JavaIndex, ctx: ClassInfo | None, name: str) -> ClassInfo | None:
    """Resolve a type name as written in ``ctx`` to a ClassInfo of the index."""
    n = strip_generics(name)
    if not n:
        return None
    if n in java.classes:
        return java.classes[n]
    simple = n.rsplit(".", 1)[-1]
    if ctx is not None:
        if ctx.simple_name == n:
            return ctx
        for imp in ctx.imports:
            imp_t = imp.strip().removeprefix("import ").removeprefix("static ").rstrip(";").strip()
            if imp_t.endswith("." + n) and imp_t in java.classes:
                return java.classes[imp_t]
        if ctx.package:
            cand = f"{ctx.package}.{n}"
            if cand in java.classes:
                return java.classes[cand]
        for imp in ctx.imports:
            imp_t = imp.strip().removeprefix("import ").removeprefix("static ").rstrip(";").strip()
            if imp_t.endswith(".*"):
                cand = imp_t[:-1] + n
                if cand in java.classes:
                    return java.classes[cand]
        # nested type written as Outer.Inner
        if "." in n:
            outer = resolve_class(java, ctx, n.split(".", 1)[0])
            if outer is not None and f"{outer.fqcn}.{n.split('.', 1)[1]}" in java.classes:
                return java.classes[f"{outer.fqcn}.{n.split('.', 1)[1]}"]
    hits = sorted(fq for fq, ci in java.classes.items() if ci.simple_name == simple)
    if len(hits) == 1:
        return java.classes[hits[0]]
    return None


def class_chain(java: JavaIndex, ci: ClassInfo) -> list[ClassInfo]:
    """The class followed by its resolvable superclasses (nearest first)."""
    chain = [ci]
    seen = {ci.fqcn}
    cur = ci
    while cur.superclass:
        sup = resolve_class(java, cur, cur.superclass)
        if sup is None or sup.fqcn in seen:
            break
        chain.append(sup)
        seen.add(sup.fqcn)
        cur = sup
    return chain


def roles_of(ann: str | None) -> list[str]:
    return string_literals(ann_args(ann)) if ann else []


# ------------------------------------------------------------------ registry
def _class_literals(ann: str) -> list[str]:
    return _CLASS_RE.findall(ann_args(ann))


def _tx_value(ann: str | None) -> str | None:
    if not ann:
        return None
    args = ann_args(ann)
    m = re.search(r"(\w+)\s*$", args.replace("value", "").replace("=", "").strip())
    return m.group(1) if m else None


def _xml_beans(read_source: Callable[[str], str], files: list[str]) -> list[dict[str, object]]:
    beans: list[dict[str, object]] = []
    for f in sorted(files):
        try:
            root = etree.fromstring(read_source(f).encode("utf-8"))
        except (etree.XMLSyntaxError, ValueError):
            continue
        for el in root.xpath("//*[local-name()='session' or local-name()='message-driven']"):
            def txt(tag: str, el: etree._Element = el) -> list[str]:
                res = el.xpath(f"./*[local-name()='{tag}']/text()")
                return [str(r).strip() for r in res if str(r).strip()]

            cls = txt("ejb-class")
            if not cls:
                continue
            st = txt("session-type")
            kind = "message-driven" if etree.QName(el).localname == "message-driven" else (
                _XML_KINDS.get(st[0].lower(), "other") if st else "other"
            )
            ifaces: list[str] = []
            for tag in ("business-local", "business-remote", "local", "remote"):
                ifaces.extend(txt(tag))
            beans.append({"cls": cls[0], "kind": kind, "ifaces": ifaces})
    return beans


def _business_interfaces(java: JavaIndex, ci: ClassInfo) -> list[ClassInfo]:
    found: dict[str, ClassInfo] = {}
    implemented: list[ClassInfo] = []
    for i in ci.interfaces:
        ic = resolve_class(java, ci, i)
        if ic is not None:
            implemented.append(ic)
    for a in ci.annotations:
        if ann_name(a) in ("Local", "Remote"):
            lits = _class_literals(a)
            if lits:
                for lit in lits:
                    ic = resolve_class(java, ci, lit)
                    if ic is not None:
                        found[ic.fqcn] = ic
            else:
                for ic in implemented:
                    found[ic.fqcn] = ic
    for ic in implemented:
        if any(ann_name(a) in ("Local", "Remote") for a in ic.annotations):
            found[ic.fqcn] = ic
    if not found and len(implemented) == 1:
        found[implemented[0].fqcn] = implemented[0]
    return [found[k] for k in sorted(found)]


def _ejb_method(
    m: MethodInfo, class_ann: list[str], extra_throws: list[str], class_roles: list[str]
) -> EjbMethod:
    rolesA = find_ann(m.annotations, "RolesAllowed")
    roles = roles_of(rolesA) if rolesA else []
    permit = find_ann(m.annotations, "PermitAll") is not None
    if rolesA is None and not permit:
        roles = list(class_roles)
        permit = find_ann(class_ann, "PermitAll") is not None and not roles
    tx = _tx_value(find_ann(m.annotations, "TransactionAttribute")) or _tx_value(
        find_ann(class_ann, "TransactionAttribute")
    )
    throws = sorted(set(m.throws) | set(extra_throws))
    return EjbMethod(
        name=m.name,
        signature=m.signature,
        return_type=m.return_type,
        start_line=m.start_line,
        end_line=m.end_line,
        roles_allowed=roles,
        permit_all=permit,
        tx_attribute=tx,
        throws=throws,
    )


def _jndi(bean: ClassInfo, ifaces: list[ClassInfo], kind: str) -> list[str]:
    if kind == "message-driven":
        return []
    simple = bean.simple_name
    for a in bean.annotations:
        if ann_name(a) in SESSION_KINDS:
            nm = parse_kwargs(ann_args(a)).get("name")
            if nm:
                simple = unquote(nm)
    names = [f"java:global/*/{simple}", f"java:module/{simple}"]
    for ic in ifaces:
        names.append(f"java:global/*/{simple}!{ic.fqcn}")
        names.append(f"java:module/{simple}!{ic.fqcn}")
    return sorted(set(names))


def build_ejb_registry(
    java: JavaIndex, read_source: Callable[[str], str], ejb_jar_files: list[str]
) -> list[EjbRecord]:
    """EJB beans from annotations and ejb-jar.xml, sorted by bean FQCN."""
    kinds: dict[str, str] = {}
    xml_ifaces: dict[str, list[str]] = {}
    for fq, ci in java.classes.items():
        if ci.kind != "class":
            continue
        for a in ci.annotations:
            n = ann_name(a)
            if n in SESSION_KINDS:
                kinds[fq] = SESSION_KINDS[n]
    for bean in _xml_beans(read_source, ejb_jar_files):
        cls = str(bean["cls"])
        ci = java.classes.get(cls)
        if ci is None:
            continue
        kinds.setdefault(cls, str(bean["kind"]))
        xml_ifaces.setdefault(cls, []).extend(str(i) for i in bean["ifaces"])  # type: ignore[attr-defined]

    records: list[EjbRecord] = []
    for fq in sorted(kinds):
        ci = java.classes[fq]
        ifaces = {ic.fqcn: ic for ic in _business_interfaces(java, ci)}
        for name in xml_ifaces.get(fq, []):
            ic = resolve_class(java, ci, name)
            if ic is not None:
                ifaces[ic.fqcn] = ic
            else:
                ifaces.setdefault(name, ClassInfo(fqcn=name, simple_name=name.rsplit(".", 1)[-1], package="", file=""))
        iface_list = [ifaces[k] for k in sorted(ifaces)]
        class_roles = roles_of(find_ann(ci.annotations, "RolesAllowed"))

        iface_methods: dict[tuple[str, int], MethodInfo] = {}
        for ic in iface_list:
            for im in ic.methods:
                iface_methods.setdefault((im.name, len(im.params)), im)

        publics = [
            m
            for m in ci.methods
            if "public" in m.modifiers and not any(ann_name(a) in _LIFECYCLE for a in m.annotations)
        ]
        methods: list[EjbMethod] = []
        if iface_methods:
            seen: set[tuple[str, int]] = set()
            for m in publics:
                key = (m.name, len(m.params))
                if key in iface_methods:
                    methods.append(_ejb_method(m, ci.annotations, iface_methods[key].throws, class_roles))
                    seen.add(key)
            for key, im in sorted(iface_methods.items(), key=lambda kv: (kv[1].start_line, kv[0])):
                if key not in seen:
                    methods.append(_ejb_method(im, ci.annotations, [], class_roles))
        else:
            methods = [_ejb_method(m, ci.annotations, [], class_roles) for m in publics]
        methods.sort(key=lambda em: (em.start_line, em.signature))

        records.append(
            EjbRecord(
                bean_fqcn=fq,
                kind=kinds[fq],  # type: ignore[arg-type]
                interfaces=[ic.fqcn for ic in iface_list],
                interface_files={ic.fqcn: ic.file for ic in iface_list if ic.file},
                impl_file=ci.file,
                methods=methods,
                class_roles_allowed=class_roles,
                jndi_names=_jndi(ci, iface_list, kinds[fq]),
            )
        )
    return records
