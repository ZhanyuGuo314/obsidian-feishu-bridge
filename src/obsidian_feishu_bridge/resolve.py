"""Explicit keep-both resolution for first-adoption collisions; no baseline reset."""

import uuid
from pathlib import Path

from .paths import suffix
from .runtime import Store, lock, now


def keep_both(root, cfg, api, token):
    root = Path(root)
    with lock(root / "state/run.lock"):
        store = Store(root / cfg["state_db"])
        try:
            decisions = store.setting("bootstrap_resolutions", {})
            if token in decisions:
                return {"already_resolved": True, "content_writes": 0}
            paused = store.setting("bootstrap_paused", [])
            issue = next((x for x in paused if x.get("remote") == token), None)
            if not issue or not issue.get("cloud_path"):
                raise ValueError("not_a_resolvable_bootstrap_collision")
            row = store.db.execute("SELECT * FROM objects WHERE token=?", (token,)).fetchone()
            if row is None or row["kind"] not in {"docx", "file", "sheet", "bitable"}:
                raise ValueError("unsupported_collision_kind")
            api.assert_scope(row["node"])
            cloud_path = Path(issue["cloud_path"])
            alias = cloud_path.with_name(
                cloud_path.stem + "~cloud-" + suffix(row["node"]) + cloud_path.suffix
            ).as_posix()
            vault = Path(cfg["vault_root"]).resolve()
            destination = (vault / alias).resolve()
            if not destination.is_relative_to(vault) or destination.exists():
                raise ValueError("keep_both_destination_is_not_free")
            with store.transaction():
                if row["kind"] == "docx":
                    store.save_pair(
                        {
                            "id": str(uuid.uuid4()),
                            "path": alias,
                            "doc": token,
                            "node": row["node"],
                            "space": row["space"],
                            "parent": row["parent"],
                        },
                        None,
                        None,
                    )
                else:
                    resources = store.setting("resource_files", {})
                    resources[token] = {
                        "path": alias,
                        "doc": token,
                        "node": row["node"],
                        "space": row["space"],
                        "kind": row["kind"],
                        "title": row["title"],
                        "pending_projection": True,
                        "url": cfg.get("cloud_base_url", "https://my.feishu.cn").rstrip("/")
                        + "/wiki/"
                        + row["node"],
                    }
                    store.set("resource_files", resources)
                local_path = issue["local"]
                if (
                    cfg["bootstrap_mode"] in {"local", "both"}
                    and not store.db.execute(
                        "SELECT id FROM pairs WHERE path=?", (local_path,)
                    ).fetchone()
                ):
                    store.save_pair(
                        {
                            "id": str(uuid.uuid4()),
                            "path": local_path,
                            "doc": None,
                            "node": None,
                            "space": row["space"],
                            "parent": None,
                        },
                        None,
                        None,
                    )
                excluded = store.setting("excluded_local_files", [])
                if cfg["bootstrap_mode"] in {"local", "both"}:
                    excluded = [p for p in excluded if p != local_path]
                store.set("excluded_local_files", excluded)
                store.set("bootstrap_paused", [x for x in paused if x.get("remote") != token])
                decisions[token] = {"action": "keep_both", "local_alias": alias, "at": now()}
                store.set("bootstrap_resolutions", decisions)
            return {
                "resolved": True,
                "local_alias": alias,
                "content_writes": 0,
                "next": "sync --approve",
            }
        finally:
            store.db.close()
