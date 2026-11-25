"""Versioned proposal models returned by the analyzer / normalizer / resolver agents.

Bump ``SCHEMA_VERSION`` whenever any model here changes shape; it is part of every cache key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    """A citation the verifier checks mechanically against the packet's numbered lines."""

    claim: str
    file: str
    line: int
    snippet: str


class FieldSpec(_Strict):
    """An inline schema field."""

    name: str
    type: str = "string"  # string|integer|number|boolean|object|array|binary or entity simple name
    format: str | None = None
    required: bool = False
    description: str | None = None
    enum: list[str] = Field(default_factory=list)
    items_ref: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class ParamSpec(_Strict):
    name: str  # API name (camelCase); legacy name goes in ``legacy_name``
    legacy_name: str | None = None
    type: str = "string"
    format: str | None = None
    required: bool = False
    description: str | None = None
    enum: list[str] = Field(default_factory=list)
    default: str | int | bool | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class RequestBody(_Strict):
    content_type: str = "application/json"
    schema_ref: str | None = None  # entity/DTO simple name or derived e.g. ``UserCreateRequest``
    fields: list[FieldSpec] = Field(default_factory=list)
    required: list[str] = Field(default_factory=list)


class ResponseSpec(_Strict):
    status: int
    description: str = ""
    content_type: str | None = None
    schema_ref: str | None = None
    fields: list[FieldSpec] = Field(default_factory=list)
    is_array: bool = False
    paginated: bool = False
    evidence: list[Evidence] = Field(default_factory=list)  # required for non standard statuses


class LegacySource(_Strict):
    module: str
    servlet: str
    op: str
    http_methods: list[str] = Field(default_factory=list)
    jsps: list[str] = Field(default_factory=list)


class RoleSpec(_Strict):
    name: str
    evidence: list[Evidence] = Field(default_factory=list)


class Operation(_Strict):
    http_method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str  # relative to api_prefix, e.g. ``/users/{userId}``
    summary: str
    resource: str  # kebab plural noun
    tag: str
    path_params: list[ParamSpec] = Field(default_factory=list)
    query_params: list[ParamSpec] = Field(default_factory=list)
    request_body: RequestBody | None = None
    responses: list[ResponseSpec] = Field(default_factory=list)
    roles: list[RoleSpec] = Field(default_factory=list)
    idempotent: bool = False
    legacy_source: LegacySource
    ejb_methods: list[str] = Field(default_factory=list)  # ``FQCN#method(paramTypes)``
    evidence: list[Evidence] = Field(default_factory=list)
    hint_override_reason: str | None = None  # required when deviating from the mapping hint


class LookupNeed(_Strict):
    type: str  # kebab, e.g. ``countries``
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)


class EndpointProposal(_Strict):
    packet_id: str
    operations: list[Operation] = Field(default_factory=list)
    no_api_needed: str | None = None  # reason, or null
    lookups_needed: list[LookupNeed] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# ------------------------------------------------------------------ normalizer
class OperationRename(_Strict):
    key: str  # ``METHOD /path``
    operation_id: str
    summary: str | None = None
    description: str | None = None


class TagDescription(_Strict):
    tag: str
    description: str


class RenameMap(_Strict):
    tag: str
    operations: list[OperationRename] = Field(default_factory=list)
    tag_descriptions: list[TagDescription] = Field(default_factory=list)


# ------------------------------------------------------------------ resolver
class ConflictSuggestion(_Strict):
    conflict_id: str
    action: Literal["accept", "drop", "rename_path", "override"]
    chosen_packet_id: str | None = None
    new_path: str | None = None
    rationale: str


class ResolverOutput(_Strict):
    suggestions: list[ConflictSuggestion] = Field(default_factory=list)
