"""Stage orchestration: index (stage 1) and the end-to-end ``run``."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

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
from j2ee_openapi_generator.workspace import Workspace, save_index_bundle

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


def build_index(cfg: Config, workers: int | None = None) -> IndexBundle:
    """Run every indexer (no LLM) and return the bundle. Deterministic for any worker count."""
    w = workers or cfg.worker_count()
    repo = cfg.repo_path
    read = make_reader(repo)
    inv = build_inventory(cfg, w)
    web = build_web_index(cfg, inv)
    java = build_java_index(cfg, inv, w)
    file_module = {f.path: f.module for f in inv if f.module}
    web = merge_annotation_servlets(web, annotated_servlets(java, file_module))
    dispatch = extract_dispatch(cfg, java, web.servlets, read)

    ejb_jars = sorted(f.path for f in inv if f.kind == "ejb_xml")
    ejbs = build_ejb_registry(java, read, ejb_jars)
    entities = build_entity_registry(java, ejbs)
    domain = DomainIndex(ejbs=ejbs, entities=entities)

    jsp_files = {f.path for f in inv if f.kind == "jsp"}
    roots = {m.name: m.root_dir for m in web.modules}
    facts: list[DispatchEntry] = []
    for e in dispatch:
        e.facts = compute_branch_facts(e, java, read, ejbs, roots.get(e.module, ""), jsp_files)
        facts.append(e)

    params = cfg.dispatch.param_names
    items = [
        (f.path, read(f.path), f.module, params, f.kind == "js")
        for f in inv
        if f.kind in ("jsp", "js")
    ]
    jsps = sorted(_map(_jsp_worker, items, w), key=lambda j: j.file)

    bundles: list[PropertiesBundle] = [
        parse_properties(f.path, read(f.path)) for f in inv if f.kind == "properties"
    ]
    java_sources = {f.path: read(f.path) for f in inv if f.kind == "java"}
    props = build_properties_index(bundles, jsps, java_sources)
    links = link_references(jsps, web, params)
    logs = [Path(p) if Path(p).is_absolute() else cfg.base_dir / p for p in cfg.access_logs]
    access = load_access_counts(logs, web, params)
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
    bundle = build_index(cfg)
    save_index_bundle(ws, bundle)
    from j2ee_openapi_generator.packets.builder import build_packets, write_packets

    write_packets(ws, build_packets(cfg, bundle, cfg.worker_count()))
    return bundle
