"""``oapi`` command line interface. Commands delegate to ``pipeline``."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from j2ee_openapi_generator import pipeline
from j2ee_openapi_generator.config import Config, load_config
from j2ee_openapi_generator.index.models import IndexBundle

app = typer.Typer(help="Deterministic J2EE to OpenAPI contract extractor.", no_args_is_help=True)
console = Console()

ConfigOpt = typer.Option(Path("oapi.yaml"), "--config", "-c", help="Path to oapi.yaml")


def _cfg(path: Path) -> Config:
    if not path.exists():
        typer.echo(f"config not found: {path}", err=True)
        raise typer.Exit(2)
    return load_config(path)


@app.command()
def index(config: Path = ConfigOpt) -> None:
    """Stage 1: build indexes and work packets (no LLM)."""
    b = pipeline.run_index(_cfg(config))
    console.print(f"indexed {len(b.files)} files, {len(b.dispatch)} entry points")
    if not b.dispatch:
        _explain_no_entry_points(b)


def _explain_no_entry_points(b: IndexBundle) -> None:
    """Say which stage lost the entry points (modules, servlets, or class resolution)."""
    kinds: dict[str, int] = {}
    for f in b.files:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    console.print(f"[yellow]0 entry points[/yellow]; files by kind: {dict(sorted(kinds.items()))}")
    console.print(f"modules: {len(b.web.modules)}, servlets: {len(b.web.servlets)}")
    if not b.web.modules:
        console.print(
            "  no module found: no WEB-INF/web.xml matched include_globs/exclude_globs "
            "and `modules:` is empty in the config"
        )
    elif not b.web.servlets:
        console.print(
            "  modules found but no servlets registered (empty web.xml, or no @WebServlet "
            "classes under a module root)"
        )
    else:
        missing = [s.class_fqcn for s in b.web.servlets if s.class_fqcn not in b.java.classes]
        console.print(
            f"  {len(missing)}/{len(b.web.servlets)} servlet classes not found in the Java "
            f"index (source outside include_globs, or excluded). First: {missing[:5]}"
        )


@app.command()
def analyze(
    config: Path = ConfigOpt,
    only: str | None = typer.Option(None, "--only"),
    offline: bool = typer.Option(False, "--offline"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Stage 2: LLM map over packets."""
    console.print(pipeline.stage_analyze(_cfg(config), only=only, offline=offline, force=force))


@app.command()
def verify(config: Path = ConfigOpt) -> None:
    """Stage 3: deterministic verification of proposals."""
    cfg = _cfg(config)
    counts = pipeline.stage_verify(cfg)
    console.print(counts)
    from j2ee_openapi_generator.verify.verifier import rejected_feedback
    from j2ee_openapi_generator.workspace import Workspace

    for pid, reasons in sorted(rejected_feedback(Workspace(cfg)).items()):
        console.print(f"[red]rejected[/red] {pid}")
        for r in reasons:
            console.print(f"  - {r}")


@app.command()
def aggregate(
    config: Path = ConfigOpt,
    resolve_with_llm: bool = typer.Option(False, "--resolve-with-llm"),
) -> None:
    """Stage 4: merge into the SQLite store."""
    for w in pipeline.stage_aggregate(_cfg(config), resolve_with_llm=resolve_with_llm):
        console.print(f"warning: {w}")


@app.command()
def normalize(
    config: Path = ConfigOpt,
    offline: bool = typer.Option(False, "--offline"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Stage 5: naming normalization."""
    n = pipeline.stage_normalize(_cfg(config), offline=offline, force=force)
    console.print(f"normalize finished with {n} issue(s)")


@app.command()
def emit(config: Path = ConfigOpt) -> None:
    """Stage 6: write out/openapi.yml."""
    console.print(str(pipeline.stage_emit(_cfg(config))))


@app.command()
def report(config: Path = ConfigOpt) -> None:
    """Stage 7: coverage and open-question reports."""
    pipeline.stage_report(_cfg(config))


@app.command()
def validate(
    config: Path = ConfigOpt,
    baseline: Path | None = typer.Option(None, "--baseline", help="Previous openapi.yml"),
) -> None:
    """Validate out/openapi.yml."""
    results = pipeline.stage_validate(_cfg(config), baseline)
    bad = False
    for r in results:
        state = "skipped" if r.skipped else ("ok" if r.ok else "FAILED")
        console.print(f"{r.tool}: {state}")
        if r.output and not r.ok:
            console.print(r.output)
        bad = bad or (not r.ok and not r.skipped)
    if bad:
        raise typer.Exit(1)


@app.command()
def run(
    config: Path = ConfigOpt,
    offline: bool = typer.Option(False, "--offline"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Run every stage in order."""
    cfg = _cfg(config)
    pipeline.run_all(cfg, offline=offline, force=force)
    console.print(f"wrote {cfg.out_path / 'openapi.yml'}")


@app.command()
def doctor(config: Path = ConfigOpt) -> None:
    """Probe which structured modes the endpoint supports."""
    from j2ee_openapi_generator.llm.client import run_doctor

    table = Table("check", "status", "detail")
    for row in run_doctor(_cfg(config)):
        detail = str(row.get("error") or row.get("server_context") or "")
        table.add_row(str(row["check"]), str(row["status"]), detail)
    console.print(table)
