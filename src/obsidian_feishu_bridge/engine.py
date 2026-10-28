"""Peer synchronizer: independent baselines, per-note isolation, durable writes."""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
import xml.etree.ElementTree as E
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .codec import (
    PARSER,
    Unsupported,
    docroot,
    frontmatter,
    hash_bytes,
    render,
    semantic,
    to_markdown,
)
from .runtime import APIError, Lark, Store, atomic, dump, guarded_local_write, lock, now


class SyncEngine:
    def __init__(self, root, cfg, api=None):
        self.root = Path(root)
        self.cfg = cfg
        self.vault = Path(cfg["vault_root"]).resolve()
        self.store = Store(self.root / cfg.get("state_db", "state/sync.sqlite"))
        self.api = api or Lark(cfg, root, False)
        self.media_hashes = self.store.setting("media_hashes", {})
        self.pending_images = {}
        self.report = {
            "at": now(),
            "synced": [],
            "paused": [],
            "deferred": [],
            "unchanged": 0,
            "reads": 0,
            "writes": 0,
            "llm_calls": 0,
            "pdf_downloads": 0,
        }

    def local_paths(self):
        excluded = set(self.cfg.get("exclude", []))
        for space in self.cfg["spaces"]:
            for path in sorted((self.vault / space["name"]).rglob("*.md")):
                relative = path.relative_to(self.vault).as_posix()
                if not any(part in excluded for part in Path(relative).parts):
                    yield space, path, relative

    def prepare_new_peers(self):
        pass

    def resource_changed(self, pair):
        return False

    def render_baseline(self, text, note):
        return render(text, self.resolver(note))

    def representations_equal(self, pair, local, cloud):
        return self.same(
            "".join(render(local.decode("utf-8-sig"), self.resolver(pair["path"]))),
            cloud["content"],
        )

    def asset_snapshot(self, note):
        return None

    def commit_pair(self, pair, local, remote, meta=None, asset_snapshot=None):
        self.store.save_pair(pair, local, remote, meta)

    def safe_path(self, relative):
        path = (self.vault / relative).resolve()
        if not path.is_relative_to(self.vault) or path.suffix.lower() != ".md":
            raise Unsupported("path_outside_vault")
        return path

    def pause(self, path, reason, local=None, remote=None):
        if any(x["path"] == path for x in self.report["paused"]):
            return
        folder = self.root / "state/conflicts" / hash_bytes(path.encode())[:20]
        folder.mkdir(parents=True, exist_ok=True)
        if local is not None:
            atomic(folder / "local.md", local)
        if remote is not None:
            atomic(folder / "cloud.json", dump(remote).encode())
        atomic(folder / "issue.json", dump({"path": path, "reason": reason, "at": now()}).encode())
        self.report["paused"].append({"path": path, "reason": reason, "evidence": str(folder)})

    def resolver(self, note):
        def resolve(target, embed):
            base, _, heading = unquote(target).partition("#")
            candidates = (
                [self.vault / base, self.safe_path(note).parent / base]
                if base
                else [self.safe_path(note)]
            )
            files = []
            if base in self.pending_images:
                data = self.pending_images[base]
                staged = self.root / "state/assets" / (hash_bytes(data) + Path(base).suffix)
                if not staged.exists():
                    atomic(staged, data)
                return "image", "@./" + staged.relative_to(self.root).as_posix()
            for p in candidates:
                for q in [p, Path(str(p) + ".md")]:
                    q = q.resolve()
                    if q.is_relative_to(self.vault) and q.is_file() and q not in files:
                        files.append(q)
            if not files:
                raise Unsupported("missing_local_reference:" + target)
            p = files[0]
            if embed:
                if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    raise Unsupported("not_a_local_image:" + target)
                data = p.read_bytes()
                name = hash_bytes(data) + p.suffix.lower()
                staged = self.root / "state/assets" / name
                if not staged.exists():
                    atomic(staged, data)
                return "image", "@./" + staged.relative_to(self.root).as_posix()
            row = self.store.db.execute(
                "SELECT doc,node FROM pairs WHERE path=?", (p.relative_to(self.vault).as_posix(),)
            ).fetchone()
            if not row or not row["node"]:
                raise Unsupported("unpaired_link:" + target)
            if heading:
                raise Unsupported("heading_link_requires_block_mapping:" + target)
            return "link", "https://my.feishu.cn/wiki/" + row["node"]

        return resolve

    def fetch(self, token):
        self.report["reads"] += 1
        return self.api.fetch(token)

    def download_image(self, token):
        if not token:
            raise Unsupported("image_without_token")
        folder = self.root / "state/downloads"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (hash_bytes(token.encode())[:24] + ".image")
        if not dest.exists():
            self.api.call(
                [
                    "docs",
                    "+media-download",
                    "--token",
                    token,
                    "--output",
                    dest.relative_to(self.root).as_posix(),
                ]
            )
        if not dest.exists():
            raise Unsupported("image_download_missing")
        data = dest.read_bytes()
        self.media_hashes[token] = hash_bytes(data)
        self.store.set("media_hashes", self.media_hashes)
        return data

    def verify_new_images(self, before, after):
        previous = {n.get("src") for n in docroot(before).iter("img")}
        for n in docroot(after).iter("img"):
            if n.get("src") in previous:
                continue
            match = re.match(r"([a-f0-9]{64})\.", n.get("name", ""))
            data = self.download_image(n.get("src"))
            if match and hash_bytes(data) != match[1]:
                raise Unsupported("image_byte_hash_mismatch")

    def image_map(self, xml):
        result = {}
        for n in docroot(xml).iter("img"):
            path = n.get("path")
            if path:
                p = (self.root / path.removeprefix("@./")).resolve()
                if not p.is_relative_to(self.root.resolve()):
                    raise Unsupported("staged_image_outside_package")
                result[path] = hash_bytes(p.read_bytes())
            else:
                m = re.match(r"([a-f0-9]{64})\.", n.get("name", ""))
                if m:
                    result[n.get("src")] = m[1]
                elif n.get("src") in self.media_hashes:
                    result[n.get("src")] = self.media_hashes[n.get("src")]
        return result

    def sem(self, xml):
        return semantic(xml, self.image_map(xml))

    def same(self, a, b):
        return self.sem(a) == self.sem(b)

    def close(self):
        self.store.db.close()

    def discover(self, force=False):
        if not force and time.time() - self.store.setting("discovered_at", 0) < self.cfg.get(
            "discovery_seconds", 3600
        ):
            return
        inventory = []
        frontier = [(x["space_id"], x.get("root_node", ""), x["name"]) for x in self.cfg["spaces"]]
        seen = set()
        while frontier:
            space, parent, path = frontier.pop()
            if (space, parent) in seen:
                raise Unsupported("wiki_cycle")
            seen.add((space, parent))
            for n in self.api.children(space, parent):
                if n["node_token"] in self.cfg.get("excluded_nodes", []):
                    continue
                n = dict(n, space_id=space, full_path=path + "/" + n.get("title", "无标题"))
                inventory.append(n)
                if n.get("has_child"):
                    frontier.append((space, n["node_token"], n["full_path"]))
        # Absence is a candidate, never permission to delete a note.
        live = {x["obj_token"] for x in inventory}
        for p in self.store.pairs():
            if p["doc"] and p["doc"] not in live:
                self.pause(p["path"], "remote_missing_review_no_delete")
        for n in inventory:
            obj = self.store.db.execute(
                "SELECT * FROM objects WHERE token=?", (n["obj_token"],)
            ).fetchone()
            if obj:
                self.store.db.execute(
                    "UPDATE objects SET parent=?,title=? WHERE token=?",
                    (n.get("parent_node_token", ""), n.get("title", ""), n["obj_token"]),
                )
                continue
            if n["obj_type"] != "docx":
                self.store.db.execute(
                    "INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?,?,?)",
                    (
                        n["obj_token"],
                        n["node_token"],
                        n["obj_type"],
                        n["space_id"],
                        n.get("parent_node_token", ""),
                        n.get("title", ""),
                        "cloud_only",
                    ),
                )
                continue
            path = n["full_path"] + ".md"
            # Windows forbidden characters/path collisions are explicit paused items.
            if any(re.search(r'[<>:"|?*]', part) for part in Path(path).parts):
                self.pause(path, "windows_path_requires_name_mapping")
                continue
            if self.safe_path(path).exists():
                self.pause(path, "unpaired_cloud_local_name_collision")
                continue
            p = {
                "id": str(uuid.uuid4()),
                "path": path,
                "doc": n["obj_token"],
                "node": n["node_token"],
                "space": n["space_id"],
                "parent": n.get("parent_node_token", ""),
            }
            self.store.save_pair(p, None, None)
        self.store.db.commit()
        self.store.set("discovered_at", time.time())

    def mark_pending(self, doc, signature):
        pending = self.store.setting("pending_cloud", {})
        pending[doc] = signature
        self.store.set("pending_cloud", pending)

    def clear_pending(self, doc):
        pending = self.store.setting("pending_cloud", {})
        pending.pop(doc, None)
        self.store.set("pending_cloud", pending)

    def markdown_pull(self, p, doc):
        def link(url):
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower()
            if not (
                hostname in {"feishu.cn", "larksuite.com"}
                or hostname.endswith((".feishu.cn", ".larksuite.com"))
            ):
                return url
            if not re.match(r"^/(wiki|docx)/[^/]+/?$", parsed.path):
                return url
            token = urlsplit(url).path.rstrip("/").split("/")[-1]
            row = self.store.db.execute(
                "SELECT path FROM pairs WHERE node=? OR doc=?", (token, token)
            ).fetchone()
            if row and not urlsplit(url).fragment:
                return row["path"]
            return url

        # Only media declared as images is downloaded. PDFs are never passed here.
        def image(n):
            token = n.get("src")
            data = self.download_image(token)
            ext = (
                ".png"
                if data.startswith(b"\x89PNG")
                else ".jpg"
                if data.startswith(b"\xff\xd8")
                else ".webp"
                if data[8:12] == b"WEBP"
                else None
            )
            if not ext:
                raise Unsupported("unknown_image_encoding")
            local = self.vault / "_attachments/feishu" / (hash_bytes(data) + ext)
            self.media_hashes[token] = hash_bytes(data)
            self.store.set("media_hashes", self.media_hashes)
            self.pending_images[local.relative_to(self.vault).as_posix()] = data
            return local.relative_to(self.vault).as_posix()

        return to_markdown(doc["content"], link, image)

    def pull_data(self, p, local, base, cloud):
        if local is None or base is None:
            body = self.markdown_pull(p, cloud)
            data = body.encode("utf-8")
            if not self.same("".join(render(body, self.resolver(p["path"]))), cloud["content"]):
                raise Unsupported("cloud_projection_not_lossless")
            return data
        # Preserve endpoint-specific presentation (e.g. removed source links).
        # Apply only changed cloud blocks to the corresponding local source spans.
        text = local.decode("utf-8-sig")
        head, body = frontmatter(text)
        lines = body.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        records = []
        mk = []
        mrecord = []
        for token in PARSER.parse(body):
            if token.level != 0 or token.map is None or token.type.endswith("_close"):
                continue
            start, end = token.map
            raw = "".join(lines[start:end])
            blocks = render(raw, self.resolver(p["path"]))
            record = {"start": offsets[start], "end": offsets[end]}
            records.append(record)
            for block in blocks:
                key = dump(self.sem(block))
                mk.append(key)
                mrecord.append(len(records) - 1)
        old = [
            E.tostring(n, encoding="unicode")
            for n in docroot(base["content"])
            if n.tag != "title" and self.sem(E.tostring(n, encoding="unicode"))
        ]
        new = [
            E.tostring(n, encoding="unicode")
            for n in docroot(cloud["content"])
            if n.tag != "title" and self.sem(E.tostring(n, encoding="unicode"))
        ]
        ok = [dump(self.sem(x)) for x in old]
        nk = [dump(self.sem(x)) for x in new]
        mapping = {}
        for a, b, size in difflib.SequenceMatcher(
            None, mk, ok, autojunk=False
        ).get_matching_blocks():
            for i in range(size):
                mapping[b + i] = a + i
        edits = []
        for tag, a, b, c, d in difflib.SequenceMatcher(None, ok, nk, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            if tag == "delete":
                raise Unsupported("cloud_body_deletion_requires_review")
            changed = "".join(new[c:d])
            projected = self.markdown_pull(p, dict(cloud, content=changed))
            if not self.same("".join(render(projected, self.resolver(p["path"]))), changed):
                raise Unsupported("changed_cloud_fragment_not_lossless")
            if a == b:
                if a == len(ok):
                    start = end = len(body)
                elif a == 0:
                    start = end = 0
                elif a - 1 in mapping and ok.count(ok[a - 1]) == 1:
                    start = end = records[mrecord[mapping[a - 1]]]["end"]
                else:
                    raise Unsupported("cloud_insert_anchor_not_uniquely_mapped")
                projected = "\n\n" + projected + "\n\n"
            else:
                if b - a != 1 or a not in mapping or ok.count(ok[a]) != 1:
                    raise Unsupported("cloud_change_not_uniquely_mapped")
                mi = mapping[a]
                ri = mrecord[mi]
                if mrecord.count(ri) != 1:
                    raise Unsupported("cloud_change_spans_multiple_local_blocks")
                start, end = records[ri]["start"], records[ri]["end"]
                projected += "\n\n"
            edits.append((start, end, projected))
        # Reject overlapping source patches; apply in reverse to preserve offsets.
        ordered = sorted(edits)
        if any(x[1] > y[0] for x, y in zip(ordered, ordered[1:])):
            raise Unsupported("overlapping_cloud_source_spans")
        for start, end, value in reversed(ordered):
            body = body[:start] + value + body[end:]
        return (head + body).encode("utf-8")

    def replace_local(self, p, data, expected, doc, meta):
        path = self.safe_path(p["path"])
        actual = path.read_bytes() if path.exists() else None
        if actual != expected:
            raise Unsupported("local_changed_before_write")
        latest = self.fetch(p["doc"])
        if latest["revision_id"] != doc["revision_id"] or not self.same(
            latest["content"], doc["content"]
        ):
            raise Unsupported("remote_changed_before_local_write")
        opid = hash_bytes((p["id"] + hash_bytes(data)).encode())
        payload = {
            "path": p["path"],
            "before_hash": hash_bytes(expected) if expected is not None else None,
            "after_hash": hash_bytes(data),
            "asset_snapshot": self.asset_snapshot(p["path"]),
        }
        folder = self.root / "state/operations" / opid
        folder.mkdir(parents=True, exist_ok=True)
        if actual is not None:
            atomic(folder / "before.md", actual)
        atomic(folder / "after.md", data)
        atomic(folder / "cloud.json", dump(doc).encode())
        self.store.op(opid, p["id"], "pull", "prepared", payload)
        # Hold an exclusive Windows file handle while checking bytes; os.replace
        # is atomic, and replacement requires the editor to release its handle.
        if path.exists() and path.read_bytes() != expected:
            raise Unsupported("local_changed_during_pull")
        for relative, image in self.pending_images.items():
            asset = self.vault / relative
            if not asset.exists():
                atomic(asset, image)
            elif asset.read_bytes() != image:
                raise Unsupported("image_content_address_collision")
        guarded_local_write(path, data, expected)
        if path.read_bytes() != data:
            raise Unsupported("local_readback_failed")
        self.report["writes"] += 1
        self.commit_pair(p, data, doc, meta, payload.get("asset_snapshot"))
        self.store.op(opid, p["id"], "pull", "committed", payload)
        self.clear_pending(p["doc"])

    def patch_plan(self, p, local, cloud):
        old = p["local"].decode("utf-8-sig")
        new = local.decode("utf-8-sig")
        # A pipe-prefixed paragraph without a delimiter row is a Markdown
        # paragraph. Preserve it exactly as such; never guess a table/header.
        base = self.render_baseline(old, p["path"])
        target = render(new, self.resolver(p["path"]))

        def keys(blocks):
            return [dump(self.sem(x)) for x in blocks]

        bk, nk = keys(base), keys(target)
        remote_nodes = list(docroot(cloud["content"]))
        rblocks = [
            E.tostring(n, encoding="unicode")
            for n in remote_nodes
            if n.tag != "title" and self.sem(E.tostring(n, encoding="unicode"))
        ]
        rk = keys(rblocks)
        mapping = {}
        for a, b, n in difflib.SequenceMatcher(None, bk, rk, autojunk=False).get_matching_blocks():
            for i in range(n):
                mapping[a + i] = b + i
        operations = []
        for tag, a, b, c, d in difflib.SequenceMatcher(None, bk, nk, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            if tag == "delete":
                raise Unsupported("body_deletion_requires_review")
            if a == b:
                if a == len(bk):
                    idx = len(rblocks)
                    anchor = E.fromstring(rblocks[-1]).get("id") if rblocks else "0"
                elif a == 0:
                    idx = 0
                    anchor = "0"
                elif a - 1 in mapping:
                    idx = mapping[a - 1] + 1
                    anchor = E.fromstring(rblocks[idx - 1]).get("id")
                else:
                    raise Unsupported("insert_anchor_ambiguous")
                command = "block_insert_after"
                block = anchor
            else:
                if b - a != 1 or a not in mapping or bk.count(bk[a]) != 1:
                    raise Unsupported("changed_block_not_uniquely_mapped")
                idx = mapping[a]
                n = E.fromstring(rblocks[idx])
                if n.tag.startswith("h") and n.tag != "hr":
                    raise Unsupported("heading_replacement_would_change_anchor")
                replacing_image = (
                    n.tag == "img" and d - c == 1 and E.fromstring(target[c]).tag == "img"
                )
                if not replacing_image and any(
                    x.tag in ("img", "whiteboard", "source", "sheet", "cite", "readonly-block")
                    for x in n.iter()
                ):
                    raise Unsupported("protected_native_block")
                command = "block_replace"
                block = n.get("id")
            if not block:
                raise Unsupported("missing_patch_block_id")
            operations.append(
                {
                    "command": command,
                    "block": block,
                    "content": "".join(target[c:d]),
                    "index": idx,
                    "remove": 0 if a == b else 1,
                    "new": target[c:d],
                }
            )
        working = list(rblocks)
        operations.sort(key=lambda op: op["index"], reverse=True)
        bounded_operations = []
        for op in operations:
            idx = op.pop("index")
            remove = op.pop("remove")
            new = op.pop("new")
            chunks = []
            chunk = []
            length = 0
            for block in new:
                if chunk and (length + len(block) > 14000 or len(chunk) >= 40):
                    chunks.append(chunk)
                    chunk = []
                    length = 0
                chunk.append(block)
                length += len(block)
            if chunk:
                chunks.append(chunk)
            previous = E.fromstring(working[idx - 1]).get("id") if idx else "0"
            for i, part in enumerate(reversed(chunks)):
                step = dict(op, content="".join(part))
                if i:
                    step["command"] = "block_insert_after"
                    step["block"] = previous
                working[idx : idx + (remove if i == 0 else 0)] = part
                step["expected_xml"] = "".join(working)
                bounded_operations.append(step)
        return bounded_operations

    def execute_patch(self, p, local, cloud, patch, meta):
        return self.execute_patches(p, local, cloud, [patch], meta)

    def preserve_version_gap(self, p, opid, payload, before, after):
        payload["version_gap"] = [before, after]
        self.store.op(opid, p["id"], "batch_patch", "concurrent_version_gap", payload)
        folder = self.root / "state/operations" / opid
        versions = []
        snapshots = []
        revisions = sorted(
            set(range(before + 1, min(after, before + 21)))
            | ({after - 1} if after > before + 1 else set())
        )
        for revision in revisions:
            cached = folder / ("intervening-revision-" + str(revision) + ".json")
            historical = (
                json.loads(cached.read_text(encoding="utf-8"))
                if cached.exists()
                else self.api.fetch_revision(p["doc"], revision)
            )
            atomic(
                folder / ("intervening-revision-" + str(revision) + ".json"),
                dump(historical).encode(),
            )
            versions.append(revision)
            snapshots.append(historical)
        atomic(
            folder / "version_gap.json",
            dump(
                {
                    "before": before,
                    "after": after,
                    "saved": versions,
                    "baseline_not_committed": True,
                }
            ).encode(),
        )
        expected = payload.get("dispatch_expected")
        if expected and len(versions) == after - before - 1:
            wanted = self.sem(expected)
            old_images = {
                x[1] for x in self.sem(payload["before"]["content"]) if x and x[0] == "img"
            }

            def compatible(current, planned):
                if current == planned:
                    return True
                if (
                    current == ["img", None]
                    and isinstance(planned, list)
                    and len(planned) == 2
                    and planned[0] == "img"
                ):
                    return (
                        isinstance(planned[1], str)
                        and bool(re.fullmatch(r"[a-f0-9]{64}", planned[1]))
                        and planned[1] not in old_images
                    )
                return (
                    isinstance(current, list)
                    and isinstance(planned, list)
                    and len(current) == len(planned)
                    and all(compatible(a, b) for a, b in zip(current, planned))
                )

            # The CLI uploads images in a second revision. Every intervening
            # state must exactly equal the planned tree except new image slots.
            if snapshots and all(compatible(self.sem(d["content"]), wanted) for d in snapshots):
                payload.pop("version_gap", None)
                payload["verified_image_binding_revisions"] = versions
                self.store.op(opid, p["id"], "batch_patch", "dispatching", payload)
                return
        raise Unsupported("concurrent_version_gap_intervening_versions_preserved")

    def execute_patches(self, p, local, cloud, patches, meta, opid=None, payload=None):
        opid = opid or hash_bytes(
            (p["id"] + str(cloud["revision_id"]) + hash_bytes(local) + dump(patches)).encode()
        )
        payload = payload or {
            "path": p["path"],
            "doc": p["doc"],
            "before": cloud,
            "local_hash": hash_bytes(local),
            "steps": patches,
            "done": 0,
        }
        folder = self.root / "state/operations" / opid
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "local.md").exists():
            atomic(folder / "local.md", local)
            atomic(folder / "before.json", dump(cloud).encode())
        latest = self.fetch(p["doc"])
        if self.safe_path(p["path"]).read_bytes() != local:
            raise Unsupported("local_changed_before_upload")
        if payload.get("dispatch_expected"):
            if not self.same(latest["content"], payload["dispatch_expected"]):
                raise Unsupported("uncertain_batch_write_no_replay")
            before = payload.get("before_next", cloud)["revision_id"]
            if latest["revision_id"] != before + 1:
                self.preserve_version_gap(p, opid, payload, before, latest["revision_id"])
            self.verify_new_images(cloud["content"], latest["content"])
            if latest.get("reference_map", {}).get("comments", {}) != cloud.get(
                "reference_map", {}
            ).get("comments", {}):
                raise Unsupported("comment_sidecar_changed")
            payload["done"] += 1
            payload.pop("dispatch_expected")
            payload["before_next"] = latest
        expected_before = payload.get("before_next", cloud)
        if latest["revision_id"] != expected_before["revision_id"] or not self.same(
            latest["content"], expected_before["content"]
        ):
            raise Unsupported("remote_revision_or_content_changed")
        self.store.op(opid, p["id"], "batch_patch", "prepared", payload)
        after = latest
        for patch in payload["steps"][payload["done"] :]:
            if self.safe_path(p["path"]).read_bytes() != local:
                raise Unsupported("local_changed_between_patches")
            payload["dispatch_expected"] = patch["expected_xml"]
            self.store.op(opid, p["id"], "batch_patch", "dispatching", payload)
            before_revision = after["revision_id"]
            self.api.update(
                p["doc"], before_revision, patch["command"], patch["content"], patch["block"]
            )
            self.report["writes"] += 1
            after = self.fetch(p["doc"])
            if after["revision_id"] != before_revision + 1:
                self.preserve_version_gap(p, opid, payload, before_revision, after["revision_id"])
            self.verify_new_images(cloud["content"], after["content"])
            if not self.same(patch["expected_xml"], after["content"]):
                raise Unsupported("complete_structure_readback_mismatch")
            if after.get("reference_map", {}).get("comments", {}) != cloud.get(
                "reference_map", {}
            ).get("comments", {}):
                raise Unsupported("comment_sidecar_changed")
            payload["done"] += 1
            payload.pop("dispatch_expected")
            payload["before_next"] = after
            self.store.op(opid, p["id"], "batch_patch", "verified_step", payload)
        # Re-read source; later edits remain pending against the uploaded snapshot.
        self.commit_pair(p, local, after, meta, payload.get("asset_snapshot"))
        self.clear_pending(p["doc"])
        self.store.op(opid, p["id"], "batch_patch", "committed", payload)

    def create_pair(self, p, local, xml, meta=None):
        opid = hash_bytes(("create:" + p["id"]).encode())
        title = Path(p["path"]).stem
        marker = "【sync-" + p["id"][:12] + "】"
        row = self.store.db.execute("SELECT * FROM operations WHERE id=?", (opid,)).fetchone()
        payload = (
            json.loads(row["payload"])
            if row
            else {
                "path": p["path"],
                "space": p["space"],
                "parent": p.get("parent", ""),
                "title": title,
                "marker": marker,
                "local_hash": hash_bytes(local),
                "xml": xml,
            }
        )
        if payload["local_hash"] != hash_bytes(local):
            raise Unsupported("creation_source_changed_pending_recovery")
        if not payload.get("doc"):
            children = self.api.children(p["space"], p.get("parent", ""))
            matches = [n for n in children if n.get("title") == marker]
            if len(matches) > 1:
                raise Unsupported("duplicate_operation_markers")
            if not matches:
                self.store.op(opid, p["id"], "create", "creating", payload)
                n = self.api.create(p["space"], p.get("parent", ""), marker)
                self.report["writes"] += 1
            else:
                n = matches[0]
            payload.update(doc=n["obj_token"], node=n["node_token"])
            self.store.op(opid, p["id"], "create", "created", payload)
        p = dict(p, doc=payload["doc"], node=payload["node"])
        doc = self.fetch(p["doc"])
        chunks = []
        chunk = ""
        for n in docroot(xml):
            value = E.tostring(n, encoding="unicode")
            if chunk and len(chunk) + len(value) > 14000:
                chunks.append(chunk)
                chunk = ""
            chunk += value
        if chunk:
            chunks.append(chunk)
        done = payload.get("chunks_done", 0)
        if payload.get("dispatch_expected"):
            if self.same(doc["content"], payload["dispatch_expected"]):
                done += 1
                payload["chunks_done"] = done
                payload.pop("dispatch_expected")
                self.store.op(opid, p["id"], "create", "recovering", payload)
            else:
                raise Unsupported("uncertain_create_write_inspect_no_retry")
        for chunk in chunks[done:]:
            expected = doc["content"] + chunk
            payload["dispatch_expected"] = expected
            self.store.op(opid, p["id"], "create", "dispatching", payload)
            self.api.update(p["doc"], doc["revision_id"], "block_insert_after", chunk, "-1")
            self.report["writes"] += 1
            after = self.fetch(p["doc"])
            self.verify_new_images(doc["content"], after["content"])
            if not self.same(expected, after["content"]):
                raise Unsupported("new_note_full_structure_mismatch")
            doc = after
            payload["chunks_done"] = payload.get("chunks_done", 0) + 1
            payload.pop("dispatch_expected")
            self.store.op(opid, p["id"], "create", "content_verified", payload)
        if not self.same(xml, doc["content"]):
            raise Unsupported("new_note_final_structure_mismatch")
        self.api.rename(p["node"], title)
        self.report["writes"] += 1
        doc = self.fetch(p["doc"])
        self.store.save_pair(p, local, doc, meta)
        self.store.op(opid, p["id"], "create", "committed", payload)
        return p

    def recover(self):
        for op in self.store.db.execute(
            "SELECT * FROM operations WHERE kind='pull' AND status!='committed'"
        ).fetchall():
            payload = json.loads(op["payload"])
            p = next((x for x in self.store.pairs() if x["id"] == op["pair_id"]), None)
            if not p:
                continue
            path = self.safe_path(p["path"])
            data = path.read_bytes() if path.exists() else None
            if data is not None and hash_bytes(data) == payload["after_hash"]:
                remote = json.loads(
                    (self.root / "state/operations" / op["id"] / "cloud.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.commit_pair(p, data, remote, asset_snapshot=payload.get("asset_snapshot"))
                self.store.op(op["id"], p["id"], "pull", "committed", payload)
            elif (hash_bytes(data) if data is not None else None) == payload["before_hash"]:
                self.store.op(
                    op["id"],
                    p["id"],
                    "pull",
                    "committed",
                    payload | {"recovered_as": "no_local_write_occurred"},
                )
            else:
                self.pause(
                    p["path"],
                    "interrupted_local_write_or_new_user_edit_preserve_both",
                    data,
                    self.fetch(p["doc"]),
                )
        for op in self.store.db.execute(
            "SELECT * FROM operations WHERE kind='batch_patch' AND status!='committed'"
        ).fetchall():
            payload = json.loads(op["payload"])
            p = next((x for x in self.store.pairs() if x["id"] == op["pair_id"]), None)
            if not p:
                continue
            if payload.get("version_gap"):
                try:
                    self.preserve_version_gap(p, op["id"], payload, *payload["version_gap"])
                except Unsupported:
                    self.pause(
                        p["path"],
                        "concurrent_version_gap_requires_review",
                        self.safe_path(p["path"]).read_bytes(),
                        self.fetch(p["doc"]),
                    )
                    continue
            try:
                local = (self.root / "state/operations" / op["id"] / "local.md").read_bytes()
                self.execute_patches(
                    p, local, payload["before"], payload["steps"], None, op["id"], payload
                )
            except (Unsupported, APIError, OSError) as error:
                self.pause(p["path"], str(error), local, self.fetch(p["doc"]))
        for op in self.store.db.execute(
            "SELECT * FROM operations WHERE status='dispatching' AND kind='patch'"
        ).fetchall():
            payload = json.loads(op["payload"])
            p = next((x for x in self.store.pairs() if x["id"] == op["pair_id"]), None)
            if not p:
                continue
            doc = self.fetch(payload["doc"])
            if self.same(doc["content"], payload["patch"]["expected_xml"]) and doc.get(
                "reference_map", {}
            ).get("comments", {}) == payload["before"].get("reference_map", {}).get("comments", {}):
                local = (self.root / "state/operations" / op["id"] / "local.md").read_bytes()
                self.store.save_pair(p, local, doc)
                self.store.op(op["id"], p["id"], "patch", "committed", payload)
            else:
                self.pause(
                    p["path"],
                    "uncertain_operation_no_automatic_replay",
                    self.safe_path(p["path"]).read_bytes(),
                    doc,
                )

    def ensure_dir(self, path, space):
        row = self.store.db.execute("SELECT * FROM dirs WHERE path=?", (path,)).fetchone()
        if row:
            return row["node"]
        parent = Path(path).parent.as_posix()
        if parent == path:
            raise Unsupported("topic_root_not_registered")
        parent_node = self.ensure_dir(parent, space)
        if not self.apply_enabled:
            raise Unsupported("new_directory_planned_no_write")
        ident = hash_bytes(("dir:" + space + ":" + path).encode())
        marker = "【sync-dir-" + ident[:10] + "】"
        op = self.store.db.execute("SELECT * FROM operations WHERE id=?", (ident,)).fetchone()
        payload = (
            json.loads(op["payload"])
            if op
            else {"path": path, "parent": parent_node, "marker": marker, "space": space}
        )
        if not payload.get("node"):
            matches = [n for n in self.api.children(space, parent_node) if n.get("title") == marker]
            if len(matches) > 1:
                raise Unsupported("ambiguous_new_directory_operation")
            if op and op["status"] == "creating" and not matches:
                raise Unsupported("uncertain_directory_creation_wait_for_receipt_or_review")
            self.store.op(ident, ident, "directory", "creating", payload)
            n = matches[0] if matches else self.api.create(space, parent_node, marker)
            payload.update(node=n["node_token"], doc=n["obj_token"])
            self.store.op(ident, ident, "directory", "created", payload)
        self.api.rename(payload["node"], Path(path).name)
        self.store.db.execute(
            "INSERT OR REPLACE INTO dirs VALUES(?,?,?)", (path, payload["node"], space)
        )
        self.store.db.execute(
            "INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?,?)",
            (
                payload["doc"],
                payload["node"],
                "docx",
                space,
                parent_node,
                Path(path).name,
                "generated_directory",
            ),
        )
        self.store.db.commit()
        self.store.op(ident, ident, "directory", "committed", payload)
        self.report["writes"] += 2
        return payload["node"]

    def note_id(self, data):
        if data is None:
            return None
        head, _ = frontmatter(data.decode("utf-8-sig"))
        field = self.cfg.get("identity_field", "sync_id")
        match = re.search(r"(?m)^" + re.escape(field) + r':\s*["\']?([a-zA-Z0-9-]+)', head)
        return match[1] if match else None

    def with_id(self, data, identifier):
        text = data.decode("utf-8-sig")
        newline = "\r\n" if "\r\n" in text else "\n"
        field = self.cfg.get("identity_field", "sync_id")
        if self.note_id(data):
            if self.note_id(data) != identifier:
                raise Unsupported("note_identity_mismatch")
            return data
        if text.startswith("---" + newline):
            text = (
                "---" + newline + field + ": " + identifier + newline + text[len("---" + newline) :]
            )
        else:
            text = "---" + newline + field + ": " + identifier + newline + "---" + newline + text
        return (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")

    def maintain_identities(self):
        if not self.apply_enabled:
            return set()
        ignored = {item["path"] for item in self.report["paused"]}
        pairs = self.store.pairs()
        byid = {p["id"]: p for p in pairs}
        found = {}
        for space, path, relative in self.local_paths():
            identifier = self.note_id(path.read_bytes())
            if identifier:
                found.setdefault(identifier, []).append(relative)
        for identifier, paths in found.items():
            if len(paths) > 1:
                for path in paths:
                    ignored.add(path)
                    self.pause(path, "duplicate_sync_identity_preserve_copies")
                continue
            p = byid.get(identifier)
            if p and p["path"] in ignored:
                continue
            if not p or p["path"] == paths[0]:
                continue
            new = paths[0]
            old = self.safe_path(p["path"])
            if old.exists():
                ignored.add(new)
                self.pause(new, "identity_collision_existing_original")
                continue
            if Path(p["path"]).parent != Path(new).parent:
                ignored.add(new)
                self.pause(new, "move_between_directories_requires_review")
                continue
            local = self.safe_path(new).read_bytes()
            if p["doc"]:
                remote = self.fetch(p["doc"])
                base = json.loads(p["remote"]) if p["remote"] else None
                if base and not self.same(remote["content"], base["content"]):
                    ignored.add(new)
                    self.pause(new, "rename_and_cloud_change_review", local, remote)
                    continue
                self.api.rename(p["node"], Path(new).stem)
                self.report["writes"] += 1
            self.store.db.execute("UPDATE pairs SET path=?,meta=NULL WHERE id=?", (new, identifier))
            self.store.db.commit()
            self.report["synced"].append(
                {"path": new, "action": "rename_peer", "previous_path": p["path"]}
            )
        for p in self.store.pairs():
            path = self.safe_path(p["path"])
            if not path.exists() or p["path"] in ignored:
                continue
            current = path.read_bytes()
            if not current.decode("utf-8-sig").strip():
                continue
            try:
                new = self.with_id(current, p["id"])
                if new != current:
                    backup = self.root / "state/identity_backups" / p["id"]
                    if not backup.exists():
                        atomic(backup, current)
                    guarded_local_write(path, new, current)
                    self.report["identity_metadata_writes"] = (
                        self.report.get("identity_metadata_writes", 0) + 1
                    )
                if p["local"] is not None:
                    base = self.with_id(p["local"], p["id"])
                    if base != p["local"]:
                        self.store.db.execute(
                            "UPDATE pairs SET local=? WHERE id=?", (base, p["id"])
                        )
                        self.store.db.commit()
            except (Unsupported, OSError) as error:
                ignored.add(p["path"])
                self.pause(p["path"], str(error), current)
        return ignored

    def register_local(self, ignored=None):
        known = {p["path"] for p in self.store.pairs()}
        for space, path, relative in self.local_paths():
            if relative in (ignored or set()):
                continue
            if relative in known:
                continue
            try:
                parent = self.ensure_dir(Path(relative).parent.as_posix(), space["space_id"])
                pair = {
                    "id": str(uuid.uuid4()),
                    "path": relative,
                    "doc": None,
                    "node": None,
                    "space": space["space_id"],
                    "parent": parent,
                }
                self.store.save_pair(pair, None, None)
            except (Unsupported, APIError, OSError) as error:
                self.pause(relative, str(error), path.read_bytes())

    def check_resources(self):
        """Observe native resources without treating them as replaceable notes."""
        pairs = {p["doc"] for p in self.store.pairs() if p["doc"]}
        dirs = {r[0] for r in self.store.db.execute("SELECT node FROM dirs")}
        objects = [
            dict(r)
            for r in self.store.db.execute("SELECT * FROM objects")
            if r["token"] not in pairs
            and r["node"] not in dirs
            and r["role"] != "generated_directory"
        ]
        requests = [
            {"doc_token": r["token"], "doc_type": r["kind"]}
            for r in objects
            if r["kind"] in ("docx", "file", "sheet")
        ]
        previous = self.store.setting("resource_metadata", {})
        observed = self.api.metas(requests) if requests else {}
        self.report["native_resource_metadata_objects"] = len(observed)
        self.report["native_resource_first_observations"] = len(set(observed) - set(previous))
        pending = self.store.setting("pending_resources", {})
        for token, sig in observed.items():
            if token in previous and sig != previous[token]:
                pending[token] = sig
        self.store.set("resource_metadata", observed)
        self.store.set("pending_resources", pending)
        bytoken = {r["token"]: r for r in objects}
        for token in pending:
            obj = bytoken.get(token, {})
            self.pause(
                "Cloud resources/" + obj.get("title", token),
                "native_resource_change_review_no_plaintext_overwrite",
            )
        for asset in self.store.setting("local_assets", []):
            path = (self.vault / asset["path"]).resolve()
            if not path.is_relative_to(self.vault):
                raise Unsupported("asset_path_outside_vault")
            if not path.exists() or hash_bytes(path.read_bytes()) != asset["sha256"]:
                self.pause(asset["path"], "local_resource_payload_changed_review_preserve_binary")

    def run(self, apply=False, discover=False, full=False):
        self.apply_enabled = apply
        if apply and self.cfg.get("production_writes_enabled") is not True:
            raise Unsupported("production_write_gate_closed")
        self.api.writes = apply
        with lock(self.root / "state/run.lock"):
            if apply:
                self.recover()
            self.discover(discover)
            self.check_resources()
            ignored = self.maintain_identities()
            self.register_local(ignored)
            if apply:
                ignored |= self.maintain_identities()
            self.prepare_new_peers()
            ignored |= {issue["path"] for issue in self.report["paused"]}
            pairs = self.store.pairs()
            objects = [{"doc_token": p["doc"], "doc_type": "docx"} for p in pairs if p["doc"]]
            metas = self.api.metas(objects) if objects else {}
            metadata_failures = dict(getattr(self.api, "meta_failures", {}))
            for p in pairs:
                if p["path"] in ignored:
                    continue
                path = self.safe_path(p["path"])
                local = path.read_bytes() if path.exists() else None
                if p["doc"] in metadata_failures:
                    self.pause(
                        p["path"],
                        "cloud_metadata_unavailable_no_delete:" + str(metadata_failures[p["doc"]]),
                        local,
                    )
                    continue
                signature = metas.get(p["doc"])
                cloud = None
                try:
                    pending_ops = self.store.db.execute(
                        "SELECT status FROM operations WHERE pair_id=? AND kind IN ('patch','batch_patch','pull') AND status!='committed'",
                        (p["id"],),
                    ).fetchone()
                    if pending_ops:
                        raise Unsupported("uncertain_operation_pending_review")
                    if p["local"] is not None and local is None:
                        raise Unsupported("local_missing_or_rename_review_no_delete")
                    if not p["doc"]:
                        if local is None or not local.decode("utf-8-sig").strip():
                            self.report["deferred"].append(
                                {"path": p["path"], "reason": "empty_note"}
                            )
                            continue
                        xml = "".join(render(local.decode("utf-8-sig"), self.resolver(p["path"])))
                        if apply:
                            self.create_pair(p, local, xml)
                        self.report["synced"].append(
                            {
                                "path": p["path"],
                                "action": "create_cloud" if apply else "plan_create_cloud",
                            }
                        )
                        continue
                    local_changed = local != p["local"] or self.resource_changed(p)
                    due = full or time.time() - p["checked"] >= self.cfg.get("sweep_seconds", 86400)
                    if signature != p["meta"]:
                        self.mark_pending(p["doc"], signature)
                    if not (
                        local_changed
                        or due
                        or p["remote"] is None
                        or p["doc"] in self.store.setting("pending_cloud", {})
                    ):
                        self.report["unchanged"] += 1
                        continue
                    cloud = self.fetch(p["doc"])
                    base = json.loads(p["remote"]) if p["remote"] else None
                    remote_changed = not base or not self.same(base["content"], cloud["content"])
                    if base and not remote_changed and not local_changed:
                        self.store.save_pair(p, p["local"], cloud, signature)
                        self.clear_pending(p["doc"])
                        self.report["unchanged"] += 1
                        continue
                    if local_changed and remote_changed and base:
                        if self.representations_equal(p, local, cloud):
                            self.commit_pair(
                                p, local, cloud, signature, self.asset_snapshot(p["path"])
                            )
                            self.clear_pending(p["doc"])
                            self.report["synced"].append(
                                {"path": p["path"], "action": "converged_without_write"}
                            )
                            continue
                        raise Unsupported("both_sides_changed_preserve_both")
                    if remote_changed:
                        if local is not None and p["local"] is None:
                            raise Unsupported("new_cloud_local_collision")
                        # Prove projection roundtrip before touching the user's local file.
                        data = self.pull_data(p, local, base, cloud)
                        if apply:
                            self.replace_local(p, data, local, cloud, signature)
                        self.report["synced"].append(
                            {"path": p["path"], "action": "pull" if apply else "plan_pull"}
                        )
                        continue
                    patches = self.patch_plan(p, local, cloud)
                    if not patches:
                        # Frontmatter/whitespace changes are local-only, no cloud write.
                        self.commit_pair(p, local, cloud, signature, self.asset_snapshot(p["path"]))
                        self.clear_pending(p["doc"])
                        continue
                    if apply:
                        self.execute_patches(p, local, cloud, patches, signature)
                    self.report["synced"].append(
                        {"path": p["path"], "action": "push" if apply else "plan_push"}
                    )
                except (Unsupported, APIError, ValueError, OSError) as error:
                    self.pause(p["path"], str(error), local, cloud)
            self.report["mode"] = "sync" if apply else "check"
            self.report["api_calls"] = len(getattr(self.api, "calls", []))
            self.report["complete"] = not self.report["paused"]
            self.store.event("run", self.report)
            atomic(self.root / "state/latest.json", dump(self.report).encode())
            return self.report
