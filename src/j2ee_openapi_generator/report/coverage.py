"""Coverage, open-question and conflict reports (plain files under the output directory)."""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from typing import Any

from j2ee_openapi_generator.aggregate.models import ConflictRow, CoverageRow, StoreSnapshot
from j2ee_openapi_generator.emit.writer import dump_openapi

STATUSES = ("mapped", "no_api_needed", "rejected", "conflict", "failed")
_HEADER = ["entry_id", "status", "operations", "usage_count", "confidence", "reasons"]


def _check(rows: list[CoverageRow]) -> list[CoverageRow]:
    for r in rows:
        if r.status not in STATUSES:
            raise ValueError(f"coverage row {r.entry_id!r} has invalid status {r.status!r}")
    return sorted(rows, key=lambda r: r.entry_id)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _csv(rows: list[CoverageRow]) -> str:
    buf = StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_HEADER)
    for r in rows:
        w.writerow(
            [
                r.entry_id,
                r.status,
                " | ".join(r.operations),
                r.usage_count,
                f"{r.confidence:.2f}",
                " | ".join(r.reasons),
            ]
        )
    return buf.getvalue()


def _coverage_md(rows: list[CoverageRow]) -> str:
    lines = ["# Coverage", "", "## Totals", "", "| Status | Entry points |", "| --- | ---: |"]
    for s in STATUSES:
        lines.append(f"| {s} | {sum(1 for r in rows if r.status == s)} |")
    lines.append(f"| total | {len(rows)} |")
    lines += [
        "",
        "## Entry points",
        "",
        "| Entry point | Status | Operations | Usage | Confidence | Reasons |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {_cell(r.entry_id)} | {r.status} | {_cell(' | '.join(r.operations))} "
            f"| {r.usage_count} | {r.confidence:.2f} | {_cell('; '.join(r.reasons))} |"
        )
    return "\n".join(lines) + "\n"


def _questions_md(questions: dict[str, list[str]]) -> str:
    lines = ["# Open questions", ""]
    tags = sorted(t for t, qs in questions.items() if qs)
    if not tags:
        lines.append("No open questions.")
    for tag in tags:
        lines += [f"## {tag}", ""]
        lines += [f"- {q}" for q in sorted(set(questions[tag]))]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _conflicts_md(conflicts: list[ConflictRow]) -> str:
    open_ones = sorted((c for c in conflicts if not c.resolved), key=lambda c: c.id)
    lines = ["# Conflicts", ""]
    if not open_ones:
        lines.append("No conflicts.")
    for c in open_ones:
        lines += [f"## {c.id}", "", f"- Kind: {c.kind}", f"- Summary: {c.summary}"]
        lines.append(f"- Packets: {', '.join(sorted(c.packet_ids))}")
        lines.append("")
        for i, proposal in enumerate(c.proposals):
            label = c.packet_ids[i] if i < len(c.packet_ids) else f"proposal {i + 1}"
            body: dict[str, Any] = dict(proposal)
            lines += [f"### Proposal {i + 1}: {label}", "", "```yaml"]
            lines += [dump_openapi(body).rstrip("\n"), "```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_reports(snapshot: StoreSnapshot, out_dir: Path) -> None:
    """Write coverage.csv, coverage.md, open-questions.md and conflicts.md into ``out_dir``."""
    rows = _check(list(snapshot.coverage))
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "coverage.csv": _csv(rows),
        "coverage.md": _coverage_md(rows),
        "open-questions.md": _questions_md(snapshot.open_questions),
        "conflicts.md": _conflicts_md(snapshot.conflicts),
    }
    for name, text in files.items():
        with (out_dir / name).open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
