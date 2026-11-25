"""Per-tag naming normalizer: LLM rename maps, validated, with deterministic fallbacks."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Literal, Protocol

import inflect
from pydantic import BaseModel, Field

from j2ee_openapi_generator.aggregate.models import OperationRow
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import sha256_obj, sha256_text
from j2ee_openapi_generator.llm.base import LLMClient
from j2ee_openapi_generator.llm.cache import Cache, CacheMiss, normalizer_key
from j2ee_openapi_generator.llm.schemas import SCHEMA_VERSION, RenameMap
from j2ee_openapi_generator.progress import ProgressTracker, fmt_duration, metrics
from j2ee_openapi_generator.prompts import PROMPT_VERSION, render_prompt
from j2ee_openapi_generator.workspace import Workspace

WARNING_PREFIX = "warning: "
_ID_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")
MAX_SUMMARY = 120
MAX_DESCRIPTION = 1000
MAX_TAG_DESCRIPTION = 500
_EM_DASH = "—"
_inflect = inflect.engine()


class NormalizeStore(Protocol):
    def operations(self) -> list[OperationRow]: ...

    def set_operation_texts(
        self,
        method: str,
        path: str,
        operation_id: str | None,
        summary: str | None,
        description: str | None,
    ) -> None: ...

    def set_tag_description(self, tag: str, description: str) -> None: ...

    def tag_descriptions(self) -> dict[str, str]: ...

    def tags(self) -> list[str]: ...


class TagReport(BaseModel):
    tag: str
    status: Literal["ok", "fallback", "failed"]
    errors: list[str] = Field(default_factory=list)
    cached: bool = False


class NormalizeReport(BaseModel):
    tags: list[TagReport] = Field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [f"{t.tag}: {e}" for t in self.tags for e in t.errors]


# ------------------------------------------------------------------ listing
def _key(row: OperationRow) -> str:
    return f"{row.method.upper()} {row.path}"


def _sorted_rows(rows: list[OperationRow]) -> list[OperationRow]:
    return sorted(rows, key=lambda r: (r.path, r.method.upper()))


def _schema_names(row: OperationRow) -> str:
    op = row.operation
    names: set[str] = set()
    if op.request_body and op.request_body.schema_ref:
        names.add(op.request_body.schema_ref)
    for r in op.responses:
        if r.schema_ref:
            names.add(r.schema_ref)
    return ",".join(sorted(names)) or "-"


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split()) or "-"


def build_listing(tag: str, rows: list[OperationRow]) -> str:
    """Compact deterministic listing of the operations of ``tag``."""
    lines = [
        f"{_key(r)} | {r.operation_id or '-'} | {_one_line(r.operation.summary)} | "
        f"{_schema_names(r)}"
        for r in _sorted_rows([r for r in rows if r.operation.tag == tag])
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ validation
def _clean_text(text: str) -> str:
    return text.replace(_EM_DASH, ", ")


def validate_rename_map(
    rm: RenameMap, rows: list[OperationRow], existing_ids: set[str]
) -> tuple[RenameMap, list[str]]:
    """Validate and clean a rename map for the tag ``rm.tag``.

    Returns ``(cleaned, messages)``. Messages starting with ``warning: `` are non fatal
    (unknown keys, skipped); any other message means the map must be rejected.
    """
    msgs: list[str] = []
    tag = rm.tag
    tag_rows = [r for r in rows if r.operation.tag == tag]
    if not tag_rows:
        msgs.append(f"no operations for tag {tag!r}")
    valid_keys = {_key(r) for r in tag_rows}
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    ops = []
    for o in rm.operations:
        if o.key not in valid_keys:
            msgs.append(f"{WARNING_PREFIX}unknown key {o.key!r} skipped")
            continue
        if o.key in seen_keys:
            msgs.append(f"duplicate key {o.key!r}")
            continue
        seen_keys.add(o.key)
        oid = o.operation_id
        if not _ID_RE.match(oid) or len(oid) < 2:
            msgs.append(f"{o.key}: invalid operationId {oid!r}")
        elif oid in seen_ids:
            msgs.append(f"{o.key}: duplicate operationId {oid!r}")
        elif oid in existing_ids:
            msgs.append(f"{o.key}: operationId {oid!r} already used elsewhere")
        seen_ids.add(oid)
        summary = o.summary
        if summary is not None:
            summary = _clean_text(summary).strip()
            if not summary:
                msgs.append(f"{o.key}: empty summary")
            elif "\n" in summary or "\r" in summary:
                msgs.append(f"{o.key}: summary spans several lines")
            elif len(summary) > MAX_SUMMARY:
                msgs.append(f"{o.key}: summary longer than {MAX_SUMMARY} chars")
        desc = o.description
        if desc is not None:
            desc = _clean_text(desc).strip()
            if len(desc) > MAX_DESCRIPTION:
                msgs.append(f"{o.key}: description longer than {MAX_DESCRIPTION} chars")
        ops.append(o.model_copy(update={"summary": summary, "description": desc or None}))
    tds = []
    for td in rm.tag_descriptions:
        if td.tag != tag:
            msgs.append(f"tag description for {td.tag!r}, expected {tag!r}")
            continue
        text = _clean_text(td.description).strip()
        if len(text) > MAX_TAG_DESCRIPTION:
            msgs.append(f"tag description longer than {MAX_TAG_DESCRIPTION} chars")
        tds.append(td.model_copy(update={"description": text}))
    return RenameMap(tag=tag, operations=ops, tag_descriptions=tds), msgs


def fatal_errors(msgs: list[str]) -> list[str]:
    return [m for m in msgs if not m.startswith(WARNING_PREFIX)]


# ------------------------------------------------------------------ fallback ids
_VERBS = frozenset(
    "list get create update replace patch delete search export import upload download submit "
    "approve cancel confirm start finish reset send lookup find add remove set close open "
    "activate deactivate archive restore validate generate print view read check".split()
)
_METHOD_VERBS_ITEM = {
    "GET": "get",
    "PUT": "replace",
    "PATCH": "update",
    "DELETE": "delete",
    "POST": "create",
}


def _tokens(seg: str) -> list[str]:
    parts = re.split(r"[^A-Za-z0-9]+", seg)
    out: list[str] = []
    for p in parts:
        out.extend(re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", p))
    return [t.lower() for t in out if t]


def _singular(word: str) -> str:
    res = _inflect.singular_noun(word)
    return res if isinstance(res, str) and res else word


def _plural(word: str) -> str:
    res = _inflect.plural_noun(_singular(word))
    return res if isinstance(res, str) and res else word


def _pascal(tokens: list[str]) -> str:
    return "".join(t[:1].upper() + t[1:] for t in tokens)


def _noun(segs: list[str], plural: bool) -> str:
    """Pascal noun from static segments; earlier ones singular, last one per ``plural``."""
    pieces: list[str] = []
    for i, seg in enumerate(segs):
        toks = _tokens(seg)
        if not toks:
            continue
        last = i == len(segs) - 1
        toks[-1] = _plural(toks[-1]) if (last and plural) else _singular(toks[-1])
        pieces.append(_pascal(toks))
    return "".join(pieces) or "Resource"


def fallback_operation_id(method: str, path: str, taken: set[str]) -> str:
    """Deterministic verbNoun operationId from method and path; unique against ``taken``.

    The chosen id is added to ``taken``.
    """
    m = method.upper()
    segs = [s for s in path.split("/") if s]
    is_param = [s.startswith("{") and s.endswith("}") for s in segs]
    static = [(s, p) for s, p in zip(segs, is_param, strict=True)]
    verb: str
    name: str
    if segs and segs[0].lower() in ("lookups", "lookup") and len(segs) >= 2:
        rest = [s for s, p in static[1:] if not p]
        verb, name = "lookup", _noun(rest or ["resource"], True)
    else:
        # trailing action segment
        action_toks: list[str] = []
        end = len(segs)
        if end >= 2 and not is_param[-1]:
            toks = _tokens(segs[-1])
            if toks and toks[0] in _VERBS:
                action_toks = toks
                end -= 1
        stat = [s for s, p in zip(segs[:end], is_param[:end], strict=True) if not p]
        item = end > 0 and is_param[end - 1]
        if action_toks:
            after_param = item
            verb = action_toks[0]
            extra = _pascal(action_toks[1:])
            name = extra + _noun(stat, plural=not after_param)
        elif item:
            verb = _METHOD_VERBS_ITEM.get(m, "get")
            name = _noun(stat, plural=False)
        else:
            if m == "GET":
                verb, name = "list", _noun(stat, plural=True)
            elif m == "POST":
                verb, name = "create", _noun(stat, plural=False)
            else:
                verb = _METHOD_VERBS_ITEM.get(m, "get")
                name = _noun(stat, plural=True)
    base = verb + name
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}{n}"
        n += 1
    taken.add(cand)
    return cand


# ------------------------------------------------------------------ run
def _style_guide_text(cfg: Config) -> str:
    p = cfg.style_guide_path
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _build_messages(style_guide: str, listing: str) -> list[dict[str, str]]:
    schema = json.dumps(RenameMap.model_json_schema(), sort_keys=True, indent=2)
    prompt = render_prompt("normalizer", style_guide=style_guide, listing=listing, schema=schema)
    return [{"role": "user", "content": prompt}]


class _Fetched(BaseModel):
    parsed: dict[str, object] | None = None
    errors: list[str] = Field(default_factory=list)
    cached: bool = False


async def run_normalize(
    cfg: Config,
    client: LLMClient,
    ws: Workspace,
    store: NormalizeStore,
    *,
    offline: bool = False,
    force: bool = False,
) -> NormalizeReport:
    rows = store.operations()
    tags = sorted({r.operation.tag for r in rows} | set(store.tags()))
    tags = [t for t in tags if any(r.operation.tag == t for r in rows)]
    style_guide = _style_guide_text(cfg)
    style_hash = sha256_text(style_guide)
    cache = Cache(ws.cache_dir)
    listings = {t: build_listing(t, rows) for t in tags}
    keys = {
        t: normalizer_key(
            t,
            sha256_obj(listings[t]),
            PROMPT_VERSION,
            SCHEMA_VERSION,
            style_hash,
            client.model_name,
            client.structured_mode,
            client.temperature,
        )
        for t in tags
    }

    cached_hits: dict[str, dict[str, object]] = {}
    if not force:
        for t in tags:
            rec = cache.get(keys[t])
            if rec and rec.get("ok") and isinstance(rec.get("parsed"), dict):
                cached_hits[t] = rec["parsed"]
    if offline:
        misses = [t for t in tags if t not in cached_hits]
        if misses:
            raise CacheMiss(f"offline: no cached normalizer result for tags {misses}")

    sem = asyncio.Semaphore(max(1, cfg.llm.max_concurrency))
    metrics.log(
        f"normalize: {len(tags)} tag(s), {len(cached_hits)} cached, "
        f"{len(tags) - len(cached_hits)} to send to LLM"
    )
    tracker = ProgressTracker("normalize", len(tags))

    async def fetch(tag: str) -> _Fetched:
        if tag in cached_hits:
            metrics.record_cached("normalize", (cache.get(keys[tag]) or {}).get("usage"))
            tracker.advance(f"{tag} (cache)")
            return _Fetched(parsed=cached_hits[tag], cached=True)
        messages = _build_messages(style_guide, listings[tag])
        async with sem:
            t0 = time.monotonic()
            try:
                res = await client.structured(
                    purpose="normalize",
                    item_id=tag,
                    messages=messages,
                    response_model=RenameMap,
                )
            except Exception as exc:  # noqa: BLE001 - any client failure falls back
                tracker.advance(f"{tag} FAILED")
                return _Fetched(errors=[f"llm call failed: {type(exc).__name__}: {exc}"])
            dt = time.monotonic() - t0
        metrics.record_call("normalize", res, dt)
        tracker.advance(
            f"{tag} {'ok' if res.ok else 'FAILED'} {fmt_duration(dt)}, "
            f"{res.usage.get('total_tokens', 0):,} tok"
        )
        if res.ok and res.parsed is not None:
            cache.put(
                keys[tag],
                res.request,
                res.raw,
                res.parsed,
                ok=True,
                errors=res.errors,
                usage=res.usage,
            )
            return _Fetched(parsed=res.parsed)
        return _Fetched(errors=list(res.errors) or ["llm returned no result"])

    fetched = dict(zip(tags, await asyncio.gather(*(fetch(t) for t in tags)), strict=True))

    taken: set[str] = set()
    report = NormalizeReport()
    for tag in tags:
        f = fetched[tag]
        tag_rows = _sorted_rows([r for r in rows if r.operation.tag == tag])
        errors = list(f.errors)
        status: Literal["ok", "fallback", "failed"] = "failed" if f.errors else "ok"
        cleaned: RenameMap | None = None
        if f.parsed is not None:
            try:
                rm = RenameMap.model_validate(f.parsed)
            except ValueError as exc:
                errors.append(f"invalid rename map: {exc}")
                status = "fallback"
            else:
                if rm.tag != tag:
                    errors.append(f"rename map tag {rm.tag!r} != {tag!r}")
                    status = "fallback"
                else:
                    c, msgs = validate_rename_map(rm, rows, taken)
                    errors.extend(msgs)
                    if fatal_errors(msgs):
                        status = "fallback"
                    else:
                        cleaned = c
        by_key = {o.key: o for o in cleaned.operations} if cleaned else {}
        taken.update(o.operation_id for o in by_key.values())
        for row in tag_rows:
            ren = by_key.get(_key(row))
            if ren is not None:
                oid = ren.operation_id
                summary = ren.summary or row.operation.summary
                desc = ren.description
            else:
                oid = fallback_operation_id(row.method, row.path, taken)
                summary = row.operation.summary
                desc = row.description
            store.set_operation_texts(row.method, row.path, oid, summary, desc)
        if cleaned is not None:
            for td in cleaned.tag_descriptions:
                if td.description:
                    store.set_tag_description(td.tag, td.description)
        if cleaned is not None and len(by_key) < len(tag_rows):
            errors.append(f"{WARNING_PREFIX}{len(tag_rows) - len(by_key)} operations lack a rename")
            status = "fallback" if status == "ok" else status
        report.tags.append(TagReport(tag=tag, status=status, errors=errors, cached=f.cached))
    return report


def normalize_sync(
    cfg: Config,
    client: LLMClient,
    ws: Workspace,
    store: NormalizeStore,
    *,
    offline: bool = False,
    force: bool = False,
) -> NormalizeReport:
    return asyncio.run(run_normalize(cfg, client, ws, store, offline=offline, force=force))
