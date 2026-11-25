"""Progress logging and run metrics (stage timings, LLM tokens, cache hits).

Progress lines go to stderr so stdout stays machine-friendly. A process-wide
``metrics`` instance collects numbers; the CLI prints ``summary_tables`` at the end.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TextIO

from rich.table import Table


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds + 0.5), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


@dataclass
class _Usage:
    calls: int = 0
    failed: int = 0
    repaired: int = 0
    cached: int = 0
    prompt: int = 0
    completion: int = 0
    total: int = 0
    cached_total: int = 0  # tokens avoided by cache replays
    seconds: float = 0.0


@dataclass
class Metrics:
    stream: TextIO | None = None
    started: float = field(default_factory=time.monotonic)
    stages: list[tuple[str, float]] = field(default_factory=list)
    usage: dict[str, _Usage] = field(default_factory=lambda: defaultdict(_Usage))
    counters: dict[str, int] = field(default_factory=dict)
    quiet: bool = False

    def reset(self) -> None:
        self.started = time.monotonic()
        self.stages.clear()
        self.usage.clear()
        self.counters.clear()

    def log(self, msg: str) -> None:
        if self.quiet:
            return
        out = self.stream or sys.stderr
        print(f"[{fmt_duration(time.monotonic() - self.started):>7}] {msg}", file=out, flush=True)

    def set_count(self, name: str, n: int) -> None:
        self.counters[name] = n

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        self.log(f"▶ {name}: started")
        t0 = time.monotonic()
        try:
            yield
        finally:
            dt = time.monotonic() - t0
            self.stages.append((name, dt))
            self.log(f"✔ {name}: finished in {fmt_duration(dt)}")

    def record_call(self, purpose: str, res: Any, seconds: float) -> None:
        u = self.usage[purpose]
        u.calls += 1
        u.seconds += seconds
        if not getattr(res, "ok", False):
            u.failed += 1
        if getattr(res, "repaired", False):
            u.repaired += 1
        usage = getattr(res, "usage", None) or {}
        u.prompt += int(usage.get("prompt_tokens", 0))
        u.completion += int(usage.get("completion_tokens", 0))
        u.total += int(usage.get("total_tokens", 0))

    def record_cached(self, purpose: str, usage: dict[str, int] | None) -> None:
        u = self.usage[purpose]
        u.cached += 1
        u.cached_total += int((usage or {}).get("total_tokens", 0))

    def total_tokens(self) -> tuple[int, int, int]:
        us = self.usage.values()
        return (
            sum(u.prompt for u in us),
            sum(u.completion for u in us),
            sum(u.total for u in us),
        )

    def summary_tables(self) -> list[Table]:
        wall = time.monotonic() - self.started
        tables: list[Table] = []
        if self.stages:
            t = Table("stage", "time", "share", title="Run metrics: stages")
            for name, dt in self.stages:
                t.add_row(name, fmt_duration(dt), f"{100 * dt / wall:.0f}%" if wall else "-")
            t.add_row("[bold]total (wall)[/bold]", f"[bold]{fmt_duration(wall)}[/bold]", "")
            tables.append(t)
        if self.usage:
            t = Table(
                "purpose",
                "calls",
                "cached",
                "failed",
                "repair",
                "tok in",
                "tok out",
                "tok total",
                "avg time",
                title="Run metrics: LLM",
            )
            us = self.usage
            for name, u in sorted(us.items()):
                avg = fmt_duration(u.seconds / u.calls) if u.calls else "-"
                t.add_row(
                    name,
                    str(u.calls),
                    str(u.cached),
                    str(u.failed),
                    str(u.repaired),
                    f"{u.prompt:,}",
                    f"{u.completion:,}",
                    f"{u.total:,}",
                    avg,
                )
            p, c, tot = self.total_tokens()
            t.add_row(
                "[bold]total[/bold]",
                str(sum(u.calls for u in us.values())),
                str(sum(u.cached for u in us.values())),
                str(sum(u.failed for u in us.values())),
                str(sum(u.repaired for u in us.values())),
                f"{p:,}",
                f"{c:,}",
                f"[bold]{tot:,}[/bold]",
                "",
            )
            saved = sum(u.cached_total for u in us.values())
            if saved:
                t.caption = f"{saved:,} tokens avoided via cache replays"
            tables.append(t)
        if self.counters:
            t = Table("metric", "value", title="Run metrics: counts")
            for k, v in sorted(self.counters.items()):
                t.add_row(k, f"{v:,}")
            tables.append(t)
        return tables


metrics = Metrics()


class ProgressTracker:
    """Logs ``label [done/total pct, eta]`` as items complete."""

    def __init__(self, label: str, total: int, *, every: int = 1) -> None:
        self.label = label
        self.total = total
        self.every = max(1, every)
        self.done = 0
        self.t0 = time.monotonic()

    def advance(self, detail: str = "") -> None:
        self.done += 1
        if self.done != self.total and self.done % self.every:
            return
        el = time.monotonic() - self.t0
        eta = ""
        if 0 < self.done < self.total:
            eta = f", eta {fmt_duration(el / self.done * (self.total - self.done))}"
        pct = 100 * self.done / self.total if self.total else 100
        head = f"{self.label} [{self.done}/{self.total} {pct:.0f}%{eta}]"
        metrics.log(head + (f" {detail}" if detail else ""))
