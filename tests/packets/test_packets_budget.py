from __future__ import annotations

import math

import pytest

from j2ee_openapi_generator.config import LLMConfig
from j2ee_openapi_generator.packets import budget as bud
from j2ee_openapi_generator.packets.budget import (
    compute_hash,
    count_tokens,
    finalize,
    fit_packet,
    format_chunk_text,
    packet_tokens,
    parse_chunk_text,
    split_blocks,
    token_budget,
)
from j2ee_openapi_generator.packets.models import (
    EntryPointMeta,
    MappingHints,
    SourceChunk,
    WorkPacket,
)

SLACK = 60  # room for the trimmed notes, far below one dropped chunk


def chunk(file: str, start: int, n: int, role: str, filler: str = "x" * 40) -> SourceChunk:
    numbered = [(start + i, f"    {filler} {i};") for i in range(n)]
    return SourceChunk(
        file=file,
        start_line=start,
        end_line=start + n - 1,
        role=role,
        text=format_chunk_text(file, numbered),
    )


def branch_chunk(blocks: int, lines_per_block: int = 3) -> SourceChunk:
    numbered: list[tuple[int, str]] = []
    n = 100
    for b in range(blocks):
        numbered.append((n, f"        if (a{b} == null) {{"))
        n += 1
        for k in range(lines_per_block - 2):
            numbered.append((n, f'            call{b}_{k}("some text to take tokens up");'))
            n += 1
        numbered.append((n, "        }"))
        n += 1
    return SourceChunk(
        file="A.java",
        start_line=100,
        end_line=n - 1,
        role="branch",
        text=format_chunk_text("A.java", numbered),
    )


def packet(chunks: list[SourceChunk]) -> WorkPacket:
    return WorkPacket(
        id="m:S#op",
        meta=EntryPointMeta(
            id="m:S#op",
            module="m",
            servlet_fqcn="p.S",
            op="op",
            param_name="op",
            style="if",
            http_methods=["GET"],
            url_patterns=["/S"],
        ),
        chunks=chunks,
        hints=MappingHints(http_method="GET", shape="item"),
        properties={"k": "v"},
    )


def test_count_tokens_deterministic_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert count_tokens("") == 0
    assert count_tokens("hello world") == count_tokens("hello world") > 0
    monkeypatch.setattr(bud, "_ENCODER", None)
    monkeypatch.setattr(bud, "_ENCODER_LOADED", True)
    assert count_tokens("abcdefghi") == math.ceil(9 / 4) == 3
    assert count_tokens("") == 0


def test_fallback_when_tiktoken_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    def boom(name: str) -> None:
        raise RuntimeError("offline")

    monkeypatch.setattr(tiktoken, "get_encoding", boom)
    monkeypatch.setattr(bud, "_ENCODER", None)
    monkeypatch.setattr(bud, "_ENCODER_LOADED", False)
    assert count_tokens("x" * 40) == 10


def test_token_budget_math() -> None:
    cfg = LLMConfig(context_tokens=32000, max_output_tokens=4096)
    assert token_budget(cfg, 5000) == 32000 - 5000 - 4096 - 3200
    assert token_budget(cfg, 100000) == 0
    small = LLMConfig(context_tokens=1000, max_output_tokens=100)
    assert token_budget(small, 100) == 700


def test_chunk_format_roundtrip() -> None:
    text = format_chunk_text("a/B.java", [(9, "x"), (10, "y"), (12345, "z")])
    assert text.split("\n")[0] == "=== FILE a/B.java (lines 9-12345) ==="
    assert text.split("\n")[1] == "   9| x"
    assert text.split("\n")[3] == "12345| z"
    assert parse_chunk_text(text) == [(9, "x"), (10, "y"), (12345, "z")]


def test_split_blocks_top_level() -> None:
    numbered = parse_chunk_text(branch_chunk(3).text)
    blocks = split_blocks(numbered)
    assert len(blocks) == 3
    assert [b[0][0] for b in blocks] == [100, 103, 106]
    # a single nested statement stays one block
    nested = [(1, "if (a) {"), (2, "  if (b) {"), (3, "    x();"), (4, "  }"), (5, "}")]
    assert len(split_blocks(nested)) == 1
    # a range that is one opening block splits into its inner statements
    case = [(1, "case A: {"), (2, "  x();"), (3, "  y();"), (4, "  break;"), (5, "}")]
    assert [[n for n, _ in b] for b in split_blocks(case)] == [[1, 2], [3], [4, 5]]
    # braces in strings and comments do not count
    tricky = [(1, 'log("{");'), (2, "foo(); // }"), (3, "bar();")]
    assert len(split_blocks(tricky)) == 3


