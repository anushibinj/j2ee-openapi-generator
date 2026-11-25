from __future__ import annotations

import pytest
from emit_report_support import entities, entity, field

from j2ee_openapi_generator.aggregate.schema_gen import SchemaGenerator, UnknownSchema, camel
from j2ee_openapi_generator.llm.schemas import FieldSpec


def gen(version: str = "3.1.0", **kw: object) -> SchemaGenerator:
    return SchemaGenerator(entities(), version, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("java", "expected"),
    [
        ("String", {"type": "string"}),
        ("int", {"type": "integer", "format": "int32"}),
        ("Integer", {"type": "integer", "format": "int32"}),
        ("short", {"type": "integer", "format": "int32"}),
        ("byte", {"type": "integer", "format": "int32"}),
        ("long", {"type": "integer", "format": "int64"}),
        ("Long", {"type": "integer", "format": "int64"}),
        ("double", {"type": "number", "format": "double"}),
        ("float", {"type": "number", "format": "float"}),
        ("BigDecimal", {"type": "string", "format": "decimal"}),
        ("boolean", {"type": "boolean"}),
        ("Boolean", {"type": "boolean"}),
        ("Date", {"type": "string", "format": "date-time"}),
        ("Timestamp", {"type": "string", "format": "date-time"}),
        ("LocalDateTime", {"type": "string", "format": "date-time"}),
        ("Instant", {"type": "string", "format": "date-time"}),
        ("OffsetDateTime", {"type": "string", "format": "date-time"}),
        ("ZonedDateTime", {"type": "string", "format": "date-time"}),
        ("LocalDate", {"type": "string", "format": "date"}),
        ("LocalTime", {"type": "string", "format": "time"}),
        ("java.util.UUID", {"type": "string", "format": "uuid"}),
        ("List<String>", {"type": "array", "items": {"type": "string"}}),
        ("Set<Long>", {"type": "array", "items": {"type": "integer", "format": "int64"}}),
        ("Collection<Integer>", {"type": "array", "items": {"type": "integer", "format": "int32"}}),
        (
            "Map<String, BigDecimal>",
            {"type": "object", "additionalProperties": {"type": "string", "format": "decimal"}},
        ),
        ("Whatever", {"type": "object"}),
    ],
)
def test_type_table(java: str, expected: dict[str, object]) -> None:
    assert gen().type_schema(java) == expected


def test_byte_array_contexts() -> None:
    assert gen("3.1.0").type_schema("byte[]") == {"type": "string", "contentEncoding": "base64"}
    assert gen("3.0.3").type_schema("byte[]") == {"type": "string", "format": "byte"}
    for v in ("3.1.0", "3.0.3"):
        assert gen(v).type_schema("byte[]", multipart=True) == {
            "type": "string",
            "format": "binary",
        }


def test_entity_collection_refs_and_enums() -> None:
    g = gen()
    assert g.type_schema("List<Order>") == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Order"},
    }
    assert g.type_schema("UserStatus") == {
        "type": "string",
        "enum": ["ACTIVE", "SUSPENDED", "CLOSED"],
    }
    assert g.type_schema("String", enum_values=["A", "B"]) == {"type": "string", "enum": ["A", "B"]}


def test_base_schema_user() -> None:
    s = gen().schema_for("User")
    p = s["properties"]
    assert p["id"] == {"type": "integer", "format": "int64", "readOnly": True}
    assert p["version"]["readOnly"] is True
    assert p["name"] == {"type": "string", "minLength": 2, "maxLength": 80}
    assert p["email"]["pattern"] == "[^@]+@[^@]+"
    assert p["email"]["maxLength"] == 120
    assert p["status"] == {"type": "string", "enum": ["ACTIVE", "SUSPENDED", "CLOSED"]}
    assert p["creditLimit"] == {"type": "string", "format": "decimal"}
    assert "phones" not in p
    assert s["required"] == ["email", "name", "status"]
    assert list(p) == sorted(p)


def test_many_to_one_uses_target_id() -> None:
    p = gen().schema_for("Order")["properties"]
    assert p["customerId"] == {"type": "integer", "format": "int64"}
    assert "customer" not in p
    assert "lines" not in p


def test_many_to_one_unknown_target_is_string() -> None:
    ents = [entity("A", "entity", [field("b", "B", relationship="ManyToOne", target_type="B")])]
    s = SchemaGenerator(ents, "3.1.0").schema_for("A")
    assert s["properties"]["bId"] == {"type": "string"}


def test_include_collections() -> None:
    g = gen(include_collections={"User.phones"})
    p = g.schema_for("User")["properties"]
    assert p["phones"] == {"type": "array", "items": {"$ref": "#/components/schemas/Phone"}}
    assert "Phone" in g.components()


def test_derived_schemas() -> None:
    g = gen()
    create = g.schema_for("User", "create")
    assert "id" not in create["properties"] and "version" not in create["properties"]
    assert create["required"] == ["email", "name", "status"]
    update = g.schema_for("User", "update")
    assert "id" not in update["properties"]
    patch = g.schema_for("User", "patch")
    assert "required" not in patch
    assert "name" in patch["properties"]
    page = g.schema_for("User", "page")
    assert page["properties"]["content"]["items"] == {"$ref": "#/components/schemas/User"}
    assert set(page["properties"]) == {"content", "page", "size", "totalElements", "totalPages"}
    comps = g.components()
    assert "User" in comps  # page registers its item schema


