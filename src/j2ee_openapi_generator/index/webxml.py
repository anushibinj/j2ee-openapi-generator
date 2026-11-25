"""web.xml parsing, servlet-spec URL matching and security-constraint lookup.

Module discovery (when ``cfg.modules`` is empty): every ``WEB-INF/web.xml`` in the inventory is a
module. The module name is the directory name of the module root. The root is the directory that
contains ``src`` when the path looks like ``<root>/src/main/webapp/WEB-INF/web.xml``, otherwise
the directory above ``WEB-INF``. The context root is ``'/' + name``. When ``cfg.modules`` is given,
name, web_xml, context_root and tag_prefix come from the config (an empty context_root falls back
to ``'/' + name``) and the root directory is derived from ``web_xml`` the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from lxml import etree

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.models import (
    FileRecord,
    FilterRecord,
    ModuleRecord,
    SecurityConstraint,
    ServletRecord,
    WebIndex,
)


def _local(el: Any) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return str(etree.QName(tag).localname)


def _kids(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c) == name]


def _text(el: Any, name: str) -> str:
    for c in _kids(el, name):
        return (c.text or "").strip()
    return ""


def _texts(el: Any, name: str) -> list[str]:
    return [t for t in ((c.text or "").strip() for c in _kids(el, name)) if t]


def parse_web_xml(path: Path, rel: str) -> dict[str, Any]:
    """Parse one web.xml into plain dicts (namespace-agnostic).

    Keys: ``servlets`` (name, class_fqcn, url_patterns, init_params), ``filters``
    (name, class_name, url_patterns), ``constraints`` (SecurityConstraint list) and ``roles``.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.parse(str(path), parser).getroot()
    mappings: dict[str, list[str]] = {}
    for m in _kids(root, "servlet-mapping"):
        mappings.setdefault(_text(m, "servlet-name"), []).extend(_texts(m, "url-pattern"))
    fmaps: dict[str, list[str]] = {}
    for m in _kids(root, "filter-mapping"):
        fmaps.setdefault(_text(m, "filter-name"), []).extend(_texts(m, "url-pattern"))

    servlets: list[dict[str, Any]] = []
    for s in _kids(root, "servlet"):
        name = _text(s, "servlet-name")
        cls = _text(s, "servlet-class")
        if not name or not cls:
            continue
        params = {_text(p, "param-name"): _text(p, "param-value") for p in _kids(s, "init-param")}
        servlets.append(
            {
                "name": name,
                "class_fqcn": cls,
                "url_patterns": sorted(set(mappings.get(name, []))),
                "init_params": {k: params[k] for k in sorted(params) if k},
            }
        )
    filters = [
        {
            "name": _text(f, "filter-name"),
            "class_name": _text(f, "filter-class"),
            "url_patterns": sorted(set(fmaps.get(_text(f, "filter-name"), []))),
        }
        for f in _kids(root, "filter")
    ]
    constraints: list[SecurityConstraint] = []
    for sc in _kids(root, "security-constraint"):
        pats: list[str] = []
        methods: list[str] = []
        for coll in _kids(sc, "web-resource-collection"):
            pats.extend(_texts(coll, "url-pattern"))
            methods.extend(m.upper() for m in _texts(coll, "http-method"))
        roles: list[str] = []
        for ac in _kids(sc, "auth-constraint"):
            roles.extend(_texts(ac, "role-name"))
        constraints.append(
            SecurityConstraint(
                url_patterns=sorted(set(pats)),
                http_methods=sorted(set(methods)),
                roles=sorted(set(roles)),
            )
        )
    constraints.sort(key=lambda c: (c.url_patterns, c.http_methods, c.roles))
    roles_decl = sorted({r for sr in _kids(root, "security-role") for r in _texts(sr, "role-name")})
    return {
        "web_xml": rel,
        "servlets": servlets,
        "filters": sorted(filters, key=lambda f: (str(f["name"]), str(f["class_name"]))),
        "constraints": constraints,
        "roles": roles_decl,
    }


def module_root_dir(web_xml: str) -> str:
    """Repo-relative module root for a web.xml path ('' when the repo itself is the module)."""
    p = PurePosixPath(web_xml)
    parts = p.parts
    if len(parts) >= 5 and parts[-4:-1] == ("main", "webapp", "WEB-INF") and parts[-5] == "src":
        root = PurePosixPath(*parts[:-5]) if len(parts) > 5 else PurePosixPath(".")
    else:
        root = p.parent.parent
    s = root.as_posix()
    return "" if s == "." else s


def discover_modules(cfg: Config, inventory: Sequence[FileRecord]) -> list[ModuleRecord]:
    """Module records (no web.xml content yet), from config or auto-discovery, sorted by name."""
    out: list[ModuleRecord] = []
    if cfg.modules:
        for m in cfg.modules:
            root = module_root_dir(m.web_xml)
            out.append(
                ModuleRecord(
                    name=m.name,
                    web_xml=m.web_xml,
                    root_dir=root,
                    context_root=m.context_root or "/" + m.name,
                    tag_prefix=m.tag_prefix,
                )
            )
    else:
        for f in inventory:
            if f.kind != "web_xml" or "WEB-INF" not in PurePosixPath(f.path).parts:
                continue
            root = module_root_dir(f.path)
            name = PurePosixPath(root).name if root else cfg.repo_path.name
            out.append(
                ModuleRecord(name=name, web_xml=f.path, root_dir=root, context_root="/" + name)
            )
    return sorted(out, key=lambda m: (m.name, m.web_xml))


