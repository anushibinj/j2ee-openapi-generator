"""Mechanical verification of analyzer proposals against their work packets.

Every rule yields reason strings with a stable code prefix (``E-CITE-FILE:`` ...). An empty
reason list means the proposal is verified. Nothing here calls an LLM; all checks are regex and
set membership over the packet's own numbered source lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from j2ee_openapi_generator.aggregate.models import ProposalRecord, VerifiedProposal
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import id_to_filename
from j2ee_openapi_generator.index.models import IndexBundle
from j2ee_openapi_generator.llm.schemas import EndpointProposal, Evidence, Operation
from j2ee_openapi_generator.packets.models import WorkPacket
from j2ee_openapi_generator.workspace import Workspace, read_json, write_json

STANDARD_STATUSES = frozenset({200, 201, 202, 204, 400, 401, 403, 500})
PRIMITIVE_SCHEMAS = frozenset(
    {"string", "integer", "number", "boolean", "object", "array", "binary", "Problem"}
)
DERIVED_SUFFIXES = ("CreateRequest", "UpdateRequest", "Patch", "Page")
STANDARD_QUERY = frozenset({"page", "size", "sort"})
WINDOW = 2

_LINE = re.compile(r"^\s*(\d+)\| ?(.*)$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_GET_PARAM = re.compile(r'getParameter(?:Values)?\(\s*"([^"]+)"')
_GET_PARAM_CONST = re.compile(r"getParameter(?:Values)?\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)")
_FORM_INPUT = re.compile(r"""\bname\s*=\s*["']([^"']+)["']""")
_URL_QUERY = re.compile(r"[?&]([A-Za-z_][A-Za-z0-9_.-]*)=")
_PATH_VAR = re.compile(r"\{([^}/]+)\}")
_ROLES_ALLOWED = re.compile(r"@RolesAllowed\s*\(([^)]*)\)")
_STATUS_EVIDENCE = re.compile(
    r"sendError|setStatus|SC_|throw|catch|Exception|required|NotNull|null|isEmpty"
    r"|@Version|OptimisticLock"
)
_ID_NAME = re.compile(r"[a-z0-9](Id|Number|Code)$")
_SESSION = re.compile(r"getSession|getAttribute")

JAVA_KEYWORDS = frozenset(
    "abstract assert boolean break byte case catch char class const continue default do double "
    "else enum extends final finally float for goto if implements import instanceof int "
    "interface long native new package private protected public return short static strictfp "
    "super switch synchronized this throw throws transient try void volatile while true false "
    "null var".split()
)


# ------------------------------------------------------------------ packet text
def parse_packet_lines(packet: WorkPacket) -> dict[str, dict[int, str]]:
    """Map file -> {line number -> code text} from the chunks' ``NNNN| code`` text."""
    out: dict[str, dict[int, str]] = {}
    for chunk in packet.chunks:
        lines = out.setdefault(chunk.file, {})
        for raw in chunk.text.splitlines():
            m = _LINE.match(raw)
            if m:
                lines[int(m.group(1))] = m.group(2)
    return out


def _norm(text: str) -> str:
    return " ".join(text.split())


def _window(lines: dict[int, str], line: int) -> str:
    return " ".join(lines[n] for n in range(line - WINDOW, line + WINDOW + 1) if n in lines)


def _snippet_tokens(snippet: str) -> tuple[list[str], list[str]]:
    literals = [m.group(1) for m in _STRING.finditer(snippet)]
    rest = _STRING.sub(" ", snippet)
    idents = sorted({t for t in _IDENT.findall(rest) if len(t) > 1 and t not in JAVA_KEYWORDS})
    return idents, literals


# ------------------------------------------------------------------ packet facts
class _Facts:
    """Everything the rules need from a packet, computed once."""

    def __init__(self, packet: WorkPacket) -> None:
        self.packet = packet
        self.lines = parse_packet_lines(packet)
        self.allowed = set(packet.allowed_files) | {c.file for c in packet.chunks}
        code_all = "\n".join(t for f in self.lines.values() for _, t in sorted(f.items()))
        self.code = code_all
        legacy: set[str] = set(_GET_PARAM.findall(code_all))
        for const in _GET_PARAM_CONST.findall(code_all):
            short = const.rsplit(".", 1)[-1]
            m = re.search(rf'\b{re.escape(short)}\s*=\s*"([^"]*)"', code_all)
            if m:
                legacy.add(m.group(1))
        for chunk in packet.chunks:
            if chunk.role == "jsp_request":
                legacy.update(_FORM_INPUT.findall(chunk.text))
        legacy.update(_URL_QUERY.findall(code_all))
        self.legacy = legacy
        self.reads_id = any(p.lower() == "id" or p.endswith("Id") for p in legacy)
        self.uses_session = bool(_SESSION.search(code_all))
        self.packet_roles = {
            r for m in _ROLES_ALLOWED.finditer(code_all) for r in _unquote_all(m.group(1))
        }


