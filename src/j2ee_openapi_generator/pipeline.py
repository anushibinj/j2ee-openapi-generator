"""Stage orchestration: index (stage 1) and the end-to-end ``run``."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.access_logs import load_access_counts
from j2ee_openapi_generator.index.branch_facts import compute_branch_facts
from j2ee_openapi_generator.index.dispatch import extract_dispatch
from j2ee_openapi_generator.index.ejb import build_ejb_registry
from j2ee_openapi_generator.index.entities import build_entity_registry
from j2ee_openapi_generator.index.inventory import build_inventory
from j2ee_openapi_generator.index.java_symbols import build_java_index
from j2ee_openapi_generator.index.jsp import parse_js_calls, parse_jsp
from j2ee_openapi_generator.index.linker import link_references
from j2ee_openapi_generator.index.models import (
    DispatchEntry,
    DomainIndex,
    IndexBundle,
    JavaIndex,
    JspFacts,
    PropertiesBundle,
)
from j2ee_openapi_generator.index.properties import build_properties_index, parse_properties
from j2ee_openapi_generator.index.webxml import build_web_index, merge_annotation_servlets
from j2ee_openapi_generator.progress import ProgressTracker, metrics
from j2ee_openapi_generator.workspace import Workspace, load_index_bundle, save_index_bundle

if TYPE_CHECKING:
    from j2ee_openapi_generator.aggregate.models import StoreSnapshot
    from j2ee_openapi_generator.report.validate import ValidationResult

_URL_ATTR = re.compile(r'(?:urlPatterns|value)\s*=\s*(\{[^}]*\}|"[^"]*")|^"[^"]*"$|^\{[^}]*\}$')
_NAME_ATTR = re.compile(r'name\s*=\s*"([^"]*)"')


def make_reader(repo: Path) -> Callable[[str], str]:
    cache: dict[str, str] = {}

    def read(rel: str) -> str:
        if rel not in cache:
            cache[rel] = (repo / rel).read_text(encoding="utf-8", errors="replace")
        return cache[rel]

    return read


def annotated_servlets(
    java: JavaIndex, file_module: dict[str, str]
) -> list[tuple[str, str, list[str], str]]:
    out: list[tuple[str, str, list[str], str]] = []
    for fqcn, cls in sorted(java.classes.items()):
        for ann in cls.annotations:
            if not ann.startswith("WebServlet"):
                continue
            body = ann[len("WebServlet") :].strip()
            body = body[1:-1] if body.startswith("(") and body.endswith(")") else ""
            pats: list[str] = []
            m = re.search(r'(?:urlPatterns|value)\s*=\s*(\{[^}]*\}|"[^"]*")', body)
            raw = m.group(1) if m else (body if body.startswith(('"', "{")) else "")
            pats = re.findall(r'"([^"]*)"', raw)
            nm = _NAME_ATTR.search(body)
            module = file_module.get(cls.file)
            if module:
                out.append((module, fqcn, pats, nm.group(1) if nm else cls.simple_name))
    return out


def _jsp_worker(args: tuple[str, str, str | None, list[str], bool]) -> JspFacts:
    rel, text, module, params, is_js = args
    if is_js:
        return JspFacts(file=rel, module=module, js_calls=parse_js_calls(rel, text, params))
    return parse_jsp(rel, text, module, params)


def _map(
    fn: Callable[..., object],
    items: list[tuple[str, str, str | None, list[str], bool]],
    workers: int,
) -> list[JspFacts]:
    if workers <= 1 or len(items) < 8:
        return [fn(i) for i in items]  # type: ignore[misc]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items, chunksize=8))  # type: ignore[arg-type]


def _prelude_entry(e: DispatchEntry, java: JavaIndex) -> DispatchEntry | None:
    """Lines of the dispatching method before the first branch, as a pseudo entry."""
    if e.style not in ("if", "switch", "default"):
        return None
    best = None
    for c in java.classes.values():
        if c.file != e.file:
            continue
        for m in c.methods:
            if m.start_line <= e.start_line and e.end_line <= m.end_line:
                if best is None or m.start_line > best.start_line:
                    best = m
    if best is None or best.start_line >= e.start_line:
        return None
    return e.model_copy(update={"start_line": best.start_line, "end_line": e.start_line - 1})


def build_index(cfg: Config, workers: int | None = None) -> IndexBundle:
    """Run every indexer (no LLM) and return the bundle. Deterministic for any worker count."""
    w = workers or cfg.worker_count()
    repo = cfg.repo_path
    read = make_reader(repo)
    metrics.log(f"index: scanning {repo} with {w} worker(s)")
    inv = build_inventory(cfg, w)
    kinds: dict[str, int] = {}
    for f in inv:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    metrics.log(
        f"index: inventory done, {len(inv)} files ("
        + ", ".join(f"{k}={n}" for k, n in sorted(kinds.items()))
        + ")"
    )
    web = build_web_index(cfg, inv)
    metrics.log(
        f"index: web.xml parsed, {len(web.modules)} module(s), {len(web.servlets)} servlet(s)"
    )
    metrics.log(f"index: parsing {kinds.get('java', 0)} java file(s)")
    java = build_java_index(cfg, inv, w)
    metrics.log(f"index: java symbols done, {len(java.classes)} class(es)")
    file_module = {f.path: f.module for f in inv if f.module}
    web = merge_annotation_servlets(web, annotated_servlets(java, file_module))
    dispatch = extract_dispatch(cfg, java, web.servlets, read)
    metrics.log(f"index: {len(dispatch)} dispatch entry point(s) found")

    ejb_jars = sorted(f.path for f in inv if f.kind == "ejb_xml")
    ejbs = build_ejb_registry(java, read, ejb_jars)
    entities = build_entity_registry(java, ejbs)
    domain = DomainIndex(ejbs=ejbs, entities=entities)
    metrics.log(f"index: domain registry done, {len(ejbs)} EJB(s), {len(entities)} entities")

    jsp_files = {f.path for f in inv if f.kind == "jsp"}
    roots = {m.name: m.root_dir for m in web.modules}
    facts: list[DispatchEntry] = []
    tracker = ProgressTracker(
        "index: branch facts", len(dispatch), every=max(1, len(dispatch) // 20)
    )
    for e in dispatch:
        root = roots.get(e.module, "")
        e.facts = compute_branch_facts(e, java, read, ejbs, root, jsp_files)
        prelude = _prelude_entry(e, java)
        if prelude is not None:  # EJB calls made before the dispatch apply to every branch
            pre = compute_branch_facts(prelude, java, read, ejbs, root, jsp_files)
            e.facts.ejb_calls = sorted({*e.facts.ejb_calls, *pre.ejb_calls})
        facts.append(e)
        tracker.advance()

    params = cfg.dispatch.param_names
    items = [
        (f.path, read(f.path), f.module, params, f.kind == "js")
        for f in inv
        if f.kind in ("jsp", "js")
    ]
    metrics.log(f"index: parsing {len(items)} JSP/JS file(s)")
    jsps = sorted(_map(_jsp_worker, items, w), key=lambda j: j.file)

    bundles: list[PropertiesBundle] = [
        parse_properties(f.path, read(f.path)) for f in inv if f.kind == "properties"
    ]
    java_sources = {f.path: read(f.path) for f in inv if f.kind == "java"}
    props = build_properties_index(bundles, jsps, java_sources)
    links = link_references(jsps, web, params)
    logs = [Path(p) if Path(p).is_absolute() else cfg.base_dir / p for p in cfg.access_logs]
    access = load_access_counts(logs, web, params)
    metrics.log(f"index: linked references, {len(logs)} access log(s) read")
    return IndexBundle(
        files=inv,
        web=web,
        java=java,
        dispatch=facts,
        domain=domain,
        jsps=jsps,
        properties=props,
        links=links,
        access=access,
    )


def run_index(cfg: Config) -> IndexBundle:
    ws = Workspace(cfg)
    ws.ensure()
    with metrics.stage("index"):
        bundle = build_index(cfg)
        save_index_bundle(ws, bundle)
    from j2ee_openapi_generator.packets.builder import build_packets, write_packets

    with metrics.stage("packets"):
        metrics.log(f"packets: building for {len(bundle.dispatch)} entry point(s)")
        packets = build_packets(cfg, bundle, cfg.worker_count())
        write_packets(ws, packets)
        metrics.log(f"packets: wrote {len(packets)} packet(s)")
    metrics.set_count("files indexed", len(bundle.files))
    metrics.set_count("entry points", len(bundle.dispatch))
    metrics.set_count("work packets", len(packets))
    return bundle


# ----------------------------------------------------------------- later stages
def stage_analyze(
    cfg: Config, *, only: str | None = None, offline: bool = False, force: bool = False
) -> dict[str, int]:
    from j2ee_openapi_generator.analyze.analyzer import analysis_summary, analyze_sync
    from j2ee_openapi_generator.llm.client import make_client

    ws = Workspace(cfg)
    ws.ensure()
    client = make_client(cfg)
    with metrics.stage("analyze"):
        recs = analyze_sync(cfg, client, ws, only=only, offline=offline, force=force)
    summary = analysis_summary(recs)
    metrics.set_count("proposals ok", summary["ok"])
    metrics.set_count("proposals failed", summary["failed"])
    return summary


def stage_verify(cfg: Config) -> dict[str, int]:
    from j2ee_openapi_generator.verify.verifier import verify_all

    ws = Workspace(cfg)
    bundle = load_index_bundle(ws)
    with metrics.stage("verify"):
        metrics.log("verify: checking proposals against the index")
        out = verify_all(cfg, ws, bundle, cfg.worker_count())
    counts: dict[str, int] = {}
    for v in out:
        counts[v.status] = counts.get(v.status, 0) + 1
    metrics.log("verify: " + ", ".join(f"{k}={n}" for k, n in sorted(counts.items())))
    return counts


def stage_reanalyze(cfg: Config, *, offline: bool = False, force: bool = False) -> int:
    """Re-analyze rejected proposals once with the verifier reasons as feedback."""
    from j2ee_openapi_generator.analyze.analyzer import analyze_sync
    from j2ee_openapi_generator.llm.client import make_client
    from j2ee_openapi_generator.verify.verifier import rejected_feedback

    ws = Workspace(cfg)
    fb = rejected_feedback(ws)
    if not fb:
        return 0
    with metrics.stage("re-analyze rejected"):
        analyze_sync(
            cfg,
            make_client(cfg),
            ws,
            offline=offline,
            force=force,
            attempt=2,
            feedback=fb,
            only_ids=set(fb),
        )
    return len(fb)


def stage_aggregate(
    cfg: Config, *, resolve_with_llm: bool = False, offline: bool = False
) -> list[str]:
    from j2ee_openapi_generator.aggregate.run import run_aggregate
    from j2ee_openapi_generator.llm.client import make_client

    ws = Workspace(cfg)
    bundle = load_index_bundle(ws)
    client = make_client(cfg) if resolve_with_llm else None
    with metrics.stage("aggregate"):
        store, warnings = run_aggregate(
            cfg, ws, bundle, client=client, resolve_with_llm=resolve_with_llm, offline=offline
        )
        store.close()
    metrics.set_count("aggregate warnings", len(warnings))
    return warnings


def stage_normalize(cfg: Config, *, offline: bool = False, force: bool = False) -> int:
    from j2ee_openapi_generator.aggregate.store import Store
    from j2ee_openapi_generator.llm.client import make_client
    from j2ee_openapi_generator.normalize.normalizer import normalize_sync

    ws = Workspace(cfg)
    store = Store(ws.db_path)
    try:
        with metrics.stage("normalize"):
            report = normalize_sync(cfg, make_client(cfg), ws, store, offline=offline, force=force)
    finally:
        store.close()
    return len(report.errors)


def load_snapshot(cfg: Config) -> StoreSnapshot:
    from j2ee_openapi_generator.aggregate.run import derive_error_codes
    from j2ee_openapi_generator.aggregate.store import Store

    ws = Workspace(cfg)
    bundle = load_index_bundle(ws)
    store = Store(ws.db_path)
    try:
        return store.snapshot(bundle.domain.entities, derive_error_codes(bundle))
    finally:
        store.close()


def stage_emit(cfg: Config) -> Path:
    from j2ee_openapi_generator.emit.builder import build_openapi
    from j2ee_openapi_generator.emit.writer import write_openapi

    ws = Workspace(cfg)
    ws.out_dir.mkdir(parents=True, exist_ok=True)
    with metrics.stage("emit"):
        snap = load_snapshot(cfg)
        doc = build_openapi(snap, cfg)
        path = ws.out_dir / "openapi.yml"
        write_openapi(doc, path)
        metrics.log(f"emit: wrote {path}")
    paths = doc.get("paths", {}) if isinstance(doc, dict) else {}
    metrics.set_count("api paths", len(paths))
    metrics.set_count("operations", sum(len(v) for v in paths.values() if isinstance(v, dict)))
    return path


def stage_report(cfg: Config) -> None:
    from j2ee_openapi_generator.report.coverage import write_reports

    ws = Workspace(cfg)
    ws.out_dir.mkdir(parents=True, exist_ok=True)
    with metrics.stage("report"):
        write_reports(load_snapshot(cfg), ws.out_dir)


def stage_validate(cfg: Config, baseline: Path | None = None) -> list[ValidationResult]:
    from j2ee_openapi_generator.report.validate import validate_openapi

    ws = Workspace(cfg)
    with metrics.stage("validate"):
        return validate_openapi(ws.out_dir / "openapi.yml", baseline)


def run_all(cfg: Config, *, offline: bool = False, force: bool = False) -> None:
    """Every stage in order, including the single verifier-feedback re-analysis."""
    run_index(cfg)
    stage_analyze(cfg, offline=offline, force=force)
    stage_verify(cfg)
    if stage_reanalyze(cfg, offline=offline, force=force):
        stage_verify(cfg)
    stage_aggregate(cfg, offline=offline)
    stage_normalize(cfg, offline=offline, force=force)
    stage_emit(cfg)
    stage_report(cfg)
