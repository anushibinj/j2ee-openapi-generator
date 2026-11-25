"""JSP and JavaScript fact extraction.

The JSP is first masked (scriptlets, expressions, declarations, directives and comments become
spaces, newlines kept) so every offset keeps its original line. Facts are then found with a
tolerant regex tag tokenizer. Limitation: anything produced inside scriptlets is invisible.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence

from j2ee_openapi_generator.index.models import (
    FormFact,
    FormInput,
    JsCall,
    JspFacts,
    LinkFact,
)

_BLANK_RE = re.compile(r"[^\r\n]")
_JSP_COMMENT_RE = re.compile(r"<%--.*?--%>", re.DOTALL)
_JSP_SCRIPT_RE = re.compile(r"<%.*?%>", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _blank(match: re.Match[str]) -> str:
    return _BLANK_RE.sub(" ", match.group(0))


def mask_jsp(source: str) -> str:
    """Blank out ``<% %>``, ``<%= %>``, ``<%! %>``, ``<%@ %>`` and ``<%-- --%>`` blocks.

    The result has the same length and the same newline positions as ``source``.
    """
    return _JSP_SCRIPT_RE.sub(_blank, _JSP_COMMENT_RE.sub(_blank, source))


# ------------------------------------------------------------------ includes
_DIRECTIVE_INCLUDE_RE = re.compile(r"<%@\s*include\b[^%]*?\bfile\s*=\s*([\"'])(.*?)\1", re.DOTALL)
_TAG_INCLUDE_RE = re.compile(
    r"<(?:jsp:include|c:import)\b[^>]*?\b(?:page|url)\s*=\s*([\"'])(.*?)\1", re.DOTALL
)


def _extract_includes(comment_masked: str) -> list[str]:
    found = {m.group(2).strip() for m in _DIRECTIVE_INCLUDE_RE.finditer(comment_masked)}
    found |= {m.group(2).strip() for m in _TAG_INCLUDE_RE.finditer(comment_masked)}
    return sorted(x for x in found if x)


# ------------------------------------------------------------------ tag tokenizer
_TAG_RE = re.compile(
    r"<(/?)([A-Za-z][\w:.-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>",
)
_ATTR_RE = re.compile(r"([^\s=/\"'<>]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?")
_SCRIPT_SPAN_RE = re.compile(r"<(script|style)\b[^>]*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE)
_C_URL_RE = re.compile(r"<c:url\b[^>]*?\bvalue\s*=\s*([\"'])(.*?)\1", re.DOTALL)


class _Tag:
    __slots__ = ("attrs", "closing", "name", "pos")

    def __init__(self, pos: int, closing: bool, name: str, attrs: dict[str, str]) -> None:
        self.pos = pos
        self.closing = closing
        self.name = name
        self.attrs = attrs


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        name = m.group(1).lower()
        val = m.group(2)
        if val is None:
            val = ""
        elif val[:1] in "\"'" and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]
        if name not in attrs:
            attrs[name] = val
    return attrs


def _tokenize(text: str, skip_spans: list[tuple[int, int]]) -> list[_Tag]:
    tags: list[_Tag] = []
    starts = [s for s, _ in skip_spans]
    for m in _TAG_RE.finditer(text):
        i = bisect_right(starts, m.start()) - 1
        if i >= 0 and skip_spans[i][0] <= m.start() < skip_spans[i][1]:
            continue
        tags.append(_Tag(m.start(), bool(m.group(1)), m.group(2).lower(), _parse_attrs(m.group(3))))
    return tags


def _unamp(value: str) -> str:
    return value.replace("&amp;", "&")


def _resolve_c_url(value: str) -> str:
    m = _C_URL_RE.search(value)
    return m.group(2) if m else value


def split_query(url: str) -> tuple[str, dict[str, str]]:
    """Split ``path?a=1&b`` into the path and an ordered query dict (raw, not decoded)."""
    url = _unamp(url)
    if "#" in url and not url.startswith("#"):
        url = url.split("#", 1)[0]
    path, _, qs = url.partition("?")
    query: dict[str, str] = {}
    for part in qs.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        if k and k not in query:
            query[k] = v
    return path, query


# ------------------------------------------------------------------ EL
_EL_RE = re.compile(r"\$\{([^}]*)\}")
_EL_STR_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
_EL_FUNC_RE = re.compile(r"[A-Za-z_]\w*:[A-Za-z_]\w*\s*\(")
_EL_PATH_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(\s*\()?")
_EL_KEYWORDS = frozenset(
    {
        "empty", "not", "and", "or", "eq", "ne", "lt", "gt", "le", "ge",
        "div", "mod", "true", "false", "null", "instanceof",
    }
)  # fmt: skip


def _el_refs(text: str) -> list[str]:
    refs: set[str] = set()
    for m in _EL_RE.finditer(text):
        expr = _EL_STR_RE.sub(" ", m.group(1))
        expr = _EL_FUNC_RE.sub("(", expr)
        for p in _EL_PATH_RE.finditer(expr):
            if p.group(2) or p.group(1) in _EL_KEYWORDS:
                continue
            refs.add(p.group(1))
    return sorted(refs)


def _el_inner(value: str) -> str:
    m = re.fullmatch(r"\s*\$\{(.*)\}\s*", value, re.DOTALL)
    return m.group(1).strip() if m else value.strip()


_MSG_RE = re.compile(r"<fmt:message\b[^>]*?\bkey\s*=\s*([\"'])(.*?)\1", re.DOTALL)


# ------------------------------------------------------------------ JS
_STR = r"""(["'`])((?:\\.|(?!\1)[^\\\n])*)\1"""
_FETCH_RE = re.compile(r"\bfetch\s*\(\s*" + _STR)
_AJAX_RE = re.compile(r"(?:\$|\bjQuery)\s*\.\s*ajax\s*\(\s*\{")
_SHORT_RE = re.compile(
    r"(?:\$|\bjQuery)\s*\.\s*(get|post|getJSON)\s*\(\s*([\"'`])((?:\\.|(?!\2)[^\\\n])*)\2"
)
_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}


def _strip_js_comments(text: str) -> str:
    """Blank JS comments (keeping newlines and string contents)."""
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote or (c == "\n" and quote != "`"):
                quote = ""
            i += 1
        elif c in "\"'`":
            quote = c
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(_BLANK_RE.sub(" ", text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _balanced(text: str, open_idx: int) -> int:
    """Index of the bracket closing the one at ``open_idx`` (or len(text))."""
    depth = 0
    quote = ""
    i, n = open_idx, len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


_KEY_RE = re.compile(r"\s*(?:([\"'])(.*?)\1|([A-Za-z_$][\w$]*))\s*:")
_LITERAL_RE = re.compile(r"""(["'])(.*)\1""", re.DOTALL)


def _object_entries(body: str) -> list[tuple[str, str | None]]:
    """Top-level ``key: value`` pairs of an object literal body; value is a literal or None."""
    entries: list[tuple[str, str | None]] = []
    i, n = 0, len(body)
    while i < n:
        m = _KEY_RE.match(body, i)
        if not m:
            break
        key = m.group(2) if m.group(2) is not None else m.group(3)
        j = m.end()
        start = j
        depth = 0
        quote = ""
        while j < n:
            c = body[j]
            if quote:
                if c == "\\":
                    j += 1
                elif c == quote:
                    quote = ""
            elif c in "\"'`":
                quote = c
            elif c in "{[(":
                depth += 1
            elif c in "}])":
                depth -= 1
            elif c == "," and depth == 0:
                break
            j += 1
        value = body[start:j].strip()
        lit = _LITERAL_RE.fullmatch(value)
        entries.append((key, lit.group(2) if lit and lit.group(1) not in value[1:-1] else None))
        i = j + 1
    return entries


def _call_params(
    url: str,
    data: list[tuple[str, str | None]],
    dispatch_params: Sequence[str],
) -> tuple[str, list[str]]:
    path, query = split_query(url)
    params = set(query)
    merged = dict(query)
    for key, lit in data:
        params.add(key)
        if key in dispatch_params and lit is not None and key not in merged:
            merged[key] = lit
    if merged != query:
        url = path + "?" + "&".join(f"{k}={v}" for k, v in merged.items())
    return url, sorted(p for p in params if p)


def _line_starts(text: str) -> list[int]:
    return [0] + [m.end() for m in re.finditer(r"\n", text)]


def _scan_js(
    rel_path: str, text: str, line_base: int, dispatch_params: Sequence[str]
) -> list[JsCall]:
    text = _strip_js_comments(text)
    starts = _line_starts(text)

    def line_of(pos: int) -> int:
        return line_base + bisect_right(starts, pos) - 1

    def following_object(pos: int) -> list[tuple[str, str | None]]:
        m = re.compile(r"\s*,\s*\{").match(text, pos)
        if not m:
            return []
        open_idx = m.end() - 1
        return _object_entries(text[open_idx + 1 : _balanced(text, open_idx)])

    calls: list[JsCall] = []

    def add(pos: int, kind: str, url: str, method: str, data: list[tuple[str, str | None]]) -> None:
        url, params = _call_params(url, data, dispatch_params)
        calls.append(
            JsCall(
                file=rel_path,
                line=line_of(pos),
                kind=kind,
                url=url,
                method=method,
                params=params,
            )
        )

    for m in _FETCH_RE.finditer(text):
        opts = following_object(m.end())
        method = "GET"
        for k, v in opts:
            if k == "method" and v and v.upper() in _HTTP_METHODS:
                method = v.upper()
        add(m.start(), "fetch", m.group(2), method, [])
    for m in re.finditer(
        r"\.open\s*\(\s*([\"'])(\w+)\1\s*,\s*([\"'`])((?:\\.|(?!\3)[^\\\n])*)\3", text
    ):
        if m.group(2).upper() in _HTTP_METHODS:
            add(m.start(), "xhr", m.group(4), m.group(2).upper(), [])
    for m in _AJAX_RE.finditer(text):
        open_idx = m.end() - 1
        body = text[open_idx + 1 : _balanced(text, open_idx)]
        entries = _object_entries(body)
        url = next((v for k, v in entries if k == "url" and v), None)
        if url is None:
            continue
        method = "GET"
        for k, v in entries:
            if k in ("type", "method") and v and v.upper() in _HTTP_METHODS:
                method = v.upper()
        data: list[tuple[str, str | None]] = []
        dm = re.search(r"\bdata\s*:\s*\{", body)
        if dm:
            o = dm.end() - 1
            data = _object_entries(body[o + 1 : _balanced(body, o)])
        add(m.start(), "ajax", url, method, data)
    for m in _SHORT_RE.finditer(text):
        verb = m.group(1)
        add(
            m.start(),
            "post" if verb == "post" else "get",
            m.group(3),
            "POST" if verb == "post" else "GET",
            following_object(m.end()),
        )
    calls.sort(key=lambda c: (c.line, c.url, c.kind))
    return calls


def parse_js_calls(
    rel_path: str, text: str, dispatch_params: Sequence[str] = ("op",)
) -> list[JsCall]:
    """Find server calls (fetch, XHR, jQuery ajax/get/post/getJSON) in JavaScript source.

    Literal values of dispatch params in a data object (``{op: 'delete'}``) are appended to the
    returned ``url`` query so the linker can see the op.
    """
    return _scan_js(rel_path, text, 1, dispatch_params)


# ------------------------------------------------------------------ JSP parse
_NON_NAMED_TYPES = frozenset({"submit", "button", "reset", "image"})


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse_jsp(
    rel_path: str,
    source: str,
    module: str | None,
    dispatch_params: list[str],
) -> JspFacts:
    """Extract forms, links, EL refs, forEach, message keys, includes and JS calls."""
    comment_masked = _JSP_COMMENT_RE.sub(_blank, source)
    includes = _extract_includes(comment_masked)
    text = _HTML_COMMENT_RE.sub(_blank, _JSP_SCRIPT_RE.sub(_blank, comment_masked))
    starts = _line_starts(text)

    def line_of(pos: int) -> int:
        return bisect_right(starts, pos)

    spans = [(m.start(2), m.end(2)) for m in _SCRIPT_SPAN_RE.finditer(text)]
    tags = _tokenize(text, spans)

    forms: list[FormFact] = []
    links: list[LinkFact] = []
    foreach: set[tuple[str, str]] = set()
    defaults: dict[str, str] = {}
    cur: FormFact | None = None
    cur_url_link: LinkFact | None = None
    select: FormInput | None = None
    select_selected: str | None = None
    option_first: str | None = None

    def close_select() -> None:
        nonlocal select, select_selected, option_first
        if select is not None:
            chosen = select_selected if select_selected is not None else None
            select.value = chosen
            if chosen is not None and "${" not in chosen:
                defaults.setdefault(select.name, chosen)
        select, select_selected, option_first = None, None, None

    def add_input(inp: FormInput) -> None:
        if cur is not None:
            cur.inputs.append(inp)

    for t in tags:
        a = t.attrs
        if t.name == "form":
            if t.closing:
                close_select()
                if cur is not None:
                    forms.append(cur)
                    cur = None
                continue
            close_select()
            if cur is not None:
                forms.append(cur)
            cur = FormFact(
                file=rel_path,
                line=line_of(t.pos),
                action=_unamp(_resolve_c_url(a.get("action", ""))),
                method=(a.get("method") or "GET").upper(),
                enctype=a.get("enctype") or None,
            )
        elif t.name == "input" and not t.closing:
            name = a.get("name")
            itype = (a.get("type") or "text").lower()
            if not name or itype in _NON_NAMED_TYPES:
                continue
            inp = FormInput(
                name=name,
                type=itype,
                required="required" in a,
                maxlength=_int_or_none(a.get("maxlength")),
                pattern=a.get("pattern"),
                value=a.get("value"),
                line=line_of(t.pos),
            )
            add_input(inp)
            if inp.value is not None and "${" not in inp.value:
                defaults.setdefault(name, inp.value)
            if cur is not None and itype == "hidden" and name in dispatch_params and cur.op is None:
                cur.op = inp.value
                cur.op_param = name
        elif t.name == "textarea" and not t.closing:
            name = a.get("name")
            if name:
                add_input(
                    FormInput(
                        name=name,
                        type="textarea",
                        required="required" in a,
                        maxlength=_int_or_none(a.get("maxlength")),
                        line=line_of(t.pos),
                    )
                )
        elif t.name == "select":
            if t.closing:
                close_select()
            elif a.get("name"):
                close_select()
                select = FormInput(
                    name=a["name"],
                    type="select",
                    required="required" in a,
                    line=line_of(t.pos),
                )
                add_input(select)
        elif t.name == "option" and not t.closing and select is not None:
            val = a.get("value")
            if val is None:
                continue
            select.options.append(val)
            if "selected" in a and select_selected is None:
                select_selected = val
        elif t.name == "a" and not t.closing:
            href = _resolve_c_url(a.get("href", "")).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            _, query = split_query(href)
            links.append(LinkFact(file=rel_path, line=line_of(t.pos), url=href, query=query))
        elif t.name == "c:url":
            if t.closing:
                cur_url_link = None
            elif a.get("value") and not a.get("value", "").startswith("#"):
                v = a["value"]
                _, query = split_query(v)
                cur_url_link = LinkFact(file=rel_path, line=line_of(t.pos), url=v, query=query)
                links.append(cur_url_link)
        elif t.name == "c:param" and cur_url_link is not None and a.get("name"):
            cur_url_link.query.setdefault(a["name"], a.get("value", ""))
        elif t.name == "c:foreach" and not t.closing and a.get("items"):
            foreach.add((_el_inner(a["items"]), a.get("var", "")))
    close_select()
    if cur is not None:
        forms.append(cur)

    js_calls: list[JsCall] = []
    for m in _SCRIPT_SPAN_RE.finditer(text):
        if m.group(1).lower() != "script":
            continue
        head = text[m.start() : m.start(2)]
        if re.search(r"\bsrc\s*=", head):
            continue
        base = line_of(m.start(2))
        js_calls.extend(_scan_js(rel_path, m.group(2), base, dispatch_params))
    js_calls.sort(key=lambda c: (c.line, c.url, c.kind))

    keys = {m.group(2).strip() for m in _MSG_RE.finditer(text) if "${" not in m.group(2)}
    return JspFacts(
        file=rel_path,
        module=module,
        forms=forms,
        links=links,
        el_refs=_el_refs(text),
        foreach=sorted(foreach),
        message_keys=sorted(k for k in keys if k),
        includes=includes,
        js_calls=js_calls,
        form_defaults=dict(sorted(defaults.items())),
    )
