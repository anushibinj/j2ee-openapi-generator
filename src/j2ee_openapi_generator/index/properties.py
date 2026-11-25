"""Java ``.properties`` parsing and a default-locale bundle index."""

from __future__ import annotations

import posixpath
import re

from j2ee_openapi_generator.index.models import JspFacts, PropertiesBundle, PropertiesIndex

_LOCALE_RE = re.compile(r"^(?P<base>.+?)_[a-z]{2}(?:_[A-Z]{2})?$")
_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}


def _unescape(raw: str) -> str:
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[i + 2 : i + 6]):
            out.append(chr(int(raw[i + 2 : i + 6], 16)))
            i += 6
        else:
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
    return "".join(out)


def _logical_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].lstrip(" \t\f")
        i += 1
        if not line or line[0] in "#!":
            continue
        while True:
            trailing = len(line) - len(line.rstrip("\\"))
            if trailing % 2 == 0 or i >= len(lines):
                break
            line = line[:-1] + lines[i].lstrip(" \t\f")
            i += 1
        result.append(line)
    return result


def _split_entry(line: str) -> tuple[str, str]:
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c in "=: \t\f":
            break
        i += 1
    key = line[:i]
    rest = line[i:].lstrip(" \t\f")
    if rest[:1] in ("=", ":"):
        rest = rest[1:].lstrip(" \t\f")
    return key, rest


def _stem(rel_path: str) -> str:
    name = posixpath.basename(rel_path)
    return name[: -len(".properties")] if name.endswith(".properties") else name


def parse_properties(rel_path: str, text: str) -> PropertiesBundle:
    """Parse ``.properties`` text; ``base_name`` is the stem without a ``_xx``/``_xx_YY`` suffix."""
    entries: dict[str, str] = {}
    for line in _logical_lines(text):
        key, value = _split_entry(line)
        entries[_unescape(key)] = _unescape(value)
    stem = _stem(rel_path)
    m = _LOCALE_RE.match(stem)
    return PropertiesBundle(
        file=rel_path,
        base_name=m.group("base") if m else stem,
        entries=dict(sorted(entries.items())),
    )


def build_properties_index(
    bundles: list[PropertiesBundle],
    jsp_facts: list[JspFacts],
    java_sources: dict[str, str],
) -> PropertiesIndex:
    """Keep default-locale bundles and record which keys each JSP/Java file references.

    A localized file (``messages_fr.properties``) is dropped only when its default sibling
    (``messages.properties`` in the same directory) exists. ``key_refs`` maps the referencing
    file to the sorted keys it uses that exist in some kept bundle: JSP ``fmt:message`` keys and
    any Java string literal exactly equal to a bundle key.
    """
    files = {b.file for b in bundles}
    kept: list[PropertiesBundle] = []
    for b in sorted(bundles, key=lambda x: x.file):
        d = posixpath.dirname(b.file)
        default = posixpath.join(d, b.base_name + ".properties")
        if default != b.file and default in files:
            continue
        kept.append(b.model_copy(update={"base_name": _stem(b.file)}))
    known: set[str] = set()
    for b in kept:
        known.update(b.entries)

    refs: dict[str, set[str]] = {}
    for facts in jsp_facts:
        hit = {k for k in facts.message_keys if k in known}
        if hit:
            refs.setdefault(facts.file, set()).update(hit)
    literal_re = re.compile(r'"((?:\\.|[^"\\\n])*)"')
    for path, src in java_sources.items():
        hit = {_unescape(m.group(1)) for m in literal_re.finditer(src)} & known
        if hit:
            refs.setdefault(path, set()).update(hit)
    return PropertiesIndex(
        bundles=kept,
        key_refs={f: sorted(k) for f, k in sorted(refs.items())},
    )


def lookup_keys(index: PropertiesIndex, keys: list[str]) -> dict[str, str]:
    """Texts for the given keys (first bundle by file order wins); unknown keys are omitted."""
    result: dict[str, str] = {}
    for key in sorted(set(keys)):
        for b in index.bundles:
            if key in b.entries:
                result[key] = b.entries[key]
                break
    return result
