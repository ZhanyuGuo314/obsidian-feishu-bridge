"""User profiles live outside the repository and never contain credentials."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

from .runtime import atomic, dump

RESOURCE_DEFAULTS = {
    "images": "embed",
    "pdf": "link",
    "file": "link",
    "sheet": "link",
    "bitable": "link",
    "whiteboard": "link",
    "unknown": "pause",
}


def data_home() -> Path:
    override = os.environ.get("OFB_DATA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "obsidian-feishu-bridge"


def profile_path(name: str = "default", home: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
        raise ValueError("invalid_profile_name")
    return (home or data_home()) / "profiles" / name


def local_prefix(value: str) -> str:
    value = value.replace("\\", "/")
    if value.startswith("/"):
        raise ValueError("local_prefix_must_be_relative")
    value = value.strip("/")
    if value == ".":
        return ""
    parts = PurePosixPath(value).parts
    if any(p in ("..", ".obsidian", ".ofb") for p in parts) or ":" in value:
        raise ValueError("local_prefix_must_stay_in_vault")
    return value


def cli_path(value: str | None = None) -> str:
    binary = value or shutil.which("lark-cli")
    if not binary:
        raise ValueError("lark_cli_not_found: install official larksuite/cli first")
    path = Path(binary).expanduser().resolve()
    if not path.is_file():
        raise ValueError("lark_cli_path_not_a_file")
    return str(path)


def create_config(vault, binary, mounts, mode="both", resources=None):
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError("vault_directory_does_not_exist")
    if mode not in {"local", "cloud", "both"}:
        raise ValueError("invalid_bootstrap_mode")
    seen_prefixes, seen_roots = [], set()
    normalized = []
    for mount in mounts:
        prefix = local_prefix(mount.get("local_prefix", ""))
        root = mount["root_node"]
        if not root or root in seen_roots:
            raise ValueError("duplicate_or_missing_remote_root")
        key = prefix.casefold()
        if any(
            key == p or not p or not key or key.startswith(p + "/") or p.startswith(key + "/")
            for p in seen_prefixes
        ):
            raise ValueError("overlapping_local_mounts")
        seen_prefixes.append(key)
        seen_roots.add(root)
        normalized.append(
            {"local_prefix": prefix, "space_id": str(mount["space_id"]), "root_node": root}
        )
    if not normalized:
        raise ValueError("at_least_one_mapping_required")
    policy = RESOURCE_DEFAULTS | (resources or {})
    allowed = {
        "images": {"embed", "link"},
        "pdf": {"link"},
        "file": {"link"},
        "sheet": {"link"},
        "bitable": {"link"},
        "whiteboard": {"link"},
        "unknown": {"pause"},
    }
    if set(policy) != set(allowed) or any(policy[k] not in allowed[k] for k in policy):
        raise ValueError("unsupported_resource_policy")
    return {
        "schema": 1,
        "vault_root": str(vault),
        "cli_path": str(Path(binary).resolve()),
        "identity": "user",
        "mappings": normalized,
        "bootstrap_mode": mode,
        "resources": policy,
        "production_writes_enabled": False,
        "llm_enabled": False,
        "state_db": "state/sync.sqlite",
        "timeout_seconds": 90,
        "sweep_seconds": 86400,
        "discovery_seconds": 3600,
        "exclude": [".obsidian", ".trash", ".git", ".ofb", ".obsidian-feishu-bridge"],
        "identity_field": "ofb_id",
        "local_files": "referenced",
        "path_layout": "document_tree",
        "conflict_policy": "pause_preserve_both",
    }


def validate_config(value):
    if value.get("schema") != 1:
        raise ValueError("unsupported_config_schema")
    if value.get("llm_enabled") is not False or value.get("identity") != "user":
        raise ValueError("invalid_identity_or_model_policy")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", value.get("identity_field", "ofb_id")):
        raise ValueError("invalid_identity_field")
    if value.get("local_files", "referenced") not in {"referenced", "pause"}:
        raise ValueError("unsupported_local_file_policy")
    if value.get("path_layout", "document_tree") != "document_tree":
        raise ValueError("unsupported_path_layout")
    if value.get("conflict_policy", "pause_preserve_both") != "pause_preserve_both":
        raise ValueError("unsupported_conflict_policy")
    if value.get("state_db", "state/sync.sqlite") != "state/sync.sqlite":
        raise ValueError("select_profile_data_home_instead_of_external_database_path")
    checked = create_config(
        value["vault_root"],
        value["cli_path"],
        value["mappings"],
        value.get("bootstrap_mode", "both"),
        value.get("resources"),
    )
    checked.update(value)
    return checked


def engine_config(value):
    """Normalized mounts feed the reusable engine without fixed topic names."""
    result = dict(value)
    result["spaces"] = [
        {"name": m["local_prefix"], "space_id": m["space_id"], "root_node": m["root_node"]}
        for m in value["mappings"]
    ]
    return result


def load_profile(root):
    return validate_config(json.loads((Path(root) / "config.json").read_text(encoding="utf-8")))


def save_profile(root, value):
    atomic(Path(root) / "config.json", (dump(value) + "\n").encode())
