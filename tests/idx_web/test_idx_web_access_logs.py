from __future__ import annotations

from pathlib import Path

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.access_logs import load_access_counts, usage_for
from j2ee_openapi_generator.index.inventory import build_inventory
from j2ee_openapi_generator.index.webxml import build_web_index

FIX = Path(__file__).parents[1] / "fixtures" / "legacy-app"

LOG = """\
10.0.0.1 - - [10/Oct/2023:13:55:36 +0000] "GET /accounts/UserServlet?op=list&page=2 HTTP/1.1" 200 5
10.0.0.1 - bob [10/Oct/2023:13:55:37 +0000] "POST /accounts/UserServlet?op=list HTTP/1.1" 200 12
10.0.0.2 - - [10/Oct/2023:13:55:38 +0000] "GET /accounts/UserServlet?op=read&id=1 HTTP/1.1" 200 12
10.0.0.2 - - [10/Oct/2023:13:55:38 +0000] "GET /accounts/UserServlet;jsessionid=AB HTTP/1.1" 200 12
10.0.0.2 - - [10/Oct/2023:13:55:38 +0000] "GET /sales/OrderServlet?action=find HTTP/1.1" 200 12
10.0.0.2 - - [10/Oct/2023:13:55:38 +0000] "GET /nowhere/Foo?op=x HTTP/1.1" 404 12
2023-10-10 13:55:40,123 "GET /accounts/Unknown?op=zzz HTTP/1.1" 404 -
garbage line "GET
\x00\x01 not a log
"""


def test_counts(tmp_path: Path) -> None:
    cfg = Config(repo_root=FIX)
    web = build_web_index(cfg, build_inventory(cfg))
    log = tmp_path / "access.log"
    log.write_text(LOG)
    counts = load_access_counts([log, tmp_path / "missing.log"], web, ["op", "action"])
    got = [(c.url_pattern, c.op, c.count) for c in counts]
    assert sorted(got, key=lambda t: (t[0], t[1] or "")) == [
        ("/OrderServlet", "find", 1),
        ("/UserServlet", None, 1),
        ("/UserServlet", "list", 2),
        ("/UserServlet", "read", 1),
    ]
    assert usage_for(counts, ["/UserServlet"], "list") == 2
    assert usage_for(counts, ["/UserServlet"], None) == 4
    assert usage_for(counts, ["/nope"], "list") == 0
