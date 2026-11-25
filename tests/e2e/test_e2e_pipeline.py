"""End-to-end pipeline on the fake legacy app with the fake LLM."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate
from typer.testing import CliRunner

from j2ee_openapi_generator.cli import app
from j2ee_openapi_generator.config import Config, load_config
from j2ee_openapi_generator.ids import sha256_file
from j2ee_openapi_generator.pipeline import build_index, run_all
from j2ee_openapi_generator.workspace import Workspace, save_index_bundle

FIX = Path(__file__).parents[1] / "fixtures"
GOLDEN = Path(__file__).parents[1] / "golden" / "openapi.yml"

EXPECTED_IDS = sorted(
    [
        "accounts:ProfileServlet#update",
        "accounts:ProfileServlet#view",
        "accounts:UserServlet#<default>",
        "accounts:UserServlet#delete",
        "accounts:UserServlet#edit",
        "accounts:UserServlet#list",
        "accounts:UserServlet#new",
        "accounts:UserServlet#read",
        "accounts:UserServlet#save",
        "sales:LookupServlet#countries",
        "sales:LookupServlet#statuses",
        "sales:OrderServlet#<default>",
        "sales:OrderServlet#confirm",
        "sales:OrderServlet#detail",
        "sales:OrderServlet#export",
        "sales:OrderServlet#list",
        "sales:OrderServlet#step1",
        "sales:OrderServlet#step2",
        "sales:OrderServlet#view",
        "sales:ReportServlet#daily",
        "sales:ReportServlet#summary",
    ]
)


def make_cfg(tmp: Path, workers: int | str = 1) -> Config:
    cfg = load_config(FIX / "oapi.yaml")
    cfg.work_dir = tmp / "work"
    cfg.out_dir = tmp / "out"
    cfg.review_dir = tmp / "review"
    cfg.workers = workers  # type: ignore[assignment]
    return cfg


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_full_pipeline_matches_golden_and_validates(tmp_path: Path) -> None:
    repo_before = tree_hashes(FIX / "legacy-app")
    cfg = make_cfg(tmp_path)
    run_all(cfg)
    out = (cfg.out_path / "openapi.yml").read_bytes()
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_bytes(out)
    assert out == GOLDEN.read_bytes()
    validate(yaml.safe_load(out))
    assert tree_hashes(FIX / "legacy-app") == repo_before  # repo is read-only


def test_coverage_lists_every_entry_point(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    run_all(cfg)
    rows = list(csv.DictReader((cfg.out_path / "coverage.csv").open()))
    assert sorted(r["entry_id"] for r in rows) == EXPECTED_IDS
    statuses = {r["status"] for r in rows}
    assert statuses <= {"mapped", "no_api_needed", "rejected", "conflict", "failed"}
    by_id = {r["entry_id"]: r["status"] for r in rows}
    assert by_id["sales:ReportServlet#daily"] == "mapped"
    assert by_id["sales:LookupServlet#statuses"] == "mapped"  # repaired on attempt 2
    assert by_id["sales:OrderServlet#view"] == "conflict"
    assert by_id["accounts:UserServlet#new"] == "no_api_needed"
    assert "GET /orders/{}" in (cfg.out_path / "conflicts.md").read_text()


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    a = make_cfg(tmp_path / "a")
    b = make_cfg(tmp_path / "b")
    run_all(a)
    run_all(b)
    run_all(b)  # idempotent re-run over the same work dir (uses the cache)
    for name in ("openapi.yml", "coverage.csv", "coverage.md", "conflicts.md"):
        assert (a.out_path / name).read_bytes() == (b.out_path / name).read_bytes()


def test_index_workers_1_vs_4_identical(tmp_path: Path) -> None:
    outs = []
    for w in (1, 4):
        cfg = make_cfg(tmp_path / f"w{w}", workers=w)
        ws = Workspace(cfg)
        ws.ensure()
        save_index_bundle(ws, build_index(cfg))
        from j2ee_openapi_generator.packets.builder import build_packets, write_packets
        from j2ee_openapi_generator.workspace import load_index_bundle

        write_packets(ws, build_packets(cfg, load_index_bundle(ws), w))
        outs.append((tree_hashes(ws.index_dir), tree_hashes(ws.packets_dir)))
    assert outs[0] == outs[1]
    assert len(outs[0][1]) == 21


def test_cli_run_and_help(tmp_path: Path) -> None:
    cfg_file = tmp_path / "oapi.yaml"
    data = yaml.safe_load((FIX / "oapi.yaml").read_text())
    data.update(
        repo_root=str(FIX / "legacy-app"),
        work_dir=str(tmp_path / "w"),
        out_dir=str(tmp_path / "o"),
        review_dir=str(tmp_path / "r"),
        style_guide=str(FIX.parent.parent / "style-guide" / "api-style-guide.md"),
    )
    data["llm"]["fake_dir"] = str(FIX / "llm")
    cfg_file.write_text(yaml.safe_dump(data))
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    res = runner.invoke(app, ["run", "-c", str(cfg_file)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "o" / "openapi.yml").read_bytes() == GOLDEN.read_bytes()
    res = runner.invoke(app, ["validate", "-c", str(cfg_file)])
    assert res.exit_code == 0, res.output


@pytest.mark.parametrize("stage", ["index", "analyze", "verify"])
def test_cli_stage_commands_exist(stage: str) -> None:
    assert CliRunner().invoke(app, [stage, "--help"]).exit_code == 0