def _unquote_all(text: str) -> list[str]:
    return [m.group(1) for m in _STRING.finditer(text)]


# ------------------------------------------------------------------ evidence
def _check_evidence(facts: _Facts, ev: Evidence, where: str) -> list[str]:
    if ev.file not in facts.allowed:
        return [f"E-CITE-FILE: {where}: file {ev.file} is not in the packet"]
    lines = facts.lines.get(ev.file, {})
    if ev.line not in lines:
        return [f"E-CITE-LINE: {where}: {ev.file}:{ev.line} is not a numbered line of the packet"]
    if not ev.snippet.strip():
        return [f"E-CITE-SNIPPET: {where}: {ev.file}:{ev.line} has an empty snippet"]
    window = _window(lines, ev.line)
    norm_window = _norm(window)
    squeezed = "".join(window.split())
    window_idents = set(_IDENT.findall(window))
    idents, literals = _snippet_tokens(ev.snippet)
    missing: list[str] = [i for i in idents if i not in window_idents]
    for lit in literals:
        if _norm(lit) not in norm_window and "".join(lit.split()) not in squeezed:
            missing.append(f'"{lit}"')
    if missing:
        return [
            f"E-CITE-SNIPPET: {where}: {ev.file}:{ev.line} snippet tokens not found near the "
            f"line: {', '.join(missing)}"
        ]
    return []


def _op_label(op: Operation) -> str:
    return f"{op.http_method} {op.path}"


def _evidence_items(op: Operation) -> list[tuple[str, Evidence]]:
    label = _op_label(op)
    items: list[tuple[str, Evidence]] = []
    items += [(f"{label} operation", e) for e in op.evidence]
    for p in op.path_params:
        items += [(f"{label} path param {p.name}", e) for e in p.evidence]
    for p in op.query_params:
        items += [(f"{label} query param {p.name}", e) for e in p.evidence]
    if op.request_body:
        for f in op.request_body.fields:
            items += [(f"{label} request field {f.name}", e) for e in f.evidence]
    for r in op.responses:
        items += [(f"{label} response {r.status}", e) for e in r.evidence]
        for f in r.fields:
            items += [(f"{label} response {r.status} field {f.name}", e) for e in f.evidence]
    for role in op.roles:
        items += [(f"{label} role {role.name}", e) for e in role.evidence]
    return items


# ------------------------------------------------------------------ rules
def _check_params(facts: _Facts, op: Operation, dispatch: set[str]) -> list[str]:
    out: list[str] = []
    label = _op_label(op)
    paginated = any(r.paginated for r in op.responses)
    for p in op.query_params:
        where = f"{label} query param {p.name}"
        if p.legacy_name is not None:
            if p.legacy_name in dispatch:
                out.append(f"E-PARAM: {where}: dispatch parameter {p.legacy_name} is not API")
            elif p.legacy_name not in facts.legacy:
                out.append(f"E-PARAM: {where}: legacy parameter {p.legacy_name} not read in packet")
        elif p.name in STANDARD_QUERY and paginated:
            continue
        elif p.name not in facts.legacy or p.name in dispatch:
            out.append(f"E-PARAM: {where}: no legacy_name and {p.name} not read in packet")
    for p in op.path_params:
        where = f"{label} path param {p.name}"
        if p.legacy_name is not None:
            if p.legacy_name not in facts.legacy or p.legacy_name in dispatch:
                out.append(f"E-PARAM: {where}: legacy parameter {p.legacy_name} not read in packet")
        elif p.name in facts.legacy and p.name not in dispatch:
            continue
        elif _ID_NAME.search(p.name) and (facts.reads_id or facts.uses_session):
            continue
        else:
            out.append(f"E-PARAM: {where}: not derivable from any legacy parameter")
    declared = {p.name for p in op.path_params}
    template = set(_PATH_VAR.findall(op.path))
    for name in sorted(template - declared):
        out.append(f"E-PARAM: {label}: path variable {{{name}}} has no path parameter")
    for name in sorted(declared - template):
        out.append(f"E-PARAM: {label}: path parameter {name} is not in the path")
    return out


