"""Token counting, budget arithmetic, trimming and splitting of work packets."""

from __future__ import annotations

import math
import re
from typing import Any

from j2ee_openapi_generator.config import LLMConfig
from j2ee_openapi_generator.ids import sha256_obj
from j2ee_openapi_generator.packets.models import SourceChunk, WorkPacket

_ENCODER: Any = None
_ENCODER_LOADED = False

MARGIN_FRACTION = 0.10
TRIM_ORDER = ("ejb_impl", "jsp_response", "helper")

_NUMBERED = re.compile(r"^\s*(\d+)\| ?(.*)$")
_HEADER = re.compile(r"^=== FILE (.+?) \(lines (\d+)-(\d+)\) ===$")


def _encoder() -> Any:
    """Lazily load tiktoken ``cl100k_base``; ``None`` when it cannot be loaded (offline)."""
    global _ENCODER, _ENCODER_LOADED
    if not _ENCODER_LOADED:
        _ENCODER_LOADED = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def count_tokens(text: str) -> int:
    """Token count with ``cl100k_base``; falls back to ``ceil(len/4)`` when unavailable."""
    enc = _encoder()
    if enc is None:
        return math.ceil(len(text) / 4)
    return len(enc.encode(text, disallowed_special=()))


def token_budget(cfg: LLMConfig, prompt_tokens: int) -> int:
    """Tokens left for packet content: context - prompt - max output - 10% of context."""
    margin = math.ceil(cfg.context_tokens * MARGIN_FRACTION)
    return max(0, cfg.context_tokens - prompt_tokens - cfg.max_output_tokens - margin)


# ------------------------------------------------------------------ chunk text
def format_chunk_text(file: str, numbered: list[tuple[int, str]]) -> str:
    """Header ``=== FILE f (lines a-b) ===`` then ``f"{n:4d}| {text}"`` per line."""
    start, end = numbered[0][0], numbered[-1][0]
    head = f"=== FILE {file} (lines {start}-{end}) ==="
    return "\n".join([head, *(f"{n:4d}| {t}" for n, t in numbered)])


def parse_chunk_text(text: str) -> list[tuple[int, str]]:
    """Inverse of :func:`format_chunk_text` for the numbered lines."""
    out: list[tuple[int, str]] = []
    for line in text.split("\n"):
        m = _NUMBERED.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


# ------------------------------------------------------------------ finalize
def packet_tokens(packet: WorkPacket) -> int:
    """Tokens of the packet as serialized, ignoring the estimate and hash fields."""
    plain = packet.model_copy(update={"token_estimate": 0, "packet_hash": ""})
    return count_tokens(plain.model_dump_json())


def compute_hash(packet: WorkPacket) -> str:
    return sha256_obj(packet.model_copy(update={"packet_hash": ""}).model_dump(mode="json"))


def finalize(packet: WorkPacket) -> WorkPacket:
    """Set allowed_files, token_estimate and packet_hash (in that order)."""
    p = packet.model_copy(
        update={"allowed_files": sorted({c.file for c in packet.chunks}), "packet_hash": ""}
    )
    p = p.model_copy(update={"token_estimate": packet_tokens(p)})
    return p.model_copy(update={"packet_hash": compute_hash(p)})


# ------------------------------------------------------------------ splitting
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def _code_only(text: str) -> str:
    return _STRING.sub('""', text).split("//", 1)[0]


def _blocks_at(numbered: list[tuple[int, str]], base: int) -> list[list[tuple[int, str]]]:
    blocks: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    depth = 0
    for n, text in numbered:
        cur.append((n, text))
        code = _code_only(text)
        depth += code.count("{") - code.count("}")
        if depth <= base and code.strip().endswith((";", "}")):
            blocks.append(cur)
            cur = []
            depth = base if depth > base else depth
    if cur:
        blocks.append(cur)
    # a block that is only closing braces belongs to the previous block
    merged: list[list[tuple[int, str]]] = []
    for blk in blocks:
        if merged and all(set(t.strip()) <= set("});,") for _, t in blk):
            merged[-1].extend(blk)
        else:
            merged.append(blk)
    return merged


