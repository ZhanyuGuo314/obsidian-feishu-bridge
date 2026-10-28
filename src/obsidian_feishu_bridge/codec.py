"""Deterministic Markdown/DocxXML adapters. Unknown structures fail closed."""

from __future__ import annotations

import hashlib
import html
import json
import re
import xml.etree.ElementTree as E
from copy import deepcopy
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

PARSER = MarkdownIt("commonmark", {"html": True}).enable("table").use(dollarmath_plugin)


class Unsupported(ValueError):
    pass


def hash_bytes(b):
    return hashlib.sha256(b).hexdigest()


def docroot(xml):
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise Unsupported("xml_entity")
    return E.fromstring("<root>" + xml + "</root>")


def xmlstr(nodes):
    return "".join(E.tostring(n, encoding="unicode") for n in nodes)


def frontmatter(text):
    m = re.match(r"\A---\s*\n.*?\n---\s*(?:\n|$)", text, re.S)
    return (m[0], text[m.end() :]) if m else ("", text)


def render(text, resolver=None):
    """Returns independent top-level XML blocks; resources resolved by caller."""
    _, body = frontmatter(text.replace("\r\n", "\n"))
    # Obsidian embeds and links, excluding code spans/fences according to parser maps.
    protected = []
    offsets = [0]
    for line in body.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    for t in PARSER.parse(body):
        if t.type in ("fence", "code_block") and t.map:
            protected.append((offsets[t.map[0]], offsets[t.map[1]]))
    protected.extend((m.start(), m.end()) for m in re.finditer(r"(`+)[^`]*?\1", body))

    def wikilink(m):
        if any(a <= m.start() < b for a, b in protected):
            return m[0]
        value = m[2].replace("\\|", "|")
        target, _, label = value.partition("|")
        if not resolver:
            raise Unsupported("unresolved_wikilink:" + target)
        kind, url = resolver(target, bool(m[1]))
        if kind == "image":
            return f"![{label}](<{url}>)"
        return f"[{label or Path(target).stem}](<{url}>)"

    body = re.sub(r"(!?)\[\[([^\]\n]+)\]\]", wikilink, body)

    def emit(tokens):
        out = []
        for t in tokens:
            typ = t.type
            if typ == "inline":
                out.append(emit(t.children or []))
            elif typ == "text":
                out.append(html.escape(t.content))
            elif typ in ("softbreak", "hardbreak"):
                out.append("<br/>")
            elif typ == "code_inline":
                out.append("<code>" + html.escape(t.content) + "</code>")
            elif typ in ("fence", "code_block"):
                out.append(
                    '<pre lang="'
                    + html.escape((t.info.split() or ["plaintext"])[0], quote=True)
                    + '"><code>'
                    + html.escape(t.content.rstrip("\n"))
                    + "</code></pre>"
                )
            elif typ.startswith("math_"):
                value = "<latex>" + html.escape(t.content.strip()) + "</latex>"
                out.append(
                    value
                    if typ.startswith("math_inline")
                    else '<p align="center">' + value + "</p>"
                )
            elif typ == "image":
                src = t.attrGet("src") or ""
                if resolver and not src.startswith(("@./", "https://", "http://")):
                    _, src = resolver(src, True)
                if src.startswith("@./"):
                    attr = "path"
                else:
                    raise Unsupported("image_requires_local_original:" + src)
                out.append(
                    "<img "
                    + attr
                    + '="'
                    + html.escape(src, quote=True)
                    + '" name="'
                    + html.escape(Path(src).name, quote=True)
                    + '" caption="'
                    + html.escape(t.content, quote=True)
                    + '"/>'
                )
            elif typ in ("html_inline", "html_block"):
                raw = t.content.strip()
                if re.fullmatch(r"</?(b|em|u|del|span)\s*>", raw) or re.fullmatch(
                    r"<br\s*/?>", raw
                ):
                    out.append(raw.replace("<br>", "<br/>"))
                else:
                    raise Unsupported("raw_html:" + raw[:60])
            elif typ == "hr":
                out.append("<hr/>")
            elif typ.endswith("_open"):
                tag = "b" if typ == "strong_open" else t.tag
                if typ == "link_open":
                    url = t.attrGet("href") or ""
                    if resolver and not url.startswith(("http://", "https://", "mailto:", "#")):
                        _, url = resolver(url, False)
                    out.append('<a href="' + html.escape(url, quote=True) + '">')
                elif tag in ("td", "th"):
                    out.append("<" + tag + "><p>")
                elif tag == "ol" and t.attrGet("start"):
                    out.append('<ol start="' + str(t.attrGet("start")) + '">')
                else:
                    out.append("<" + tag + ">")
            elif typ.endswith("_close"):
                tag = "b" if typ == "strong_close" else t.tag
                out.append("</p></" + tag + ">" if tag in ("td", "th") else "</" + tag + ">")
            else:
                raise Unsupported("markdown_token:" + typ)
        return "".join(out)

    root = docroot(emit(PARSER.parse(body)))

    # Inline images must be lifted out of paragraphs, preserving leading/trailing text.
    def lift(parent):
        for n in list(parent):
            if n.tag == "p" and any(c.tag == "img" for c in n):
                pos = list(parent).index(n)
                parent.remove(n)
                pieces = []
                p = E.Element("p", n.attrib)
                p.text = n.text
                for child in list(n):
                    if child.tag == "img":
                        if p.text or len(p):
                            pieces.append(p)
                        tail = child.tail
                        child.tail = None
                        pieces.append(child)
                        p = E.Element("p", n.attrib)
                        p.text = tail
                    else:
                        p.append(child)
                if p.text or len(p):
                    pieces.append(p)
                for i, p in enumerate(pieces):
                    parent.insert(pos + i, p)
            else:
                lift(n)

    lift(root)
    return [E.tostring(n, encoding="unicode") for n in root]


