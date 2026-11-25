from __future__ import annotations

from typing import Any

import pytest
from emit_report_support import config, entities, snapshot

from j2ee_openapi_generator.aggregate.models import OperationRow, StoreSnapshot
from j2ee_openapi_generator.emit.builder import (
    DanglingRef,
    build_openapi,
    check_refs,
    collect_refs,
    derive_operation_id,
)
from j2ee_openapi_generator.llm.schemas import (
    FieldSpec,
    LegacySource,
    Operation,
    ParamSpec,
    RequestBody,
    ResponseSpec,
    RoleSpec,
)

STD = {"400", "401", "403", "500"}


def op(
    method: str,
    path: str,
    responses: list[ResponseSpec],
    body: RequestBody | None = None,
    **kw: Any,
) -> OperationRow:
    names = [s[1:-1] for s in path.split("/") if s.startswith("{")]
    operation = Operation(
        http_method=method,  # type: ignore[arg-type]
        path=path,
        summary=f"{method} {path}",
        resource="things",
        tag="Things",
        path_params=[
            ParamSpec(name=n, type="integer", format="int64", required=True) for n in names
        ],
        request_body=body,
        responses=responses,
        legacy_source=LegacySource(module="m", servlet="S", op="o", http_methods=["GET"]),
        **kw,
    )
    return OperationRow(method=method, path=path, operation=operation, packet_ids=["m:S#o"])


def r(status: int, ref: str | None = None, **kw: Any) -> ResponseSpec:
    return ResponseSpec(status=status, schema_ref=ref, description=f"s{status}", **kw)


def build(rows: list[OperationRow], version: str = "3.1.0", prov: bool = True) -> dict[str, Any]:
    snap = StoreSnapshot(operations=rows, entities=entities())
    return build_openapi(snap, config(version, prov))


def codes(doc: dict[str, Any], path: str, method: str) -> set[str]:
    return set(doc["paths"]["/v1" + path][method]["responses"])