def _check_statuses(facts: _Facts, op: Operation) -> list[str]:
    out: list[str] = []
    for r in op.responses:
        where = f"{_op_label(op)} response {r.status}"
        if not 100 <= r.status <= 599:
            out.append(f"E-STATUS: {where}: invalid status code")
            continue
        if r.status in STANDARD_STATUSES:
            continue
        if not r.evidence:
            out.append(f"E-STATUS: {where}: non-standard status needs evidence")
            continue
        if not any(_status_backed(facts, e) for e in r.evidence):
            out.append(f"E-STATUS: {where}: evidence shows no error, status or validation code")
    return out


def _status_backed(facts: _Facts, ev: Evidence) -> bool:
    lines = facts.lines.get(ev.file, {})
    if ev.line not in lines:
        return False  # reported by the citation rule
    return bool(_STATUS_EVIDENCE.search(_window(lines, ev.line) + " " + ev.snippet))


def _check_roles(op: Operation, known_roles: set[str]) -> list[str]:
    out: list[str] = []
    for role in op.roles:
        where = f"{_op_label(op)} role {role.name}"
        if role.name not in known_roles:
            out.append(f"E-ROLE: {where}: unknown role")
        if not role.evidence:
            out.append(f"E-EVIDENCE-MISSING: {where}: role needs evidence")
    return out


def _schema_ok(ref: str, known: set[str]) -> bool:
    if ref in known or ref in PRIMITIVE_SCHEMAS:
        return True
    return any(ref.endswith(s) and ref[: -len(s)] in known for s in DERIVED_SUFFIXES)


def _check_schemas(op: Operation, known: set[str]) -> list[str]:
    out: list[str] = []
    label = _op_label(op)

    def check(where: str, ref: str | None, fields: Iterable[Any], covered: bool = False) -> None:
        field_list = list(fields)
        if ref is not None and not field_list and not _schema_ok(ref, known):
            out.append(f"E-SCHEMA: {where}: schema {ref} does not resolve")
        for f in field_list:
            if f.items_ref is not None and not _schema_ok(f.items_ref, known):
                out.append(f"E-SCHEMA: {where} field {f.name}: items {f.items_ref} unresolved")
            if not f.evidence and not covered:
                out.append(f"E-EVIDENCE-MISSING: {where} field {f.name}: inline field needs it")

    if op.request_body is not None:
        check(f"{label} request body", op.request_body.schema_ref, op.request_body.fields)
    for r in op.responses:
        check(f"{label} response {r.status}", r.schema_ref, r.fields, bool(r.evidence))
    return out


def _check_hints(packet: WorkPacket, proposal: EndpointProposal) -> list[str]:
    hint = packet.hints.http_method.upper()
    if hint == "NONE" or packet.hints.shape in ("action", "no_api_needed"):
        return []
    ops = proposal.operations
    if any(o.http_method == hint for o in ops):
        return []
    return [
        f"E-HINT: {_op_label(o)}: differs from hint {hint} without hint_override_reason"
        for o in ops
        if not (o.hint_override_reason or "").strip()
    ]


