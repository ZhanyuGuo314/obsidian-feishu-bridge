"""Explicit initial adoption with read-only matching and a reviewable plan."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .codec import Unsupported, frontmatter, hash_bytes, render, semantic_hash
from .config import save_profile
from .paths import child_paths, suffix
from .projection import project
from .runtime import Store, atomic, dump, lock, now


def resolve_target(api, value):
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.username or parsed.password:
            raise ValueError("target_url_must_not_contain_credentials")
        parts = parsed.path.strip("/").split("/")
        if parsed.scheme != "https" or len(parts) != 2 or parts[0] != "wiki":
            raise ValueError("target_must_be_a_wiki_url_or_node_token")
        token = parts[1]
    else:
        token = value
    if not token or any(c in token for c in "/\\\r\n "):
        raise ValueError("invalid_wiki_node_token")
    data = api.call(["wiki", "+node-get", "--node-token", token])
    node = data.get("node", data)
    if not node.get("space_id") or not node.get("node_token"):
        raise ValueError("target_resolution_incomplete")
    if node.get("node_type", "origin") != "origin":
        raise ValueError("select_an_origin_wiki_root_not_a_shortcut")
    host = (parsed.hostname or "").lower()
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme
        and (
            host.endswith(".feishu.cn")
            or host.endswith(".larksuite.com")
            or host in {"feishu.cn", "larksuite.com"}
        )
        else "https://my.feishu.cn"
    )
    return {
        "space_id": str(node["space_id"]),
        "root_node": node["node_token"],
        "title": node.get("title", ""),
        "cloud_base_url": origin,
    }


def excluded(path, cfg):
    return any(part in cfg.get("exclude", []) for part in Path(path).parts)


def local_inventory(cfg, runtime_root=None):
    vault = Path(cfg["vault_root"])
    rows = []
    for mount in cfg["mappings"]:
        for path in sorted((vault / mount["local_prefix"]).rglob("*.md")):
            if runtime_root and path.resolve().is_relative_to(Path(runtime_root).resolve()):
                continue
            if not path.resolve().is_relative_to(vault.resolve()):
                raise ValueError("local_symlink_escapes_vault")
            relative = path.relative_to(vault).as_posix()
            if excluded(relative, cfg):
                continue
            data = path.read_bytes()
            row = {
                "path": relative,
                "hash": hash_bytes(data),
                "empty": not frontmatter(data.decode("utf-8-sig"))[1].strip(),
                "space_id": mount["space_id"],
                "root_node": mount["root_node"],
            }
            # Unresolved links/resources remain candidates, never justify a guess.
            try:
                row["semantic"] = semantic_hash("".join(render(data.decode("utf-8-sig"))))
            except (Unsupported, ValueError) as error:
                row["semantic"] = None
                row["reason"] = str(error)
            rows.append(row)
    return rows


def remote_inventory(cfg, api, fetch_content=True):
    rows, dirs = [], []
    origin = cfg.get("cloud_base_url", "https://my.feishu.cn").rstrip("/")
    for mount in cfg["mappings"]:
        stack = [(mount["root_node"], mount["local_prefix"])]
        dirs.append(
            {
                "path": mount["local_prefix"] or ".",
                "node": mount["root_node"],
                "space": mount["space_id"],
            }
        )
        visited = set()
        while stack:
            parent, prefix = stack.pop()
            if parent in visited:
                raise ValueError("remote_node_cycle")
            visited.add(parent)
            children = api.children(mount["space_id"], parent)
            paths = child_paths(prefix, children)
            for node in children:
                location = paths[node["node_token"]]
                row = {
                    "node": node["node_token"],
                    "doc": node["obj_token"],
                    "kind": node["obj_type"],
                    "space": mount["space_id"],
                    "parent": parent,
                    "title": node.get("title", "Untitled"),
                    "path": location["path"],
                    "url": origin + "/wiki/" + node["node_token"],
                    "container": location["is_container"],
                }
                if node.get("node_type") == "shortcut":
                    row["kind"] = "shortcut"
                    row["doc"] = row["node"]
                if node.get("has_child"):
                    dirs.append(
                        {
                            "path": location["child_prefix"],
                            "node": row["node"],
                            "space": row["space"],
                        }
                    )
                    stack.append((row["node"], location["child_prefix"]))
                if row["kind"] == "docx" and fetch_content:
                    doc = api.fetch(row["doc"])
                    row["snapshot"] = doc
                    try:
                        projection = project(doc["content"], row["url"], cfg["resources"])
                        row["semantic"] = semantic_hash(projection)
                    except (Unsupported, ValueError) as error:
                        row["semantic"] = None
                        row["reason"] = str(error)
                else:
                    row["semantic"] = None
                rows.append(row)
    return rows, dirs


def make_plan(root, cfg, api):
    root = Path(root)
    runtime_root = root.parent if root.parent.name == "profiles" else root
    local = local_inventory(cfg, runtime_root)
    remote, dirs = remote_inventory(cfg, api)
    by_local_hash, by_remote_hash = defaultdict(list), defaultdict(list)
    for row in local:
        if row.get("semantic") and not row["empty"]:
            by_local_hash[row["semantic"]].append(row)
    for row in remote:
        if row.get("semantic"):
            by_remote_hash[row["semantic"]].append(row)
    matches, matched_local, matched_remote = [], set(), set()
    # Content matching is limited to one local and one cloud instance.
    for digest in by_local_hash.keys() & by_remote_hash.keys():
        left, right = by_local_hash[digest], by_remote_hash[digest]
        if len(left) == len(right) == 1:
            matches.append(
                {
                    "local": left[0]["path"],
                    "remote": right[0]["doc"],
                    "method": "unique_semantic_match",
                }
            )
            matched_local.add(left[0]["path"])
            matched_remote.add(right[0]["doc"])
    unmatched_local = [x for x in local if x["path"] not in matched_local]
    unmatched_remote = [x for x in remote if x["doc"] not in matched_remote]
    path_map = {x["path"].casefold(): x for x in local}
    conflicts = []
    for row in unmatched_remote:
        other = path_map.get(row["path"].casefold())
        if other:
            conflicts.append(
                {
                    "local": other["path"],
                    "remote": row["doc"],
                    "cloud_path": row["path"],
                    "reason": "same_path_without_shared_verified_baseline",
                }
            )
    mode = cfg["bootstrap_mode"]
    actions = []
    actions += [{"action": "pair_equal", **match} for match in matches]
    for row in unmatched_local:
        actions.append(
            {
                "action": "defer_empty"
                if row["empty"]
                else "register_local"
                if mode in {"local", "both"}
                else "preserve_local_outside_cloud_adoption",
                "local": row["path"],
            }
        )
    for row in unmatched_remote:
        actions.append(
            {
                "action": "register_cloud"
                if mode in {"cloud", "both"}
                else "preserve_cloud_outside_local_adoption",
                "remote": row["doc"],
                "path": row["path"],
                "kind": row["kind"],
            }
        )
    plan = {
        "schema": 1,
        "id": str(uuid.uuid4()),
        "at": now(),
        "mode": mode,
        "config_hash": hash_bytes(dump(cfg).encode()),
        "local": local,
        "remote": remote,
        "dirs": dirs,
        "matches": matches,
        "conflicts": conflicts,
        "actions": actions,
        "write_effect": "adopt state only; run sync separately for endpoint writes",
    }
    plan["fingerprint"] = hash_bytes(dump(plan).encode())
    atomic(root / "setup-plan.json", (dump(plan) + "\n").encode())
    return plan


def apply_plan(root, cfg, api, decisions=None):
    """Accept a concrete plan without synchronizing or overwriting content."""
    root = Path(root)
    plan = json.loads((root / "setup-plan.json").read_text(encoding="utf-8"))
    fingerprint = plan.pop("fingerprint")
    if hash_bytes(dump(plan).encode()) != fingerprint:
        raise ValueError("setup_plan_tampered")
    if plan["config_hash"] != hash_bytes(dump(cfg).encode()):
        raise ValueError("setup_config_changed_replan")
    decisions = decisions or {}
    unknown = set(decisions) - {x["remote"] for x in plan["conflicts"]}
    if unknown or any(v not in {"keep_both", "pause"} for v in decisions.values()):
        raise ValueError("invalid_bootstrap_decisions")
    for row in plan["local"]:
        path = Path(cfg["vault_root"]) / row["path"]
        if not path.exists() or hash_bytes(path.read_bytes()) != row["hash"]:
            raise ValueError("local_changed_since_setup_plan:" + row["path"])
    for row in plan["remote"]:
        if row["kind"] == "docx":
            current = api.fetch(row["doc"])
            if current["revision_id"] != row["snapshot"]["revision_id"]:
                raise ValueError("cloud_changed_since_setup_plan:" + row["doc"])
    with lock(root / "state/run.lock"):
        target = root / cfg["state_db"]
        if target.exists():
            existing = Store(target)
            try:
                if existing.pairs() or existing.setting("bootstrap_complete"):
                    raise ValueError("profile_already_bootstrapped_never_reset_baselines")
            finally:
                existing.db.close()
        temporary = target.with_name("bootstrap-" + uuid.uuid4().hex + ".sqlite")
        store = Store(temporary)
        try:
            if store.pairs() or store.setting("bootstrap_complete"):
                raise ValueError("profile_already_bootstrapped_never_reset_baselines")
            local = {r["path"]: r for r in plan["local"]}
            remote = {r["doc"]: r for r in plan["remote"]}
            matched_local, matched_remote = set(), set()
            paused = []
            for d in plan["dirs"]:
                store.db.execute(
                    "INSERT INTO dirs VALUES(?,?,?)", (d["path"], d["node"], d["space"])
                )
            for match in plan["matches"]:
                row = remote[match["remote"]]
                data = (Path(cfg["vault_root"]) / match["local"]).read_bytes()
                store.save_pair(
                    {
                        "id": str(uuid.uuid4()),
                        "path": match["local"],
                        "doc": row["doc"],
                        "node": row["node"],
                        "space": row["space"],
                        "parent": row["parent"],
                    },
                    data,
                    row["snapshot"],
                )
                matched_local.add(match["local"])
                matched_remote.add(row["doc"])
            resource_files, excluded_local = {}, []
            for action in plan["actions"]:
                if action["action"] in {"register_local", "defer_empty"}:
                    row = local[action["local"]]
                    collisions = [c for c in plan["conflicts"] if c["local"] == row["path"]]
                    if collisions and any(
                        decisions.get(c["remote"]) != "keep_both" for c in collisions
                    ):
                        excluded_local.append(row["path"])
                        paused.extend(c for c in collisions if c not in paused)
                        continue
                    store.save_pair(
                        {
                            "id": str(uuid.uuid4()),
                            "path": row["path"],
                            "doc": None,
                            "node": None,
                            "space": row["space_id"],
                            "parent": None,
                        },
                        None,
                        None,
                    )
                elif action["action"] == "preserve_local_outside_cloud_adoption":
                    excluded_local.append(action["local"])
                elif action["action"] == "register_cloud":
                    row = remote[action["remote"]]
                    conflict = next(
                        (c for c in plan["conflicts"] if c["remote"] == row["doc"]), None
                    )
                    path = row["path"]
                    if conflict:
                        if decisions.get(row["doc"]) != "keep_both":
                            paused.append(conflict)
                            continue
                        p = Path(path)
                        path = p.with_name(
                            p.stem + "~cloud-" + suffix(row["node"]) + p.suffix
                        ).as_posix()
                    if row["kind"] == "docx":
                        store.save_pair(
                            {
                                "id": str(uuid.uuid4()),
                                "path": path,
                                "doc": row["doc"],
                                "node": row["node"],
                                "space": row["space"],
                                "parent": row["parent"],
                            },
                            None,
                            None,
                        )
                    elif row["kind"] in {"file", "sheet", "bitable"}:
                        resource_files[row["doc"]] = {
                            **row,
                            "path": path,
                            "pending_projection": True,
                        }
                    else:
                        paused.append(
                            {
                                "remote": row["doc"],
                                "reason": "unsupported_cloud_object:" + row["kind"],
                            }
                        )
            for row in plan["remote"]:
                store.db.execute(
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
            store.db.commit()
            store.set("resource_files", resource_files)
            store.set("excluded_local_files", excluded_local)
            store.set("bootstrap_paused", paused)
            store.set(
                "bootstrap_complete", {"plan": plan["id"], "fingerprint": fingerprint, "at": now()}
            )
            store.set("discovered_at", time.time())
            result = {
                "profile_ready": True,
                "pairs": len(store.pairs()),
                "paused": paused,
                "resource_projections": len(resource_files),
                "content_writes": 0,
            }
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store.db.close()
            os.replace(temporary, target)
            cfg = dict(cfg, production_writes_enabled=True)
            save_profile(root, cfg)
            return result
        finally:
            store.db.close()
            for path in [temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")]:
                if path.exists():
                    path.unlink()
