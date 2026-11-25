from __future__ import annotations

import hashlib
from pathlib import Path

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.inventory import build_inventory, matches_globs

FIX = Path(__file__).parents[1] / "fixtures" / "legacy-app"


def test_globs() -> None:
    assert matches_globs("a/test/b.java", ["**/test/**"])
    assert matches_globs("test/b.java", ["**/test/**"])
    assert not matches_globs("a/latest/b.java", ["**/test/**"])
    assert matches_globs("web.xml", ["**/web.xml"])
    assert matches_globs("a/b/C.java", ["**/*.java"])
    assert not matches_globs("a/b/C.javax", ["**/*.java"])
    assert matches_globs("a/B.java", ["a/*.java"])
    assert not matches_globs("a/b/B.java", ["a/*.java"])


def test_fixture_inventory() -> None:
    inv = build_inventory(Config(repo_root=FIX))
    paths = [f.path for f in inv]
    assert paths == sorted(paths)
    assert not any("UserServletTest" in p for p in paths)
    by = {f.path: f for f in inv}
    ejb = by["accounts/src/main/resources/META-INF/ejb-jar.xml"]
    assert ejb.kind == "ejb_xml"
    assert ejb.module == "accounts"
    web = by["sales/src/main/webapp/WEB-INF/web.xml"]
    assert web.kind == "web_xml" and web.module == "sales"
    assert {f.module for f in inv} == {"accounts", "sales"}
    p = FIX / web.path
    assert web.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
    assert web.size == p.stat().st_size


def test_workers_identical() -> None:
    cfg = Config(repo_root=FIX)
    a = build_inventory(cfg, workers=1)
    b = build_inventory(cfg, workers=4)
    assert [f.model_dump() for f in a] == [f.model_dump() for f in b]


def test_tmp_repo_kinds_and_excludes(tmp_path: Path) -> None:
    for rel, body in {
        "m/src/main/webapp/WEB-INF/web.xml": "<web-app/>",
        "m/src/main/java/A.java": "class A{}",
        "m/src/main/webapp/x.jsp": "x",
        "m/src/main/webapp/t.tag": "x",
        "m/target/B.java": "x",
        "m/src/test/java/CTest.java": "x",
        "m/src/main/resources/a.properties": "k=v",
        "m/readme.txt": "x",
    }.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    inv = build_inventory(Config(repo_root=tmp_path))
    assert {f.path: f.kind for f in inv} == {
        "m/src/main/java/A.java": "java",
        "m/src/main/resources/a.properties": "properties",
        "m/src/main/webapp/WEB-INF/web.xml": "web_xml",
        "m/src/main/webapp/t.tag": "jsp",
        "m/src/main/webapp/x.jsp": "jsp",
    }
    assert {f.module for f in inv} == {"m"}
    h1 = [f.sha256 for f in inv]
    assert h1 == [f.sha256 for f in build_inventory(Config(repo_root=tmp_path))]
