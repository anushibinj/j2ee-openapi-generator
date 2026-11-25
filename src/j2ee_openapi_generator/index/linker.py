"""Resolve JSP/JS URLs to servlets and ops."""

from __future__ import annotations

import posixpath
import re

from j2ee_openapi_generator.index.jsp import _resolve_c_url, split_query
from j2ee_openapi_generator.index.models import (
    JspFacts,
    LinkedRef,
    ModuleRecord,
    ServletRecord,
    WebIndex,
)

_CTX_RE = re.compile(r"^\$\{[^}]*[cC]ontextPath\s*\}")
_STATIC_EXT = frozenset(
    {
        ".css", ".js", ".png", ".gif", ".jpg", ".jpeg", ".svg", ".ico", ".html", ".htm",
        ".woff", ".woff2", ".ttf", ".map", ".json", ".txt", ".pdf",
    }
)  # fmt: skip


def _norm_pattern(p: str) -> str:
    if p.startswith("*.") or p.startswith("/"):
        return p
    return "/" + p


def match_servlet(servlets: list[ServletRecord], path: str) -> ServletRecord | None:
    """Servlet-spec mapping: exact, longest prefix, extension, then default ``/``."""
    best_prefix: tuple[int, ServletRecord] | None = None
    ext_hit: ServletRecord | None = None
    default_hit: ServletRecord | None = None
    for sv in sorted(servlets, key=lambda s: (s.module, s.class_fqcn)):
        for raw in sv.url_patterns:
            pat = _norm_pattern(raw.strip())
            if pat == path:
                return sv
            if pat.endswith("/*"):
                pre = pat[:-2]
                if (path == pre or path.startswith(pre + "/")) and (
                    best_prefix is None or len(pre) > best_prefix[0]
                ):
                    best_prefix = (len(pre), sv)
            elif pat.startswith("*."):
                if posixpath.basename(path).endswith(pat[1:]) and ext_hit is None:
                    ext_hit = sv
            elif pat == "/" and default_hit is None:
                default_hit = sv
    if best_prefix is not None:
        return best_prefix[1]
    if ext_hit is not None:
        return ext_hit
    ext = posixpath.splitext(path)[1].lower()
    if default_hit is not None and ext not in _STATIC_EXT:
        return default_hit
    return None


def _module_of(file: str, modules: list[ModuleRecord]) -> str | None:
    best: ModuleRecord | None = None
    for m in modules:
        root = m.root_dir.strip("/")
        if (not root or file.startswith(root + "/")) and (
            best is None or len(root) > len(best.root_dir.strip("/"))
        ):
            best = m
    return best.name if best else None


def _web_path(file: str, module: ModuleRecord | None) -> str:
    idx = file.find("/webapp/")
    if idx >= 0:
        return file[idx + len("/webapp/") :]
    if file.startswith("webapp/"):
        return file[len("webapp/") :]
    if module is not None:
        root = module.root_dir.strip("/")
        if root and file.startswith(root + "/"):
            return file[len(root) + 1 :]
    return file


def _op_of(query: dict[str, str], dispatch_params: list[str]) -> str | None:
    for p in dispatch_params:
        v = query.get(p)
        if v is not None and v != "" and "${" not in v:
            return v
    return None


class _Resolver:
    def __init__(self, web: WebIndex) -> None:
        self.web = web
        self.modules = {m.name: m for m in web.modules}
        self.roots: dict[str, str] = {m.name: m.context_root.rstrip("/") for m in web.modules}
        for sv in web.servlets:
            self.roots.setdefault(sv.module, sv.context_root.rstrip("/"))
        self.by_module: dict[str, list[ServletRecord]] = {}
        for sv in web.servlets:
            self.by_module.setdefault(sv.module, []).append(sv)

    def resolve(
        self, url: str, jsp: JspFacts, module: str | None
    ) -> tuple[ServletRecord, dict[str, str]] | None:
        url = _resolve_c_url(url).strip()
        path, query = split_query(url)
        path = re.sub(r";jsessionid=[^/?]*", "", path, flags=re.IGNORECASE)
        had_ctx = False
        m = _CTX_RE.match(path)
        if m:
            path = path[m.end() :]
            had_ctx = True
        if not path or "${" in path or "<%" in path or path.startswith(("#", "javascript:")):
            return None
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path) or path.startswith("//"):
            return None

        mods = [module] if module else sorted(self.by_module)
        candidates: list[tuple[str, str]] = []  # (module, module-relative path)
        if path.startswith("/"):
            # a context root prefix selects the module, otherwise the path is context-relative
            for name, root in sorted(self.roots.items(), key=lambda kv: -len(kv[1])):
                if root and (path == root or path.startswith(root + "/")) and not had_ctx:
                    candidates.append((name, path[len(root) :] or "/"))
                    break
            for mod in mods:
                candidates.append((mod, path))
        else:
            mrec = self.modules.get(module) if module else None
            base = posixpath.dirname(_web_path(jsp.file, mrec))
            for mod in mods:
                candidates.append((mod, posixpath.normpath(posixpath.join("/", base, path))))
            for mod in mods:
                candidates.append((mod, posixpath.normpath(posixpath.join("/", path))))
        for mod, cpath in candidates:
            sv = match_servlet(self.by_module.get(mod, []), cpath)
            if sv is not None:
                return sv, query
        return None


def link_references(
    jsps: list[JspFacts], web: WebIndex, dispatch_params: list[str]
) -> list[LinkedRef]:
    """Link every form, anchor and JS call to a servlet entry (``op`` may be None)."""
    resolver = _Resolver(web)
    refs: list[LinkedRef] = []

    def emit(
        jsp: JspFacts,
        module: str | None,
        url: str,
        line: int,
        kind: str,
        method: str,
        explicit_op: str | None,
        names: list[str],
    ) -> None:
        hit = resolver.resolve(url, jsp, module)
        if hit is None:
            return
        sv, query = hit
        op = _op_of(query, dispatch_params) or explicit_op
        params = {k for k in [*query, *names] if k and k not in dispatch_params and "${" not in k}
        refs.append(
            LinkedRef(
                source_file=jsp.file,
                line=line,
                kind=kind,
                servlet_fqcn=sv.class_fqcn,
                module=sv.module,
                op=op,
                http_method=method.upper(),
                params=sorted(params),
            )
        )

    for jsp in jsps:
        module = jsp.module or _module_of(jsp.file, web.modules)
        for form in jsp.forms:
            if not form.action:
                continue
            emit(
                jsp, module, form.action, form.line, "form", form.method, form.op,
                [i.name for i in form.inputs],
            )  # fmt: skip
        for link in jsp.links:
            emit(jsp, module, link.url, link.line, "link", "GET", None, list(link.query))
        for call in jsp.js_calls:
            emit(jsp, module, call.url, call.line, "js", call.method, None, call.params)

    refs.sort(
        key=lambda r: (
            r.module,
            r.servlet_fqcn,
            r.op or "",
            r.source_file,
            r.line,
            r.kind,
            r.http_method,
        )  # fmt: skip
    )
    return refs


def jsps_for_entry(refs: list[LinkedRef], servlet_fqcn: str, op: str | None) -> list[str]:
    """Sorted unique source files whose refs target the servlet with exactly this op."""
    return sorted({r.source_file for r in refs if r.servlet_fqcn == servlet_fqcn and r.op == op})