def test_ref_registers_derived_by_name() -> None:
    g = gen()
    assert g.ref("UserCreateRequest") == {"$ref": "#/components/schemas/UserCreateRequest"}
    assert set(g.components()) == {"UserCreateRequest"}
    g.ref("OrderPage")
    assert {"OrderPage", "Order"} <= set(g.components())


def test_cycle_safe() -> None:
    a = entity("A", "entity", [field("id", "Long", is_id=True), field("b", "B")])
    b = entity("B", "entity", [field("a", "A"), field("many", "List<A>")])
    g = SchemaGenerator([a, b], "3.1.0")
    g.ref("A")
    comps = g.components()
    assert comps["A"]["properties"]["b"] == {"$ref": "#/components/schemas/B"}
    assert comps["B"]["properties"]["a"] == {"$ref": "#/components/schemas/A"}
    assert comps["B"]["properties"]["many"]["items"] == {"$ref": "#/components/schemas/A"}


def test_cycle_via_relationships_and_collections() -> None:
    g = gen(include_collections={"Order.lines", "OrderLine.order"})
    g.ref("Order")
    g.ref("OrderLine")
    comps = g.components()
    assert comps["Order"]["properties"]["lines"]["items"] == {
        "$ref": "#/components/schemas/OrderLine"
    }


def test_nullable_31_vs_30() -> None:
    ents = [
        entity(
            "N",
            "entity",
            [
                field("a", "String", nullable=True),
                field("e", "UserStatus", nullable=True, enumerated=True),
                field("r", "Other", nullable=True),
                field("req", "String", nullable=True, not_null=True),
            ],
        ),
        entity("Other", "dto", [field("x", "String")]),
        entity("UserStatus", "enum", enum=["A"]),
    ]
    p31 = SchemaGenerator(ents, "3.1.0").schema_for("N")["properties"]
    p30 = SchemaGenerator(ents, "3.0.3").schema_for("N")["properties"]
    assert p31["a"] == {"type": ["string", "null"]}
    assert p30["a"] == {"type": "string", "nullable": True}
    assert p31["e"]["enum"] == ["A", None]
    assert p31["r"] == {"oneOf": [{"$ref": "#/components/schemas/Other"}, {"type": "null"}]}
    assert p30["r"] == {"allOf": [{"$ref": "#/components/schemas/Other"}], "nullable": True}
    assert p31["req"] == {"type": "string"}


def test_array_size_constraints_and_transient() -> None:
    ents = [
        entity(
            "S",
            "entity",
            [
                field("tags", "List<String>", size_min=1, size_max=5),
                field("skip", "String", transient=True),
            ],
        )
    ]
    p = SchemaGenerator(ents, "3.1.0").schema_for("S")["properties"]
    assert p["tags"]["minItems"] == 1 and p["tags"]["maxItems"] == 5
    assert "skip" not in p


def test_unknown_schema() -> None:
    with pytest.raises(UnknownSchema):
        gen().schema_for("Nope")
    with pytest.raises(UnknownSchema):
        gen().ref("Nope")
    lenient = gen(lenient=True)
    assert lenient.schema_for("Nope") == {"type": "object"}
    lenient.ref("Nope")
    assert lenient.components()["Nope"] == {"type": "object"}


def test_enum_entity_schema() -> None:
    assert gen().schema_for("UserStatus") == {
        "type": "string",
        "enum": ["ACTIVE", "SUSPENDED", "CLOSED"],
    }
    with pytest.raises(UnknownSchema):
        gen().schema_for("UserStatus", "patch")


def test_inline_schema_from_fieldspecs() -> None:
    g = gen()
    fields = [
        FieldSpec(name="code", type="string", required=True, description="ISO code"),
        FieldSpec(name="day", type="string", format="date"),
        FieldSpec(name="kind", enum=["A", "B"]),
        FieldSpec(name="user", type="User"),
        FieldSpec(name="orders", type="array", items_ref="Order"),
        FieldSpec(name="tags", type="array", items_ref="string"),
        FieldSpec(name="blob", type="binary"),
    ]
    s = g.inline_schema(fields, ["day"])
    assert s["required"] == ["code", "day"]
    props = s["properties"]
    assert props["code"] == {"description": "ISO code", "type": "string"}
    assert props["day"] == {"format": "date", "type": "string"}
    assert props["kind"]["enum"] == ["A", "B"]
    assert props["user"] == {"$ref": "#/components/schemas/User"}
    assert props["orders"]["items"] == {"$ref": "#/components/schemas/Order"}
    assert props["tags"]["items"] == {"type": "string"}
    assert props["blob"] == {"format": "binary", "type": "string"}


def test_camel() -> None:
    assert camel("order_number") == "orderNumber"
    assert camel("OrderNumber") == "orderNumber"
    assert camel("orderNumber") == "orderNumber"
    assert camel("URL") == "URL"
