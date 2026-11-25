from __future__ import annotations

from pathlib import Path

import pytest
from bundle_helper import build_bundle

from j2ee_openapi_generator.config import Config, load_config
from j2ee_openapi_generator.ids import id_to_filename
from j2ee_openapi_generator.index.models import IndexBundle
from j2ee_openapi_generator.packets.budget import compute_hash, parse_chunk_text
from j2ee_openapi_generator.packets.builder import (
    build_packet,
    build_packets,
    render_chunk,
    write_packets,
)
from j2ee_openapi_generator.packets.models import WorkPacket
from j2ee_openapi_generator.workspace import Workspace, read_json

FIXTURES = Path(__file__).parents[1] / "fixtures"
EXPECTED_IDS = sorted(
    [
        "accounts:UserServlet#list",
        "accounts:UserServlet#read",
        "accounts:UserServlet#edit",
        "accounts:UserServlet#new",
        "accounts:UserServlet#save",
        "accounts:UserServlet#delete",
        "accounts:UserServlet#<default>",
        "accounts:ProfileServlet#view",
        "accounts:ProfileServlet#update",
        "sales:OrderServlet#list",
        "sales:OrderServlet#view",
        "sales:OrderServlet#detail",
        "sales:OrderServlet#step1",
        "sales:OrderServlet#step2",
        "sales:OrderServlet#confirm",
        "sales:OrderServlet#export",
        "sales:OrderServlet#<default>",
        "sales:ReportServlet#summary",
        "sales:ReportServlet#daily",
        "sales:LookupServlet#countries",
        "sales:LookupServlet#statuses",
    ]
)


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Config:
    c = load_config(FIXTURES / "oapi.yaml")
    work = tmp_path_factory.mktemp("work")
    log = work / "access.log"
    log.write_text(
        "\n".join(
            ['1.1.1.1 - - [d] "GET /accounts/UserServlet?op=list HTTP/1.1" 200 5'] * 3
            + ['1.1.1.1 - - [d] "POST /accounts/UserServlet?op=delete HTTP/1.1" 204 0']
            + ['1.1.1.1 - - [d] "GET /sales/UserServlet?op=list HTTP/1.1" 200 5']
        )
        + "\n"
    )
    c.work_dir = work
    c.out_dir = work / "out"
    c.review_dir = work / "review"
    c.access_logs = [str(log)]
    return c


@pytest.fixture(scope="module")
def bundle(cfg: Config) -> IndexBundle:
    return build_bundle(cfg)


@pytest.fixture(scope="module")
def packets(cfg: Config, bundle: IndexBundle) -> dict[str, WorkPacket]:
    return {p.id: p for p in build_packets(cfg, bundle)}


def roles(p: WorkPacket) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in p.chunks:
        out.setdefault(c.role, []).append(c.text)
    return out


def test_render_chunk_format() -> None:
    lines = ["a", "b", "c", "d"]
    c = render_chunk("x/Y.java", lines, 2, 3, "branch")
    assert c.text == "=== FILE x/Y.java (lines 2-3) ===\n   2| b\n   3| c"
    assert (c.start_line, c.end_line, c.role) == (2, 3, "branch")
    assert render_chunk("f", lines, 3, 99, "helper").end_line == 4


def test_all_entry_points(packets: dict[str, WorkPacket]) -> None:
    assert sorted(packets) == EXPECTED_IDS
    assert list(packets) == sorted(packets)


def test_chunks_match_real_files(packets: dict[str, WorkPacket], cfg: Config) -> None:
    for p in packets.values():
        assert p.packet_hash == compute_hash(p) and p.token_estimate > 0
        assert p.allowed_files == sorted({c.file for c in p.chunks})
        for c in p.chunks:
            src = (cfg.repo_path / c.file).read_text(encoding="utf-8").split("\n")
            assert c.text.startswith(f"=== FILE {c.file} (lines {c.start_line}-{c.end_line}) ===")
            numbered = parse_chunk_text(c.text)
            assert numbered and (numbered[0][0], numbered[-1][0]) == (c.start_line, c.end_line)
            for n, text in numbered:
                assert src[n - 1].rstrip() == text.rstrip()


def test_user_save(packets: dict[str, WorkPacket]) -> None:
    p = packets["accounts:UserServlet#save"]
    r = roles(p)
    branch = next(c for c in p.chunks if c.role == "branch")
    assert (branch.start_line, branch.end_line) == (68, 69)
    helper = next(c for c in p.chunks if c.role == "helper")
    assert (helper.start_line, helper.end_line) == (83, 117)
    assert "private void saveUser" in helper.text
    assert any(
        "User create(User user);" in t and "interface UserManagerLocal" in t
        for t in r["ejb_interface"]
    )
    assert any("public User update(User user, Long expectedVersion)" in t for t in r["ejb_impl"])
    assert any("private String name;" in t and "@Size(min = 2, max = 80)" in t for t in r["entity"])
    assert not any("public void setName" in t for t in r["entity"])
    assert any('name="email"' in t for t in r["jsp_request"])
    assert p.meta.url_patterns == ["/UserServlet"] and p.meta.context_root == "/accounts"
    assert p.meta.security_roles == ["admin", "user"]
    assert p.meta.security_constraints[0]["roles"] == ["admin"]
    assert "accounts/src/main/webapp/userEdit.jsp" in p.meta.jsps
    assert p.properties["error.user.name.required"]
    assert p.hints.http_method == "PUT" and p.hints.shape == "item"
    assert p.hints.resource_candidates == ["users"]


