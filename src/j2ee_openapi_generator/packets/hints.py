"""Deterministic mapping hints: legacy op name -> HTTP method, shape and resource nouns."""

from __future__ import annotations

import re
from functools import lru_cache

import inflect

from j2ee_openapi_generator.index.ejb import all_type_names
from j2ee_openapi_generator.index.models import DispatchEntry, EjbMethod, EntityRecord, IndexBundle
from j2ee_openapi_generator.packets.models import MappingHints

LIST_VERBS = frozenset({"list", "search", "find", "query", "all"})
READ_VERBS = frozenset({"read", "view", "get", "show", "detail", "details", "load"})
CREATE_VERBS = frozenset({"add", "create", "new", "insert"})
UPDATE_VERBS = frozenset({"update", "modify"})
DELETE_VERBS = frozenset({"delete", "remove"})
BINARY_VERBS = frozenset({"export", "download", "print"})
MULTIPART_VERBS = frozenset({"upload", "import"})
_SERVLET_SUFFIXES = ("Servlet", "Controller", "Action")
_ENTITY_SUFFIX = re.compile(r"(Dto|DTO|Entity|Bean|VO|Vo)$")
_MUTATING = ("create", "add", "insert", "save", "update", "modify", "delete", "remove", "persist")


@lru_cache(maxsize=1)
def _engine() -> inflect.engine:
    return inflect.engine()