def split_blocks(numbered: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Split lines at line boundaries between top-level statements (brace depth tracking).

    When the range is one block that opens on its first line (``case X: {`` or ``if (...) {``),
    the statements inside that block are the split units and the opening line stays with the
    first of them.
    """
    blocks = _blocks_at(numbered, 0)
    if len(blocks) == 1 and len(numbered) > 2:
        first = _code_only(numbered[0][1])
        inner = first.count("{") - first.count("}")
        if inner > 0:
            deeper = _blocks_at(numbered, inner)
            if len(deeper) > 1:
                return deeper
    return blocks


def _chunk_from(src: SourceChunk, numbered: list[tuple[int, str]]) -> SourceChunk:
    return SourceChunk(
        file=src.file,
        start_line=numbered[0][0],
        end_line=numbered[-1][0],
        role=src.role,
        text=format_chunk_text(src.file, numbered),
    )


def _part(
    packet: WorkPacket,
    index: int,
    chunks: list[SourceChunk],
    keep_context: bool,
    notes: list[str],
) -> WorkPacket:
    return packet.model_copy(
        update={
            "id": f"{packet.id}~p{index}",
            "parent_id": packet.id,
            "partial": True,
            "part_index": index,
            "chunks": chunks,
            "properties": dict(packet.properties) if keep_context else {},
            "trimmed": [*packet.trimmed, *notes],
        }
    )


def split_packet(packet: WorkPacket, budget: int) -> list[WorkPacket]:
    """Split the branch chunks into sub-packets that each fit ``budget`` when possible."""
    context = [c for c in packet.chunks if c.role != "branch"]
    # (source chunk, block lines) in order
    blocks: list[tuple[SourceChunk, list[tuple[int, str]]]] = []
    for c in packet.chunks:
        if c.role == "branch":
            blocks.extend((c, b) for b in split_blocks(parse_chunk_text(c.text)))

    groups: list[list[tuple[SourceChunk, list[tuple[int, str]]]]] = []
    cur: list[tuple[SourceChunk, list[tuple[int, str]]]] = []

    def build(group: list[tuple[SourceChunk, list[tuple[int, str]]]], index: int) -> WorkPacket:
        return _assemble(packet, group, context if index == 1 else [], index)

    for blk in blocks:
        trial = [*cur, blk]
        if cur and packet_tokens(build(trial, len(groups) + 1)) > budget:
            groups.append(cur)
            cur = [blk]
        else:
            cur = trial
    if cur:
        groups.append(cur)
    parts: list[WorkPacket] = []
    for i, group in enumerate(groups, start=1):
        part = build(group, i)
        if packet_tokens(part) > budget:
            part = part.model_copy(update={"trimmed": [*part.trimmed, "oversized"]})
        parts.append(finalize(part))
    return parts


def _assemble(
    packet: WorkPacket,
    group: list[tuple[SourceChunk, list[tuple[int, str]]]],
    context: list[SourceChunk],
    index: int,
) -> WorkPacket:
    chunks: list[SourceChunk] = []
    run_src: SourceChunk | None = None
    run: list[tuple[int, str]] = []
    for src, lines in group:
        if run_src is not None and src is run_src:
            run.extend(lines)
            continue
        if run_src is not None:
            chunks.append(_chunk_from(run_src, run))
        run_src, run = src, list(lines)
    if run_src is not None:
        chunks.append(_chunk_from(run_src, run))
    return _part(packet, index, [*chunks, *context], index == 1, [])


# ------------------------------------------------------------------ fit
def fit_packet(packet: WorkPacket, budget: int) -> list[WorkPacket]:
    """Return ``[packet]`` when it fits, else trim, else split into partial sub-packets.

    Trim order: ``ejb_impl`` chunks (last first), then ``jsp_response``, then ``helper``.
    Branch chunks are never trimmed.
    """
    current = packet
    if packet_tokens(current) <= budget:
        return [finalize(current)]
    trimmed = list(packet.trimmed)
    for role in TRIM_ORDER:
        while packet_tokens(current.model_copy(update={"trimmed": trimmed})) > budget:
            idx = max((i for i, c in enumerate(current.chunks) if c.role == role), default=-1)
            if idx < 0:
                break
            gone = current.chunks[idx]
            trimmed.append(f"{role}:{gone.file}:{gone.start_line}-{gone.end_line}")
            current = current.model_copy(
                update={"chunks": [c for i, c in enumerate(current.chunks) if i != idx]}
            )
        if packet_tokens(current.model_copy(update={"trimmed": trimmed})) <= budget:
            break
    current = current.model_copy(update={"trimmed": trimmed})
    if packet_tokens(current) <= budget:
        return [finalize(current)]
    return split_packet(current, budget)
