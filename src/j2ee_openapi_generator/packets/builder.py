"""Build work packets: bounded, line-numbered source slices for each entry point.

Chunk text format (documented contract, parsed by the verifier and the budget splitter)::

    === FILE path/to/File.java (lines 120-188) ===
     120| first line
     121| second line

Each line is ``f"{n:4d}| {text}"``: the real file line number right-aligned in at least four
columns, then ``| `` and the source text verbatim. Numbers above 9999 simply widen the column.
Chunks may skip lines (JSP response excerpts); numbering always stays the real line number.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import entry_point_id, id_to_filename
from j2ee_openapi_generator.index.access_logs import usage_for
from j2ee_openapi_generator.index.ejb import all_type_names
from j2ee_openapi_generator.index.linker import jsps_for_entry
from j2ee_openapi_generator.index.models import (
    ClassInfo,
    DispatchEntry,
    EjbMethod,
    EjbRecord,
    EntityRecord,
    IndexBundle,
    LinkedRef,
    ServletRecord,
)
from j2ee_openapi_generator.index.properties import lookup_keys
from j2ee_openapi_generator.index.webxml import constraints_for
from j2ee_openapi_generator.packets.budget import (
    count_tokens,
    fit_packet,
    format_chunk_text,
    token_budget,
)
from j2ee_openapi_generator.packets.hints import (
    compute_hints,
    only_forwards_form,
    pick_primary_entity,
    servlet_noun,
)
from j2ee_openapi_generator.packets.models import EntryPointMeta, SourceChunk, WorkPacket
from j2ee_openapi_generator.prompts import load_prompt
from j2ee_openapi_generator.workspace import Workspace, write_json

ROLE_ORDER = (
    "branch",
    "dispatch",
    "helper",
    "ejb_class",
    "web_security",
    "ejb_interface",
    "ejb_impl",
    "entity",
    "jsp_request",
    "jsp_response",
)
WHOLE_JSP_LINES = 120
SMALL_CLASS_LINES = 40
_FORM_OPEN = re.compile(r"<form\b", re.I)
_FORM_CLOSE = re.compile(r"</form\s*>", re.I)
_RESPONSE_LINE = re.compile(r"forEach|fmt:message|\$\{|<input\b|<select\b|<textarea\b", re.I)
_STRING_LITERAL = re.compile(r'"((?:\\.|[^"\\\n])*)"')
_DECL = re.compile(r"\b(class|interface|enum)\b")
_BINARY_FREE = ("text/html", "application/json", "text/plain", "application/xml", "text/xml")


# ------------------------------------------------------------------ chunk rendering
def render_chunk(file: str, lines: list[str], start: int, end: int, role: str) -> SourceChunk:
    """Render lines ``start..end`` (1-based, inclusive, clamped) of ``lines`` as a chunk."""
    lo = max(1, start)
    hi = min(len(lines), end)
    return _chunk(file, [(n, lines[n - 1]) for n in range(lo, hi + 1)], role)


def _chunk(file: str, numbered: list[tuple[int, str]], role: str) -> SourceChunk:
    return SourceChunk(
        file=file,
        start_line=numbered[0][0],
        end_line=numbered[-1][0],
        role=role,
        text=format_chunk_text(file, numbered),
    )


def _ranges(numbers: Iterable[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


class _Sources:
    """Per-process cache of source lines."""

    def __init__(self, read_source: Callable[[str], str]) -> None:
        self._read = read_source
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, path: str) -> list[str] | None:
        if path not in self._cache:
            try:
                text = self._read(path)
            except OSError:
                self._cache[path] = None
            else:
                self._cache[path] = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return self._cache[path]


# ------------------------------------------------------------------ java side
def _decl_line(lines: list[str], start: int) -> int:
    for n in range(start, min(len(lines), start + 6) + 1):
        if _DECL.search(lines[n - 1]):
            return n
    return start


def _ejb_for(bundle: IndexBundle, fqcn: str) -> EjbRecord | None:
    for ejb in bundle.domain.ejbs:
        if fqcn == ejb.bean_fqcn or fqcn in ejb.interfaces:
            return ejb
    return None


def _sig_types(m: EjbMethod) -> list[str]:
    inner = m.signature[m.signature.find("(") + 1 : m.signature.rfind(")")]
    names = list(all_type_names(m.return_type))
    for t in inner.split(","):
        if t:
            names.extend(all_type_names(t))
    return names


def _ejb_chunks(
    entry: DispatchEntry, bundle: IndexBundle, src: _Sources
) -> tuple[list[SourceChunk], list[str]]:
    """Interface and implementation chunks of the called EJB methods, plus their type names."""
    iface_lines: dict[str, set[int]] = {}
    impl: list[SourceChunk] = []
    seen_impl: set[tuple[str, int]] = set()
    types: list[str] = []
    for call in sorted(set(entry.facts.ejb_calls)):
        fqcn, _, sig = call.partition("#")
        ejb = _ejb_for(bundle, fqcn)
        if ejb is None:
            continue
        method = next((m for m in ejb.methods if m.signature == sig), None)
        if method is None:
            continue
        types.extend(_sig_types(method))
        iface_file = ejb.interface_files.get(fqcn)
        ci = bundle.java.classes.get(fqcn)
        if iface_file and ci is not None:
            ilines = src.lines(iface_file)
            im = next((m for m in ci.methods if m.signature == sig), None)
            if ilines is not None and im is not None:
                nums = iface_lines.setdefault(iface_file, set())
                nums.add(_decl_line(ilines, ci.start_line))
                nums.update(range(im.start_line, im.end_line + 1))
        key = (ejb.impl_file, method.start_line)
        olines = src.lines(ejb.impl_file)
        if olines is not None and key not in seen_impl:
            seen_impl.add(key)
            impl.append(
                render_chunk(ejb.impl_file, olines, method.start_line, method.end_line, "ejb_impl")
            )
    iface: list[SourceChunk] = []
    for path in sorted(iface_lines):
        ilines = src.lines(path)
        if ilines is None:
            continue
        picked = sorted(n for n in iface_lines[path] if 1 <= n <= len(ilines))
        iface.append(_chunk(path, [(n, ilines[n - 1]) for n in picked], "ejb_interface"))
    return [*iface, *impl], types


def _module_prefix(bundle: IndexBundle, module: str) -> str:
    for m in bundle.web.modules:
        if m.name == module:
            return m.root_dir + "/" if m.root_dir else ""
    return ""


def _entity_chunk(rec: EntityRecord, src: _Sources) -> SourceChunk | None:
    lines = src.lines(rec.file)
    if lines is None:
        return None
    decl = _decl_line(lines, rec.start_line)
    if rec.kind == "enum" or not rec.fields:
        return render_chunk(rec.file, lines, decl, rec.end_line, "entity")
    nums: set[int] = {decl}
    for f in rec.fields:
        if f.line < 1 or f.line > len(lines):
            continue
        first = f.line
        while first > decl + 1 and lines[first - 2].strip().startswith("@"):
            first -= 1
        last = f.line
        while last < len(lines) and ";" not in lines[last - 1]:
            last += 1
        nums.update(range(first, last + 1))
    return _chunk(rec.file, [(n, lines[n - 1]) for n in sorted(nums)], "entity")


def _entity_chunks(
    entry: DispatchEntry, bundle: IndexBundle, src: _Sources, type_names: list[str]
) -> list[SourceChunk]:
    prefix = _module_prefix(bundle, entry.module)
    by_name: dict[str, list[EntityRecord]] = {}
    for e in bundle.domain.entities:
        by_name.setdefault(e.simple_name, []).append(e)

    def pick(name: str) -> EntityRecord | None:
        cands = by_name.get(name, [])
        local = [c for c in cands if c.file.startswith(prefix)]
        return (local or cands)[0] if (local or cands) else None

    chosen: dict[str, EntityRecord] = {}
    queue = sorted(set(type_names))
    for depth in range(2):
        nxt: list[str] = []
        for name in queue:
            rec = pick(name)
            if rec is None or rec.fqcn in chosen:
                continue
            chosen[rec.fqcn] = rec
            for f in rec.fields:
                if f.enumerated or f.relationship or depth == 0:
                    nxt.extend(all_type_names(f.java_type))
        queue = sorted(set(nxt))
    chunks = [c for r in chosen.values() if (c := _entity_chunk(r, src)) is not None]
    # exception classes thrown or caught by the branch
    for exc in sorted({*entry.facts.exceptions_thrown, *entry.facts.exceptions_caught}):
        ci = _class_by_simple(bundle, exc, prefix)
        lines = src.lines(ci.file) if ci else None
        if ci is not None and lines is not None and ci.end_line - ci.start_line < SMALL_CLASS_LINES:
            chunks.append(
                render_chunk(
                    ci.file, lines, _decl_line(lines, ci.start_line), ci.end_line, "entity"
                )
            )
    return chunks


def _class_by_simple(bundle: IndexBundle, simple: str, prefix: str) -> ClassInfo | None:
    cands = [c for c in bundle.java.classes.values() if c.simple_name == simple]
    local = [c for c in cands if c.file.startswith(prefix)]
    return (local or cands)[0] if (local or cands) else None


# ------------------------------------------------------------------ jsp side
def _form_range(lines: list[str], line: int) -> tuple[int, int] | None:
    start = None
    for n in range(min(line, len(lines)), max(0, line - 4), -1):
        if _FORM_OPEN.search(lines[n - 1]):
            start = n
            break
    if start is None:
        return None
    for n in range(start, min(len(lines), start + 80) + 1):
        if _FORM_CLOSE.search(lines[n - 1]):
            return start, n
    return start, min(len(lines), start + 20)


def _request_chunks(refs: list[LinkedRef], src: _Sources) -> list[SourceChunk]:
    wanted: dict[str, set[int]] = {}
    for r in refs:
        lines = src.lines(r.source_file)
        if lines is None:
            continue
        nums = wanted.setdefault(r.source_file, set())
        rng = _form_range(lines, r.line) if r.kind == "form" else None
        if rng is None:
            rng = (r.line, min(len(lines), r.line + (2 if r.kind == "js" else 0)))
        nums.update(range(rng[0], rng[1] + 1))
    out: list[SourceChunk] = []
    for path in sorted(wanted):
        lines = src.lines(path) or []
        for lo, hi in _ranges(n for n in wanted[path] if 1 <= n <= len(lines)):
            out.append(render_chunk(path, lines, lo, hi, "jsp_request"))
    return out


def _response_chunk(path: str, src: _Sources) -> SourceChunk | None:
    lines = src.lines(path)
    if not lines:
        return None
    if len(lines) <= WHOLE_JSP_LINES:
        return render_chunk(path, lines, 1, len(lines), "jsp_response")
    nums: set[int] = set()
    for i, text in enumerate(lines, start=1):
        if _RESPONSE_LINE.search(text):
            nums.update(n for n in (i - 1, i, i + 1) if 1 <= n <= len(lines))
    if not nums:
        return None
    return _chunk(path, [(n, lines[n - 1]) for n in sorted(nums)], "jsp_response")


# ------------------------------------------------------------------ packet
def _servlet_record(bundle: IndexBundle, entry: DispatchEntry) -> ServletRecord | None:
    for s in bundle.web.servlets:
        if s.module == entry.module and s.class_fqcn == entry.servlet_fqcn:
            return s
    return None


def _is_binary(entry: DispatchEntry) -> bool:
    f = entry.facts
    return bool(f.content_disposition) or any(
        not ct.lower().startswith(_BINARY_FREE) for ct in f.content_types
    )


MAX_DISPATCH_LINES = 40
MAX_WEB_XML_LINES = 200


def _context_chunks(
    entry: DispatchEntry, bundle: IndexBundle, src: _Sources, module_web_xml: str | None
) -> list[SourceChunk]:
    """Dispatcher lines before the branch, EJB class headers and the module's web.xml.

    These make role, constraint and registration evidence citeable.
    """
    out: list[SourceChunk] = []
    lines = src.lines(entry.file) or []
    classes = [c for c in bundle.java.classes.values() if c.file == entry.file]
    if entry.style == "map":
        for c in classes:
            if c.fqcn == entry.servlet_fqcn:
                for m in c.methods:
                    if m.name == "init":
                        out.append(
                            render_chunk(entry.file, lines, m.start_line, m.end_line, "dispatch")
                        )
    elif entry.style in ("if", "switch", "default"):
        best = None
        for c in classes:
            for m in c.methods:
                if m.start_line <= entry.start_line and entry.end_line <= m.end_line:
                    if best is None or m.start_line > best.start_line:
                        best = m
        if best is not None and best.start_line < entry.start_line:
            start = max(best.start_line, entry.start_line - MAX_DISPATCH_LINES)
            out.append(render_chunk(entry.file, lines, start, entry.start_line - 1, "dispatch"))
    seen: set[str] = set()
    for call in sorted(set(entry.facts.ejb_calls)):
        ejb = _ejb_for(bundle, call.partition("#")[0])
        if ejb is None or ejb.bean_fqcn in seen:
            continue
        seen.add(ejb.bean_fqcn)
        ci = bundle.java.classes.get(ejb.bean_fqcn)
        olines = src.lines(ejb.impl_file)
        if ci is not None and olines is not None:
            decl = _decl_line(olines, ci.start_line)
            out.append(render_chunk(ejb.impl_file, olines, ci.start_line, decl, "ejb_class"))
    if module_web_xml:
        wl = src.lines(module_web_xml)
        if wl is not None and len(wl) <= MAX_WEB_XML_LINES:
            out.append(render_chunk(module_web_xml, wl, 1, len(wl), "web_security"))
    return out


def _sort_chunks(chunks: list[SourceChunk]) -> list[SourceChunk]:
    seen: set[tuple[str, int, int, str]] = set()
    unique: list[SourceChunk] = []
    for c in chunks:
        key = (c.file, c.start_line, c.end_line, c.role)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return sorted(
        unique, key=lambda c: (ROLE_ORDER.index(c.role), c.file, c.start_line, c.end_line)
    )


def build_packet(
    entry: DispatchEntry,
    bundle: IndexBundle,
    read_source: Callable[[str], str],
    cfg: Config,
    prompt_tokens: int,
) -> list[WorkPacket]:
    """Build the packet of one entry point; more than one only when it had to be split."""
    return _build(entry, bundle, _Sources(read_source), cfg, prompt_tokens)


def _build(
    entry: DispatchEntry, bundle: IndexBundle, src: _Sources, cfg: Config, prompt_tokens: int
) -> list[WorkPacket]:
    f = entry.facts
    servlet = _servlet_record(bundle, entry)
    patterns = sorted(servlet.url_patterns) if servlet else []
    module = next((m for m in bundle.web.modules if m.name == entry.module), None)
    link_op = None if entry.op == "<default>" else entry.op
    refs = [
        r
        for r in bundle.links
        if r.servlet_fqcn == entry.servlet_fqcn and r.op == link_op and r.module == entry.module
    ]
    submitting = jsps_for_entry(refs, entry.servlet_fqcn, link_op)
    forwards = sorted(set(f.forwards))
    jsps = sorted({*forwards, *submitting})

    chunks: list[SourceChunk] = []
    code_ranges: list[tuple[str, int, int]] = [(entry.file, entry.start_line, entry.end_line)]
    lines = src.lines(entry.file) or []
    chunks.append(render_chunk(entry.file, lines, entry.start_line, entry.end_line, "branch"))
    for _name, s, e in sorted(f.helper_ranges, key=lambda h: (h[1], h[2], h[0])):
        if entry.start_line <= s and e <= entry.end_line:
            continue
        chunks.append(render_chunk(entry.file, lines, s, e, "helper"))
        code_ranges.append((entry.file, s, e))
    ejb_chunks, type_names = _ejb_chunks(entry, bundle, src)
    chunks.extend(ejb_chunks)
    chunks.extend(_context_chunks(entry, bundle, src, module.web_xml if module else None))
    chunks.extend(_entity_chunks(entry, bundle, src, type_names))
    chunks.extend(_request_chunks(refs, src))
    for path in forwards:
        rc = _response_chunk(path, src)
        if rc is not None:
            chunks.append(rc)

    keys: set[str] = set()
    for path in jsps:
        keys.update(bundle.properties.key_refs.get(path, []))
    known = {k for b in bundle.properties.bundles for k in b.entries}
    for path, s, e in code_ranges:
        for text in (src.lines(path) or [])[s - 1 : e]:
            keys.update(k for k in _STRING_LITERAL.findall(text) if k in known)
    properties = lookup_keys(bundle.properties, sorted(keys))

    primary = pick_primary_entity(entry, bundle)
    noun = servlet_noun(entry.servlet_simple_name).lower()
    has_id = any(p.lower() in ("id", f"{noun}id") for p in f.params)
    hints = compute_hints(
        entry,
        entry.servlet_simple_name,
        primary,
        has_id_param=has_id,
        forwards_only_form=only_forwards_form(entry),
        binary_response=_is_binary(entry),
        multipart=bool(f.multipart_parts),
    )
    if entry.op == "<default>":
        usage = sum(
            c.count
            for c in bundle.access
            if c.op is None and c.module == entry.module and c.url_pattern in set(patterns)
        )
    else:
        usage = usage_for(bundle.access, patterns, entry.op, entry.module)

    meta = EntryPointMeta(
        id=entry_point_id(entry.module, entry.servlet_simple_name, entry.op),
        module=entry.module,
        servlet_fqcn=entry.servlet_fqcn,
        op=entry.op,
        param_name=entry.param_name,
        style=entry.style,
        http_methods=sorted(entry.http_methods),
        url_patterns=patterns,
        context_root=(servlet.context_root if servlet else "")
        or (module.context_root if module else ""),
        security_roles=sorted(module.security_roles) if module else [],
        security_constraints=[c.model_dump(mode="json") for c in constraints_for(module, patterns)]
        if module
        else [],
        jsps=jsps,
    )
    packet = WorkPacket(
        id=meta.id,
        meta=meta,
        chunks=_sort_chunks(chunks),
        properties=properties,
        hints=hints,
        usage_count=usage,
    )
    return fit_packet(packet, token_budget(cfg.llm, prompt_tokens))


# ------------------------------------------------------------------ parallel driver
_G_BUNDLE: IndexBundle | None = None
_G_CFG: Config | None = None
_G_SRC: _Sources | None = None
_G_PROMPT_TOKENS = 0


def _init_worker(bundle: IndexBundle, cfg: Config, prompt_tokens: int) -> None:
    global _G_BUNDLE, _G_CFG, _G_SRC, _G_PROMPT_TOKENS
    _G_BUNDLE, _G_CFG, _G_PROMPT_TOKENS = bundle, cfg, prompt_tokens
    repo = cfg.repo_path
    _G_SRC = _Sources(lambda rel: (repo / rel).read_text(encoding="utf-8", errors="replace"))


def _work(index: int) -> list[WorkPacket]:
    assert _G_BUNDLE is not None and _G_CFG is not None and _G_SRC is not None
    return _build(_G_BUNDLE.dispatch[index], _G_BUNDLE, _G_SRC, _G_CFG, _G_PROMPT_TOKENS)


def analyzer_prompt_tokens(cfg: Config) -> int:
    """Tokens of the analyzer template plus the style guide (the fixed prompt overhead)."""
    path = cfg.style_guide_path
    guide = path.read_text(encoding="utf-8") if path.is_file() else ""
    return count_tokens(load_prompt("analyzer")) + count_tokens(guide)


def build_packets(cfg: Config, bundle: IndexBundle, workers: int = 1) -> list[WorkPacket]:
    """Packets for every dispatch entry, sorted by packet id; identical for any ``workers``."""
    prompt_tokens = analyzer_prompt_tokens(cfg)
    indexes = list(range(len(bundle.dispatch)))
    if workers > 1 and len(indexes) > 1:
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(bundle, cfg, prompt_tokens)
        ) as pool:
            results = list(pool.map(_work, indexes, chunksize=4))
    else:
        _init_worker(bundle, cfg, prompt_tokens)
        results = [_work(i) for i in indexes]
    return sorted((p for group in results for p in group), key=lambda p: p.id)


def write_packets(ws: Workspace, packets: list[WorkPacket]) -> list[Path]:
    """Write ``packets/{id_to_filename(id)}.json`` and delete stale files in that directory."""
    ws.packets_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for p in packets:
        path = ws.packets_dir / f"{id_to_filename(p.id)}.json"
        write_json(
            path,
            p.model_dump(mode="json"),
        )
        written[path.name] = path
    for old in ws.packets_dir.glob("*.json"):
        if old.name not in written:
            old.unlink()
    return [written[k] for k in sorted(written)]
