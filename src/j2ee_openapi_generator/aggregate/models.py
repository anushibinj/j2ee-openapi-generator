"""Aggregate-stage contracts: store rows, review decisions, conflicts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from j2ee_openapi_generator.index.models import EntityRecord
from j2ee_openapi_generator.llm.schemas import EndpointProposal, Operation


class VerifiedProposal(BaseModel):
    """Output of verify for one packet (``.oapi/verified/{id}.json``)."""

    packet_id: str
    status: Literal["verified", "rejected", "no_api_needed", "failed"]
    reasons: list[str] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    no_api_needed: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    lookups_needed: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    attempts: int = 1


class OperationRow(BaseModel):
    """A merged operation in the store (key = method + normalized path)."""

    method: str
    path: str  # normalized template, e.g. ``/users/{id}``
    operation: Operation
    packet_ids: list[str]
    operation_id: str | None = None
    description: str | None = None


class ConflictRow(BaseModel):
    id: str  # stable: sha256-based short id
    kind: Literal["incompatible_duplicate", "ejb_path_mismatch", "review_rejected"]
    summary: str
    packet_ids: list[str]
    proposals: list[dict[str, object]] = Field(default_factory=list)
    resolved: bool = False


class _D(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Decision(_D):
    """One entry of ``review/decisions.yaml``, keyed by packet ID or conflict ID."""

    key: str
    action: Literal["accept", "override", "drop", "rename_path"]
    reason: str | None = None
    overrides: dict[str, object] = Field(default_factory=dict)  # for ``override``
    new_path: str | None = None  # for ``rename_path``
    chosen_packet_id: str | None = None  # conflict resolution: which proposal wins


class DecisionsFile(_D):
    version: int = 1
    decisions: list[Decision] = Field(default_factory=list)


class CoverageRow(BaseModel):
    """Per entry point status for the coverage report."""

    entry_id: str
    status: Literal["mapped", "no_api_needed", "rejected", "conflict", "failed"]
    operations: list[str] = Field(default_factory=list)  # ``METHOD /path``
    usage_count: int = 0
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class StoreSnapshot(BaseModel):
    """Everything emit and report need, read out of the SQLite store. Plain data, sorted."""

    operations: list[OperationRow] = Field(default_factory=list)
    entities: list[EntityRecord] = Field(default_factory=list)
    tag_descriptions: dict[str, str] = Field(default_factory=dict)
    error_codes: dict[str, str] = Field(default_factory=dict)  # properties key -> stable code
    conflicts: list[ConflictRow] = Field(default_factory=list)
    coverage: list[CoverageRow] = Field(default_factory=list)
    open_questions: dict[str, list[str]] = Field(default_factory=dict)  # tag -> questions
    lookup_types: list[str] = Field(default_factory=list)


class ProposalRecord(BaseModel):
    """Analyzer output for one packet (``.oapi/proposals/{id}.json``)."""

    packet_id: str
    attempt: int = 1
    ok: bool
    errors: list[str] = Field(default_factory=list)
    proposal: EndpointProposal | None = None
    cache_key: str = ""
