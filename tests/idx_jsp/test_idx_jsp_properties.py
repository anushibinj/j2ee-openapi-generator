from pathlib import Path

from j2ee_openapi_generator.index.jsp import parse_jsp
from j2ee_openapi_generator.index.models import PropertiesBundle
from j2ee_openapi_generator.index.properties import (
    build_properties_index,
    lookup_keys,
    parse_properties,
)

APP = Path(__file__).parents[1] / "fixtures" / "legacy-app"


def test_parse_edge_cases() -> None:
    text = (
        "# comment\n"
        "! also comment\n"
        "a=1\n"
        "b : two words\n"
        "c three\n"
        "  d   =  x\\\n"
        "      continued\n"
        "e=\\u00e9t\\u00E9\\tTab\n"
        "f\\ g=key with space\n"
        "h=trailing\\\\\n"
        "i\n"
        "j=a=b:c\n"
        "k=dup\n"
        "k=last\n"
    )
    b = parse_properties("x/app.properties", text)
    assert b.entries == {
        "a": "1",
        "b": "two words",
        "c": "three",
        "d": "xcontinued",
        "e": "été\tTab",
        "f g": "key with space",
        "h": "trailing\\",
        "i": "",
        "j": "a=b:c",
        "k": "last",
    }
    assert b.base_name == "app"


def test_base_name_locale() -> None:
    assert parse_properties("m_fr.properties", "").base_name == "m"
    assert parse_properties("m_pt_BR.properties", "").base_name == "m"
    assert parse_properties("m.properties", "").base_name == "m"


def test_fixture_bundles_and_index() -> None:
    res = APP / "accounts/src/main/resources"
    en = parse_properties("accounts/messages.properties", (res / "messages.properties").read_text())
    fr = parse_properties(
        "accounts/messages_fr.properties", (res / "messages_fr.properties").read_text()
    )
    assert en.entries["error.user.inuse"] == "User is still referenced"
    assert set(en.entries) == set(fr.entries)
    jsp = parse_jsp(
        "accounts/userList.jsp", (APP / "accounts/src/main/webapp/userList.jsp").read_text(),
        "accounts", ["op"],
    )  # fmt: skip
    java = {"A.java": 'x.getString("error.user.inuse"); y("nope.key");'}
    idx = build_properties_index([fr, en], [jsp], java)
    assert [b.file for b in idx.bundles] == ["accounts/messages.properties"]
    assert idx.key_refs["A.java"] == ["error.user.inuse"]
    assert "user.name" in idx.key_refs["accounts/userList.jsp"]
    assert lookup_keys(idx, ["error.user.inuse", "zzz"]) == {
        "error.user.inuse": "User is still referenced"
    }


def test_lone_locale_like_file_is_kept() -> None:
    b = PropertiesBundle(file="d/app_db.properties", base_name="app", entries={"k": "v"})
    idx = build_properties_index([b], [], {})
    assert [(x.file, x.base_name) for x in idx.bundles] == [("d/app_db.properties", "app_db")]
