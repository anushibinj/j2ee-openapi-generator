"""Progress logging and run metrics."""

from __future__ import annotations

import io

import j2ee_openapi_generator.progress as progress
from j2ee_openapi_generator.llm.base import StructuredResult
from j2ee_openapi_generator.progress import Metrics, ProgressTracker, fmt_duration


def test_fmt_duration() -> None:
    assert fmt_duration(3.21) == "3.2s"
    assert fmt_duration(125) == "2m05s"
    assert fmt_duration(3725) == "1h02m05s"


def test_token_totals_and_cache_savings() -> None:
    m = Metrics(stream=io.StringIO())
    res = StructuredResult(
        ok=True,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        repaired=True,
    )
    m.record_call("analyze", res, 2.0)
    m.record_call("analyze", StructuredResult(ok=False), 1.0)
    m.record_cached("analyze", {"total_tokens": 40})
    assert m.total_tokens() == (10, 5, 15)
    u = m.usage["analyze"]
    assert (u.calls, u.failed, u.repaired, u.cached, u.cached_total) == (2, 1, 1, 1, 40)
    assert len(m.summary_tables()) == 1


def test_stage_and_tracker_log(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    buf = io.StringIO()
    m = Metrics(stream=buf)
    monkeypatch.setattr(progress, "metrics", m)
    with m.stage("demo"):
        t = ProgressTracker("work", 2)
        t.advance("a")
        t.advance("b")
    out = buf.getvalue()
    assert "▶ demo: started" in out and "✔ demo: finished" in out
    assert "work [1/2 50%" in out and "work [2/2 100%] b" in out
    assert [n for n, _ in m.stages] == ["demo"]