def test_fits_unchanged_and_finalized() -> None:
    p = packet([branch_chunk(1), chunk("B.java", 1, 3, "helper")])
    out = fit_packet(p, 10_000)
    assert len(out) == 1
    f = out[0]
    assert f.trimmed == [] and f.allowed_files == ["A.java", "B.java"]
    assert f.token_estimate == packet_tokens(f) > 0
    assert f.packet_hash == compute_hash(f) and len(f.packet_hash) == 64


def test_trim_order() -> None:
    chunks = [
        branch_chunk(1),
        chunk("H.java", 1, 40, "helper"),
        chunk("I.java", 1, 4, "ejb_interface"),
        chunk("E1.java", 1, 40, "ejb_impl"),
        chunk("E2.java", 1, 40, "ejb_impl"),
        chunk("J.jsp", 1, 40, "jsp_response"),
        chunk("Ent.java", 1, 4, "entity"),
    ]
    p = packet(chunks)
    full = packet_tokens(p)
    one = packet_tokens(packet([c for c in chunks if c.file != "E2.java"]))
    # just enough to drop the last ejb_impl chunk
    out = fit_packet(p, one + SLACK)
    assert len(out) == 1 and not out[0].partial
    assert out[0].trimmed == ["ejb_impl:E2.java:1-40"]
    assert {c.file for c in out[0].chunks} == {
        "A.java",
        "H.java",
        "I.java",
        "E1.java",
        "J.jsp",
        "Ent.java",
    }
    # tighter: ejb_impl both go, then jsp_response, then helper, never branch
    no_impl = packet_tokens(packet([c for c in chunks if c.role != "ejb_impl"]))
    out = fit_packet(p, no_impl + SLACK)
    assert out[0].trimmed == ["ejb_impl:E2.java:1-40", "ejb_impl:E1.java:1-40"]
    no_jsp = packet_tokens(
        packet([c for c in chunks if c.role not in ("ejb_impl", "jsp_response")])
    )
    out = fit_packet(p, no_jsp + SLACK)
    assert [t.split(":")[0] for t in out[0].trimmed] == ["ejb_impl", "ejb_impl", "jsp_response"]
    tight = packet_tokens(packet([chunks[0], chunks[2], chunks[6]]))
    out = fit_packet(p, tight + SLACK)
    assert [t.split(":")[0] for t in out[0].trimmed] == [
        "ejb_impl",
        "ejb_impl",
        "jsp_response",
        "helper",
    ]
    kept = {c.role for c in out[0].chunks}
    assert kept == {"branch", "ejb_interface", "entity"}
    assert full > one > no_impl > no_jsp > tight


def test_split_into_partials() -> None:
    b = branch_chunk(8, lines_per_block=4)
    ctx = chunk("I.java", 1, 6, "ejb_interface")
    p = packet([b, ctx])
    one_block = packet_tokens(packet([branch_chunk(2, 4), ctx]))
    out = fit_packet(p, one_block)
    assert len(out) > 1
    assert [x.id for x in out] == [f"m:S#op~p{i}" for i in range(1, len(out) + 1)]
    assert all(x.partial and x.parent_id == "m:S#op" for x in out)
    assert [x.part_index for x in out] == list(range(1, len(out) + 1))
    # context only in part 1
    assert any(c.role == "ejb_interface" for c in out[0].chunks)
    assert all(c.role == "branch" for x in out[1:] for c in x.chunks)
    assert out[0].properties == {"k": "v"} and all(x.properties == {} for x in out[1:])
    # numbered lines intact and complete, headers rewritten
    got: list[tuple[int, str]] = []
    for x in out:
        for c in x.chunks:
            if c.role != "branch":
                continue
            lines = parse_chunk_text(c.text)
            assert c.text.startswith(f"=== FILE A.java (lines {lines[0][0]}-{lines[-1][0]}) ===")
            assert (c.start_line, c.end_line) == (lines[0][0], lines[-1][0])
            got.extend(lines)
    assert got == parse_chunk_text(b.text)
    for x in out:
        assert x.packet_hash == compute_hash(x)
        assert x.allowed_files == sorted({c.file for c in x.chunks})
    # deterministic
    again = fit_packet(p, one_block)
    assert [x.packet_hash for x in again] == [x.packet_hash for x in out]
    assert "oversized" not in sum((x.trimmed for x in out), [])


def test_hash_changes_with_content() -> None:
    a = finalize(packet([branch_chunk(1)]))
    b = finalize(packet([branch_chunk(2)]))
    assert a.packet_hash != b.packet_hash
    assert finalize(a).packet_hash == a.packet_hash


def test_oversized_single_block() -> None:
    long_stmt = [(1, "call(" + ", ".join(f"arg{i}" for i in range(80)) + ");")]
    p = packet(
        [
            SourceChunk(
                file="A.java",
                start_line=1,
                end_line=1,
                role="branch",
                text=format_chunk_text("A.java", long_stmt),
            )
        ]
    )
    out = fit_packet(p, 10)
    assert len(out) == 1 and out[0].partial
    assert "oversized" in out[0].trimmed
