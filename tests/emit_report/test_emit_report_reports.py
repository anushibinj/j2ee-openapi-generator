from __future__ import annotations

import csv
from pathlib import Path

import pytest
from emit_report_support import snapshot

from j2ee_openapi_generator.aggregate.models import CoverageRow, StoreSnapshot
from j2ee_openapi_generator.report.coverage import STATUSES, write_reports


def test_files_written(tmp_path: Path) -> None:
    write_reports(snapshot(), tmp_path / "out")
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["conflicts.md", "coverage.csv", "coverage.md", "open-questions.md"]


def test_coverage_csv(tmp_path: Path) -> None:
    write_reports(snapshot(), tmp_path)
    text = (tmp_path / "coverage.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "entry_id,status,operations,usage_count,confidence,reasons"
    rows = list(csv.DictReader(text.splitlines()))
    ids = [r["entry_id"] for r in rows]
    assert ids == sorted(ids)
    save = next(r for r in rows if r["entry_id"] == "accounts:UserServlet#save")
    assert save["operations"] == "POST /users | PUT /users/{userId}"
    assert save["status"] == "mapped"
    assert "unknown" not in text


def test_coverage_md_totals(tmp_path: Path) -> None:
    write_reports(snapshot(), tmp_path)
    md = (tmp_path / "coverage.md").read_text(encoding="utf-8")
    head = md.split("## Entry points")[0]
    for s in STATUSES:
        assert f"| {s} |" in head
    assert "| mapped | 2 |" in head and "| conflict | 1 |" in head and "| failed | 0 |" in head
    assert "| total | 4 |" in head
    assert "unknown" not in md
    assert "| accounts:UserServlet#edit | no_api_needed |" in md


def test_open_questions_grouped_and_sorted(tmp_path: Path) -> None:
    write_reports(snapshot(), tmp_path)
    md = (tmp_path / "open-questions.md").read_text(encoding="utf-8")
    assert md.index("## Accounts Users") < md.index("## Sales Orders")
    assert md.index("Is the order number") < md.index("Zeta question")
    assert "- Should users be soft deleted?" in md


def test_conflicts_side_by_side(tmp_path: Path) -> None:
    write_reports(snapshot(), tmp_path)
    md = (tmp_path / "conflicts.md").read_text(encoding="utf-8")
    assert "## c1" in md and "## c0" not in md  # resolved ones are not listed
    assert md.count("```yaml") == 2
    assert "type: integer" in md and "type: string" in md
    assert "sales:OrderServlet#detail" in md


def test_no_conflicts_message(tmp_path: Path) -> None:
    write_reports(StoreSnapshot(), tmp_path)
    assert "No conflicts" in (tmp_path / "conflicts.md").read_text(encoding="utf-8")
    assert "No open questions" in (tmp_path / "open-questions.md").read_text(encoding="utf-8")
    assert (tmp_path / "coverage.csv").read_text(encoding="utf-8").count("\n") == 1


def test_reports_deterministic(tmp_path: Path) -> None:
    write_reports(snapshot(), tmp_path / "a")
    write_reports(snapshot(), tmp_path / "b")
    for name in ("coverage.csv", "coverage.md", "open-questions.md", "conflicts.md"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_invalid_status_asserted(tmp_path: Path) -> None:
    bad = CoverageRow.model_construct(
        entry_id="x", status="unknown", operations=[], usage_count=0, confidence=0.0, reasons=[]
    )
    with pytest.raises(ValueError, match="invalid status"):
        write_reports(StoreSnapshot(coverage=[bad]), tmp_path)
