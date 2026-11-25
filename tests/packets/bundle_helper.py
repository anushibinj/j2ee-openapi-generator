"""Build a complete IndexBundle for a repo by calling the index builders in pipeline order.

Reused by packet, verify and pipeline tests. No file under ``cfg.repo_path`` is written.
"""

from __future__ import annotations

import re
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
    IndexBundle,
    JspFacts,
    PropertiesBundle,
)
from j2ee_openapi_generator.index.properties import build_properties_index, parse_properties
from j2ee_openapi_generator.index.webxml import build_web_index, merge_annotation_servlets

_URL_PATTERNS = re.compile(r'"([^"]*)"')




def build_bundle(cfg: Config, workers: int = 1) -> IndexBundle:
    """Run inventory, web, java, dispatch, facts, ejb/entities, jsp, properties, links, access."""
    repo = cfg.repo_path

    def read_source(rel: str) -> str:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")

    files = build_inventory(cfg, workers)
    web = build_web_index(cfg, files)
    java = build_java_index(cfg, files, workers)

    # @WebServlet servlets: (module, fqcn, patterns, name)
    module_of = {f.path: f.module for f in files}
    annotated: list[tuple[str, str, list[str], str]] = []
    for fqcn, ci in sorted(java.classes.items()):
        for ann in ci.annotations:
            if not ann.startswith("WebServlet"):
                continue
            module = module_of.get(ci.file)
            if module is None:
                continue
            name_m = re.search(r'name\s*=\s*"([^"]*)"', ann)
            pat_part = re.sub(r'name\s*=\s*"[^"]*"', "", ann)
            patterns = _URL_PATTERNS.findall(pat_part)
            annotated.append((module, fqcn, patterns, name_m.group(1) if name_m else ""))
    web = merge_annotation_servlets(web, annotated)

    ejb_jars = [f.path for f in files if f.kind == "ejb_xml"]
    ejbs = build_ejb_registry(java, read_source, ejb_jars)
    entities = build_entity_registry(java, ejbs)

    jsp_paths = {f.path for f in files if f.kind == "jsp"}
    entries = extract_dispatch(cfg, java, web.servlets, read_source)
    roots = {m.name: m.root_dir for m in web.modules}
    entries = [
        e.model_copy(
            update={
                "facts": compute_branch_facts(
                    e, java, read_source, ejbs, roots.get(e.module, ""), jsp_paths
                )
            }
        )
        for e in entries
    ]
    entries = _sorted_entries(entries)

    params = list(cfg.dispatch.param_names)
    jsps: list[JspFacts] = []
    bundles: list[PropertiesBundle] = []
    for f in files:
        if f.kind == "jsp":
            jsps.append(parse_jsp(f.path, read_source(f.path), f.module, params))
        elif f.kind == "js":
            calls = parse_js_calls(f.path, read_source(f.path), params)
            if calls:
                jsps.append(JspFacts(file=f.path, module=f.module, js_calls=calls))
        elif f.kind == "properties":
            bundles.append(parse_properties(f.path, read_source(f.path)))
    jsps.sort(key=lambda j: j.file)
    java_sources = {
        f.path: read_source(f.path) for f in files if f.kind == "java"
    }
    props = build_properties_index(bundles, jsps, java_sources)
    links = link_references(jsps, web, params)
    logs = [p if p.is_absolute() else cfg.base_dir / p for p in map(Path, cfg.access_logs)]
    access = load_access_counts(logs, web, params)
    from j2ee_openapi_generator.index.models import DomainIndex

    return IndexBundle(
        files=files,
        web=web,
        java=java,
        dispatch=entries,
        domain=DomainIndex(ejbs=ejbs, entities=entities),
        jsps=jsps,
        properties=props,
        links=links,
        access=access,
    )


def _sorted_entries(entries: list[DispatchEntry]) -> list[DispatchEntry]:
    return sorted(entries, key=lambda e: (e.module, e.servlet_simple_name, e.op))
