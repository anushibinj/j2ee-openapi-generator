"""Shared index records. Every stage reads/writes these as JSON under ``.oapi/index``.

All models are frozen-ish value objects; lists are always emitted sorted by the producer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"]


class FileRecord(BaseModel):
    """One inventoried file. ``path`` is POSIX, relative to repo_root."""

    path: str
    sha256: str
    size: int
    kind: Literal["java", "jsp", "web_xml", "ejb_xml", "properties", "js", "other"]
    module: str | None = None


class SecurityConstraint(BaseModel):
    url_patterns: list[str]
    http_methods: list[str] = Field(default_factory=list)  # empty = all
    roles: list[str] = Field(default_factory=list)


class FilterRecord(BaseModel):
    name: str
    class_name: str
    url_patterns: list[str] = Field(default_factory=list)


class ServletRecord(BaseModel):
    """A servlet registered through web.xml or @WebServlet."""

    module: str
    name: str
    class_fqcn: str
    url_patterns: list[str]  # raw patterns, e.g. ``/UserServlet`` or ``*.do``
    context_root: str = ""
    source: Literal["web.xml", "annotation"] = "web.xml"
    init_params: dict[str, str] = Field(default_factory=dict)

    @property
    def simple_name(self) -> str:
        return self.class_fqcn.rsplit(".", 1)[-1]


class ModuleRecord(BaseModel):
    name: str
    web_xml: str  # repo-relative path
    root_dir: str  # repo-relative dir containing src (module root)
    context_root: str
    tag_prefix: str = ""
    security_constraints: list[SecurityConstraint] = Field(default_factory=list)
    filters: list[FilterRecord] = Field(default_factory=list)
    security_roles: list[str] = Field(default_factory=list)


class WebIndex(BaseModel):
    modules: list[ModuleRecord] = Field(default_factory=list)
    servlets: list[ServletRecord] = Field(default_factory=list)


# ---------------------------------------------------------------- Java symbols
class FieldInfo(BaseModel):
    name: str
    type: str  # as written, generics included e.g. ``List<String>``
    resolved_type: str | None = None  # FQCN of the raw type when resolvable
    annotations: list[str] = Field(default_factory=list)  # e.g. ``Column(nullable=false)``
    line: int = 0
    modifiers: list[str] = Field(default_factory=list)
    initializer: str | None = None


class MethodInfo(BaseModel):
    name: str
    return_type: str
    params: list[tuple[str, str]] = Field(default_factory=list)  # (type, name)
    annotations: list[str] = Field(default_factory=list)
    modifiers: list[str] = Field(default_factory=list)
    throws: list[str] = Field(default_factory=list)
    start_line: int
    end_line: int

    @property
    def signature(self) -> str:
        return f"{self.name}({','.join(t for t, _ in self.params)})"


class ClassInfo(BaseModel):
    """Lightweight class symbol. Misses: generics bounds, overload resolution, lambdas' types."""

    fqcn: str
    simple_name: str
    package: str
    file: str
    kind: Literal["class", "interface", "enum", "annotation"] = "class"
    imports: list[str] = Field(default_factory=list)
    superclass: str | None = None  # FQCN if resolved else as written
    interfaces: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    fields: list[FieldInfo] = Field(default_factory=list)
    methods: list[MethodInfo] = Field(default_factory=list)
    constants: dict[str, str] = Field(default_factory=dict)  # static final String NAME -> value
    enum_constants: list[str] = Field(default_factory=list)
    start_line: int = 1
    end_line: int = 1


class JavaIndex(BaseModel):
    classes: dict[str, ClassInfo] = Field(default_factory=dict)  # keyed by fqcn, sorted


# ---------------------------------------------------------------- dispatch + facts
class BranchFacts(BaseModel):
    """What one op branch reads, writes and calls. Line numbers are real file lines."""

    params: list[str] = Field(default_factory=list)  # getParameter names
    param_lines: dict[str, int] = Field(default_factory=dict)  # name -> first line
    multi_params: list[str] = Field(default_factory=list)  # getParameterValues names
    reads_param_map: bool = False
    multipart_parts: list[str] = Field(default_factory=list)
    session_reads: list[str] = Field(default_factory=list)
    session_writes: list[str] = Field(default_factory=list)
    attributes_set: list[str] = Field(default_factory=list)
    forwards: list[str] = Field(default_factory=list)  # resolved JSP paths (repo-relative if found)
    includes: list[str] = Field(default_factory=list)
    redirects: list[str] = Field(default_factory=list)
    status_codes: list[int] = Field(default_factory=list)
    status_lines: dict[str, int] = Field(default_factory=dict)  # str(code) -> line
    writes_response: bool = False
    content_types: list[str] = Field(default_factory=list)
    content_disposition: list[str] = Field(default_factory=list)
    exceptions_thrown: list[str] = Field(default_factory=list)
    exceptions_caught: list[str] = Field(default_factory=list)
    ejb_calls: list[str] = Field(default_factory=list)  # ``FQCN#method(paramTypes)`` best effort
    helper_calls: list[str] = Field(default_factory=list)  # same-class helpers followed (depth<=2)
    helper_ranges: list[tuple[str, int, int]] = Field(default_factory=list)  # name,start,end


class DispatchEntry(BaseModel):
    """One op value of one servlet with its code range (an entry point before packets)."""

    module: str
    servlet_fqcn: str
    op: str  # op value, or ``<default>``
    param_name: str  # which request param carried it (``op`` ...)
    style: Literal["if", "switch", "map", "reflection", "default"]
    file: str
    start_line: int
    end_line: int
    http_methods: list[str] = Field(default_factory=lambda: ["GET", "POST"])
    constant: str | None = None  # constant name if value came from one
    handler_method: str | None = None  # reflection / map-bound method name
    facts: BranchFacts = Field(default_factory=BranchFacts)

    @property
    def servlet_simple_name(self) -> str:
        return self.servlet_fqcn.rsplit(".", 1)[-1]


