"""Tiny tree-sitter based JavaIndex builder used only by the idx_facts tests."""

from __future__ import annotations

import re
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from j2ee_openapi_generator.index.models import ClassInfo, FieldInfo, JavaIndex, MethodInfo

LEGACY = Path(__file__).parents[1] / "fixtures" / "legacy-app"
_PARSER = Parser(Language(tree_sitter_java.language()))


def _t(n: Node | None) -> str:
    return n.text.decode() if n is not None and n.text is not None else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _mods(n: Node) -> tuple[list[str], list[str]]:
    anns: list[str] = []
    mods: list[str] = []
    for c in n.children:
        if c.type == "modifiers":
            for m in c.children:
                if m.type in ("marker_annotation", "annotation"):
                    anns.append(_norm(_t(m)).lstrip("@"))
                else:
                    mods.append(_t(m))
    return anns, mods


def read_source(rel: str) -> str:
    return (LEGACY / rel).read_text(encoding="utf-8")


def build_index() -> JavaIndex:
    classes: dict[str, ClassInfo] = {}
    for path in sorted(LEGACY.rglob("*.java")):
        rel = path.relative_to(LEGACY).as_posix()
        if "/test/" in rel:
            continue
        root = _PARSER.parse(path.read_bytes()).root_node
        pkg = ""
        imports: list[str] = []
        for c in root.children:
            if c.type == "package_declaration":
                pkg = _t(c.named_children[0])
            elif c.type == "import_declaration":
                imports.append(
                    _t(c).removeprefix("import ").removeprefix("static ").rstrip(";").strip()
                )
        for d in root.children:
            if d.type not in ("class_declaration", "interface_declaration", "enum_declaration"):
                continue
            name = _t(d.child_by_field_name("name"))
            anns, _ = _mods(d)
            sup = d.child_by_field_name("superclass")
            ifs: list[str] = []
            for _key in ("interfaces", "extends_interfaces"):
                for c in d.children:
                    if c.type in ("super_interfaces", "extends_interfaces"):
                        ifs += [_t(x) for x in _walk_types(c)]
            ci = ClassInfo(
                fqcn=f"{pkg}.{name}",
                simple_name=name,
                package=pkg,
                file=rel,
                kind={
                    "class_declaration": "class",
                    "interface_declaration": "interface",
                    "enum_declaration": "enum",
                }[d.type],  # type: ignore[arg-type]
                imports=imports,
                superclass=_t(sup.named_children[0]) if sup is not None else None,
                interfaces=sorted(set(ifs)),
                annotations=anns,
                start_line=d.start_point[0] + 1,
                end_line=d.end_point[0] + 1,
            )
            body = d.child_by_field_name("body")
            for m in body.named_children if body is not None else []:
                if m.type == "enum_constant":
                    ci.enum_constants.append(_t(m.child_by_field_name("name")))
                elif m.type == "enum_body_declarations":
                    pass
                elif m.type == "field_declaration":
                    fa, fm = _mods(m)
                    for dec in [x for x in m.children if x.type == "variable_declarator"]:
                        val = dec.child_by_field_name("value")
                        fi = FieldInfo(
                            name=_t(dec.child_by_field_name("name")),
                            type=_norm(_t(m.child_by_field_name("type"))),
                            annotations=fa,
                            line=m.start_point[0] + 1,
                            modifiers=fm,
                            initializer=_t(val) if val is not None else None,
                        )
                        ci.fields.append(fi)
                        if (
                            "static" in fm
                            and "final" in fm
                            and val is not None
                            and val.type == "string_literal"
                        ):
                            ci.constants[fi.name] = _t(val)
                elif m.type == "method_declaration" or m.type == "constructor_declaration":
                    ma, mm = _mods(m)
                    params: list[tuple[str, str]] = []
                    for p in m.child_by_field_name("parameters").named_children:  # type: ignore[union-attr]
                        params.append(
                            (
                                _norm(_t(p.child_by_field_name("type"))),
                                _t(p.child_by_field_name("name")),
                            )
                        )
                    throws = [
                        _t(x) for c in m.children if c.type == "throws" for x in c.named_children
                    ]
                    ci.methods.append(
                        MethodInfo(
                            name=_t(m.child_by_field_name("name")),
                            return_type=_t(m.child_by_field_name("type")),
                            params=params,
                            annotations=ma,
                            modifiers=mm if d.type != "interface_declaration" else [*mm, "public"],
                            throws=throws,
                            start_line=m.start_point[0] + 1,
                            end_line=m.end_point[0] + 1,
                        )
                    )
            classes[ci.fqcn] = ci
    return JavaIndex(classes=dict(sorted(classes.items())))


def _walk_types(n: Node) -> list[Node]:
    out: list[Node] = []
    for c in n.named_children:
        if c.type == "type_list":
            out += list(c.named_children)
        elif c.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            out.append(c)
    return out


def jsp_files() -> set[str]:
    return {p.relative_to(LEGACY).as_posix() for p in LEGACY.rglob("*.jsp")}
