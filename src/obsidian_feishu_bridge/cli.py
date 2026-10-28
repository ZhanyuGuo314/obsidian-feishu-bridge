"""Public commands: guided setup, reviewable adoption and profile-scoped sync."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

from . import __version__
from .bootstrap import apply_plan, make_plan, resolve_target
from .bridge import Bridge
from .config import cli_path, create_config, engine_config, load_profile, profile_path, save_profile
from .resolve import keep_both
from .runtime import APIError, Lark, atomic, dump, now


def parser():
    p = argparse.ArgumentParser(prog="ofb", description="Obsidian ↔ Feishu Bridge")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--profile", default="default")
    p.add_argument("--data-home", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Choose Vault and Wiki scope; no content writes")
    init.add_argument("--vault")
    init.add_argument("--wiki")
    init.add_argument("--local-prefix", default="")
    init.add_argument("--mode", choices=["local", "cloud", "both"])
    init.add_argument("--cli")
    auth = sub.add_parser("auth", help="Delegate setup/login to the official CLI")
    auth.add_argument("--cli")
    auth.add_argument("--setup", action="store_true")
    sub.add_parser("plan", help="Read-only first-adoption plan")
    adopt = sub.add_parser("bootstrap", help="Adopt a reviewed plan into persistent state")
    adopt.add_argument("--approve", action="store_true")
    adopt.add_argument("--decisions", type=Path)
    resolution = sub.add_parser(
        "resolve", help="Keep both versions of an initial-adoption collision"
    )
    resolution.add_argument("--cloud", required=True)
    resolution.add_argument("--keep-both", action="store_true", required=True)
    resolution.add_argument("--approve", action="store_true")
    for name in ["check", "sync"]:
        s = sub.add_parser(name)
        s.add_argument("--full", action="store_true")
        s.add_argument("--discover", action="store_true")
        if name == "sync":
            s.add_argument("--approve", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--network", action="store_true")
    sub.add_parser("status")
    sub.add_parser("profiles", help="List profiles without opening note content")
    skill = sub.add_parser("install-skill", help="Install the bundled agent skill")
    skill.add_argument("--output", type=Path)
    skill.add_argument("--replace", action="store_true")
    watch = sub.add_parser("watch", help="Opt-in code-only polling, no scheduled task installation")
    watch.add_argument("--approve", action="store_true")
    watch.add_argument("--interval", type=int, default=120)
    watch.add_argument("--once", action="store_true")
    return p


def run_once(root, cfg, args, quiet=False):
    bridge = Bridge(root, engine_config(cfg))
    try:
        if not bridge.store.setting("bootstrap_complete"):
            raise ValueError("profile_not_bootstrapped: run plan then bootstrap --approve")
        apply = args.command in {"sync", "watch"}
        if apply and not args.approve:
            raise ValueError("sync_requires_approve_after_user_authorization")
        result = bridge.run(
            apply=apply,
            full=getattr(args, "full", False),
            discover=getattr(args, "discover", False),
        )
        (root / "state/failure.json").unlink(missing_ok=True)
        summary = {k: v for k, v in result.items() if k not in {"synced", "paused", "deferred"}}
        summary.update(
            synced_count=len(result["synced"]),
            paused_count=len(result["paused"]),
            report=str(root / "state/latest.json"),
        )
        if not quiet:
            print(dump(summary), flush=True)
        return 0 if result["complete"] else 4
    finally:
        bridge.close()


def execute(args):
    root = profile_path(args.profile, args.data_home)
    if args.command == "profiles":
        rows = []
        for path in sorted(root.parent.glob("*/config.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                rows.append(
                    {
                        "name": path.parent.name,
                        "vault": value["vault_root"],
                        "mappings": len(value.get("mappings", [])),
                        "write_enabled": value.get("production_writes_enabled", False),
                    }
                )
            except (ValueError, KeyError):
                rows.append({"name": path.parent.name, "status": "invalid_config"})
        print(dump({"profiles": rows}))
        return 0
    if args.command == "install-skill":
        destination = (
            args.output
            or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            / "skills/obsidian-feishu-sync"
        )
        destination = destination.expanduser().resolve()
        if destination.exists() and not args.replace:
            raise ValueError("skill_destination_exists: use --replace explicitly")

        def copy(source, target):
            target.mkdir(parents=True, exist_ok=True)
            for child in source.iterdir():
                if child.is_dir():
                    copy(child, target / child.name)
                else:
                    (target / child.name).write_bytes(child.read_bytes())

        copy(files("obsidian_feishu_bridge").joinpath("skilldata"), destination)
        print(dump({"skill": str(destination / "SKILL.md"), "engine": __version__}))
        return 0
    if args.command == "auth":
        binary = cli_path(args.cli)
        if args.setup:
            code = subprocess.call([binary, "config", "init", "--new"])
            if code:
                return code
        return subprocess.call([binary, "auth", "login", "--domain", "docs,drive,wiki"])
    if args.command == "init":
        if (root / "config.json").exists():
            raise ValueError(
                "profile_exists: choose a new --profile; existing state will not be reset"
            )
        vault = args.vault or input("Obsidian Vault directory: ").strip()
        target = args.wiki or input("Target Wiki URL or node token: ").strip()
        mode = args.mode or (
            input("Initial contents: local / cloud / both [both]: ").strip() or "both"
        )
        binary = cli_path(args.cli)
        root.mkdir(parents=True, exist_ok=True)
        preliminary = {"cli_path": binary, "identity": "user", "timeout_seconds": 90}
        resolved = resolve_target(Lark(preliminary, root), target)
        cfg = create_config(
            vault,
            binary,
            [
                {
                    "local_prefix": args.local_prefix,
                    "space_id": resolved["space_id"],
                    "root_node": resolved["root_node"],
                }
            ],
            mode,
        )
        cfg["cloud_base_url"] = resolved["cloud_base_url"]
        save_profile(root, cfg)
        print(
            dump(
                {
                    "profile": args.profile,
                    "config": str(root / "config.json"),
                    "vault": cfg["vault_root"],
                    "target_title": resolved["title"],
                    "content_writes": 0,
                    "next": "ofb --profile " + args.profile + " plan",
                }
            )
        )
        return 0
    cfg = load_profile(root)
    if args.command == "resolve":
        if not args.approve:
            raise ValueError("resolution_requires_user_choice_and_approve")
        print(dump(keep_both(root, cfg, Lark(engine_config(cfg), root), args.cloud)))
        return 0
    if args.command == "plan":
        plan = make_plan(root, cfg, Lark(engine_config(cfg), root))
        print(
            dump(
                {
                    "plan": str(root / "setup-plan.json"),
                    "fingerprint": plan["fingerprint"],
                    "local_notes": len(plan["local"]),
                    "cloud_objects": len(plan["remote"]),
                    "unique_matches": len(plan["matches"]),
                    "conflicts": len(plan["conflicts"]),
                    "content_writes": 0,
                }
            )
        )
        return 0
    if args.command == "bootstrap":
        if not args.approve:
            raise ValueError("bootstrap_requires_review_and_approve")
        decisions = json.loads(args.decisions.read_text(encoding="utf-8")) if args.decisions else {}
        print(dump(apply_plan(root, cfg, Lark(engine_config(cfg), root), decisions)))
        return 0
    if args.command == "status":
        path = root / "state/latest.json"
        result = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_run"}
        )
        failure = root / "state/failure.json"
        if failure.exists():
            result = {
                "last_run": result,
                "unresolved_failure": json.loads(failure.read_text(encoding="utf-8")),
            }
        print(dump(result))
        return 0
    if args.command == "doctor":
        binary = Path(cfg["cli_path"])
        result = {
            "profile": args.profile,
            "config": str(root / "config.json"),
            "vault_exists": Path(cfg["vault_root"]).is_dir(),
            "cli_exists": binary.is_file(),
            "write_enabled": cfg["production_writes_enabled"],
            "network_checked": False,
        }
        if args.network:
            api = Lark(engine_config(cfg), root)
            for m in cfg["mappings"]:
                node = resolve_target(api, m["root_node"])
                if node["space_id"] != m["space_id"]:
                    raise ValueError("configured_target_space_mismatch")
            result["network_checked"] = True
        print(dump(result))
        return 0 if result["vault_exists"] and result["cli_exists"] else 2
    if args.command == "watch":
        if args.interval < 15:
            raise ValueError("poll_interval_must_be_at_least_15_seconds")
        print(
            dump({"status": "watching", "profile": args.profile, "interval": args.interval}),
            flush=True,
        )
        previous = None
        backoff = args.interval
        while True:
            delay = args.interval
            try:
                cfg = load_profile(root)
                code = run_once(root, cfg, args, quiet=True)
                report = json.loads((root / "state/latest.json").read_text(encoding="utf-8"))
                signature = dump(
                    {
                        "paused": [(x["path"], x["reason"]) for x in report["paused"]],
                        "synced": report["synced"],
                    }
                )
                if report["writes"] or (
                    (report["synced"] or report["paused"]) and signature != previous
                ):
                    print(
                        dump(
                            {
                                "status": "needs_review" if code == 4 else "synced",
                                "writes": report["writes"],
                                "paused": len(report["paused"]),
                                "report": str(root / "state/latest.json"),
                            }
                        ),
                        flush=True,
                    )
                previous = signature
                backoff = args.interval
            except APIError as error:
                detail = str(error)
                if any(
                    word in detail.lower() for word in ("auth", "scope", "permission", "forbidden")
                ):
                    raise
                if not any(
                    word in detail.lower()
                    for word in (
                        "timeoutexpired",
                        "connection",
                        "network",
                        "transport",
                        "rate",
                        "502",
                        "503",
                        "504",
                        "999914",
                    )
                ):
                    raise
                print(
                    dump(
                        {"status": "retry_wait", "reason": detail[:240], "retry_seconds": backoff}
                    ),
                    flush=True,
                )
                code = 2
                delay = backoff
                backoff = min(backoff * 2, 900)
            if args.once:
                return code
            time.sleep(delay)
    return run_once(root, cfg, args)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except KeyboardInterrupt:
        print(dump({"status": "stopped", "state_preserved": True}))
        return 130
    except Exception as error:
        # Details and note contents remain local; credentials are never read by us.
        result = {
            "status": "failed",
            "type": type(error).__name__,
            "reason": str(error)[:800],
            "at": now(),
            "state_preserved": True,
        }
        try:
            root = profile_path(args.profile, args.data_home)
            if (root / "config.json").exists():
                atomic(root / "state/failure.json", (dump(result) + "\n").encode())
        except (ValueError, OSError):
            pass
        print(dump(result), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
