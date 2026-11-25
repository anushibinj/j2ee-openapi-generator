"""Aggregate-stage contracts: store rows, review decisions, conflicts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from j2ee_openapi_generator.llm.schemas import Operation


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
