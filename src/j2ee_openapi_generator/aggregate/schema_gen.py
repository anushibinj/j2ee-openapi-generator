"""Pure code Java-to-JSON-Schema generation from indexed entities.

``SchemaGenerator`` turns :class:`EntityRecord` objects into OpenAPI component schemas. Schemas
become part of ``components()`` only once they were referenced through ``ref`` (directly or via a
property of another schema), so the emitted document never carries unused schemas.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any, Literal

from j2ee_openapi_generator.index.models import EntityField, EntityRecord
from j2ee_openapi_generator.llm.schemas import FieldSpec

SchemaKind = Literal["base", "create", "update", "patch", "page"]

_SUFFIXES: tuple[tuple[str, SchemaKind], ...] = (
    ("CreateRequest", "create"),
    ("UpdateRequest", "update"),
    ("Patch", "patch"),
    ("Page", "page"),
)
_KIND_SUFFIX: dict[str, str] = {
    "base": "",
    "create": "CreateRequest",
    "update": "UpdateRequest",
    "patch": "Patch",
    "page": "Page",
}

_SIMPLE: dict[str, tuple[str, str | None]] = {
    "String": ("string", None),
    "CharSequence": ("string", None),
    "char": ("string", None),
    "Character": ("string", None),
    "int": ("integer", "int32"),
    "Integer": ("integer", "int32"),
    "short": ("integer", "int32"),
    "Short": ("integer", "int32"),
    "byte": ("integer", "int32"),
    "Byte": ("integer", "int32"),
    "long": ("integer", "int64"),
    "Long": ("integer", "int64"),
    "BigInteger": ("integer", None),
    "double": ("number", "double"),
    "Double": ("number", "double"),
    "float": ("number", "float"),
    "Float": ("number", "float"),
    "BigDecimal": ("string", "decimal"),
    "boolean": ("boolean", None),
    "Boolean": ("boolean", None),
    "Date": ("string", "date-time"),
    "Timestamp": ("string", "date-time"),
    "LocalDateTime": ("string", "date-time"),
    "Instant": ("string", "date-time"),
    "OffsetDateTime": ("string", "date-time"),
    "ZonedDateTime": ("string", "date-time"),
    "LocalDate": ("string", "date"),
    "LocalTime": ("string", "time"),
    "UUID": ("string", "uuid"),
}
_COLLECTIONS = {
    "List",
    "Set",
    "Collection",
    "Iterable",
    "ArrayList",
    "LinkedList",
    "HashSet",
    "LinkedHashSet",
    "TreeSet",
    "SortedSet",
}
_MAPS = {"Map", "HashMap", "LinkedHashMap", "TreeMap", "SortedMap"}
_PRIMITIVE_NAMES = {"string", "integer", "number", "boolean"}
_JSON_TYPES = {"string", "integer", "number", "boolean", "object", "array"}


class UnknownSchema(ValueError):
    """Raised when a schema name is neither an indexed entity nor a derived schema of one."""


def derived_name(base: str, kind: SchemaKind) -> str:
    return base + _KIND_SUFFIX[kind]


def camel(name: str) -> str:
    """Property names are camelCase."""
    if "_" in name:
        parts = [p for p in name.split("_") if p]
        if not parts:
            return name
        head = parts[0].lower() if parts[0].isupper() else parts[0][0].lower() + parts[0][1:]
        return head + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])
    if name[:1].isupper() and not name.isupper():
        return name[0].lower() + name[1:]
    return name


def _split_top(text: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def parse_java_type(java_type: str) -> tuple[str, list[str], int]:
    """Return ``(simple raw name, generic args, array dims)`` of a Java type as written."""
    t = re.sub(r"@\w+(\([^)]*\))?\s*", "", java_type).strip()
    t = re.sub(r"^final\s+", "", t)
    dims = 0
    while t.endswith("[]"):
        dims += 1
        t = t[:-2].strip()
    args: list[str] = []
    if "<" in t:
        raw, _, rest = t.partition("<")
        inner = rest.rsplit(">", 1)[0]
        args = [re.sub(r"^\?\s*(extends|super)\s+", "", a).strip() for a in _split_top(inner)]
        t = raw.strip()
    return t.rsplit(".", 1)[-1], args, dims


def _size_map(schema: dict[str, Any], field: EntityField) -> None:
    kind = schema.get("type")
    if kind == "string" and "enum" not in schema and "format" not in schema:
        if field.size_min is not None:
            schema["minLength"] = field.size_min
        max_len = field.size_max if field.size_max is not None else field.length
        if max_len is not None:
            schema["maxLength"] = max_len
        if field.pattern:
            schema["pattern"] = field.pattern
    elif kind == "array":
        if field.size_min is not None:
            schema["minItems"] = field.size_min
        if field.size_max is not None:
            schema["maxItems"] = field.size_max


def sort_schema(obj: Any) -> Any:
    """Recursively sort dict keys (lists keep their order, except ``required``)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k in sorted(obj, key=str):
            v = sort_schema(obj[k])
            if k == "required" and isinstance(v, list):
                v = sorted(v)
            out[k] = v
        return out
    if isinstance(obj, list):
        return [sort_schema(v) for v in obj]
    return obj


