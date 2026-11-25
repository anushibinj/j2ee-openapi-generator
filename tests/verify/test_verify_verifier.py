"""Verifier rules, fixture proposals, batch driver."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from j2ee_openapi_generator.aggregate.models import ProposalRecord, VerifiedProposal
from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import id_to_filename
from j2ee_openapi_generator.index.models import (
    DomainIndex,
    EjbMethod,
    EjbRecord,
    EntityRecord,
    IndexBundle,
    ModuleRecord,
    SecurityConstraint,
    WebIndex,
)
from j2ee_openapi_generator.llm.schemas import EndpointProposal
from j2ee_openapi_generator.packets.models import (
    EntryPointMeta,
    MappingHints,
    SourceChunk,
    WorkPacket,
)
from j2ee_openapi_generator.verify.verifier import (
    build_known,
    parse_packet_lines,
    rejected_feedback,
    verify_all,
    verify_proposal,
)
from j2ee_openapi_generator.workspace import Workspace, write_json

FIXTURES = Path(__file__).parents[1] / "fixtures"
APP = FIXTURES / "legacy-app"
LLM = FIXTURES / "llm"
SERVLET_FILE = "m/Foo.java"

SOURCE = """\
public class FooServlet {
    void process(HttpServletRequest request, HttpServletResponse response) {
        String op = request.getParameter("op");
        if ("get".equals(op)) {
            Long id = Long.valueOf(request.getParameter("id"));
            int page = parse(request.getParameter(PARAM_PAGE));
            Foo foo = mgr.find(id);
            if (foo == null) {
                response.sendError(HttpServletResponse.SC_NOT_FOUND, "missing");
                return;
            }
            response.getWriter().write("ok");
        }
    }
    static final String PARAM_PAGE = "page";
}
"""


def render(file: str, text: str, role: str = "branch") -> SourceChunk:
    lines = text.splitlines()
    body = "\n".join(f"{i:04d}| {ln}" for i, ln in enumerate(lines, 1))
    return SourceChunk(
        file=file,
        start_line=1,
        end_line=len(lines),
        role=role,
        text=f"=== FILE {file} (lines 1-{len(lines)}) ===\n{body}",
    )


def make_packet(
    chunks: list[SourceChunk] | None = None,
    *,
    pid: str = "m:FooServlet#get",
    hint: str = "GET",
    shape: str = "item",
) -> WorkPacket:
    chunks = chunks if chunks is not None else [render(SERVLET_FILE, SOURCE)]
    return WorkPacket(
        id=pid,
        meta=EntryPointMeta(
            id=pid,
            module="m",
            servlet_fqcn="com.acme.m.FooServlet",
            op="get",
            param_name="op",
            style="if",
            http_methods=["GET"],
            url_patterns=["/Foo"],
        ),
        chunks=chunks,
        hints=MappingHints(http_method=hint, shape=shape),
        allowed_files=sorted({c.file for c in chunks}),
    )


def ev(line: int, snippet: str, file: str = SERVLET_FILE) -> dict[str, Any]:
    return {"claim": "c", "file": file, "line": line, "snippet": snippet}


def base_op() -> dict[str, Any]:
    return {
        "http_method": "GET",
        "path": "/foos/{fooId}",
        "summary": "s",
        "resource": "foos",
        "tag": "Foos",
        "path_params": [
            {
                "name": "fooId",
                "legacy_name": "id",
                "evidence": [ev(5, 'request.getParameter("id")')],
            }
        ],
        "query_params": [],
        "responses": [
            {
                "status": 200,
                "schema_ref": "Foo",
                "evidence": [ev(11, 'response.getWriter().write("ok");')],
            },
            {
                "status": 404,
                "schema_ref": "Problem",
                "evidence": [ev(9, "response.sendError(HttpServletResponse.SC_NOT_FOUND)")],
            },
        ],
        "roles": [
            {"name": "admin", "evidence": [ev(3, 'String op = request.getParameter("op");')]}
        ],
        "legacy_source": {"module": "m", "servlet": "FooServlet", "op": "get"},
        "evidence": [ev(4, 'if ("get".equals(op)) {')],
    }


def proposal(op: dict[str, Any] | None = None, **extra: Any) -> EndpointProposal:
    data: dict[str, Any] = {"packet_id": "m:FooServlet#get", "operations": [op or base_op()]}
    data.update(extra)
    return EndpointProposal.model_validate(data)


def run(
    prop: EndpointProposal,
    packet: WorkPacket | None = None,
    schemas: set[str] | None = None,
    roles: set[str] | None = None,
) -> list[str]:
    return verify_proposal(
        packet or make_packet(),
        prop,
        schemas if schemas is not None else {"Foo"},
        roles if roles is not None else {"admin"},
        ["op", "action"],
    )


def codes(reasons: list[str]) -> set[str]:
    return {r.split(":", 1)[0] for r in reasons}


# ------------------------------------------------------------------ parsing
def test_parse_packet_lines() -> None:
    lines = parse_packet_lines(make_packet())
    assert lines[SERVLET_FILE][3] == '        String op = request.getParameter("op");'
    assert 0 not in lines[SERVLET_FILE]


def test_baseline_verifies() -> None:
    assert run(proposal()) == []


# ------------------------------------------------------------------ citations
def test_cite_file() -> None:
    op = base_op()
    op["evidence"] = [ev(4, 'if ("get".equals(op)) {', file="other/File.java")]
    assert codes(run(proposal(op))) == {"E-CITE-FILE"}


def test_cite_line() -> None:
    op = base_op()
    op["evidence"] = [ev(500, "x")]
    assert codes(run(proposal(op))) == {"E-CITE-LINE"}


def test_cite_snippet_identifier_and_literal() -> None:
    op = base_op()
    op["evidence"] = [ev(4, "if (loadFromDatabase(op)) {")]
    assert codes(run(proposal(op))) == {"E-CITE-SNIPPET"}
    op["evidence"] = [ev(4, 'if ("delete".equals(op)) {')]
    assert codes(run(proposal(op))) == {"E-CITE-SNIPPET"}


def test_cite_tolerates_whitespace_and_offset() -> None:
    op = base_op()
    op["evidence"] = [ev(5, 'if  ( "get" . equals( op ) )  {')]  # two lines off
    assert run(proposal(op)) == []


def test_cite_too_far_fails() -> None:
    op = base_op()
    op["evidence"] = [ev(12, 'if ("get".equals(op)) {')]
    assert codes(run(proposal(op))) == {"E-CITE-SNIPPET"}


def test_param_and_role_evidence_is_checked() -> None:
    op = base_op()
    op["path_params"][0]["evidence"] = [ev(999, "x")]
    op["roles"][0]["evidence"] = [ev(4, "bogusIdentifier()")]
    assert codes(run(proposal(op))) == {"E-CITE-LINE", "E-CITE-SNIPPET"}


def test_lookup_evidence_checked() -> None:
    bad = {"type": "x", "reason": "r", "evidence": [ev(999, "x")]}
    assert codes(run(proposal(lookups_needed=[bad]))) == {"E-CITE-LINE"}


def test_operation_needs_evidence() -> None:
    op = base_op()
    op["evidence"] = []
    assert codes(run(proposal(op))) == {"E-EVIDENCE-MISSING"}


# ------------------------------------------------------------------ params
def test_param_unknown_legacy_name() -> None:
    op = base_op()
    op["query_params"] = [{"name": "color", "legacy_name": "colour"}]
    assert codes(run(proposal(op))) == {"E-PARAM"}


def test_param_no_legacy_name_must_exist() -> None:
    op = base_op()
    op["query_params"] = [{"name": "id"}]  # read in packet: fine
    assert run(proposal(op)) == []
    op["query_params"] = [{"name": "nothing"}]
    assert codes(run(proposal(op))) == {"E-PARAM"}


def test_param_constant_is_resolved() -> None:
    op = base_op()
    op["query_params"] = [{"name": "pageNo", "legacy_name": "page"}]
    assert run(proposal(op)) == []


def test_param_form_input_and_url_query() -> None:
    jsp = render("m/f.jsp", '<input name="color" type="text">\n<a href="x?size=3">', "jsp_request")
    packet = make_packet([render(SERVLET_FILE, SOURCE), jsp])
    op = base_op()
    op["query_params"] = [
        {"name": "color", "legacy_name": "color"},
        {"name": "size", "legacy_name": "size"},
    ]
    assert run(proposal(op), packet) == []


def test_param_standard_paging_needs_paginated() -> None:
    op = base_op()
    op["query_params"] = [{"name": "sort"}]
    assert codes(run(proposal(op))) == {"E-PARAM"}
    op["responses"][0]["paginated"] = True
    assert run(proposal(op)) == []


def test_param_dispatch_param_rejected() -> None:
    op = base_op()
    op["query_params"] = [{"name": "op", "legacy_name": "op"}]
    assert codes(run(proposal(op))) == {"E-PARAM"}


def test_path_param_derivation() -> None:
    op = base_op()
    op["path_params"] = [{"name": "fooId"}]  # derived from id
    assert run(proposal(op)) == []
    op["path_params"] = [{"name": "foo"}]
    op["path"] = "/foos/{foo}"
    assert codes(run(proposal(op))) == {"E-PARAM"}
    packet = make_packet([render(SERVLET_FILE, "class A { void f() { x(); } }")])
    op["path_params"] = [{"name": "fooId"}]
    op["path"] = "/foos/{fooId}"
    op["evidence"] = [ev(1, "class A")]
    op["roles"], op["responses"] = [], [{"status": 200, "schema_ref": "string"}]
    assert codes(run(proposal(op), packet)) == {"E-PARAM"}


def test_path_template_must_match_params() -> None:
    op = base_op()
    op["path"] = "/foos"
    assert codes(run(proposal(op))) == {"E-PARAM"}
    op = base_op()
    op["path"] = "/foos/{fooId}/{other}"
    assert codes(run(proposal(op))) == {"E-PARAM"}


# ------------------------------------------------------------------ status
def test_status_needs_evidence_and_backing() -> None:
    op = base_op()
    op["responses"][1]["evidence"] = []
    assert codes(run(proposal(op))) == {"E-STATUS"}
    op["responses"][1]["evidence"] = [ev(1, "public class FooServlet {")]
    assert codes(run(proposal(op))) == {"E-STATUS"}
    op["responses"][1]["evidence"] = [ev(8, "if (foo == null) {")]
    assert run(proposal(op)) == []


def test_status_standard_needs_no_evidence() -> None:
    op = base_op()
    op["responses"] = [{"status": 400, "schema_ref": "Problem"}]
    assert run(proposal(op)) == []
    op["responses"] = [{"status": 409, "schema_ref": "Problem"}]
    assert codes(run(proposal(op))) == {"E-STATUS"}


# ------------------------------------------------------------------ roles
def test_role_unknown_or_without_evidence() -> None:
    op = base_op()
    assert codes(run(proposal(op), roles={"other"})) == {"E-ROLE"}
    op["roles"][0]["evidence"] = []
    assert codes(run(proposal(op))) == {"E-EVIDENCE-MISSING"}


def test_role_from_packet_ejb_chunk() -> None:
    ejb = render("m/Bean.java", '@RolesAllowed({"boss", "admin"})\nclass Bean {}', "ejb_impl")
    packet = make_packet([render(SERVLET_FILE, SOURCE), ejb])
    op = base_op()
    op["roles"] = [{"name": "boss", "evidence": [ev(1, '@RolesAllowed({"boss"})', "m/Bean.java")]}]
    assert run(proposal(op), packet) == []


# ------------------------------------------------------------------ schemas
def test_schema_resolution() -> None:
    op = base_op()
    op["responses"][0]["schema_ref"] = "Unknown"
    assert codes(run(proposal(op))) == {"E-SCHEMA"}
    for ref in ("FooCreateRequest", "FooPatch", "FooPage", "integer"):
        op["responses"][0]["schema_ref"] = ref
        assert run(proposal(op)) == []
    op["request_body"] = {"schema_ref": "BarUpdateRequest"}
    assert codes(run(proposal(op))) == {"E-SCHEMA"}


def test_inline_fields_need_evidence() -> None:
    op = base_op()
    op["responses"][0]["schema_ref"] = "Inline"
    op["responses"][0]["fields"] = [{"name": "a"}]
    assert run(proposal(op)) == []  # the response evidence covers its inline fields
    op["responses"][0]["evidence"] = []
    assert codes(run(proposal(op))) == {"E-EVIDENCE-MISSING"}
    op = base_op()
    op["request_body"] = {"fields": [{"name": "a"}]}
    assert codes(run(proposal(op))) == {"E-EVIDENCE-MISSING"}
    op["responses"][0]["schema_ref"] = "Inline"
    op["responses"][0]["fields"] = [{"name": "a"}]
    op["request_body"]["fields"][0]["evidence"] = [ev(7, "Foo foo = mgr.find(id);")]
    assert run(proposal(op)) == []
    op["responses"][0]["evidence"] = []
    assert codes(run(proposal(op))) == {"E-EVIDENCE-MISSING"}
    op["responses"][0]["fields"] = [{"name": "a", "evidence": [ev(7, "Foo foo = mgr.find(id);")]}]
    assert run(proposal(op)) == []


# ------------------------------------------------------------------ hints and identity
def test_hint_deviation() -> None:
    op = base_op()
    op["http_method"] = "POST"
    assert codes(run(proposal(op), make_packet(hint="GET"))) == {"E-HINT"}
    op["hint_override_reason"] = "mutates state"
    assert run(proposal(op), make_packet(hint="GET")) == []
    op.pop("hint_override_reason")
    assert run(proposal(op), make_packet(hint="NONE")) == []
    assert run(proposal(op), make_packet(hint="GET", shape="action")) == []


def test_packet_id_and_legacy_source() -> None:
    assert codes(run(proposal(packet_id="m:FooServlet#other"))) == {"E-PACKET-ID"}
    op = base_op()
    op["legacy_source"]["op"] = "zzz"
    assert codes(run(proposal(op))) == {"E-PACKET-ID"}


def test_no_api_needed_rules() -> None:
    empty = EndpointProposal(packet_id="m:FooServlet#get", no_api_needed="redirect only")
    assert run(empty) == []
    neither = EndpointProposal(packet_id="m:FooServlet#get")
    assert codes(run(neither)) == {"E-NOAPI"}
    both = proposal(no_api_needed="x")
    assert codes(run(both)) == {"E-NOAPI"}
    lookup = {"type": "x", "reason": "r", "evidence": [ev(4, 'if ("get".equals(op)) {')]}
    assert run(proposal(no_api_needed="x", lookups_needed=[lookup])) == []


LICENSE_SOURCE = """\
public class LicenseAdminServlet {
    public void doPost(HttpServletRequest req, HttpServletResponse resp) {
        String op = UtilRequest.getParameter(req, "op");
        LicenseProperties(req, out);
    }
    public void LicenseProperties(HttpServletRequest req, PrintWriter out) {
        eResolver.setVariable("AllCaseFolder", String.valueOf(LicenseCache.getCurrentMax(1)));
        SystemSessionEJBLocal systemSession = getSystemSessionEjb();
        List<LicenseGroupTO> groups = systemSession.getLicenseGroups();
    }
}
"""
PAGE_SOURCE = """\
public class AdminServlet {
    public void doGet(HttpServletRequest req, HttpServletResponse resp) {
        req.getRequestDispatcher("/frameset.jsp").forward(req, resp);
    }
}
"""


def no_api(reason: str, **extra: Any) -> EndpointProposal:
    return EndpointProposal(packet_id="m:FooServlet#get", no_api_needed=reason, **extra)


def test_no_api_needed_rejected_when_code_reads_data() -> None:
    packet = make_packet([render(SERVLET_FILE, LICENSE_SOURCE)], hint="NONE", shape="no_api_needed")
    reasons = run(no_api("Default POST performs no action"), packet)
    assert codes(reasons) == {"E-NOAPI-DATA"}
    assert "LicenseCache.getCurrentMax" in reasons[0] or "getSystemSessionEjb" in reasons[0]


def test_no_api_needed_rejected_on_indexer_action_hint() -> None:
    """The indexer saw EJB calls (POST/action hint) even though no data line matched."""
    packet = make_packet([render(SERVLET_FILE, PAGE_SOURCE)], hint="POST", shape="action")
    packet.hints.reason = "default branch calls EJB methods; model as an action"
    reasons = run(no_api("Renders admin UI frameset"), packet)
    assert codes(reasons) == {"E-NOAPI-DATA"} and "calls EJB methods" in reasons[0]


def test_no_api_needed_page_only_stays_valid() -> None:
    packet = make_packet([render(SERVLET_FILE, PAGE_SOURCE)], hint="NONE", shape="no_api_needed")
    assert run(no_api("Renders admin UI frameset, no data read or written"), packet) == []


def test_no_api_needed_allowed_when_covered_elsewhere_or_with_lookups() -> None:
    packet = make_packet([render(SERVLET_FILE, LICENSE_SOURCE)], hint="POST", shape="action")
    covered = "Only loads the same record as op=read; covered by GET /licenses"
    assert run(no_api(covered), packet) == []
    lookup = {"type": "x", "reason": "r", "evidence": [ev(4, "LicenseProperties(req, out);")]}
    assert run(no_api("form only", lookups_needed=[lookup]), packet) == []


def test_no_api_data_check_ignores_non_source_chunks() -> None:
    """Data access words in a JSP response or property chunk do not count."""
    packet = make_packet(
        [
            render(SERVLET_FILE, PAGE_SOURCE),
            render("m/page.jsp", "<% new LicenseDAO().get(); %>", "jsp_response"),
        ],
        hint="NONE",
        shape="no_api_needed",
    )
    assert run(no_api("static page"), packet) == []


def test_reasons_sorted() -> None:
    op = base_op()
    op["evidence"] = [ev(500, "x"), ev(4, "bogusThing")]
    reasons = run(proposal(op))
    assert reasons == sorted(reasons)


# ------------------------------------------------------------------ fixtures
HINTS = {"GET": "item", "POST": "collection"}


def fixture_packet(entry_id: str, prop: EndpointProposal) -> WorkPacket:
    module, rest = entry_id.split(":", 1)
    servlet, op = rest.split("#", 1)
    chunks: list[SourceChunk] = []
    for path in sorted((APP / module).rglob("*")):
        if not path.is_file() or "/test/" in path.as_posix():
            continue
        if path.suffix == ".xml" and path.name != "web.xml":
            continue
        rel = path.relative_to(APP).as_posix()
        if path.name == "web.xml":
            role = "helper"
        elif path.suffix == ".java":
            role = "ejb_impl" if "/ejb/" in rel else "entity" if "/model/" in rel else "branch"
        elif path.suffix == ".jsp":
            role = "jsp_request"
        else:
            continue
        chunks.append(render(rel, path.read_text(encoding="utf-8"), role))
    hint = prop.operations[0].http_method if prop.operations else "NONE"
    return WorkPacket(
        id=entry_id,
        meta=EntryPointMeta(
            id=entry_id,
            module=module,
            servlet_fqcn=f"com.acme.{module}.web.{servlet}",
            op=op,
            param_name="op",
            style="if",
            http_methods=["GET"],
            url_patterns=[],
        ),
        chunks=chunks,
        hints=MappingHints(http_method=hint, shape="action"),
        allowed_files=sorted(c.file for c in chunks),
    )


KNOWN_SCHEMAS = {
    "User", "Order", "OrderDraft", "OrderSummaryDto", "DailyReport", "Country", "UserStatus",
    "OrderLine", "OrderStatus",
}  # fmt: skip
KNOWN_ROLES = {"user", "admin", "sales", "manager"}
DISPATCH = ["op", "action", "cmd"]


def load_canned(name: str) -> EndpointProposal:
    return EndpointProposal.model_validate(json.loads((LLM / name).read_text("utf-8")))


def canned_names() -> list[str]:
    return sorted(
        p.name
        for p in LLM.glob("*.json")
        if not p.name.startswith("_") and ".repair." not in p.name and "statuses" not in p.name
    )


@pytest.mark.parametrize("name", canned_names())
def test_canned_fixture_verifies(name: str) -> None:
    prop = load_canned(name)
    packet = fixture_packet(prop.packet_id, prop)
    assert verify_proposal(packet, prop, KNOWN_SCHEMAS, KNOWN_ROLES, DISPATCH) == []


def test_canned_statuses_rejected_then_repaired() -> None:
    bad = load_canned("sales--LookupServlet__statuses.json")
    packet = fixture_packet(bad.packet_id, bad)
    reasons = verify_proposal(packet, bad, KNOWN_SCHEMAS, KNOWN_ROLES, DISPATCH)
    assert {"E-CITE-LINE", "E-CITE-SNIPPET"} <= codes(reasons)
    good = load_canned("sales--LookupServlet__statuses.repair.json")
    assert verify_proposal(packet, good, KNOWN_SCHEMAS, KNOWN_ROLES, DISPATCH) == []


# ------------------------------------------------------------------ build_known
def test_build_known() -> None:
    bundle = IndexBundle(
        web=WebIndex(
            modules=[
                ModuleRecord(
                    name="m",
                    web_xml="w",
                    root_dir="m",
                    context_root="/m",
                    security_roles=["a"],
                    security_constraints=[SecurityConstraint(url_patterns=["/x"], roles=["b"])],
                )
            ]
        ),
        domain=DomainIndex(
            ejbs=[
                EjbRecord(
                    bean_fqcn="B",
                    kind="stateless",
                    impl_file="B.java",
                    class_roles_allowed=["c"],
                    methods=[
                        EjbMethod(
                            name="f",
                            signature="f()",
                            return_type="void",
                            start_line=1,
                            end_line=2,
                            roles_allowed=["d"],
                        )
                    ],
                )
            ],
            entities=[
                EntityRecord(
                    fqcn="p.User",
                    simple_name="User",
                    kind="entity",
                    file="U.java",
                    start_line=1,
                    end_line=2,
                )  # fmt: skip
            ],
        ),
    )
    schemas, roles = build_known(bundle)
    assert schemas == {"User"}
    assert roles == {"a", "b", "c", "d"}


# ------------------------------------------------------------------ verify_all
def setup_ws(tmp_path: Path) -> tuple[Config, Workspace]:
    cfg = Config(repo_root=tmp_path, work_dir=str(tmp_path / "w"))
    ws = Workspace(cfg)
    ws.ensure()
    return cfg, ws


def put_packet(ws: Workspace, packet: WorkPacket) -> None:
    write_json(ws.packets_dir / f"{id_to_filename(packet.id)}.json", packet.model_dump(mode="json"))


def put_record(ws: Workspace, rec: ProposalRecord) -> None:
    write_json(
        ws.proposals_dir / f"{id_to_filename(rec.packet_id)}.json", rec.model_dump(mode="json")
    )


def populate(ws: Workspace) -> None:
    good = proposal()
    put_packet(ws, make_packet())
    put_record(ws, ProposalRecord(packet_id=good.packet_id, ok=True, proposal=good))

    pid2 = "m:FooServlet#get~p2"
    packet2 = make_packet(pid="m:FooServlet#get~p2")
    packet2.partial, packet2.parent_id, packet2.part_index = True, "m:FooServlet#get", 2
    put_packet(ws, packet2)
    bad_op = base_op()
    bad_op["evidence"] = [ev(500, "x")]
    bad = proposal(bad_op).model_copy(update={"packet_id": pid2})
    put_record(ws, ProposalRecord(packet_id=pid2, ok=True, proposal=bad))

    for pid, rec in (
        ("m:A#noapi", EndpointProposal(packet_id="m:A#noapi", no_api_needed="redirect")),
    ):
        p = make_packet(pid=pid)
        p.meta.servlet_fqcn = "com.acme.m.A"
        put_packet(ws, p)
        put_record(ws, ProposalRecord(packet_id=pid, ok=True, proposal=rec))
    put_packet(ws, make_packet(pid="m:B#none"))  # no proposal
    put_packet(ws, make_packet(pid="m:C#bad"))
    put_record(ws, ProposalRecord(packet_id="m:C#bad", ok=False, errors=["bad json"], attempt=2))


def bundle() -> IndexBundle:
    return IndexBundle(
        domain=DomainIndex(
            entities=[
                EntityRecord(
                    fqcn="p.Foo",
                    simple_name="Foo",
                    kind="entity",
                    file="Foo.java",
                    start_line=1,
                    end_line=2,
                )  # fmt: skip
            ]
        ),
        web=WebIndex(
            modules=[
                ModuleRecord(
                    name="m", web_xml="w", root_dir="m", context_root="/m", security_roles=["admin"]
                )
            ]
        ),
    )


def test_verify_all_statuses_and_feedback(tmp_path: Path) -> None:
    cfg, ws = setup_ws(tmp_path)
    populate(ws)
    res = verify_all(cfg, ws, bundle())
    by_id = {v.packet_id: v for v in res}
    assert [v.packet_id for v in res] == sorted(by_id)
    assert by_id["m:FooServlet#get"].status == "verified"
    assert by_id["m:FooServlet#get"].operations[0].path == "/foos/{fooId}"
    assert by_id["m:FooServlet#get~p2"].status == "rejected"
    assert by_id["m:A#noapi"].status == "no_api_needed"
    assert by_id["m:A#noapi"].no_api_needed == "redirect"
    assert by_id["m:B#none"].status == "failed"
    assert by_id["m:B#none"].reasons == ["no proposal"]
    assert by_id["m:C#bad"].reasons == ["bad json"]
    assert by_id["m:C#bad"].attempts == 2
    assert len(list(ws.verified_dir.glob("*.json"))) == 5
    fb = rejected_feedback(ws)
    assert set(fb) == {"m:FooServlet#get~p2"}
    assert "m:C#bad" not in fb
    assert "m:FooServlet#get" not in fb


def test_feedback_includes_first_attempt_failures(tmp_path: Path) -> None:
    cfg, ws = setup_ws(tmp_path)
    put_packet(ws, make_packet(pid="m:D#x"))
    put_record(ws, ProposalRecord(packet_id="m:D#x", ok=False, errors=["boom"], attempt=1))
    verify_all(cfg, ws, bundle())
    assert rejected_feedback(ws) == {"m:D#x": ["boom"]}


def snapshot(ws: Workspace) -> dict[str, str]:
    return {p.name: p.read_text("utf-8") for p in sorted(ws.verified_dir.glob("*.json"))}


def test_workers_identical(tmp_path: Path) -> None:
    cfg, ws = setup_ws(tmp_path)
    populate(ws)
    r1 = verify_all(cfg, ws, bundle(), workers=1)
    s1 = snapshot(ws)
    r4 = verify_all(cfg, ws, bundle(), workers=4)
    assert snapshot(ws) == s1
    assert r1 == r4
    assert all(isinstance(v, VerifiedProposal) for v in r4)


def test_stale_files_removed(tmp_path: Path) -> None:
    cfg, ws = setup_ws(tmp_path)
    populate(ws)
    (ws.verified_dir / "stale--Gone__x.json").write_text("{}", encoding="utf-8")
    verify_all(cfg, ws, bundle())
    assert not (ws.verified_dir / "stale--Gone__x.json").exists()
    assert len(snapshot(ws)) == 5