VOLATILE = {
    "id",
    "scale",
    "height",
    "width",
    "seq-marker",
    "seq",
    "resource-id",
    "col-width",
    "vertical-align",
    "background-color",
}


def canonical_node(node, image_map=None):
    """Semantic structure includes order, text, formula, code, links and resources."""
    node = deepcopy(node)

    # Table cell and list paragraphs are wrappers in the service representation.
    def key(n):
        tag = n.tag
        if tag == "title":
            return None
        if tag == "colgroup":
            return None
        if tag == "img":
            src = n.get("src") or n.get("path") or n.get("token")
            if image_map and src in image_map:
                src = image_map[src]
            return ["img", src]
        attrs = {
            k: v
            for k, v in n.attrib.items()
            if k not in VOLATILE
            and k not in ("name", "caption", "align", "mime", "start", "text-color")
        }
        if tag == "pre":
            attrs = {"lang": n.get("lang", "plaintext").lower()}
        if tag in ("latex", "code"):
            value = (
                "".join(
                    ("\n" if c.tag == "br" else "".join(c.itertext())) + (c.tail or "") for c in n
                )
                if len(n)
                else ""
            )
            value = (n.text or "") + value
            if tag == "latex":
                value = re.sub(r"\s+", " ", value).strip()
            return [tag, attrs, value]
        parts = []
        if n.text:
            parts.append(n.text.replace("\u00a0", " "))
        for c in n:
            value = key(c)
            if value is not None:
                if c.tag == "p" and tag in ("li", "td", "th"):
                    if parts:
                        parts.append(["br", {}, []])
                    parts.extend(value[2])
                else:
                    parts.append(value)
            if c.tail:
                parts.append(c.tail.replace("\u00a0", " "))
        # API frequently drops whitespace-only paragraphs at document boundaries.
        if tag == "p" and not attrs and not any(str(p).strip() for p in parts):
            return None
        return [tag, attrs, parts]

    return key(node)


def semantic(xml, image_map=None):
    return [k for n in docroot(xml) if (k := canonical_node(n, image_map)) is not None]


def semantic_hash(xml):
    return hash_bytes(json.dumps(semantic(xml), ensure_ascii=False, sort_keys=True).encode())


def block_signatures(xml):
    return [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in semantic(xml)]


def to_markdown(xml, link_resolver=None, image_resolver=None):
    """Loss-checkable native-to-Markdown projection; opaque native blocks stop."""

    def esc(s):
        return re.sub(r"([\\`*\[\]<>])", r"\\\1", s or "")

    def inner(n):
        return esc(n.text) + "".join(emit(c) + esc(c.tail) for c in n)

    def literal(n):
        return (n.text or "") + "".join(
            ("\n" if c.tag == "br" else literal(c)) + (c.tail or "") for c in n
        )

    def emit(n):
        t = n.tag
        if t == "title" or t == "colgroup":
            return ""
        if t == "br":
            return "\n"
        if t == "p":
            return inner(n) + "\n\n"
        if re.fullmatch(r"h[1-6]", t):
            return "#" * int(t[1]) + " " + inner(n) + "\n\n"
        if t == "b":
            return "**" + inner(n) + "**"
        if t == "em":
            return "*" + inner(n) + "*"
        if t == "code":
            s = literal(n)
            f = "`" * (max([len(x) for x in re.findall(r"`+", s)] + [0]) + 1)
            return f + s + f
        if t == "pre":
            s = literal(n)
            f = "`" * max(3, max([len(x) + 1 for x in re.findall(r"`+", s)] + [3]))
            return f + (n.get("lang") or "") + "\n" + s + "\n" + f + "\n\n"
        if t == "latex":
            return "$" + literal(n) + "$"
        if t == "a":
            url = n.get("href", "")
            if link_resolver:
                url = link_resolver(url)
            return "[" + inner(n) + "](<" + url + ">)"
        if t == "hr":
            return "---\n\n"
        if t in ("ul", "ol"):
            lines = []
            for i, c in enumerate(n):
                if c.tag != "li":
                    raise Unsupported("list_child")
                value = inner(c).strip().splitlines()
                prefix = f"{i + 1}. " if t == "ol" else "- "
                lines.append(
                    prefix
                    + (value[0] if value else "")
                    + "\n"
                    + "".join("  " + s + "\n" for s in value[1:])
                )
            return "".join(lines) + "\n"
        if t == "blockquote":
            return "".join("> " + s + "\n" for s in inner(n).rstrip().splitlines()) + "\n"
        if t == "table":
            rows = []
            for row in n.iter("tr"):
                cells = [
                    inner(c).strip().replace("\n", "<br/>").replace("|", "\\|")
                    for c in row
                    if c.tag in ("td", "th")
                ]
                if any(c.get("rowspan") or c.get("colspan") for c in row):
                    raise Unsupported("merged_cells")
                rows.append(cells)
            if not rows:
                return ""
            return (
                "\n".join(
                    "| " + " | ".join(c) + " |"
                    for c in [rows[0], ["---"] * len(rows[0])] + rows[1:]
                )
                + "\n\n"
            )
        if t == "img":
            if not image_resolver:
                raise Unsupported("image_download_adapter_required")
            return "![](<" + image_resolver(n) + ">)\n\n"
        raise Unsupported("native_block:" + t)

    return "".join(emit(n) for n in docroot(xml)).rstrip() + "\n"
