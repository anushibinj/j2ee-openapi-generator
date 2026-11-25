from __future__ import annotations

from pathlib import Path

from j2ee_openapi_generator.config import Config, ModuleConfig
from j2ee_openapi_generator.index.inventory import build_inventory
from j2ee_openapi_generator.index.models import WebIndex
from j2ee_openapi_generator.index.webxml import (
    build_web_index,
    constraints_for,
    match_url_pattern,
    merge_annotation_servlets,
    resolve_servlet,
)

FIX = Path(__file__).parents[1] / "fixtures" / "legacy-app"


def _web() -> WebIndex:
    cfg = Config(repo_root=FIX)
    return build_web_index(cfg, build_inventory(cfg))


def test_fixture_web_index() -> None:
    web = _web()
    assert [m.name for m in web.modules] == ["accounts", "sales"]
    acc, sales = web.modules
    assert acc.context_root == "/accounts" and acc.root_dir == "accounts"
    assert acc.security_roles == ["admin", "user"]
    assert [f.name for f in acc.filters] == ["EncodingFilter"]
    assert acc.filters[0].url_patterns == ["/*"]
    (sc,) = acc.security_constraints
    assert sc.url_patterns == ["/UserServlet"]
    assert sc.http_methods == ["DELETE", "POST"] and sc.roles == ["admin"]
    assert sales.security_roles == ["manager", "sales"]
    keys = [(s.module, s.class_fqcn, s.name) for s in web.servlets]
    assert keys == sorted(keys)
    user = next(s for s in web.servlets if s.name == "UserServlet")
    assert user.url_patterns == ["/UserServlet"]
    assert user.init_params == {"defaultPageSize": "20"}
    assert user.context_root == "/accounts"
    assert {s.name for s in web.servlets if s.module == "sales"} == {
        "OrderServlet",
        "ReportServlet",
        "LookupServlet",
    }
    osr = next(s for s in web.servlets if s.name == "OrderServlet")
    (oc,) = constraints_for(sales, osr.url_patterns)
    assert oc.roles == ["sales"] and oc.http_methods == ["POST"]


def test_config_modules() -> None:
    cfg = Config(
        repo_root=FIX,
        modules=[
            ModuleConfig(
                name="acc",
                web_xml="accounts/src/main/webapp/WEB-INF/web.xml",
                tag_prefix="Acc",
            )
        ],
    )
    web = build_web_index(cfg, build_inventory(cfg))
    assert web.modules[0].context_root == "/acc"
    assert web.modules[0].tag_prefix == "Acc"
    assert [s.name for s in web.servlets] == ["UserServlet"]


def test_merge_annotations() -> None:
    web = merge_annotation_servlets(
        _web(),
        [
            ("accounts", "com.acme.accounts.web.ProfileServlet", ["/profile"]),
            ("accounts", "com.acme.accounts.web.UserServlet", ["/other"]),
        ],
    )
    prof = next(s for s in web.servlets if s.name == "ProfileServlet")
    assert prof.source == "annotation" and prof.url_patterns == ["/profile"]
    assert prof.context_root == "/accounts"
    user = next(s for s in web.servlets if s.name == "UserServlet")
    assert user.url_patterns == ["/UserServlet"]
    keys = [(s.module, s.class_fqcn, s.name) for s in web.servlets]
    assert keys == sorted(keys)
    assert resolve_servlet(web, "accounts", "/profile") is not None


def test_match_url_pattern() -> None:
    assert match_url_pattern("/a", "/a")
    assert not match_url_pattern("/a", "/a/")
    assert match_url_pattern("/a/*", "/a")
    assert match_url_pattern("/a/*", "/a/b/c")
    assert not match_url_pattern("/a/*", "/ab")
    assert match_url_pattern("*.do", "/x/y.do")
    assert not match_url_pattern("*.do", "/x/y.dox")
    assert match_url_pattern("/", "/anything")
    assert match_url_pattern("/*", "/anything")


def test_resolve_precedence() -> None:
    from j2ee_openapi_generator.index.models import ModuleRecord, ServletRecord

    def sv(name: str, *pats: str) -> ServletRecord:
        return ServletRecord(module="m", name=name, class_fqcn="p." + name, url_patterns=list(pats))

    web = WebIndex(
        modules=[ModuleRecord(name="m", web_xml="w", root_dir="m", context_root="/m")],
        servlets=[
            sv("Default", "/"),
            sv("Ext", "*.do"),
            sv("Short", "/a/*"),
            sv("Long", "/a/b/*"),
            sv("Exact", "/a/b/c.do"),
        ],
    )

    def r(p: str) -> str | None:
        s = resolve_servlet(web, "m", p)
        return s.name if s else None

    assert r("/a/b/c.do") == "Exact"
    assert r("/a/b/d.do") == "Long"
    assert r("/a/x") == "Short"
    assert r("/z/q.do") == "Ext"
    assert r("/other") == "Default"
    assert resolve_servlet(web, "nomod", "/a") is None
