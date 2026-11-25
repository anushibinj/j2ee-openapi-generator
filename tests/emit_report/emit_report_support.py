"""Hand-built snapshot and config shared by the emit/report tests."""

from __future__ import annotations

import json
from pathlib import Path

from j2ee_openapi_generator.aggregate.models import (
    ConflictRow,
    CoverageRow,
    OperationRow,
    StoreSnapshot,
)
from j2ee_openapi_generator.config import Config, OpenApiConfig
from j2ee_openapi_generator.index.models import EntityField, EntityRecord
from j2ee_openapi_generator.llm.schemas import EndpointProposal, Operation, RenameMap

LLM = Path(__file__).parents[1] / "fixtures" / "llm"


def field(name: str, java_type: str, **kw: object) -> EntityField:
    return EntityField(name=name, java_type=java_type, **kw)  # type: ignore[arg-type]


def entity(
    name: str, kind: str, fields: list[EntityField] | None = None, enum: list[str] | None = None
) -> EntityRecord:
    return EntityRecord(
        fqcn=f"com.acme.{name}",
        simple_name=name,
        kind=kind,  # type: ignore[arg-type]
        file=f"{name}.java",
        start_line=1,
        end_line=10,
        fields=fields or [],
        enum_values=enum or [],
    )


def entities() -> list[EntityRecord]:
    user = entity(
        "User",
        "entity",
        [
            field("id", "Long", is_id=True),
            field("version", "Long", is_version=True),
            field("name", "String", not_null=True, size_min=2, size_max=80, length=80),
            field("email", "String", not_null=True, pattern="[^@]+@[^@]+", length=120),
            field("status", "UserStatus", enumerated=True, nullable=False),
            field("birthDate", "Date"),
            field("creditLimit", "BigDecimal"),
            field("locale", "String", nullable=True),
            field("phones", "List<Phone>", relationship="OneToMany", target_type="Phone"),
        ],
    )
    phone = entity(
        "Phone",
        "entity",
        [
            field("id", "Long", is_id=True),
            field("user", "User", relationship="ManyToOne", target_type="User"),
            field("number", "String", pattern="[0-9+ ]+"),
        ],
    )
    customer = entity(
        "Customer",
        "entity",
        [field("id", "Long", is_id=True), field("name", "String", not_null=True)],
    )
    order = entity(
        "Order",
        "entity",
        [
            field("id", "Long", is_id=True),
            field("version", "Long", is_version=True),
            field("orderNumber", "String", pattern="ORD-[0-9]{4}-[0-9]{4}", length=13),
            field("customer", "Customer", relationship="ManyToOne", target_type="Customer",
                  not_null=True),
            field("status", "OrderStatus", enumerated=True, nullable=False),
            field("total", "BigDecimal", nullable=False),
            field("orderDate", "Date"),
            field("notes", "String", size_max=500, length=500),
            field("lines", "List<OrderLine>", relationship="OneToMany", target_type="OrderLine"),
        ],
    )
    line = entity(
        "OrderLine",
        "entity",
        [
            field("id", "Long", is_id=True),
            field("order", "Order", relationship="ManyToOne", target_type="Order"),
            field("quantity", "int", nullable=False),
            field("unitPrice", "BigDecimal"),
        ],
    )
    draft = entity(
        "OrderDraft",
        "dto",
        [
            field("customerId", "Long", not_null=True),
            field("notes", "String", size_max=500),
            field("lines", "List<OrderLine>", elem_type="OrderLine"),
        ],
    )
    summary = entity(
        "OrderSummaryDto",
        "dto",
        [
            field("orderNumber", "String"),
            field("customerName", "String"),
            field("total", "BigDecimal"),
        ],
    )
    return [
        user,
        phone,
        customer,
        order,
        line,
        draft,
        summary,
        entity("UserStatus", "enum", enum=["ACTIVE", "SUSPENDED", "CLOSED"]),
        entity("OrderStatus", "enum", enum=["DRAFT", "SUBMITTED", "SHIPPED", "CANCELLED"]),
    ]


def _renames() -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for f in sorted((LLM / "_normalizer").glob("*.json")):
        rm = RenameMap.model_validate_json(f.read_text(encoding="utf-8"))
        for o in rm.operations:
            out[o.key] = (o.operation_id, o.description)
    return out


def rows() -> list[OperationRow]:
    renames = _renames()
    result: list[OperationRow] = []
    for f in sorted(LLM.glob("*.json")):
        if f.name.startswith("_") or f.name.endswith(".repair.json"):
            continue
        if f.name == "sales--LookupServlet__statuses.json":
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        if "operations" not in data:
            continue
        prop = EndpointProposal.model_validate(data)
        for op in prop.operations:
            path = op.path
            if prop.packet_id == "sales:OrderServlet#detail":
                path = "/orders/by-number/{orderNumber}"
                op = op.model_copy(update={"path": path})
            key = f"{op.http_method} {path}"
            oid, desc = renames.get(key, (None, None))
            result.append(
                OperationRow(
                    method=op.http_method,
                    path=path,
                    operation=op,
                    packet_ids=[prop.packet_id],
                    operation_id=oid,
                    description=desc,
                )
            )
    return sorted(result, key=lambda r: (r.path, r.method))


def snapshot() -> StoreSnapshot:
    return StoreSnapshot(
        operations=rows(),
        entities=entities(),
        tag_descriptions={
            "Accounts Users": "Operations of the accounts users area.",
            "Sales Orders": "Operations of the sales orders area.",
        },
        error_codes={"error.user.inuse": "USER_IN_USE", "error.user.modified": "USER_MODIFIED"},
        conflicts=[
            ConflictRow(
                id="c1",
                kind="incompatible_duplicate",
                summary="GET /orders/{} claimed twice",
                packet_ids=["sales:OrderServlet#detail", "sales:OrderServlet#view"],
                proposals=[{"path": "/orders/{orderId}", "type": "integer"},
                           {"path": "/orders/{orderId}", "type": "string"}],
            ),
            ConflictRow(
                id="c0",
                kind="review_rejected",
                summary="already handled",
                packet_ids=["x:Y#z"],
                resolved=True,
            ),
        ],
        coverage=[
            CoverageRow(entry_id="sales:OrderServlet#list", status="mapped",
                        operations=["GET /orders"], usage_count=12, confidence=0.9),
            CoverageRow(entry_id="accounts:UserServlet#edit", status="no_api_needed",
                        reasons=["same data as read"], confidence=0.8),
            CoverageRow(entry_id="accounts:UserServlet#save", status="mapped",
                        operations=["POST /users", "PUT /users/{userId}"], usage_count=3,
                        confidence=0.7),
            CoverageRow(entry_id="sales:OrderServlet#view", status="conflict",
                        reasons=["conflicts with detail"]),
        ],
        open_questions={
            "Sales Orders": ["Is the order number unique across customers?", "Zeta question?"],
            "Accounts Users": ["Should users be soft deleted?"],
        },
        lookup_types=["countries", "statuses"],
    )


def config(version: str = "3.1.0", provenance: bool = True) -> Config:
    return Config(
        repo_root=Path("."),
        openapi=OpenApiConfig(
            version=version,  # type: ignore[arg-type]
            title="Acme API",
            provenance=provenance,
            servers=[{"url": "https://api.acme.example"}],
        ),
    )
