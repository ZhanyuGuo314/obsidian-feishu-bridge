"""Policy-driven local projections preserve native cloud resource ownership."""

from __future__ import annotations

import xml.etree.ElementTree as E
from urllib.parse import urlsplit

from .codec import Unsupported, docroot, xmlstr

NATIVE = {
    "whiteboard": "whiteboard",
    "sheet": "sheet",
    "bitable": "bitable",
    "source": "file",
    "figure": "file",
    "readonly-block": "native",
}


def project(xml: str, url: str, resources: dict) -> str:
    root = docroot(xml)

    def walk(parent):
        for node in list(parent):
            kind = NATIVE.get(node.tag)
            if node.tag == "cite" and node.get("type") == "doc":
                kind = "document_reference"
            if node.tag == "img" and resources.get("images") == "link":
                kind = "image"
            if kind:
                if (
                    kind not in {"native", "image", "document_reference"}
                    and resources.get(kind) != "link"
                ):
                    raise Unsupported("unsupported_native_projection:" + kind)
                if kind == "native" and node.get("type") not in {"isv", "diagram"}:
                    raise Unsupported("unknown_readonly_component:" + str(node.get("type")))
                anchor = node.get("id")
                if not anchor and kind != "document_reference":
                    raise Unsupported("native_resource_missing_block_id")
                source = next(node.iter("source"), None)
                label = source.get("name") if source is not None else node.get("name")
                label = label or "Open Feishu " + (node.get("type") or kind)
                anchor = anchor or parent.get("id")
                href = url + ("#" + anchor if anchor else "")
                if kind == "document_reference":
                    identifier = node.get("doc-id")
                    native_type = node.get("file-type")
                    paths = {
                        "wiki": "wiki",
                        "docx": "docx",
                        "sheets": "sheets",
                        "sheet": "sheets",
                        "bitable": "base",
                        "file": "file",
                    }
                    if identifier and native_type in paths:
                        parsed = urlsplit(url)
                        href = (
                            f"{parsed.scheme}://{parsed.netloc}/{paths[native_type]}/{identifier}"
                        )
                    label = (
                        node.get("title")
                        or "".join(node.itertext()).strip()
                        or "Open linked Feishu document"
                    )
                link = E.Element("a", {"href": href})
                link.text = label
                replacement = link
                if parent.tag not in {"p", "li", "td", "th", "a"}:
                    replacement = E.Element("p")
                    replacement.append(link)
                replacement.tail = node.tail
                position = list(parent).index(node)
                parent.remove(node)
                parent.insert(position, replacement)
            else:
                walk(node)

    walk(root)
    return xmlstr(list(root))


def resource_note(title: str, url: str, kind: str) -> bytes:
    """A resource entry is explicitly a projection, not an editable replica."""
    return (
        f"# {title}\n\n[Open in Feishu](<{url}>)\n\n"
        f"Resource type: `{kind}`. The native resource remains in Feishu.\n"
    ).encode()