def _servlet_simple(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[-1]


def _check_identity(packet: WorkPacket, proposal: EndpointProposal) -> list[str]:
    out: list[str] = []
    if proposal.packet_id != packet.id:
        out.append(f"E-PACKET-ID: proposal {proposal.packet_id} does not match packet {packet.id}")
    meta = packet.meta
    for op in proposal.operations:
        src = op.legacy_source
        if (src.module, src.servlet, src.op) != (
            meta.module,
            _servlet_simple(meta.servlet_fqcn),
            meta.op,
        ):
            out.append(
                f"E-PACKET-ID: {_op_label(op)}: legacy_source "
                f"{src.module}:{src.servlet}#{src.op} does not match packet meta"
            )
    if proposal.operations and proposal.no_api_needed is not None and not proposal.lookups_needed:
        out.append("E-NOAPI: no_api_needed is set but operations are present")
    if not proposal.operations and proposal.no_api_needed is None:
        out.append("E-NOAPI: proposal has neither operations nor no_api_needed")
    return out


def verify_proposal(
    packet: WorkPacket,
    proposal: EndpointProposal,
    known_schemas: set[str],
    known_roles: set[str],
    dispatch_params: list[str],
) -> list[str]:
    """Return sorted reason strings; an empty list means the proposal is verified."""
    facts = _Facts(packet)
    roles = set(known_roles) | facts.packet_roles
    dispatch = set(dispatch_params)
    out: list[str] = _check_identity(packet, proposal)
    out += _check_hints(packet, proposal)
    for need in proposal.lookups_needed:
        for e in need.evidence:
            out += _check_evidence(facts, e, f"lookup {need.type}")
    for op in proposal.operations:
        if not op.evidence:
            out.append(f"E-EVIDENCE-MISSING: {_op_label(op)}: operation needs evidence")
        for where, ev in _evidence_items(op):
            out += _check_evidence(facts, ev, where)
        out += _check_params(facts, op, dispatch)
        out += _check_statuses(facts, op)
        out += _check_roles(op, roles)
        out += _check_schemas(op, known_schemas)
    return sorted(set(out))


# ------------------------------------------------------------------ known sets
def build_known(bundle: IndexBundle) -> tuple[set[str], set[str]]:
    """(schema names, role names) known to the index."""
    schemas = {e.simple_name for e in bundle.domain.entities}
    roles: set[str] = set()
    for m in bundle.web.modules:
        roles.update(m.security_roles)
        for c in m.security_constraints:
            roles.update(c.roles)
    for ejb in bundle.domain.ejbs:
        roles.update(ejb.class_roles_allowed)
        for meth in ejb.methods:
            roles.update(meth.roles_allowed)
    return schemas, roles


# ------------------------------------------------------------------ batch driver
def _verify_record(
    packet: WorkPacket,
    record: ProposalRecord | None,
    schemas: set[str],
    roles: set[str],
    dispatch: list[str],
) -> VerifiedProposal:
    if record is None:
        return VerifiedProposal(
            packet_id=packet.id, status="failed", reasons=["no proposal"], attempts=0
        )
    if not record.ok or record.proposal is None:
        reasons = sorted(record.errors) or ["analyzer produced no valid proposal"]
        return VerifiedProposal(
            packet_id=packet.id, status="failed", reasons=reasons, attempts=record.attempt
        )
    prop = record.proposal
    reasons = verify_proposal(packet, prop, schemas, roles, dispatch)
    base: dict[str, Any] = {
        "packet_id": packet.id,
        "open_questions": list(prop.open_questions),
        "lookups_needed": sorted({n.type for n in prop.lookups_needed}),
        "confidence": prop.confidence,
        "attempts": record.attempt,
    }
    if reasons:
        return VerifiedProposal(status="rejected", reasons=reasons, **base)
    if not prop.operations:
        return VerifiedProposal(status="no_api_needed", no_api_needed=prop.no_api_needed, **base)
    return VerifiedProposal(
        status="verified",
        operations=list(prop.operations),
        no_api_needed=prop.no_api_needed,
        **base,
    )


def _verify_worker(task: dict[str, Any]) -> dict[str, Any]:
    packet = WorkPacket.model_validate(task["packet"])
    rec = ProposalRecord.model_validate(task["record"]) if task["record"] is not None else None
    result = _verify_record(packet, rec, set(task["schemas"]), set(task["roles"]), task["dispatch"])
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def verify_all(
    cfg: Config, ws: Workspace, bundle: IndexBundle, workers: int = 1
) -> list[VerifiedProposal]:
    """Verify every packet's proposal, write ``verified/*.json`` and return results by id."""
    schemas, roles = build_known(bundle)
    packets = [
        WorkPacket.model_validate(read_json(p)) for p in sorted(ws.packets_dir.glob("*.json"))
    ]
    records: dict[str, ProposalRecord] = {}
    for p in sorted(ws.proposals_dir.glob("*.json")):
        rec = ProposalRecord.model_validate(read_json(p))
        records[rec.packet_id] = rec
    tasks = [
        {
            "packet": pk.model_dump(mode="json"),
            "record": records[pk.id].model_dump(mode="json") if pk.id in records else None,
            "schemas": sorted(schemas),
            "roles": sorted(roles),
            "dispatch": sorted(cfg.dispatch.param_names),
        }
        for pk in sorted(packets, key=lambda x: x.id)
    ]
    if workers <= 1 or len(tasks) <= 1:
        raw = [_verify_worker(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            raw = list(pool.map(_verify_worker, tasks, chunksize=4))
    results = sorted((VerifiedProposal.model_validate(r) for r in raw), key=lambda v: v.packet_id)
    ws.verified_dir.mkdir(parents=True, exist_ok=True)
    keep = {f"{id_to_filename(v.packet_id)}.json" for v in results}
    for old in ws.verified_dir.glob("*.json"):
        if old.name not in keep:
            old.unlink()
    for v in results:
        write_json(
            ws.verified_dir / f"{id_to_filename(v.packet_id)}.json", v.model_dump(mode="json")
        )
    return results


def rejected_feedback(ws: Workspace) -> dict[str, list[str]]:
    """Reasons for rejected or failed first attempts: candidates for one re-analysis."""
    out: dict[str, list[str]] = {}
    for p in sorted(ws.verified_dir.glob("*.json")):
        v = VerifiedProposal.model_validate(read_json(p))
        if v.status in ("rejected", "failed") and v.attempts == 1:
            out[v.packet_id] = list(v.reasons)
    return dict(sorted(out.items()))