def test_user_delete_has_exception(packets: dict[str, WorkPacket]) -> None:
    p = packets["accounts:UserServlet#delete"]
    exc = [c for c in p.chunks if c.file.endswith("UserInUseException.java")]
    assert exc and "class UserInUseException" in exc[0].text
    assert p.hints.http_method == "DELETE"
    assert any("users.js" in c.file for c in p.chunks if c.role == "jsp_request")
    assert p.properties["error.user.inuse"].startswith("User is still referenced")


def test_user_list_pagination_jsp(packets: dict[str, WorkPacket]) -> None:
    p = packets["accounts:UserServlet#list"]
    resp = [c for c in p.chunks if c.role == "jsp_response"]
    assert [c.file.rsplit("/", 1)[-1] for c in resp] == ["userList.jsp"]
    assert 'name="page"' in resp[0].text and "c:forEach" in resp[0].text
    form = [c for c in p.chunks if c.role == "jsp_request" and c.file.endswith("userList.jsp")]
    assert form and "<form" in form[0].text and "</form>" in form[0].text
    assert p.hints.shape == "collection"


def test_hints_across_fixture(packets: dict[str, WorkPacket]) -> None:
    def h(i: str) -> tuple[str, str]:
        return packets[i].hints.http_method, packets[i].hints.shape

    assert h("accounts:UserServlet#edit") == ("GET", "item")
    assert h("accounts:UserServlet#new") == ("NONE", "no_api_needed")
    assert h("accounts:UserServlet#<default>") == ("NONE", "no_api_needed")
    assert h("sales:OrderServlet#<default>") == ("NONE", "no_api_needed")
    assert h("sales:OrderServlet#export") == ("GET", "binary")
    assert h("sales:OrderServlet#confirm")[1] == "action"
    assert packets["sales:OrderServlet#confirm"].hints.verb_kebab == "confirm"
    assert packets["sales:OrderServlet#list"].hints.resource_candidates == ["orders"]
    assert "order-summaries" in packets["sales:OrderServlet#detail"].hints.resource_candidates


def test_order_security_and_session(packets: dict[str, WorkPacket]) -> None:
    p = packets["sales:OrderServlet#step2"]
    assert p.meta.security_roles == ["manager", "sales"]
    assert p.meta.security_constraints and p.meta.security_constraints[0]["roles"] == ["sales"]
    assert p.meta.style == "switch"
    assert any(c.role == "jsp_response" and "wizardStep2.jsp" in c.file for c in p.chunks)


def test_usage_count(packets: dict[str, WorkPacket]) -> None:
    assert packets["accounts:UserServlet#list"].usage_count == 3
    assert packets["accounts:UserServlet#delete"].usage_count == 1
    assert packets["accounts:UserServlet#read"].usage_count == 0
    assert packets["sales:OrderServlet#list"].usage_count == 0


def test_properties_values(packets: dict[str, WorkPacket]) -> None:
    p = packets["sales:OrderServlet#step1"]
    assert "order.customer" in p.properties
    assert all(isinstance(v, str) and v for v in p.properties.values())


def test_build_packet_splits_when_budget_tiny(cfg: Config, bundle: IndexBundle) -> None:
    tiny = cfg.model_copy(deep=True)
    tiny.llm.context_tokens = 640
    tiny.llm.max_output_tokens = 100
    entry = next(e for e in bundle.dispatch if e.op == "step2")
    out = build_packet(
        entry, bundle, lambda r: (cfg.repo_path / r).read_text(encoding="utf-8"), tiny, 0
    )
    assert len(out) > 1
    assert [p.id for p in out][:2] == ["sales:OrderServlet#step2~p1", "sales:OrderServlet#step2~p2"]
    assert all(p.partial and p.parent_id == "sales:OrderServlet#step2" for p in out)
    assert "jsp_response:sales/src/main/webapp/wizardStep2.jsp:1-31" in out[0].trimmed


def test_workers_identical(cfg: Config, bundle: IndexBundle) -> None:
    one = [p.model_dump_json() for p in build_packets(cfg, bundle, workers=1)]
    four = [p.model_dump_json() for p in build_packets(cfg, bundle, workers=4)]
    assert one == four and len(one) == 21


def test_write_packets_mirrors_set(cfg: Config, packets: dict[str, WorkPacket]) -> None:
    ws = Workspace(cfg)
    ws.ensure()
    stale = ws.packets_dir / "stale.json"
    stale.write_text("{}")
    paths = write_packets(ws, list(packets.values()))
    assert not stale.exists()
    names = sorted(p.name for p in ws.packets_dir.glob("*.json"))
    assert names == sorted(f"{id_to_filename(i)}.json" for i in packets)
    assert [p.name for p in paths] == names
    data = read_json(ws.packets_dir / "accounts--UserServlet__list.json")
    assert WorkPacket.model_validate(data) == packets["accounts:UserServlet#list"]
    # idempotent bytes
    before = (ws.packets_dir / "accounts--UserServlet__list.json").read_bytes()
    write_packets(ws, list(packets.values()))
    assert (ws.packets_dir / "accounts--UserServlet__list.json").read_bytes() == before