# ---------------------------------------------------------------- EJB / entities
class EjbMethod(BaseModel):
    name: str
    signature: str  # ``name(Type,Type)``
    return_type: str
    start_line: int
    end_line: int
    roles_allowed: list[str] = Field(default_factory=list)
    permit_all: bool = False
    tx_attribute: str | None = None
    throws: list[str] = Field(default_factory=list)


class EjbRecord(BaseModel):
    bean_fqcn: str
    kind: Literal["stateless", "stateful", "singleton", "message-driven", "other"]
    interfaces: list[str] = Field(default_factory=list)  # @Local/@Remote/implemented FQCNs
    interface_files: dict[str, str] = Field(default_factory=dict)  # iface fqcn -> file
    impl_file: str
    methods: list[EjbMethod] = Field(default_factory=list)
    class_roles_allowed: list[str] = Field(default_factory=list)
    jndi_names: list[str] = Field(default_factory=list)


class EntityField(BaseModel):
    name: str
    java_type: str  # as written, generics included
    elem_type: str | None = None  # element type for collections/maps values
    is_id: bool = False
    is_version: bool = False
    nullable: bool | None = None
    length: int | None = None
    not_null: bool = False
    size_min: int | None = None
    size_max: int | None = None
    pattern: str | None = None
    enumerated: bool = False
    enum_values: list[str] = Field(default_factory=list)
    relationship: Literal["ManyToOne", "OneToOne", "OneToMany", "ManyToMany"] | None = None
    target_type: str | None = None
    transient: bool = False
    line: int = 0


class EntityRecord(BaseModel):
    fqcn: str
    simple_name: str
    kind: Literal["entity", "embeddable", "dto", "enum"]
    file: str
    start_line: int
    end_line: int
    fields: list[EntityField] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)


class DomainIndex(BaseModel):
    ejbs: list[EjbRecord] = Field(default_factory=list)
    entities: list[EntityRecord] = Field(default_factory=list)


# ---------------------------------------------------------------- JSP / JS / properties
class FormInput(BaseModel):
    name: str
    type: str = "text"
    required: bool = False
    maxlength: int | None = None
    pattern: str | None = None
    value: str | None = None
    options: list[str] = Field(default_factory=list)
    line: int = 0


class FormFact(BaseModel):
    file: str
    line: int
    action: str
    method: str = "GET"
    enctype: str | None = None
    op: str | None = None  # from hidden input named like a dispatch param
    op_param: str | None = None
    inputs: list[FormInput] = Field(default_factory=list)


class LinkFact(BaseModel):
    file: str
    line: int
    url: str  # raw href
    query: dict[str, str] = Field(default_factory=dict)


class JsCall(BaseModel):
    file: str
    line: int
    kind: Literal["fetch", "xhr", "ajax", "get", "post"]
    url: str
    method: str = "GET"
    params: list[str] = Field(default_factory=list)


class JspFacts(BaseModel):
    file: str  # repo-relative
    module: str | None = None
    forms: list[FormFact] = Field(default_factory=list)
    links: list[LinkFact] = Field(default_factory=list)
    el_refs: list[str] = Field(default_factory=list)  # ``u.name`` (sorted unique)
    foreach: list[tuple[str, str]] = Field(default_factory=list)  # (items expr, var)
    message_keys: list[str] = Field(default_factory=list)  # fmt:message keys
    includes: list[str] = Field(default_factory=list)
    js_calls: list[JsCall] = Field(default_factory=list)
    form_defaults: dict[str, str] = Field(default_factory=dict)  # input name -> default value


class PropertiesBundle(BaseModel):
    file: str
    base_name: str  # without locale suffix
    entries: dict[str, str] = Field(default_factory=dict)


class PropertiesIndex(BaseModel):
    bundles: list[PropertiesBundle] = Field(default_factory=list)
    key_refs: dict[str, list[str]] = Field(default_factory=dict)  # file -> keys referenced


class LinkedRef(BaseModel):
    """Resolved JSP/JS URL -> entry point (servlet class + op)."""

    source_file: str
    line: int
    kind: Literal["form", "link", "js"]
    servlet_fqcn: str
    module: str
    op: str | None
    http_method: str = "GET"
    params: list[str] = Field(default_factory=list)


class AccessCount(BaseModel):
    module: str = ""
    url_pattern: str
    op: str | None
    count: int


class IndexBundle(BaseModel):
    """Everything stage 1 produces, loaded back for packets/verify/aggregate.

    Stored on disk as one JSON file per field under ``.oapi/index/{field}.json``
    (see ``workspace.save_index_bundle`` / ``load_index_bundle``).
    """

    files: list[FileRecord] = Field(default_factory=list)
    web: WebIndex = Field(default_factory=WebIndex)
    java: JavaIndex = Field(default_factory=JavaIndex)
    dispatch: list[DispatchEntry] = Field(default_factory=list)  # with facts, sorted by id
    domain: DomainIndex = Field(default_factory=DomainIndex)
    jsps: list[JspFacts] = Field(default_factory=list)  # includes JS-only pseudo entries
    properties: PropertiesIndex = Field(default_factory=PropertiesIndex)
    links: list[LinkedRef] = Field(default_factory=list)
    access: list[AccessCount] = Field(default_factory=list)