class SchemaGenerator:
    """Builds component schemas on demand from indexed entities."""

    def __init__(
        self,
        entities: list[EntityRecord],
        openapi_version: str,
        *,
        lenient: bool = False,
        include_collections: set[str] | None = None,
    ) -> None:
        self.v31 = openapi_version.startswith("3.1")
        self.lenient = lenient
        self.include_collections = include_collections or set()
        self._registry: dict[str, EntityRecord] = {}
        for e in sorted(entities, key=lambda e: e.fqcn):
            self._registry.setdefault(e.simple_name, e)
        self._schemas: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- name handling
    def _parse(self, name: str) -> tuple[str, SchemaKind] | None:
        if name in self._registry:
            return name, "base"
        for suffix, kind in _SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                base = name[: -len(suffix)]
                ent = self._registry.get(base)
                if ent is not None and ent.kind != "enum":
                    return base, kind
        return None

    def knows(self, name: str) -> bool:
        return name in self._schemas or self._parse(name) is not None

    def entity(self, name: str) -> EntityRecord | None:
        return self._registry.get(name)

    def base_entity(self, name: str) -> EntityRecord | None:
        parsed = self._parse(name)
        return self._registry.get(parsed[0]) if parsed else None

    def property_names(self, name: str) -> set[str]:
        parsed = self._parse(name)
        if parsed is None:
            return set(self._schemas.get(name, {}).get("properties", {}))
        base, kind = parsed
        if kind == "page":
            return {"content", "page", "size", "totalElements", "totalPages"}
        props, _ = self._entity_props(self._registry[base], kind)
        return set(props)

    # ---------------------------------------------------------------- public API
    def ref(self, name: str) -> dict[str, str]:
        """Return a ``$ref`` for ``name`` and make sure the schema is part of the components."""
        if name not in self._schemas:
            parsed = self._parse(name)
            if parsed is None:
                if not self.lenient:
                    raise UnknownSchema(f"unknown schema {name!r}")
                self._schemas[name] = {"type": "object"}
            else:
                self._schemas[name] = {}
                self._schemas[name].update(self._build(*parsed))
        return {"$ref": f"#/components/schemas/{name}"}

    def add_schema(self, name: str, schema: dict[str, Any]) -> dict[str, str]:
        """Register an ad-hoc schema (first registration of a name wins)."""
        self._schemas.setdefault(name, schema)
        return {"$ref": f"#/components/schemas/{name}"}

    def schema_for(self, name: str, kind: SchemaKind = "base") -> dict[str, Any]:
        """Return the schema of entity ``name`` for ``kind``; nested schemas get registered."""
        ent = self._registry.get(name)
        if ent is None:
            if self.lenient:
                return {"type": "object"}
            raise UnknownSchema(f"unknown schema {name!r}")
        if kind != "base" and ent.kind == "enum":
            raise UnknownSchema(f"enum {name!r} has no {kind} schema")
        return sort_schema(self._build(name, kind))

    def components(self) -> dict[str, dict[str, Any]]:
        return {k: sort_schema(copy.deepcopy(self._schemas[k])) for k in sorted(self._schemas)}

    def inline_schema(self, fields: list[FieldSpec], required: list[str]) -> dict[str, Any]:
        props: dict[str, Any] = {}
        req: set[str] = set(required)
        for f in fields:
            props[f.name] = self._field_spec_schema(f)
            if f.required:
                req.add(f.name)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        names = sorted(r for r in req if r in props)
        if names:
            schema["required"] = names
        return sort_schema(schema)

    def type_schema(
        self,
        java_type: str,
        *,
        elem_type: str | None = None,
        enum_values: Iterable[str] = (),
        multipart: bool = False,
    ) -> dict[str, Any]:
        """Schema of a Java type as written (``byte[]`` depends on the multipart context)."""
        raw, args, dims = parse_java_type(java_type)
        if dims:
            inner = java_type.strip()[:-2]
            iraw = parse_java_type(inner)[0]
            if dims == 1 and iraw in ("byte", "Byte"):
                return self._binary(multipart)
            return {"type": "array", "items": self.type_schema(inner, multipart=multipart)}
        values = list(enum_values)
        if values:
            return {"type": "string", "enum": values}
        if raw in _COLLECTIONS:
            item_t = args[0] if args else (elem_type or "Object")
            return {"type": "array", "items": self.type_schema(item_t, multipart=multipart)}
        if raw in _MAPS:
            val_t = args[1] if len(args) > 1 else (elem_type or "Object")
            val = self.type_schema(val_t, multipart=multipart)
            return {"type": "object", "additionalProperties": val if val else True}
        if raw in _SIMPLE:
            t, fmt = _SIMPLE[raw]
            return {"type": t, **({"format": fmt} if fmt else {})}
        ent = self._registry.get(raw)
        if ent is not None:
            if ent.kind == "enum":
                return {"type": "string", "enum": list(ent.enum_values)}
            return self.ref(raw)
        return {"type": "object"}

    # ---------------------------------------------------------------- internals
    def _binary(self, multipart: bool) -> dict[str, Any]:
        if multipart:
            return {"type": "string", "format": "binary"}
        if self.v31:
            return {"type": "string", "contentEncoding": "base64"}
        return {"type": "string", "format": "byte"}

    def _nullable(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.v31:
            if "$ref" in schema:
                return {"oneOf": [schema, {"type": "null"}]}
            out = dict(schema)
            t = out.get("type")
            if isinstance(t, str):
                out["type"] = [t, "null"]
            if "enum" in out and None not in out["enum"]:
                out["enum"] = [*out["enum"], None]
            return out
        if "$ref" in schema:
            return {"allOf": [schema], "nullable": True}
        out = dict(schema)
        out["nullable"] = True
        if "enum" in out and None not in out["enum"]:
            out["enum"] = [*out["enum"], None]
        return out

    def _field_spec_schema(self, f: FieldSpec) -> dict[str, Any]:
        t = f.type
        schema: dict[str, Any]
        if f.enum:
            schema = {"type": "string", "enum": list(f.enum)}
        elif t == "binary":
            schema = {"type": "string", "format": "binary"}
        elif t == "array":
            schema = {"type": "array", "items": self._items_schema(f.items_ref)}
        elif t in _JSON_TYPES:
            schema = {"type": t}
            if f.format:
                schema["format"] = f.format
        else:
            schema = self._named(t)
        if f.format and "format" not in schema and "$ref" not in schema:
            schema["format"] = f.format
        if f.description and "$ref" not in schema:
            schema["description"] = f.description
        return schema

    def _items_schema(self, items_ref: str | None) -> dict[str, Any]:
        if not items_ref:
            return {}
        if items_ref in _PRIMITIVE_NAMES:
            return {"type": items_ref}
        return self._named(items_ref)

    def _named(self, name: str) -> dict[str, Any]:
        ent = self._registry.get(name)
        if ent is not None and ent.kind == "enum":
            return {"type": "string", "enum": list(ent.enum_values)}
        if self.knows(name):
            return self.ref(name)
        if name in _SIMPLE:
            return self.type_schema(name)
        return {"type": "object"}

    def _id_schema(self, target: str | None) -> dict[str, Any]:
        ent = self._registry.get(parse_java_type(target)[0]) if target else None
        if ent is not None:
            for f in ent.fields:
                if f.is_id:
                    return self.type_schema(f.java_type)
        return {"type": "string"}

    def _collection_included(self, entity: EntityRecord, field: EntityField) -> bool:
        return (
            f"{entity.simple_name}.{field.name}" in self.include_collections
            or field.name in self.include_collections
        )

    def _field_property(
        self, entity: EntityRecord, field: EntityField
    ) -> tuple[str, dict[str, Any], bool] | None:
        """Return ``(property name, schema, readOnly)`` or None when the field is skipped."""
        if field.transient:
            return None
        rel = field.relationship
        if rel in ("ManyToOne", "OneToOne"):
            target = field.target_type or field.java_type
            return f"{camel(field.name)}Id", self._id_schema(target), False
        if rel in ("OneToMany", "ManyToMany"):
            if not self._collection_included(entity, field):
                return None
            target = field.target_type or field.elem_type or field.java_type
            raw = parse_java_type(target)[0]
            items = self._named(raw)
            return camel(field.name), {"type": "array", "items": items}, False
        if field.is_id:
            schema = self.type_schema(field.java_type)
            return "id", schema, True
        if field.is_version:
            return "version", self.type_schema(field.java_type), True
        values = list(field.enum_values)
        if field.enumerated and not values:
            ent = self._registry.get(parse_java_type(field.java_type)[0])
            values = list(ent.enum_values) if ent is not None else []
        schema = self.type_schema(field.java_type, elem_type=field.elem_type, enum_values=values)
        _size_map(schema, field)
        if field.nullable is True and not field.not_null:
            schema = self._nullable(schema)
        return camel(field.name), schema, False

    def _entity_props(
        self, entity: EntityRecord, kind: SchemaKind
    ) -> tuple[dict[str, Any], list[str]]:
        props: dict[str, Any] = {}
        required: list[str] = []
        for field in entity.fields:
            item = self._field_property(entity, field)
            if item is None:
                continue
            name, schema, read_only = item
            if read_only and kind != "base":
                continue
            if read_only:
                schema = {**schema, "readOnly": True}
            props[name] = schema
            if not read_only and (field.not_null or field.nullable is False):
                required.append(name)
        if kind == "patch":
            required = []
        return props, sorted(set(required))

    def _build(self, base: str, kind: SchemaKind) -> dict[str, Any]:
        ent = self._registry[base]
        if ent.kind == "enum":
            return {"type": "string", "enum": list(ent.enum_values)}
        if kind == "page":
            return {
                "type": "object",
                "properties": {
                    "content": {"type": "array", "items": self.ref(base)},
                    "page": {"type": "integer", "format": "int32", "minimum": 0},
                    "size": {"type": "integer", "format": "int32", "minimum": 1},
                    "totalElements": {"type": "integer", "format": "int64", "minimum": 0},
                    "totalPages": {"type": "integer", "format": "int32", "minimum": 0},
                },
                "required": ["content", "page", "size", "totalElements", "totalPages"],
            }
        props, required = self._entity_props(ent, kind)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema
