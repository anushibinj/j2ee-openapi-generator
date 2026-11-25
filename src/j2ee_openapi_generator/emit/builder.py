"""Build the single bundled OpenAPI document from a ``StoreSnapshot`` (pure, deterministic)."""

from __future__ import annotations

import re
from typing import Any

from j2ee_openapi_generator.aggregate.models import OperationRow, StoreSnapshot
from j2ee_openapi_generator.aggregate.schema_gen import SchemaGenerator, sort_schema
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.llm.schemas import Operation, ParamSpec, RequestBody, ResponseSpec

METHOD_ORDER = ["get", "post", "put", "patch", "delete"]
_PROBLEM = "application/problem+json"
_PRIMITIVES = {"string", "integer", "number", "boolean"}
_PAGING_NAMES = {"page", "size", "sort"}
_PARAM_TYPES = {"string", "integer", "number", "boolean", "array"}
_PACKET_ID = re.compile(r"^([^:#]+):([^#]+)#(.+)$")

_ERROR_COMPONENT = {
    400: "BadRequest",
    401: "Unauthorized",
    403: "Forbidden",
    404: "NotFound",
    409: "Conflict",
    412: "PreconditionFailed",
    500: "InternalServerError",
}
_ERROR_TEXT = {
    "BadRequest": "The request is malformed or failed validation.",
    "Unauthorized": "Authentication is missing or invalid.",
    "Forbidden": "The caller lacks a required role.",
    "NotFound": "The requested resource does not exist.",
    "Conflict": "The request conflicts with the current state of the resource.",
    "PreconditionFailed": "The If-Match precondition failed (stale version).",
    "InternalServerError": "Unexpected server error.",
}
_STATUS_TEXT = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
}


class DanglingRef(ValueError):
    """Raised when a ``$ref`` is external or points to a component that does not exist."""


# ---------------------------------------------------------------------------- ref checking
def _walk_refs(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v)
            else:
                _walk_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_refs(v, out)


def collect_refs(doc: Any) -> list[str]:
    refs: list[str] = []
    _walk_refs(doc, refs)
    return refs


def check_refs(doc: dict[str, Any]) -> None:
    """Assert the document is self contained: every ``$ref`` is local and resolves."""
    problems: list[str] = []
    for ref in sorted(set(collect_refs(doc))):
        if not ref.startswith("#/"):
            problems.append(f"external reference {ref}")
            continue
        node: Any = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                problems.append(f"dangling reference {ref}")
                break
    if problems:
        raise DanglingRef("; ".join(problems))


# ---------------------------------------------------------------------------- helpers
def _param_names(path: str) -> list[str]:
    return re.findall(r"\{([^}/]+)\}", path)


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ses") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _pascal(text: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_ ]+", text) if p)


def _shape(method: str, path: str) -> str:
    segs = [s for s in path.strip("/").split("/") if s]
    last_param = bool(segs) and segs[-1].startswith("{")
    if method == "GET":
        return "item" if last_param else "collection"
    if method == "POST":
        if len(segs) >= 2 and segs[-2].startswith("{") and not last_param:
            return "action"
        return "create"
    if method in ("PUT", "PATCH"):
        return "update"
    return "delete"


def derive_operation_id(method: str, path: str) -> str:
    segs = [s for s in path.strip("/").split("/") if s]
    lits = [s for s in segs if not s.startswith("{")]
    shape = _shape(method, path)
    if shape == "action" and len(lits) >= 2:
        words = _pascal(lits[-1])
        return words[:1].lower() + words[1:] + _pascal(_singular(lits[-2]))
    verb = {
        "collection": "list",
        "item": "get",
        "create": "create",
        "update": "update",
        "delete": "delete",
        "action": "run",
    }[shape]
    noun = "".join(_pascal(s) for s in lits)
    if shape in ("item", "create", "update", "delete") and lits:
        noun = "".join(_pascal(s) for s in lits[:-1]) + _pascal(_singular(lits[-1]))
    return verb + noun


def _legacy_entry(op: Operation) -> dict[str, Any]:
    ls = op.legacy_source
    return {
        "module": ls.module,
        "servlet": ls.servlet,
        "op": ls.op,
        "httpMethods": sorted(ls.http_methods),
        "jsps": sorted(ls.jsps),
    }


def legacy_sources(row: OperationRow) -> dict[str, Any] | list[dict[str, Any]]:
    primary = _legacy_entry(row.operation)
    pkey = (primary["module"], primary["servlet"], primary["op"])
    entries: dict[tuple[str, str, str], dict[str, Any]] = {pkey: primary}
    for pid in row.packet_ids:
        m = _PACKET_ID.match(pid)
        if m and (m.group(1), m.group(2), m.group(3)) not in entries:
            key = (m.group(1), m.group(2), m.group(3))
            entries[key] = {"module": key[0], "servlet": key[1], "op": key[2]}
    if len(entries) == 1:
        return primary
    return [entries[k] for k in sorted(entries)]


# ---------------------------------------------------------------------------- builder
class _Builder:
    def __init__(self, snapshot: StoreSnapshot, cfg: Config) -> None:
        self.snapshot = snapshot
        self.cfg = cfg
        self.version = cfg.openapi.version
        self.gen = SchemaGenerator(snapshot.entities, self.version, lenient=True)
        self.used_params: set[str] = set()
        self.used_responses: set[str] = set()
        self.versioned_paths = self._versioned_paths()

    # --- versioning
    def _op_schema_names(self, op: Operation) -> list[str]:
        names: list[str] = []
        if op.request_body and op.request_body.schema_ref:
            names.append(op.request_body.schema_ref)
        names += [r.schema_ref for r in op.responses if r.schema_ref]
        return names

    def _versioned_paths(self) -> set[str]:
        out: set[str] = set()
        for row in self.snapshot.operations:
            for name in self._op_schema_names(row.operation):
                ent = self.gen.base_entity(name)
                if ent is not None and any(f.is_version for f in ent.fields):
                    out.add(row.path)
        return out

    # --- schemas
    def _schema(
        self, ref: str | None, fields: list[Any], required: list[str] | None = None
    ) -> dict[str, Any]:
        req = required or []
        if ref in _PRIMITIVES:
            return {"type": ref}
        if ref == "binary":
            return {"type": "string", "format": "binary"}
        if ref and self.gen.knows(ref):
            if not fields or {f.name for f in fields} <= self.gen.property_names(ref):
                return self.gen.ref(ref)
        if fields:
            schema = self.gen.inline_schema(fields, req)
            if ref:
                return self.gen.add_schema(ref, schema)
            return schema
        if ref:
            return self.gen.ref(ref)
        return {"type": "object"}

    def _page_schema(self, ref: str | None, inner: dict[str, Any]) -> dict[str, Any]:
        if ref and self.gen.knows(f"{ref}Page"):
            return self.gen.ref(f"{ref}Page")
        item = inner
        wrapper = {
            "type": "object",
            "properties": {
                "content": {"type": "array", "items": item},
                "page": {"type": "integer", "format": "int32", "minimum": 0},
                "size": {"type": "integer", "format": "int32", "minimum": 1},
                "totalElements": {"type": "integer", "format": "int64", "minimum": 0},
                "totalPages": {"type": "integer", "format": "int32", "minimum": 0},
            },
            "required": ["content", "page", "size", "totalElements", "totalPages"],
        }
        return self.gen.add_schema(f"{ref or 'Items'}Page", sort_schema(wrapper))

    # --- parameters
    def _param(self, p: ParamSpec, where: str) -> dict[str, Any]:
        t = p.type if p.type in _PARAM_TYPES else "string"
        schema: dict[str, Any] = {"type": t}
        if t == "array":
            schema["items"] = {"type": "string"}
        if p.format and t != "array":
            schema["format"] = p.format
        if p.enum:
            schema["enum"] = list(p.enum)
        if p.default is not None:
            schema["default"] = p.default
        out: dict[str, Any] = {"name": p.name, "in": where}
        if p.description:
            out["description"] = p.description
        out["required"] = True if where == "path" else p.required
        out["schema"] = schema
        if t == "array" and where == "query":
            out["style"] = "form"
            out["explode"] = True
        return out

    def _use_param(self, name: str) -> dict[str, str]:
        self.used_params.add(name)
        return {"$ref": f"#/components/parameters/{name}"}

    def _parameters(
        self, op: Operation, path: str, paginated: bool, if_match: bool
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        declared = {p.name: p for p in op.path_params}
        for name in _param_names(path):
            p = declared.get(name) or ParamSpec(name=name, required=True)
            out.append(self._param(p, "path"))
        seen: set[str] = set()
        for q in op.query_params:
            if q.name in seen or (paginated and q.name in _PAGING_NAMES):
                continue
            seen.add(q.name)
            out.append(self._param(q, "query"))
        if paginated:
            out += [self._use_param("Page"), self._use_param("Size"), self._use_param("Sort")]
        if if_match:
            out.append(self._use_param("IfMatch"))
        return out

    # --- request body
    def _request_body(self, rb: RequestBody) -> dict[str, Any]:
        ct = rb.content_type or "application/json"
        if ct.startswith("multipart/"):
            props: dict[str, Any] = {}
            for f in rb.fields:
                props[f.name] = self.gen.inline_schema([f], [])["properties"][f.name]
            if rb.schema_ref == "binary" or not props:
                props.setdefault("file", {"type": "string", "format": "binary"})
            schema: dict[str, Any] = {"type": "object", "properties": dict(sorted(props.items()))}
            req = sorted(set(rb.required) & set(props))
            if req:
                schema["required"] = req
        else:
            schema = self._schema(rb.schema_ref, list(rb.fields), list(rb.required))
        return {"required": True, "content": {ct: {"schema": schema}}}

    # --- responses
    def _has_body(self, r: ResponseSpec) -> bool:
        return bool(r.schema_ref or r.fields or r.content_type)

    def _content(self, r: ResponseSpec) -> dict[str, Any] | None:
        ct = r.content_type
        if not self._has_body(r):
            return None
        if r.schema_ref == "binary" or (
            not r.schema_ref and not r.fields and ct and not ct.startswith("application/json")
        ):
            return {
                ct or "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        inner = self._schema(r.schema_ref, list(r.fields))
        if r.paginated:
            schema = self._page_schema(r.schema_ref, inner)
        elif r.is_array:
            schema = {"type": "array", "items": inner}
        else:
            schema = inner
        return {ct or "application/json": {"schema": schema}}

    def _success_response(
        self, status: int, spec: ResponseSpec | None, headers: dict[str, Any]
    ) -> dict[str, Any]:
        desc = (spec.description if spec else "") or _STATUS_TEXT.get(status, "Success")
        out: dict[str, Any] = {"description": desc}
        content = self._content(spec) if spec and status != 204 else None
        hdrs = dict(headers)
        if content and any(
            isinstance(v["schema"], dict) and v["schema"].get("format") == "binary"
            for v in content.values()
        ):
            hdrs["Content-Disposition"] = {
                "description": "Attachment file name of the download.",
                "schema": {"type": "string"},
            }
        if hdrs:
            out["headers"] = dict(sorted(hdrs.items()))
        if content:
            out["content"] = content
        return out

    def _pick(self, proposed: list[ResponseSpec], prefer: list[int]) -> ResponseSpec | None:
        for s in prefer:
            for r in proposed:
                if r.status == s:
                    return r
        return proposed[0] if proposed else None

    def _responses(
        self, op: Operation, row: OperationRow, shape: str, versioned: bool
    ) -> tuple[dict[str, Any], bool]:
        method = row.method.upper()
        proposed = [r for r in op.responses if 200 <= r.status < 300]
        errors = {r.status: r for r in op.responses if r.status >= 400}
        location = {
            "description": "URI of the created resource.",
            "schema": {"type": "string", "format": "uri-reference"},
        }
        etag = {"description": "Entity tag of the current version.", "schema": {"type": "string"}}
        out: dict[str, Any] = {}
        status: int
        headers: dict[str, Any] = {}
        spec: ResponseSpec | None
        if shape in ("collection", "item"):
            status, spec = 200, self._pick(proposed, [200])
            if shape == "item" and versioned:
                headers["ETag"] = etag
        elif shape == "create":
            status, spec = 201, self._pick(proposed, [201, 200])
            headers["Location"] = location
        elif shape == "delete":
            status, spec = 204, None
        elif shape == "update":
            s200 = self._pick([r for r in proposed if r.status == 200], [200])
            if s200 is not None and self._has_body(s200):
                status, spec = 200, s200
                if versioned:
                    headers["ETag"] = etag
            else:
                status, spec = 204, None
        else:
            allowed = [r for r in proposed if r.status in (200, 201, 202, 204)]
            spec = allowed[0] if allowed else None
            status = spec.status if spec else 200
            if status == 201:
                headers["Location"] = location
        out[str(status)] = self._success_response(status, spec, headers)
        # error responses
        want = {400, 401, 403, 500}
        if "{" in row.path:
            want.add(404)
        if 409 in errors:
            want.add(409)
        needs_precondition = method in ("PUT", "PATCH", "DELETE") and (versioned or 412 in errors)
        if needs_precondition:
            want.add(412)
        for code in sorted(want):
            name = _ERROR_COMPONENT[code]
            self.used_responses.add(name)
            out[str(code)] = {"$ref": f"#/components/responses/{name}"}
        for code in sorted(errors):
            if code in _ERROR_COMPONENT or code in want:
                continue
            spec_e = errors[code]
            out[str(code)] = {
                "description": spec_e.description or f"Error {code}",
                "content": {_PROBLEM: {"schema": self.gen.ref("Problem")}},
            }
        return dict(sorted(out.items())), needs_precondition

    # --- operations
    def operation(self, row: OperationRow) -> dict[str, Any]:
        op = row.operation
        method = row.method.upper()
        shape = _shape(method, row.path)
        versioned = row.path in self.versioned_paths
        paginated = shape == "collection" and any(
            r.paginated for r in op.responses if 200 <= r.status < 300
        )
        responses, if_match = self._responses(op, row, shape, versioned)
        doc: dict[str, Any] = {
            "operationId": row.operation_id or derive_operation_id(method, row.path),
            "tags": [op.tag],
            "summary": op.summary,
        }
        if row.description:
            doc["description"] = row.description
        params = self._parameters(op, row.path, paginated, if_match)
        if params:
            doc["parameters"] = params
        if op.request_body is not None and method in ("POST", "PUT", "PATCH"):
            doc["requestBody"] = self._request_body(op.request_body)
        doc["responses"] = responses
        roles = sorted({r.name for r in op.roles})
        if roles:
            doc["x-roles"] = roles
        if self.cfg.openapi.provenance:
            doc["x-legacy-source"] = legacy_sources(row)
        return doc

    # --- components
    def _problem_schemas(self) -> None:
        codes = sorted(set(self.snapshot.error_codes.values()))
        code: dict[str, Any] = {"type": "string"}
        if codes:
            code["description"] = "Stable machine readable error code. Known codes: " + (
                ", ".join(codes) + "."
            )
            code["enum"] = codes
        else:
            code["description"] = "Stable machine readable error code in UPPER_SNAKE_CASE."
        problem = {
            "type": "object",
            "description": "Problem details (RFC 9457).",
            "properties": {
                "type": {"type": "string", "format": "uri-reference"},
                "title": {"type": "string"},
                "status": {"type": "integer", "format": "int32"},
                "detail": {"type": "string"},
                "instance": {"type": "string", "format": "uri-reference"},
                "code": code,
                "errors": {"type": "array", "items": self.gen.ref("ProblemError")},
            },
            "required": ["status", "title"],
        }
        self.gen.add_schema("Problem", sort_schema(problem))

    def _register_problem_error(self) -> None:
        self.gen.add_schema(
            "ProblemError",
            sort_schema(
                {
                    "type": "object",
                    "description": "One invalid field of a request.",
                    "properties": {
                        "field": {"type": "string"},
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["code", "message"],
                }
            ),
        )

    def _parameter_components(self) -> dict[str, Any]:
        all_params: dict[str, Any] = {
            "IfMatch": {
                "name": "If-Match",
                "in": "header",
                "description": "Entity tag of the version the caller last saw.",
                "required": True,
                "schema": {"type": "string"},
            },
            "Page": {
                "name": "page",
                "in": "query",
                "description": "Zero based page index.",
                "required": False,
                "schema": {"type": "integer", "format": "int32", "minimum": 0, "default": 0},
            },
            "Size": {
                "name": "size",
                "in": "query",
                "description": "Page size.",
                "required": False,
                "schema": {
                    "type": "integer",
                    "format": "int32",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "Sort": {
                "name": "sort",
                "in": "query",
                "description": "Sort order as `field,asc` or `field,desc`; repeatable.",
                "required": False,
                "style": "form",
                "explode": True,
                "schema": {"type": "array", "items": {"type": "string"}},
            },
        }
        return {k: all_params[k] for k in sorted(self.used_params)}

    def _response_components(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in sorted(self.used_responses):
            out[name] = {
                "description": _ERROR_TEXT[name],
                "content": {_PROBLEM: {"schema": self.gen.ref("Problem")}},
            }
        return out

    def build(self) -> dict[str, Any]:
        rows = sorted(
            self.snapshot.operations,
            key=lambda r: (
                r.path,
                METHOD_ORDER.index(r.method.lower()) if r.method.lower() in METHOD_ORDER else 99,
                r.method,
            ),
        )
        if rows:
            self._register_problem_error()
            self._problem_schemas()
        prefix = self.cfg.openapi.api_prefix.rstrip("/")
        paths: dict[str, dict[str, Any]] = {}
        tags: set[str] = set()
        for row in rows:
            full = prefix + row.path
            paths.setdefault(full, {})[row.method.lower()] = self.operation(row)
            tags.add(row.operation.tag)
        tag_list: list[dict[str, str]] = []
        for t in sorted(tags):
            entry = {"name": t}
            desc = self.snapshot.tag_descriptions.get(t)
            if desc:
                entry["description"] = desc
            tag_list.append(entry)
        doc: dict[str, Any] = {
            "openapi": self.version,
            "info": {"title": self.cfg.openapi.title, "version": "1.0.0"},
        }
        if self.cfg.openapi.servers:
            doc["servers"] = [dict(sorted(s.items())) for s in self.cfg.openapi.servers]
        doc["security"] = [{"bearerAuth": []}]
        doc["tags"] = tag_list
        doc["paths"] = paths
        components: dict[str, Any] = {"schemas": self.gen.components()}
        params = self._parameter_components()
        if params:
            components["parameters"] = params
        responses = self._response_components()
        if responses:
            components["responses"] = responses
        components["securitySchemes"] = {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
        }
        doc["components"] = components
        check_refs(doc)
        return doc


def build_openapi(snapshot: StoreSnapshot, cfg: Config) -> dict[str, Any]:
    """Build the OpenAPI document as a plain dict (deterministic, self contained)."""
    return _Builder(snapshot, cfg).build()
