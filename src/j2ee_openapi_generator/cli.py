"""``oapi`` command line interface. Commands delegate to stage modules."""

from __future__ import annotations

from pathlib import Path

import typer

from j2ee_openapi_generator.config import Config, load_config

app = typer.Typer(help="Deterministic J2EE to OpenAPI contract extractor.", no_args_is_help=True)

ConfigOpt = typer.Option("oapi.yaml", "--config", "-c", help="Path to oapi.yaml")


def _cfg(path: Path) -> Config:
    return load_config(path)


@app.command()
def index(config: Path = ConfigOpt) -> None:
    """Stage 1: build indexes and work packets (no LLM)."""
    raise typer.Exit(_todo("index"))


@app.command()
def analyze(
    config: Path = ConfigOpt,
    only: str | None = typer.Option(None, "--only"),
    offline: bool = typer.Option(False, "--offline"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Stage 2: LLM map over packets."""
    raise typer.Exit(_todo("analyze"))


@app.command()
def verify(config: Path = ConfigOpt) -> None:
    """Stage 3: deterministic verification of proposals."""
    raise typer.Exit(_todo("verify"))


@app.command()
def aggregate(
    config: Path = ConfigOpt,
    resolve_with_llm: bool = typer.Option(False, "--resolve-with-llm"),
) -> None:
    """Stage 4: merge into the SQLite store."""
    raise typer.Exit(_todo("aggregate"))


@app.command()
def normalize(config: Path = ConfigOpt) -> None:
    """Stage 5: naming normalization."""
    raise typer.Exit(_todo("normalize"))


@app.command()
def emit(config: Path = ConfigOpt) -> None:
    """Stage 6: write out/openapi.yml."""
    raise typer.Exit(_todo("emit"))


@app.command()
def report(config: Path = ConfigOpt) -> None:
    """Stage 7: coverage and open-question reports."""
    raise typer.Exit(_todo("report"))


@app.command()
def validate(config: Path = ConfigOpt, baseline: Path | None = typer.Option(None)) -> None:
    """Validate out/openapi.yml."""
    raise typer.Exit(_todo("validate"))


@app.command()
def run(config: Path = ConfigOpt) -> None:
    """Run every stage in order."""
    raise typer.Exit(_todo("run"))


@app.command()
def doctor(config: Path = ConfigOpt) -> None:
    """Probe which structured modes the endpoint supports."""
    raise typer.Exit(_todo("doctor"))


def _todo(name: str) -> int:
    typer.echo(f"{name}: not implemented yet", err=True)
    return 2