def split_words(name: str) -> list[str]:
    """Split camelCase, snake_case and kebab-case into lower-case words (digits stay attached)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+|\s+", spaced) if w]


def kebab(name: str) -> str:
    """``UserAccount`` -> ``user-account``; ``step1`` -> ``step1``."""
    return "-".join(split_words(name))


def plural_kebab(name: str) -> str:
    """Kebab-case with the last word pluralized: ``UserAccount`` -> ``user-accounts``."""
    words = split_words(name)
    if not words:
        return ""
    last = words[-1]
    plural = _engine().plural_noun(last) or last
    words[-1] = str(plural)
    return "-".join(words)


def servlet_noun(servlet_name: str) -> str:
    """Servlet simple name without the Servlet/Controller/Action suffix."""
    for suf in _SERVLET_SUFFIXES:
        if servlet_name.endswith(suf) and len(servlet_name) > len(suf):
            return servlet_name[: -len(suf)]
    return servlet_name


def resource_candidates(servlet_name: str, primary_entity: str | None) -> list[str]:
    """Plural kebab nouns: servlet-derived first, then entity-derived, deduplicated."""
    out: list[str] = []
    entity = _ENTITY_SUFFIX.sub("", primary_entity) if primary_entity else ""
    for src in (servlet_noun(servlet_name), entity):
        cand = plural_kebab(src)
        if cand and cand not in out:
            out.append(cand)
    return out


def _verb_of(op: str) -> str:
    """Pick the verb of an op name: its first word, or its last word if only that is known."""
    words = split_words(op)
    if not words:
        return ""
    known = (
        LIST_VERBS | READ_VERBS | CREATE_VERBS | UPDATE_VERBS | DELETE_VERBS
        | BINARY_VERBS | MULTIPART_VERBS | {"save", "edit"}
    )  # fmt: skip
    if words[0] in known:
        return words[0]
    if words[-1] in known:
        return words[-1]
    return words[0]


def compute_hints(
    entry: DispatchEntry,
    servlet_name: str,
    primary_entity: str | None,
    has_id_param: bool,
    forwards_only_form: bool,
    binary_response: bool,
    multipart: bool,
) -> MappingHints:
    """Apply the deterministic op-name table; see the module docstring of the packets stage."""
    cands = resource_candidates(servlet_name, primary_entity)

    def hint(method: str, shape: str, reason: str, verb: str | None = None) -> MappingHints:
        return MappingHints(
            http_method=method,
            shape=shape,
            verb_kebab=verb,
            resource_candidates=cands,
            reason=reason,
        )

    op = entry.op
    if op == "<default>":
        if entry.facts.ejb_calls:
            return hint(
                "POST",
                "action",
                "default branch calls EJB methods; model as an action",
                "default",
            )
        return hint("NONE", "no_api_needed", "default branch only redirects or forwards")
    verb = _verb_of(op)
    if verb in LIST_VERBS:
        return hint("GET", "collection", f"op '{op}' is a list/search verb")
    if verb in BINARY_VERBS:
        return hint("GET", "binary", f"op '{op}' is an export/download verb")
    if verb in MULTIPART_VERBS:
        return hint("POST", "multipart", f"op '{op}' is an upload/import verb")
    if verb in DELETE_VERBS:
        return hint("DELETE", "item", f"op '{op}' is a delete verb")
    if verb in UPDATE_VERBS:
        return hint("PUT", "item", f"op '{op}' is an update verb (PATCH if only some fields)")
    if verb == "save":
        if has_id_param:
            return hint(
                "PUT",
                "item",
                "save with an id param updates (PATCH if partial update); without an id it creates (POST)",
            )
        return hint("POST", "collection", "save without an id param creates")
    if verb == "edit":
        if forwards_only_form:
            return hint("GET", "item", "edit only loads a form, same data as read")
        return hint("PUT", "item", "edit writes data (PATCH if partial update)")
    if verb == "new":
        if forwards_only_form:
            return hint("NONE", "no_api_needed", "new only renders an empty form")
        return hint("POST", "collection", "new creates a resource")
    if verb in CREATE_VERBS:
        return hint("POST", "collection", f"op '{op}' is a create verb")
    if verb in READ_VERBS:
        return hint("GET", "item", f"op '{op}' is a read verb")
    if multipart:
        return hint("POST", "multipart", "request is multipart")
    if binary_response:
        return hint("GET", "binary", "response is a binary download")
    action = kebab(op)
    return hint(
        "POST",
        "action",
        f"unknown verb '{op}': POST {{prefix}}/{{resources}}/{{id}}/{action}",
        action,
    )


# ------------------------------------------------------------------ primary entity
def _method_types(m: EjbMethod) -> list[str]:
    inner = m.signature[m.signature.find("(") + 1 : m.signature.rfind(")")]
    types = [t for t in inner.split(",") if t]
    return [*all_type_names(m.return_type), *[n for t in types for n in all_type_names(t)]]


def _module_of_file(bundle: IndexBundle, path: str) -> str | None:
    best: tuple[int, str] | None = None
    for m in bundle.web.modules:
        root = m.root_dir
        if (not root or path.startswith(root + "/")) and (best is None or len(root) > best[0]):
            best = (len(root), m.name)
    return best[1] if best else None


def pick_primary_entity(entry: DispatchEntry, bundle: IndexBundle) -> str | None:
    """Entity/DTO simple name most referenced by the EJB calls, else the servlet noun match."""
    entities: dict[str, EntityRecord] = {
        e.simple_name: e
        for e in bundle.domain.entities
        if e.kind in ("entity", "dto") and _module_of_file(bundle, e.file) in (entry.module, None)
    }
    counts: dict[str, int] = {}
    for call in entry.facts.ejb_calls:
        fqcn, _, sig = call.partition("#")
        for ejb in bundle.domain.ejbs:
            if fqcn != ejb.bean_fqcn and fqcn not in ejb.interfaces:
                continue
            for m in ejb.methods:
                if m.signature == sig:
                    for t in _method_types(m):
                        if t in entities:
                            counts[t] = counts.get(t, 0) + 1
    if counts:
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    noun = servlet_noun(entry.servlet_simple_name).lower()
    for name in sorted(entities):
        if name.lower() == noun:
            return name
    return None


def only_forwards_form(entry: DispatchEntry) -> bool:
    """True when the branch forwards to a JSP and performs no EJB mutation or response write."""
    f = entry.facts
    if not f.forwards or f.writes_response:
        return False
    return not any(c.rsplit("#", 1)[-1].lower().startswith(_MUTATING) for c in f.ejb_calls)
