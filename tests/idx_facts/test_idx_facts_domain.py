from __future__ import annotations

import pytest
from javabuild_helper import build_index, read_source

from j2ee_openapi_generator.index.ejb import build_ejb_registry
from j2ee_openapi_generator.index.entities import build_entity_registry
from j2ee_openapi_generator.index.models import ClassInfo, EjbRecord, EntityRecord, JavaIndex

JAR = ["accounts/src/main/resources/META-INF/ejb-jar.xml"]


@pytest.fixture(scope="module")
def java() -> JavaIndex:
    return build_index()


@pytest.fixture(scope="module")
def ejbs(java: JavaIndex) -> list[EjbRecord]:
    return build_ejb_registry(java, read_source, JAR)


@pytest.fixture(scope="module")
def ents(java: JavaIndex, ejbs: list[EjbRecord]) -> dict[str, EntityRecord]:
    return {e.simple_name: e for e in build_entity_registry(java, ejbs)}


def test_ejb_registry(ejbs: list[EjbRecord]) -> None:
    assert [e.bean_fqcn for e in ejbs] == [
        "com.acme.accounts.ejb.UserManagerBean",
        "com.acme.sales.ejb.OrderManagerBean",
    ]
    um = ejbs[0]
    assert um.kind == "stateless" and um.class_roles_allowed == ["admin", "user"]
    assert um.interfaces == ["com.acme.accounts.ejb.UserManagerLocal"]
    assert um.impl_file.endswith("UserManagerBean.java")
    assert "java:module/UserManagerBean" in um.jndi_names
    by = {m.signature: m for m in um.methods}
    assert by["create(User)"].roles_allowed == ["admin"]
    assert by["findUser(Long)"].roles_allowed == ["admin", "user"]
    assert by["delete(Long)"].throws == ["UserInUseException"]
    assert by["delete(Long)"].start_line == 64 and by["delete(Long)"].end_line == 74
    om = {m.name: m for m in ejbs[1].methods}
    assert om["createFromDraft"].roles_allowed == ["sales"]
    assert om["findOrders"].roles_allowed == ["sales", "manager"]
    assert "toDto" not in om


def test_ejb_jar_only_bean() -> None:
    java = JavaIndex(
        classes={
            "p.Svc": ClassInfo(
                fqcn="p.Svc",
                simple_name="Svc",
                package="p",
                file="p/Svc.java",
                interfaces=["p.SvcRemote"],
            ),
            "p.SvcRemote": ClassInfo(
                fqcn="p.SvcRemote",
                simple_name="SvcRemote",
                package="p",
                file="p/SvcRemote.java",
                kind="interface",
            ),
        }
    )
    xml = (
        '<ejb-jar xmlns="http://java.sun.com/xml/ns/javaee"><enterprise-beans><session>'
        "<ejb-name>Svc</ejb-name><business-remote>p.SvcRemote</business-remote>"
        "<ejb-class>p.Svc</ejb-class><session-type>Stateful</session-type></session>"
        "</enterprise-beans></ejb-jar>"
    )
    recs = build_ejb_registry(java, lambda _p: xml, ["META-INF/ejb-jar.xml"])
    assert len(recs) == 1 and recs[0].kind == "stateful"
    assert recs[0].interfaces == ["p.SvcRemote"]


def test_entities_kinds(ents: dict[str, EntityRecord]) -> None:
    assert ents["User"].kind == "entity"
    assert ents["Order"].kind == "entity"
    assert ents["OrderSummaryDto"].kind == "dto"
    assert ents["OrderDraft"].kind == "dto"
    assert ents["UserStatus"].kind == "enum"
    assert ents["UserStatus"].enum_values == ["ACTIVE", "SUSPENDED", "CLOSED"]
    assert ents["OrderStatus"].enum_values == ["DRAFT", "SUBMITTED", "SHIPPED", "CANCELLED"]
    assert "UserInUseException" not in ents


def test_entities_sorted(java: JavaIndex, ejbs: list[EjbRecord]) -> None:
    fq = [e.fqcn for e in build_entity_registry(java, ejbs)]
    assert fq == sorted(fq)


def test_user_fields(ents: dict[str, EntityRecord]) -> None:
    f = {x.name: x for x in ents["User"].fields}
    assert "serialVersionUID" not in f
    assert f["id"].is_id and f["version"].is_version
    n = f["name"]
    assert (n.not_null, n.size_min, n.size_max, n.nullable, n.length) == (True, 2, 80, False, 80)
    assert f["email"].pattern == "[^@]+@[^@]+"
    st = f["status"]
    assert st.enumerated and st.enum_values == ["ACTIVE", "SUSPENDED", "CLOSED"]
    ph = f["phones"]
    assert ph.relationship == "OneToMany" and ph.elem_type == "Phone"
    assert ph.target_type == "com.acme.accounts.model.Phone"
    assert f["locale"].size_max == 5 and f["locale"].length == 5


def test_relationships(ents: dict[str, EntityRecord]) -> None:
    ph = {x.name: x for x in ents["Phone"].fields}
    assert (
        ph["user"].relationship == "ManyToOne"
        and ph["user"].target_type == "com.acme.accounts.model.User"
    )
    o = {x.name: x for x in ents["Order"].fields}
    assert o["version"].is_version
    assert o["customer"].relationship == "ManyToOne"
    assert o["status"].enumerated and "SUBMITTED" in o["status"].enum_values
    assert o["lines"].elem_type == "OrderLine"
    d = {x.name: x for x in ents["OrderDraft"].fields}
    assert d["lines"].java_type == "List<OrderLine>" and d["lines"].relationship is None


def test_mapped_superclass_and_transient() -> None:
    from j2ee_openapi_generator.index.models import FieldInfo

    base = ClassInfo(
        fqcn="p.Base",
        simple_name="Base",
        package="p",
        file="p/Base.java",
        annotations=["MappedSuperclass"],
        fields=[FieldInfo(name="id", type="Long", annotations=["Id"], line=3)],
    )
    sub = ClassInfo(
        fqcn="p.Sub",
        simple_name="Sub",
        package="p",
        file="p/Sub.java",
        annotations=["Entity"],
        superclass="Base",
        fields=[
            FieldInfo(name="tmp", type="String", annotations=["Transient"], line=5),
            FieldInfo(name="K", type="int", modifiers=["static", "final"], line=6),
        ],
    )
    recs = build_entity_registry(JavaIndex(classes={"p.Base": base, "p.Sub": sub}), [])
    assert [r.fqcn for r in recs] == ["p.Sub"]
    fs = {f.name: f for f in recs[0].fields}
    assert set(fs) == {"id", "tmp"} and fs["id"].is_id and fs["tmp"].transient
