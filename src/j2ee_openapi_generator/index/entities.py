"""Entity, embeddable, DTO and enum registry (fields only)."""

from __future__ import annotations


from j2ee_openapi_generator.index.ejb import (
    all_type_names,
    ann_args,
    ann_name,
    class_chain,
    elem_type_of,
    find_ann,
    parse_kwargs,
    resolve_class,
    split_top,
    strip_generics,
    unquote,
)
from j2ee_openapi_generator.index.models import (
    ClassInfo,
    EjbRecord,
    EntityField,
    EntityRecord,
    FieldInfo,
    JavaIndex,
)

_RELATIONS = ("ManyToOne", "OneToOne", "OneToMany", "ManyToMany")
_MAX_DEPTH = 3


def _int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(v.strip())
    except ValueError:
        return None


def _make_field(java: JavaIndex, owner: ClassInfo, f: FieldInfo) -> EntityField:
    anns = f.annotations
    col = find_ann(anns, "Column")
    ckw = parse_kwargs(ann_args(col)) if col else {}
    size = find_ann(anns, "Size")
    skw = parse_kwargs(ann_args(size)) if size else {}
    pat = find_ann(anns, "Pattern")
    pkw = parse_kwargs(ann_args(pat)) if pat else {}
    nullable: bool | None = None
    if "nullable" in ckw:
        nullable = ckw["nullable"].strip().lower() != "false"
    pattern = unquote(pkw["regexp"]) if "regexp" in pkw else None

    ftype = strip_generics(f.type)
    tci = resolve_class(java, owner, f.resolved_type or ftype)
    enum_values: list[str] = []
    if tci is not None and tci.kind == "enum":
        enum_values = list(tci.enum_constants)

    relationship = None
    rel_ann = None
    for r in _RELATIONS:
        a = find_ann(anns, r)
        if a is not None:
            relationship = r
            rel_ann = a
            break
    target: str | None = None
    elem = elem_type_of(f.type)
    if relationship is not None:
        target_raw: str | None = None
        te = parse_kwargs(ann_args(rel_ann or "")).get("targetEntity")
        if te:
            target_raw = te.strip().removesuffix(".class").strip()
        elif elem:
            target_raw = elem
        else:
            target_raw = ftype
        tc = resolve_class(java, owner, target_raw) if target_raw else None
        target = tc.fqcn if tc is not None else target_raw

    transient = "transient" in f.modifiers or find_ann(anns, "Transient") is not None
    return EntityField(
        name=f.name,
        java_type=f.type,
        elem_type=elem,
        is_id=find_ann(anns, "Id") is not None or find_ann(anns, "EmbeddedId") is not None,
        is_version=find_ann(anns, "Version") is not None,
        nullable=nullable,
        length=_int(ckw.get("length")),
        not_null=find_ann(anns, "NotNull") is not None,
        size_min=_int(skw.get("min")),
        size_max=_int(skw.get("max")),
        pattern=pattern,
        enumerated=find_ann(anns, "Enumerated") is not None,
        enum_values=enum_values,
        relationship=relationship,  # type: ignore[arg-type]
        target_type=target,
        transient=transient,
        line=f.line,
    )


def _collect_fields(java: JavaIndex, ci: ClassInfo) -> list[EntityField]:
    """Own fields preceded by those of mapped superclasses; static fields skipped."""
    out: dict[str, EntityField] = {}
    order: list[str] = []
    for c in reversed(class_chain(java, ci)):
        # JPA classes only inherit state from mapped superclasses; DTOs inherit from any parent
        if c is not ci and _is_jpa(ci) is not None and not _is_mapped_super(c):
            continue
        for f in c.fields:
            if "static" in f.modifiers:
                continue
            ef = _make_field(java, c, f)
            if f.name not in out:
                order.append(f.name)
            out[f.name] = ef
    return [out[n] for n in order]


def _is_jpa(ci: ClassInfo) -> str | None:
    for a in ci.annotations:
        n = ann_name(a)
        if n == "Entity":
            return "entity"
        if n == "Embeddable":
            return "embeddable"
    return None


def _is_mapped_super(ci: ClassInfo) -> bool:
    return any(ann_name(a) == "MappedSuperclass" for a in ci.annotations)


def _param_types(signature: str) -> list[str]:
    inner = signature[signature.index("(") + 1 : signature.rindex(")")] if "(" in signature else ""
    return [p for p in split_top(inner) if p]


def build_entity_registry(java: JavaIndex, ejbs: list[EjbRecord]) -> list[EntityRecord]:
    """Entities, embeddables, DTOs reachable from EJB signatures, and referenced enums."""
    records: dict[str, EntityRecord] = {}

    def add(ci: ClassInfo, kind: str) -> None:
        if ci.fqcn in records:
            return
        if kind == "enum":
            records[ci.fqcn] = EntityRecord(
                fqcn=ci.fqcn,
                simple_name=ci.simple_name,
                kind="enum",
                file=ci.file,
                start_line=ci.start_line,
                end_line=ci.end_line,
                enum_values=list(ci.enum_constants),
            )
            return
        records[ci.fqcn] = EntityRecord(
            fqcn=ci.fqcn,
            simple_name=ci.simple_name,
            kind=kind,  # type: ignore[arg-type]
            file=ci.file,
            start_line=ci.start_line,
            end_line=ci.end_line,
            fields=_collect_fields(java, ci),
        )

    for fq in sorted(java.classes):
        ci = java.classes[fq]
        kind = _is_jpa(ci)
        if kind is not None:
            add(ci, kind)

    # seeds: types in EJB signatures
    frontier: list[tuple[ClassInfo, int]] = []
    queued: set[str] = set()

    def consider(ctxs: list[ClassInfo], type_text: str, depth: int) -> None:
        for name in all_type_names(type_text):
            ci = None
            for ctx in ctxs:
                ci = resolve_class(java, ctx, name)
                if ci is not None:
                    break
            if ci is None or ci.fqcn in queued:
                continue
            if ci.kind not in ("class", "enum"):
                continue
            queued.add(ci.fqcn)
            frontier.append((ci, depth))

    for rec in sorted(ejbs, key=lambda r: r.bean_fqcn):
        ctxs = [c for c in [java.classes.get(rec.bean_fqcn), *(java.classes.get(i) for i in rec.interfaces)] if c]
        for m in rec.methods:
            for p in _param_types(m.signature):
                consider(ctxs, p, 0)
            consider(ctxs, m.return_type, 0)

    while frontier:
        ci, depth = frontier.pop(0)
        if ci.kind == "enum":
            add(ci, "enum")
            continue
        if _is_jpa(ci) is None and not _is_mapped_super(ci):
            add(ci, "dto")
        if depth >= _MAX_DEPTH:
            continue
        for c in class_chain(java, ci):
            for f in c.fields:
                if "static" in f.modifiers:
                    continue
                consider([c], f.type, depth + 1)

    # enums referenced by the fields of every record
    for rec in sorted(list(records.values()), key=lambda r: r.fqcn):
        owner = java.classes.get(rec.fqcn)
        if owner is None or rec.kind == "enum":
            continue
        for ef in rec.fields:
            for name in all_type_names(ef.java_type):
                tc = resolve_class(java, owner, name)
                if tc is not None and tc.kind == "enum":
                    add(tc, "enum")

    return [records[k] for k in sorted(records)]