def build_web_index(cfg: Config, inventory: list[FileRecord]) -> WebIndex:
    modules = discover_modules(cfg, inventory)
    servlets: list[ServletRecord] = []
    for mod in modules:
        path = cfg.repo_path / mod.web_xml
        if not path.is_file():
            continue
        data = parse_web_xml(path, mod.web_xml)
        mod.security_constraints = list(data["constraints"])
        mod.security_roles = list(data["roles"])
        mod.filters = [FilterRecord(**f) for f in data["filters"]]
        for s in data["servlets"]:
            servlets.append(
                ServletRecord(
                    module=mod.name,
                    name=s["name"],
                    class_fqcn=s["class_fqcn"],
                    url_patterns=s["url_patterns"],
                    context_root=mod.context_root,
                    source="web.xml",
                    init_params=s["init_params"],
                )
            )
    return WebIndex(modules=modules, servlets=_sorted_servlets(servlets))


def _sorted_servlets(servlets: list[ServletRecord]) -> list[ServletRecord]:
    return sorted(servlets, key=lambda s: (s.module, s.class_fqcn, s.name))


def merge_annotation_servlets(web: WebIndex, annotated: Sequence[tuple[Any, ...]]) -> WebIndex:
    """Add @WebServlet servlets. Tuples are (module, class_fqcn, url_patterns[, name]).

    A servlet already registered in web.xml (same module and class) wins; it only picks up the
    annotation patterns when web.xml gave it none.
    """
    ctx = {m.name: m.context_root for m in web.modules}
    servlets = [s.model_copy(deep=True) for s in web.servlets]
    for item in annotated:
        module, fqcn, patterns = str(item[0]), str(item[1]), [str(p) for p in item[2]]
        name = str(item[3]) if len(item) > 3 and item[3] else fqcn.rsplit(".", 1)[-1]
        existing = [s for s in servlets if s.module == module and s.class_fqcn == fqcn]
        if existing:
            for s in existing:
                if not s.url_patterns:
                    s.url_patterns = sorted(set(patterns))
            continue
        servlets.append(
            ServletRecord(
                module=module,
                name=name,
                class_fqcn=fqcn,
                url_patterns=sorted(set(patterns)),
                context_root=ctx.get(module, ""),
                source="annotation",
            )
        )
    return WebIndex(
        modules=[m.model_copy(deep=True) for m in web.modules],
        servlets=_sorted_servlets(servlets),
    )


# ------------------------------------------------------------------ URL matching
def match_url_pattern(pattern: str, path: str) -> bool:
    """Servlet-spec matching of one pattern against a path (no query string)."""
    if pattern == "/":
        return True  # default servlet matches anything not matched otherwise
    if pattern == "":
        return path == "/"
    if pattern.startswith("*."):
        last = path.rsplit("/", 1)[-1]
        return last.endswith(pattern[1:]) and len(last) > len(pattern) - 1
    if pattern.endswith("/*"):
        prefix = pattern[:-2]
        return path == prefix or path.startswith(prefix + "/")
    return pattern == path


def _rank(pattern: str, path: str) -> tuple[int, int] | None:
    if not match_url_pattern(pattern, path):
        return None
    if pattern == "/":
        return (0, 0)
    if pattern.startswith("*."):
        return (1, len(pattern))
    if pattern.endswith("/*"):
        return (2, len(pattern))
    return (3, len(pattern))


def resolve_with_pattern(
    web: WebIndex, module: str, path: str
) -> tuple[ServletRecord, str] | None:
    """Best servlet and the pattern that matched: exact > longest prefix > extension > default."""
    best: tuple[tuple[int, int], ServletRecord, str] | None = None
    for s in web.servlets:
        if s.module != module:
            continue
        for p in s.url_patterns:
            r = _rank(p, path)
            if r is not None and (best is None or r > best[0]):
                best = (r, s, p)
    return None if best is None else (best[1], best[2])


def resolve_servlet(web: WebIndex, module: str, path: str) -> ServletRecord | None:
    hit = resolve_with_pattern(web, module, path)
    return None if hit is None else hit[0]


def _overlaps(constraint_pat: str, servlet_pat: str) -> bool:
    if constraint_pat == servlet_pat:
        return True
    if servlet_pat in ("/", "") or constraint_pat in ("/", ""):
        return constraint_pat == "/*" or servlet_pat == "/*" or constraint_pat == servlet_pat
    return match_url_pattern(constraint_pat, servlet_pat) or match_url_pattern(
        servlet_pat, constraint_pat
    )


def constraints_for(
    module: ModuleRecord, url_patterns: Sequence[str]
) -> list[SecurityConstraint]:
    """Constraints of the module whose patterns cover (or lie inside) any servlet pattern."""
    return [
        c
        for c in module.security_constraints
        if any(_overlaps(cp, sp) for cp in c.url_patterns for sp in url_patterns)
    ]
