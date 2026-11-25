"""Work packet contract. One packet per entry point (or per sub-packet when split)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceChunk(BaseModel):
    """A source excerpt. ``text`` is the rendered form including header and ``NNNN| `` prefixes."""

    file: str
    start_line: int
    end_line: int
    role: str  # branch | helper | ejb_interface | ejb_impl | entity | jsp_request | jsp_response
    text: str


class MappingHints(BaseModel):
    http_method: str  # GET/POST/PUT/PATCH/DELETE or ``NONE`` for no_api_needed
    shape: str  # collection | item | action | binary | multipart | no_api_needed
    verb_kebab: str | None = None  # for action sub-resources
    resource_candidates: list[str] = Field(default_factory=list)  # kebab plural nouns
    reason: str = ""


class EntryPointMeta(BaseModel):
    id: str
    module: str
    servlet_fqcn: str
    op: str
    param_name: str
    style: str
    http_methods: list[str]
    url_patterns: list[str]
    context_root: str = ""
    security_roles: list[str] = Field(default_factory=list)
    security_constraints: list[dict[str, object]] = Field(default_factory=list)
    jsps: list[str] = Field(default_factory=list)  # forward targets + submitting JSPs


class WorkPacket(BaseModel):
    id: str
    parent_id: str | None = None  # set on partial sub-packets
    partial: bool = False
    part_index: int = 0
    meta: EntryPointMeta
    chunks: list[SourceChunk] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)
    hints: MappingHints
    usage_count: int = 0
    trimmed: list[str] = Field(default_factory=list)  # which sections were trimmed
    allowed_files: list[str] = Field(default_factory=list)  # files citeable (sorted)
    token_estimate: int = 0
    packet_hash: str = ""  # sha256 of canonical JSON of packet with packet_hash == ""