def test_key_order_and_prefix() -> None:
    doc = build_openapi(snapshot(), config())
    assert list(doc)[:7] == [
        "openapi",
        "info",
        "servers",
        "security",
        "tags",
        "paths",
        "components",
    ]
    assert list(doc["components"]) == ["schemas", "parameters", "responses", "securitySchemes"]
    assert doc["security"] == [{"bearerAuth": []}]
    assert doc["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert all(p.startswith("/v1/") for p in doc["paths"])
    assert list(doc["paths"]) == sorted(doc["paths"])
    assert [t["name"] for t in doc["tags"]] == sorted(t["name"] for t in doc["tags"])
    users = doc["paths"]["/v1/users/{userId}"]
    assert list(users) == ["get", "put", "delete"]
    assert list(doc["components"]["schemas"]) == sorted(doc["components"]["schemas"])


def test_snapshot_every_op_has_standard_errors_and_valid_refs() -> None:
    doc = build_openapi(snapshot(), config())
    for path, item in doc["paths"].items():
        for method, o in item.items():
            assert STD <= set(o["responses"]), (path, method)
            assert o["operationId"]
            has_param = "{" in path
            assert ("404" in o["responses"]) == has_param, (path, method)
    check_refs(doc)
    assert all(ref.startswith("#/") for ref in collect_refs(doc))


def test_get_collection_and_paginated() -> None:
    doc = build([op("GET", "/things", [r(200, "User", paginated=True)])])
    o = doc["paths"]["/v1/things"]["get"]
    assert codes(doc, "/things", "get") == {"200"} | STD
    assert o["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserPage"
    }
    names = [p.get("$ref") for p in o["parameters"]]
    assert names == [f"#/components/parameters/{n}" for n in ("Page", "Size", "Sort")]
    sort = doc["components"]["parameters"]["Sort"]
    assert sort["schema"]["type"] == "array"
    assert sort["style"] == "form" and sort["explode"] is True
    assert "UserPage" in doc["components"]["schemas"] and "User" in doc["components"]["schemas"]


def test_paginated_drops_duplicate_query_params() -> None:
    row = op("GET", "/things", [r(200, "User", paginated=True)])
    row.operation.query_params = [ParamSpec(name="page"), ParamSpec(name="name")]
    doc = build([row])
    params = doc["paths"]["/v1/things"]["get"]["parameters"]
    assert [p.get("name") for p in params if "name" in p] == ["name"]


def test_get_item() -> None:
    doc = build([op("GET", "/things/{id}", [r(200, "Customer"), r(404, "Problem")])])
    assert codes(doc, "/things/{id}", "get") == {"200", "404"} | STD
    resp = doc["paths"]["/v1/things/{id}"]["get"]["responses"]["200"]
    assert "headers" not in resp  # Customer has no @Version


def test_post_create_location() -> None:
    row = op(
        "POST",
        "/things",
        [r(200, "Customer")],
        RequestBody(schema_ref="CustomerCreateRequest"),
    )
    doc = build([row])
    resp = doc["paths"]["/v1/things"]["post"]["responses"]
    assert "201" in resp and "200" not in resp
    assert "Location" in resp["201"]["headers"]
    assert doc["paths"]["/v1/things"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/CustomerCreateRequest"}


def test_put_patch_200_or_204() -> None:
    doc = build(
        [
            op("PUT", "/things/{id}", [r(200, "Customer")], RequestBody(schema_ref="Customer")),
            op("PATCH", "/things/{id}", [r(204)], RequestBody(schema_ref="CustomerPatch")),
            op("PUT", "/other/{id}", [], RequestBody(schema_ref="Customer")),
        ]
    )
    put = doc["paths"]["/v1/things/{id}"]["put"]["responses"]
    patch = doc["paths"]["/v1/things/{id}"]["patch"]["responses"]
    assert "200" in put and "204" not in put and "content" in put["200"]
    assert "204" in patch and "200" not in patch and "content" not in patch["204"]
    assert "204" in doc["paths"]["/v1/other/{id}"]["put"]["responses"]


def test_delete_204_and_409() -> None:
    doc = build([op("DELETE", "/things/{id}", [r(200), r(409, "Problem")])])
    resp = doc["paths"]["/v1/things/{id}"]["delete"]["responses"]
    assert set(resp) == {"204", "404", "409"} | STD
    assert resp["409"] == {"$ref": "#/components/responses/Conflict"}


def test_action_endpoints() -> None:
    doc = build(
        [
            op("POST", "/things/{id}/submit", []),
            op("POST", "/things/{id}/start", [r(202)]),
            op("POST", "/things/{id}/clear", [r(204)]),
            op("POST", "/things/{id}/spawn", [r(201, "Customer")]),
        ]
    )
    assert "200" in codes(doc, "/things/{id}/submit", "post")
    assert "202" in codes(doc, "/things/{id}/start", "post")
    assert "204" in codes(doc, "/things/{id}/clear", "post")
    spawn = doc["paths"]["/v1/things/{id}/spawn"]["post"]["responses"]
    assert "201" in spawn and "Location" in spawn["201"]["headers"]


def test_version_etag_if_match_412() -> None:
    doc = build(
        [
            op("GET", "/users/{id}", [r(200, "User")]),
            op("PUT", "/users/{id}", [r(200, "User")], RequestBody(schema_ref="UserUpdateRequest")),
            op("PATCH", "/users/{id}", [r(200, "User")], RequestBody(schema_ref="UserPatch")),
            op("DELETE", "/users/{id}", [r(204)]),
            op("GET", "/plain/{id}", [r(200, "Customer")]),
        ]
    )
    item = doc["paths"]["/v1/users/{id}"]
    assert "ETag" in item["get"]["responses"]["200"]["headers"]
    for m in ("put", "patch", "delete"):
        assert {"$ref": "#/components/parameters/IfMatch"} in item[m]["parameters"]
        assert item[m]["responses"]["412"] == {"$ref": "#/components/responses/PreconditionFailed"}
    assert "parameters" not in doc["paths"]["/v1/users/{id}"]["get"] or all(
        "IfMatch" not in str(p) for p in item["get"]["parameters"]
    )
    assert "412" not in doc["paths"]["/v1/plain/{id}"]["get"]["responses"]
    assert doc["components"]["parameters"]["IfMatch"]["name"] == "If-Match"
    assert "PreconditionFailed" in doc["components"]["responses"]


def test_no_if_match_without_version() -> None:
    doc = build(
        [op("PUT", "/things/{id}", [r(200, "Customer")], RequestBody(schema_ref="Customer"))]
    )
    assert "parameters" not in doc["components"] or "IfMatch" not in doc["components"]["parameters"]
    assert "412" not in codes(doc, "/things/{id}", "put")


def test_binary_and_csv_response() -> None:
    row = op("GET", "/things/export", [r(200, "binary", content_type="text/csv")])
    doc = build([row])
    resp = doc["paths"]["/v1/things/export"]["get"]["responses"]["200"]
    assert resp["content"] == {"text/csv": {"schema": {"type": "string", "format": "binary"}}}
    assert "Content-Disposition" in resp["headers"]


def test_multipart_upload() -> None:
    body = RequestBody(content_type="multipart/form-data", schema_ref="binary")
    doc = build([op("POST", "/things/import", [r(200)], body)])
    schema = doc["paths"]["/v1/things/import"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    assert schema["properties"]["file"] == {"type": "string", "format": "binary"}


def test_inline_fields_and_array_responses() -> None:
    fields = [FieldSpec(name="code", required=True), FieldSpec(name="name")]
    doc = build(
        [
            op("GET", "/things", [r(200, "Country", fields=fields, is_array=True)]),
            op("GET", "/things/names", [r(200, "string", is_array=True)]),
        ]
    )
    schema = doc["paths"]["/v1/things"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert schema == {"type": "array", "items": {"$ref": "#/components/schemas/Country"}}
    assert doc["components"]["schemas"]["Country"]["required"] == ["code"]
    names = doc["paths"]["/v1/things/names"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert names == {"type": "array", "items": {"type": "string"}}


def test_derived_ref_with_foreign_fields_uses_fields() -> None:
    body = RequestBody(schema_ref="UserPatch", fields=[FieldSpec(name="displayName")])
    doc = build([op("PATCH", "/profile", [r(200, "User")], body)])
    assert "displayName" in doc["components"]["schemas"]["UserPatch"]["properties"]


def test_x_roles_and_provenance_toggle() -> None:
    row = op("GET", "/things", [r(200)], roles=[RoleSpec(name="sales"), RoleSpec(name="admin")])
    on = build([row], prov=True)["paths"]["/v1/things"]["get"]
    off = build([row], prov=False)["paths"]["/v1/things"]["get"]
    assert on["x-roles"] == ["admin", "sales"]
    assert on["x-legacy-source"] == {
        "module": "m",
        "servlet": "S",
        "op": "o",
        "httpMethods": ["GET"],
        "jsps": [],
    }
    assert "x-legacy-source" not in off and "x-roles" in off
    noroles = build([op("GET", "/things", [r(200)])])["paths"]["/v1/things"]["get"]
    assert "x-roles" not in noroles


def test_merged_legacy_sources_is_list() -> None:
    row = op("GET", "/things", [r(200)])
    row.packet_ids = ["m:S#o", "a:B#c"]
    src = build([row])["paths"]["/v1/things"]["get"]["x-legacy-source"]
    assert [s["module"] for s in src] == ["a", "m"]
    assert src[0] == {"module": "a", "servlet": "B", "op": "c"}


def test_operation_id_fallback_and_row_id() -> None:
    assert derive_operation_id("GET", "/users") == "listUsers"
    assert derive_operation_id("GET", "/users/{id}") == "getUser"
    assert derive_operation_id("POST", "/users") == "createUser"
    assert derive_operation_id("PUT", "/users/{id}") == "updateUser"
    assert derive_operation_id("DELETE", "/users/{id}") == "deleteUser"
    assert derive_operation_id("POST", "/orders/drafts/{draftId}/submit") == "submitDraft"
    row = op("GET", "/things", [r(200)])
    row.operation_id = "listThings"
    assert build([row])["paths"]["/v1/things"]["get"]["operationId"] == "listThings"
    assert (
        build([op("GET", "/things", [r(200)])])["paths"]["/v1/things"]["get"]["operationId"]
        == "listThings"
    )


def test_problem_schema_and_error_codes() -> None:
    snap = snapshot()
    doc = build_openapi(snap, config())
    problem = doc["components"]["schemas"]["Problem"]
    assert problem["properties"]["code"]["enum"] == ["USER_IN_USE", "USER_MODIFIED"]
    assert "USER_IN_USE, USER_MODIFIED" in problem["properties"]["code"]["description"]
    assert problem["properties"]["errors"]["items"] == {"$ref": "#/components/schemas/ProblemError"}
    assert set(problem["properties"]) == {
        "type",
        "title",
        "status",
        "detail",
        "instance",
        "code",
        "errors",
    }
    for name, resp in doc["components"]["responses"].items():
        assert list(resp["content"]) == ["application/problem+json"], name
    assert set(doc["components"]["responses"]) >= {
        "BadRequest",
        "Unauthorized",
        "Forbidden",
        "NotFound",
        "Conflict",
        "InternalServerError",
    }


def test_info_servers_tags() -> None:
    doc = build_openapi(snapshot(), config())
    assert doc["info"]["title"] == "Acme API"
    assert doc["servers"] == [{"url": "https://api.acme.example"}]
    tags = {t["name"]: t.get("description") for t in doc["tags"]}
    assert tags["Accounts Users"] == "Operations of the accounts users area."
    assert tags["Sales Lookups"] is None


def test_openapi_30_and_31_differ_only_where_expected() -> None:
    d31 = build_openapi(snapshot(), config("3.1.0"))
    d30 = build_openapi(snapshot(), config("3.0.3"))
    assert d31["openapi"] == "3.1.0" and d30["openapi"] == "3.0.3"
    assert d31["components"]["schemas"]["User"]["properties"]["locale"] == {
        "type": ["string", "null"]
    }
    assert d30["components"]["schemas"]["User"]["properties"]["locale"] == {
        "type": "string",
        "nullable": True,
    }


def test_no_external_refs_and_dangling_raises() -> None:
    doc = build_openapi(snapshot(), config())
    check_refs(doc)
    bad = {"components": {"schemas": {}}, "paths": {"/x": {"$ref": "#/components/schemas/Nope"}}}
    with pytest.raises(DanglingRef, match="Nope"):
        check_refs(bad)
    with pytest.raises(DanglingRef, match="external"):
        check_refs({"a": {"$ref": "other.yaml#/X"}})


def test_empty_snapshot() -> None:
    doc = build_openapi(StoreSnapshot(), config())
    assert doc["paths"] == {} and doc["tags"] == []
    check_refs(doc)


def test_deterministic_regardless_of_input_order() -> None:
    snap = snapshot()
    rev = snap.model_copy(update={"operations": list(reversed(snap.operations))})
    assert build_openapi(snap, config()) == build_openapi(rev, config())
