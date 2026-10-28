"""Public policies and subtree layout around the stateful synchronization engine."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from .bootstrap import remote_inventory
from .codec import Unsupported, frontmatter, hash_bytes, render
from .engine import SyncEngine
from .projection import project, resource_note
from .runtime import APIError, atomic, guarded_local_write


class Bridge(SyncEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._baseline_render = False
        self._pending_assets = {}
        self._file_hashes = {}
        self._comparison_only = False

    def create_pair(self, pair, local, xml, meta=None):
        parent = pair.get("parent") or self.ensure_dir(
            Path(pair["path"]).parent.as_posix(), pair["space"]
        )
        if not parent:
            raise Unsupported("profile_creation_requires_explicit_parent")
        return super().create_pair(dict(pair, parent=parent), local, xml, meta)

    def local_paths(self):
        ignored = set(self.store.setting("excluded_local_files", []))
        ignored.update(r["path"] for r in self.store.setting("resource_files", {}).values())
        for mount, path, relative in super().local_paths():
            if not path.resolve().is_relative_to(self.vault):
                self.pause(relative, "local_symlink_escapes_vault")
                continue
            runtime_root = self.root.parent if self.root.parent.name == "profiles" else self.root
            if path.resolve().is_relative_to(runtime_root.resolve()):
                continue
            if relative not in ignored:
                yield mount, path, relative

    def cloud_url(self, node):
        return self.cfg.get("cloud_base_url", "https://my.feishu.cn").rstrip("/") + "/wiki/" + node

    def markdown_pull(self, pair, document):
        cloud = dict(
            document,
            content=project(
                document["content"], self.cloud_url(pair["node"]), self.cfg["resources"]
            ),
        )
        return super().markdown_pull(pair, cloud)

    def pull_data(self, pair, local, base, cloud):
        url = self.cloud_url(pair["node"])
        projected_cloud = dict(cloud, content=project(cloud["content"], url, self.cfg["resources"]))
        projected_base = (
            dict(base, content=project(base["content"], url, self.cfg["resources"]))
            if base
            else None
        )
        return super().pull_data(pair, local, projected_base, projected_cloud)

    def discover(self, force=False):
        if not force and time.time() - self.store.setting("discovered_at", 0) < self.cfg.get(
            "discovery_seconds", 3600
        ):
            return
        # Layout planning and paginated discovery share exactly the bootstrap rules.
        rows, dirs = remote_inventory(self.cfg, self.api, fetch_content=False)
        for d in dirs:
            self.store.db.execute(
                "INSERT OR REPLACE INTO dirs VALUES(?,?,?)", (d["path"], d["node"], d["space"])
            )
        resources = self.store.setting("resource_files", {})
        existing_pairs = {p["doc"]: p for p in self.store.pairs() if p["doc"]}
        live = {r["doc"] for r in rows}
        for doc, p in existing_pairs.items():
            if doc not in live:
                self.pause(p["path"], "remote_missing_review_no_delete")
        for row in rows:
            obj = self.store.db.execute(
                "SELECT * FROM objects WHERE token=?", (row["doc"],)
            ).fetchone()
            if obj:
                self.store.db.execute(
                    "UPDATE objects SET parent=?,title=? WHERE token=?",
                    (row["parent"], row["title"], row["doc"]),
                )
                continue
            if row["kind"] != "docx":
                if row["kind"] in {"file", "sheet", "bitable"}:
                    resources[row["doc"]] = {**row, "pending_projection": True}
                else:
                    self.pause(row["path"], "unsupported_native_object:" + row["kind"])
                self.store.db.execute(
                    "INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?,?,?)",
                    (
                        row["doc"],
                        row["node"],
                        row["kind"],
                        row["space"],
                        row["parent"],
                        row["title"],
                        "retained",
                    ),
                )
                continue
            if self.safe_path(row["path"]).exists():
                self.pause(row["path"], "unpaired_cloud_local_name_collision")
                continue
            self.store.save_pair(
                {
                    "id": str(uuid.uuid4()),
                    "path": row["path"],
                    "doc": row["doc"],
                    "node": row["node"],
                    "space": row["space"],
                    "parent": row["parent"],
                },
                None,
                None,
            )
        self.store.db.commit()
        self.store.set("resource_files", resources)
        self.store.set("discovered_at", time.time())

    def check_resources(self):
        resources = self.store.setting("resource_files", {})
        known = {p["doc"] for p in self.store.pairs() if p["doc"]}
        dirs = {r[0] for r in self.store.db.execute("SELECT node FROM dirs")}
        objects = [
            dict(r)
            for r in self.store.db.execute("SELECT * FROM objects")
            if r["token"] not in known and r["node"] not in dirs
        ]
        requests = [
            {"doc_token": r["token"], "doc_type": r["kind"]}
            for r in objects
            if r["kind"] in {"docx", "file", "sheet", "bitable"}
        ]
        observed = self.api.metas(requests) if requests else {}
        metadata_failures = dict(getattr(self.api, "meta_failures", {}))
        previous = self.store.setting("resource_metadata", {})
        self.report["native_resources_observed"] = len(observed)
        self.report["native_resource_changes"] = sum(
            t in previous and s != previous[t] for t, s in observed.items()
        )
        # Link-only resources stay native; their payload is deliberately not mirrored.
        for token, resource in resources.items():
            if token in metadata_failures:
                self.pause(
                    resource["path"],
                    "resource_metadata_unavailable_no_overwrite:" + str(metadata_failures[token]),
                )
                continue
            desired = resource_note(resource["title"], resource["url"], resource["kind"])
            path = self.safe_path(resource["path"])
            previous_bytes = resource.get("projection")
            previous_bytes = previous_bytes.encode() if previous_bytes is not None else None
            current = path.read_bytes() if path.exists() else None
            if current is not None and current != previous_bytes:
                self.report["deferred"].append(
                    {"path": resource["path"], "reason": "resource_note_annotations_preserved"}
                )
                continue
            if current != desired:
                if self.apply_enabled:
                    guarded_local_write(path, desired, current)
                    self.report["writes"] += 1
                    resource["projection"] = desired.decode()
                    resource["pending_projection"] = False
                self.report["synced"].append(
                    {
                        "path": resource["path"],
                        "action": "resource_link" if self.apply_enabled else "plan_resource_link",
                    }
                )
        self.store.set("resource_files", resources)
        self.store.set("resource_metadata", previous | observed)
        for issue in self.store.setting("bootstrap_paused", []):
            self.pause(
                issue.get("local", issue.get("cloud_path", issue.get("remote", "bootstrap"))),
                issue["reason"],
            )

    def resolver(self, note):
        original = super().resolver(note)

        def resolve(target, embed):
            base = unquote(target).partition("#")[0]
            local_candidates = [
                (self.vault / base).resolve(),
                (self.safe_path(note).parent / base).resolve(),
            ]
            source = next(
                (p for p in local_candidates if p.is_file() and p.is_relative_to(self.vault)), None
            )
            if source is not None and source.suffix.lower() != ".md":
                relative = source.relative_to(self.vault).as_posix()
                previous = self.store.setting("asset_baselines", {}).get(note, {}).get(relative)
                if self._baseline_render and previous:
                    if previous["kind"] == "image":
                        cached = (
                            self.root / "state/assets" / (previous["hash"] + source.suffix.lower())
                        )
                        if not cached.exists():
                            raise Unsupported("image_baseline_cache_missing")
                        return "image", "@./" + cached.relative_to(self.root).as_posix()
                    return "link", previous["url"]
                if not embed:
                    hosted = self.host_file(note, source)
                    if not self._baseline_render:
                        uploaded_hash = next(
                            r["source_hash"]
                            for r in self.store.setting("hosted_files", {}).values()
                            if r["url"] == hosted
                        )
                        self._pending_assets.setdefault(note, {})[relative] = {
                            "kind": "file",
                            "hash": uploaded_hash,
                            "url": hosted,
                        }
                    return "link", hosted
            if not embed:
                base = target.partition("#")[0]
                candidates = [
                    (self.vault / base).resolve(),
                    (self.safe_path(note).parent / base).resolve(),
                ]
                resources = self.store.setting("resource_files", {})
                for row in resources.values():
                    if (self.vault / row["path"]).resolve() in candidates:
                        return "link", row["url"]
            kind, url = original(target, embed)
            if embed and kind == "image" and not self._baseline_render:
                staged = self.root / url.removeprefix("@./")
                relative = source.relative_to(self.vault).as_posix() if source else base
                self._pending_assets.setdefault(note, {})[relative] = {
                    "kind": "image",
                    "hash": hash_bytes(staged.read_bytes()),
                }
            if kind == "link" and url.startswith("https://my.feishu.cn/wiki/"):
                url = self.cloud_url(url.rsplit("/", 1)[-1])
            return kind, url

        return resolve

    def resource_changed(self, pair):
        for relative, saved in (
            self.store.setting("asset_baselines", {}).get(pair["path"], {}).items()
        ):
            path = (self.vault / relative).resolve()
            if not path.is_relative_to(self.vault) or not path.exists():
                return True
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            digest = self._file_hashes.setdefault(key, None)
            if digest is None:
                digest = hash_bytes(path.read_bytes())
                self._file_hashes[key] = digest
            if digest != saved["hash"]:
                return True
        return False

    def render_baseline(self, text, note):
        self._baseline_render = True
        try:
            return render(text, self.resolver(note))
        finally:
            self._baseline_render = False

    def representations_equal(self, pair, local, cloud):
        self._comparison_only = True
        try:
            projected = project(
                cloud["content"], self.cloud_url(pair["node"]), self.cfg["resources"]
            )
            return self.same(
                "".join(render(local.decode("utf-8-sig"), self.resolver(pair["path"]))), projected
            )
        finally:
            self._comparison_only = False

    def remember_assets(self, note, snapshot=None):
        baselines = self.store.setting("asset_baselines", {})
        baselines[note] = snapshot if snapshot is not None else self._pending_assets.get(note, {})
        self.store.set("asset_baselines", baselines)

    def asset_snapshot(self, note):
        return self._pending_assets.get(note)

    def commit_pair(self, pair, local, remote, meta=None, asset_snapshot=None):
        settings = {}
        if asset_snapshot is not None:
            baselines = self.store.setting("asset_baselines", {})
            baselines[pair["path"]] = asset_snapshot
            settings["asset_baselines"] = baselines
        self.store.save_pair(pair, local, remote, meta, settings=settings)

    def replace_local(self, pair, data, expected, doc, meta):
        super().replace_local(pair, data, expected, doc, meta)

    def host_file(self, note, source):
        data = source.read_bytes()
        digest = hash_bytes(data)
        mount = next(
            m
            for m in self.cfg["mappings"]
            if not m["local_prefix"] or note.startswith(m["local_prefix"] + "/")
        )
        key = mount["root_node"] + ":" + digest
        hosted = self.store.setting("hosted_files", {})
        if key in hosted:
            return hosted[key]["url"]
        if self._comparison_only:
            raise Unsupported("uncached_attachment_during_readonly_comparison")
        if not self.apply_enabled:
            raise Unsupported(
                "local_file_upload_planned:" + source.relative_to(self.vault).as_posix()
            )
        if self.cfg.get("local_files", "referenced") != "referenced":
            raise Unsupported("local_file_upload_disabled")
        pair = next((p for p in self.store.pairs() if p["path"] == note), None)
        if not pair or not pair["node"]:
            raise Unsupported("attachment_parent_not_allocated")
        ident = "file-" + hash_bytes(key.encode())
        operation = self.store.db.execute(
            "SELECT * FROM operations WHERE id=?", (ident,)
        ).fetchone()
        payload = (
            json.loads(operation["payload"])
            if operation
            else {
                "marker": "[ofb-file-" + uuid.uuid4().hex[:12] + "]" + source.suffix.lower(),
                "parent": pair["node"],
                "source_hash": digest,
            }
        )
        matches = [
            n
            for n in self.api.children(pair["space"], payload["parent"])
            if n.get("title") == payload["marker"]
        ]
        if len(matches) > 1:
            raise Unsupported("ambiguous_attachment_operation")
        if operation and operation["status"] == "creating" and not matches:
            raise Unsupported("uncertain_attachment_creation_wait_for_receipt_or_review")
        staged = self.root / "state/uploads" / (digest + source.suffix.lower())
        if not staged.exists():
            atomic(staged, data)
        self.store.op(ident, pair["id"], "file_upload", "creating", payload)
        if matches:
            token = matches[0]["obj_token"]
        else:
            result = self.api.call(
                [
                    "drive",
                    "+upload",
                    "--file",
                    staged.relative_to(self.root).as_posix(),
                    "--wiki-token",
                    payload["parent"],
                    "--name",
                    payload["marker"],
                ]
            )
            token = result.get("file_token")
            if not token:
                raise Unsupported("upload_receipt_missing_file_token")
            self.report["writes"] += 1
        url = self.cfg.get("cloud_base_url", "https://my.feishu.cn").rstrip("/") + "/file/" + token
        hosted[key] = {"url": url, "token": token, "source_hash": digest}
        self.store.set("hosted_files", hosted)
        self.store.db.execute(
            "INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?,?,?)",
            (
                token,
                None,
                "file",
                pair["space"],
                payload["parent"],
                source.name,
                "uploaded_resource",
            ),
        )
        self.store.db.commit()
        self.store.op(ident, pair["id"], "file_upload", "committed", payload | {"token": token})
        return url

    def prepare_new_peers(self):
        if not self.apply_enabled:
            return
        staged = self.store.setting("staged_new_peers", {})
        by_id = {p["id"]: p for p in self.store.pairs()}
        for identifier, info in list(staged.items()):
            pair = by_id.get(identifier)
            if (
                not pair
                or not pair["local"]
                or not frontmatter(pair["local"].decode("utf-8-sig"))[1].strip()
            ):
                continue
            if not pair["remote"]:
                continue
            current = self.fetch(pair["doc"])
            baseline = json.loads(pair["remote"])
            if not self.same(current["content"], baseline["content"]):
                self.pause(pair["path"], "staged_title_finalization_cloud_changed")
                continue
            self.api.rename(info["node"], Path(pair["path"]).stem)
            staged.pop(identifier)
            self.store.set("staged_new_peers", staged)
            self.report["writes"] += 1
        for pair in self.store.pairs():
            if pair["doc"]:
                continue
            path = self.safe_path(pair["path"])
            if (
                not path.exists()
                or not frontmatter(path.read_text(encoding="utf-8-sig"))[1].strip()
            ):
                continue
            marker = "[ofb-" + pair["id"][:12] + "]"
            try:
                parent = pair["parent"] or self.ensure_dir(
                    Path(pair["path"]).parent.as_posix(), pair["space"]
                )
                matches = [
                    n for n in self.api.children(pair["space"], parent) if n.get("title") == marker
                ]
                if len(matches) > 1:
                    raise Unsupported("duplicate_staged_note_marker")
                ident = "allocate-" + pair["id"]
                previous_op = self.store.db.execute(
                    "SELECT status FROM operations WHERE id=?", (ident,)
                ).fetchone()
                if previous_op and previous_op["status"] == "creating" and not matches:
                    raise Unsupported("uncertain_note_creation_wait_for_receipt_or_review")
                payload = {
                    "marker": marker,
                    "parent": parent,
                    "path": pair["path"],
                    "space": pair["space"],
                }
                self.store.op(ident, pair["id"], "allocate", "creating", payload)
                node = matches[0] if matches else self.api.create(pair["space"], parent, marker)
                doc = self.fetch(node["obj_token"])
                if self.sem(doc["content"]):
                    raise Unsupported("staged_node_not_empty_requires_recovery")
                paired = dict(pair, doc=node["obj_token"], node=node["node_token"], parent=parent)
                self.store.save_pair(paired, b"", doc)
                staged[pair["id"]] = {"node": node["node_token"], "title": Path(pair["path"]).stem}
                self.store.set("staged_new_peers", staged)
                self.store.op(ident, pair["id"], "allocate", "committed", payload)
                self.report["writes"] += int(not matches)
            except (Unsupported, APIError, OSError) as error:
                self.pause(pair["path"], str(error), path.read_bytes())

    def execute_patches(self, pair, local, cloud, patches, meta, opid=None, payload=None):
        if payload is None:
            payload = {
                "path": pair["path"],
                "doc": pair["doc"],
                "before": cloud,
                "local_hash": hash_bytes(local),
                "steps": patches,
                "done": 0,
                "asset_snapshot": self._pending_assets.get(pair["path"], {}).copy(),
            }
        elif "asset_snapshot" not in payload:
            # Upgrade an interrupted pre-release operation only when all source
            # resources still render to exactly the recorded final postcondition.
            current = "".join(render(local.decode("utf-8-sig"), self.resolver(pair["path"])))
            if not self.same(current, patches[-1]["expected_xml"]):
                raise Unsupported("asset_baseline_recovery_needs_review")
            payload["asset_snapshot"] = self._pending_assets.get(pair["path"], {}).copy()
        super().execute_patches(pair, local, cloud, patches, meta, opid, payload)
        staged = self.store.setting("staged_new_peers", {})
        if pair["id"] in staged:
            info = staged[pair["id"]]
            self.api.rename(info["node"], info["title"])
            self.report["writes"] += 1
            staged.pop(pair["id"])
            self.store.set("staged_new_peers", staged)
