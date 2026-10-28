"""Project a Wiki document tree into local note locations, not folder mirroring."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from pathlib import PurePosixPath

RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


def component(title: str) -> str:
    title = unicodedata.normalize("NFC", title)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(". ")
    if not title:
        title = "Untitled"
    if title.split(".")[0].upper() in RESERVED:
        title = "_" + title
    if title.casefold() in {".obsidian", ".trash", ".git", ".ofb", ".obsidian-feishu-bridge"}:
        title = "_" + title.lstrip(".")
    return title[:100]


def suffix(token):
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def child_paths(parent: str, children: list[dict]) -> dict[str, dict]:
    counts = Counter(component(n.get("title", "")).casefold() for n in children)
    result = {}
    for n in children:
        name = component(n.get("title", ""))
        if counts[name.casefold()] > 1 or name.casefold() == "_index":
            name += "~" + suffix(n["node_token"])
        stem = str(PurePosixPath(parent) / name)
        path = stem + "/_index.md" if n.get("has_child") else stem + ".md"
        result[n["node_token"]] = {
            "path": path,
            "child_prefix": stem,
            "is_container": bool(n.get("has_child")),
        }
    return result
