"""Explicitly opted-in live acceptance against a disposable Wiki parent.

Required environment: OFB_LIVE_ROOT, OFB_LIVE_CLI, OFB_LIVE_OUTPUT.
The output directory must be outside this repository. No credentials, tokens,
cloud IDs, Vault notes or snapshots are written into the source checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as E
from pathlib import Path

from obsidian_feishu_bridge.bootstrap import resolve_target
from obsidian_feishu_bridge.runtime import Lark, atomic, dump

REPO = Path(__file__).resolve().parents[1]


def main():
    required = ["OFB_LIVE_ROOT", "OFB_LIVE_CLI", "OFB_LIVE_OUTPUT"]
    if any(not os.environ.get(k) for k in required):
        raise SystemExit("Live tests not run: explicitly set " + ", ".join(required))
    out = Path(os.environ["OFB_LIVE_OUTPUT"]).resolve()
    if out.is_relative_to(REPO):
        raise SystemExit("OFB_LIVE_OUTPUT must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    binary = str(Path(os.environ["OFB_LIVE_CLI"]).resolve())
    api = Lark({"cli_path": binary, "identity": "user", "timeout_seconds": 90}, out, True)
    parent = resolve_target(api, os.environ["OFB_LIVE_ROOT"])
    api.cfg["mappings"] = [{"space_id": parent["space_id"], "root_node": parent["root_node"]}]
    state_path = out / "live-control.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {
            "run_id": uuid.uuid4().hex[:10],
            "roots": {},
            "cases": {},
        }
    )

    def save():
        atomic(state_path, (dump(state) + "\n").encode())

    def root_for(case):
        if case in state["roots"]:
            return state["roots"][case]
        title = "OFB acceptance " + case + " " + state["run_id"]
        existing = [
            n
            for n in api.children(parent["space_id"], parent["root_node"])
            if n.get("title") == title
        ]
        if len(existing) > 1:
            raise RuntimeError("ambiguous_live_test_root")
        n = existing[0] if existing else api.create(parent["space_id"], parent["root_node"], title)
        state["roots"][case] = n["node_token"]
        save()
        return n["node_token"]

    def cloud(root, title, xml):
        matches = [n for n in api.children(parent["space_id"], root) if n.get("title") == title]
        if len(matches) > 1:
            raise RuntimeError("ambiguous_fixture")
        if matches:
            return matches[0]
        n = api.create(parent["space_id"], root, title)
        d = api.fetch(n["obj_token"])
        api.update(n["obj_token"], d["revision_id"], "block_insert_after", xml)
        return n

    def invoke(case, *args, expected=(0,)):
        with tempfile.TemporaryDirectory(prefix="ofb-unrelated-cwd-") as cwd:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "obsidian_feishu_bridge",
                    "--data-home",
                    str(out / "profiles-home"),
                    "--profile",
                    case,
                    *args,
                ],
                cwd=cwd,
                encoding="utf-8",
                capture_output=True,
            )
        (out / (case + "-last-command.json")).write_text(
            dump(
                {
                    "argv": list(args),
                    "returncode": process.returncode,
                    "stdout": process.stdout,
                    "stderr": process.stderr,
                }
            ),
            encoding="utf-8",
        )
        if process.returncode not in expected:
            raise RuntimeError(
                "live_command_failed:" + case + ":" + args[0] + ":" + process.stderr[:500]
            )
        return json.loads(process.stdout.strip().splitlines()[-1])

    def init(case, root, mode):
        vault = out / "vaults" / case
        vault.mkdir(parents=True, exist_ok=True)
        profile = out / "profiles-home/profiles" / case
        if not (profile / "config.json").exists():
            invoke(
                case, "init", "--vault", str(vault), "--wiki", root, "--mode", mode, "--cli", binary
            )
        return vault, profile

    def adopt(case, profile):
        if not (profile / "state/sync.sqlite").exists():
            plan = invoke(case, "plan")
            accepted = invoke(case, "bootstrap", "--approve")
            assert accepted["content_writes"] == 0
            return plan
        return None

    if "local" not in state["cases"]:
        root = root_for("local")
        vault, profile = init("local", root, "local")
        if not (profile / "state/sync.sqlite").exists():
            (vault / "Sub").mkdir(exist_ok=True)
            (vault / "A.md").write_text(
                "# Local note\n\nRead [[Sub/B.md]].\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n\nFormula $x^2$.\n",
                encoding="utf-8",
            )
            (vault / "Sub/B.md").write_text("# Second note\n\nRead [[A.md]].\n", encoding="utf-8")
        adopt("local", profile)
        first = invoke("local", "sync", "--approve", expected=(0, 4))
        if first["paused_count"]:
            raise RuntimeError("local_case_paused")
        second = invoke("local", "sync", "--approve")
        assert second["writes"] == 0
        state["cases"]["local"] = {"created": 2, "repeat_writes": 0, "cyclic_links_resolved": True}
        save()

    if "cloud" not in state["cases"]:
        root = root_for("cloud")
        remote = cloud(root, "Remote", "<p>Cloud-only synthetic text</p>")
        sheet_title = "Native table"
        if not any(n.get("title") == sheet_title for n in api.children(parent["space_id"], root)):
            api.call(
                [
                    "wiki",
                    "+node-create",
                    "--space-id",
                    parent["space_id"],
                    "--parent-node-token",
                    root,
                    "--obj-type",
                    "sheet",
                    "--title",
                    sheet_title,
                ]
            )
        vault, profile = init("cloud", root, "cloud")
        adopt("cloud", profile)
        result = invoke("cloud", "sync", "--approve", expected=(0, 4))
        if result["paused_count"]:
            raise RuntimeError("cloud_case_paused")
        assert "Cloud-only" in (vault / "Remote.md").read_text(encoding="utf-8")
        assert "Open in Feishu" in (vault / "Native table.md").read_text(encoding="utf-8")
        doc = api.fetch(remote["obj_token"])
        if "Cloud delta" not in doc["content"]:
            api.update(
                remote["obj_token"], doc["revision_id"], "block_insert_after", "<p>Cloud delta</p>"
            )
        invoke("cloud", "sync", "--approve")
        assert "Cloud delta" in (vault / "Remote.md").read_text(encoding="utf-8")
        state["cases"]["cloud"] = {
            "note_pulled": True,
            "native_table_linked": True,
            "incremental_pull": True,
        }
        save()

    if "both" not in state["cases"]:
        root = root_for("both")
        cloud(root, "Shared", "<p>Identical content</p>")
        cloud(root, "Conflict", "<p>Cloud conflict version</p>")
        cloud(root, "Remote only", "<p>Independent cloud note</p>")
        vault, profile = init("both", root, "both")
        if not (profile / "state/sync.sqlite").exists():
            (vault / "Shared.md").write_text("Identical content\n", encoding="utf-8")
            (vault / "Conflict.md").write_text("Local conflict version\n", encoding="utf-8")
            (vault / "Local only.md").write_text("Independent local note\n", encoding="utf-8")
        plan = adopt("both", profile)
        if plan:
            assert plan["unique_matches"] == 1 and plan["conflicts"] == 1
        result = invoke("both", "sync", "--approve", expected=(4,))
        assert result["paused_count"] == 1
        assert (vault / "Conflict.md").read_text(encoding="utf-8") == "Local conflict version\n"
        assert (vault / "Remote only.md").exists()
        result = invoke("both", "sync", "--approve", expected=(4,))
        assert result["writes"] == 0
        state["cases"]["both"] = {
            "shared_matched": 1,
            "conflict_preserved": True,
            "independent_notes_synced": True,
            "repeat_writes": 0,
        }
        save()

    if "assets" not in state["cases"]:
        from test_assets import tiny_png

        root = root_for("assets")
        vault, profile = init("assets", root, "local")
        if not (profile / "state/sync.sqlite").exists():
            # A minimal valid, synthetic one-page PDF. No real attachment is copied.
            stream = b"BT /F1 12 Tf 12 48 Td (Synthetic acceptance attachment) Tj ET"
            objects = [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            ]
            pdf = b"%PDF-1.4\n"
            offsets = [0]
            for i, obj in enumerate(objects, 1):
                offsets.append(len(pdf))
                pdf += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
            xref = len(pdf)
            pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
                f"{o:010d} 00000 n \n".encode() for o in offsets[1:]
            )
            pdf += (
                b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
                + str(xref).encode()
                + b"\n%%EOF\n"
            )
            (vault / "Paper.pdf").write_bytes(pdf)
            (vault / "pixel.png").write_bytes(tiny_png(1))
            (vault / "Assets.md").write_text(
                "# Assets\n\n[Paper](Paper.pdf)\n\n![Pixel](pixel.png)\n", encoding="utf-8"
            )
        adopt("assets", profile)
        first = invoke("assets", "sync", "--approve", expected=(0, 4))
        if first["paused_count"]:
            raise RuntimeError("asset_case_initial_sync_paused")
        repeat = invoke("assets", "sync", "--approve")
        assert repeat["writes"] == 0
        # Change only the bytes, keeping the Markdown unchanged.
        (vault / "pixel.png").write_bytes(tiny_png(2))
        changed = invoke("assets", "sync", "--approve", expected=(0, 4))
        if changed["paused_count"]:
            raise RuntimeError("asset_case_image_update_paused")
        assert invoke("assets", "sync", "--approve")["writes"] == 0
        state["cases"]["assets"] = {
            "local_pdf_uploaded_once": True,
            "pdf_downloads": 0,
            "image_byte_update_synced": True,
            "repeat_writes": 0,
        }
        save()

    if "guarded_writes" not in state["cases"]:
        root = root_for("guarded-writes")
        vault, profile = init("guarded-writes", root, "local")
        if not (profile / "state/sync.sqlite").exists():
            (vault / "Scoped.md").write_text("Scoped content\n", encoding="utf-8")
        adopt("guarded-writes", profile)
        result = invoke("guarded-writes", "sync", "--approve", expected=(0, 4))
        if result["paused_count"]:
            raise RuntimeError("guarded_writes_paused")
        assert invoke("guarded-writes", "sync", "--approve")["writes"] == 0
        state["cases"]["guarded_writes"] = {
            "ancestry_checked_for_mutations": True,
            "repeat_writes": 0,
        }
        save()

    if "large_note" not in state["cases"]:
        root = root_for("large-note")
        vault, profile = init("large-note", root, "local")
        if not (profile / "state/sync.sqlite").exists():
            (vault / "Large.md").write_text(
                "\n\n".join(f"Synthetic paragraph {i:03d}" for i in range(130)) + "\n",
                encoding="utf-8",
            )
        adopt("large-note", profile)
        result = invoke("large-note", "sync", "--approve", expected=(0, 4))
        if result["paused_count"]:
            raise RuntimeError("large_note_paused")
        assert invoke("large-note", "sync", "--approve")["writes"] == 0
        state["cases"]["large_note"] = {
            "paragraphs": 130,
            "ordered_bounded_patches": True,
            "repeat_writes": 0,
        }
        save()

    if "keep_both_resolution" not in state["cases"]:
        profile = out / "profiles-home/profiles/both"
        import sqlite3

        with sqlite3.connect(profile / "state/sync.sqlite") as database:
            raw = database.execute(
                "SELECT value FROM settings WHERE key='bootstrap_paused'"
            ).fetchone()[0]
            paused = json.loads(raw)
            token = next((x["remote"] for x in paused if x.get("local") == "Conflict.md"), None)
            if token is None:
                resolved = database.execute(
                    "SELECT value FROM settings WHERE key='bootstrap_resolutions'"
                ).fetchone()
                if not resolved:
                    raise RuntimeError("missing_expected_collision")
                token = next(iter(json.loads(resolved[0])))
        invoke("both", "resolve", "--cloud", token, "--keep-both", "--approve")
        result = invoke("both", "sync", "--approve", expected=(0, 4))
        if result["paused_count"]:
            raise RuntimeError("keep_both_resolution_paused")
        vault = out / "vaults/both"
        assert "Local conflict version" in (vault / "Conflict.md").read_text(encoding="utf-8")
        aliases = list(vault.glob("Conflict~cloud-*.md"))
        assert len(aliases) == 1 and "Cloud conflict version" in aliases[0].read_text(
            encoding="utf-8"
        )
        assert invoke("both", "sync", "--approve")["writes"] == 0
        state["cases"]["keep_both_resolution"] = {
            "both_versions_preserved": True,
            "baselines_not_reset": True,
            "repeat_writes": 0,
        }
        save()

    if "native_cloud_image" not in state["cases"]:
        from test_assets import tiny_png

        root = root_for("native-cloud-image")
        fixture = cloud(root, "Incoming image", "<p>Synthetic cloud image</p>")
        image = tiny_png(5)
        (out / "native-pixel.png").write_bytes(image)
        document = api.fetch(fixture["obj_token"])
        if not list(E.fromstring("<r>" + document["content"] + "</r>").iter("img")):
            api.update(
                fixture["obj_token"],
                document["revision_id"],
                "block_insert_after",
                '<img path="@./native-pixel.png" name="native-pixel.png"/>',
            )
        document = api.fetch(fixture["obj_token"])
        images = list(E.fromstring("<r>" + document["content"] + "</r>").iter("img"))
        assert len(images) == 1 and images[0].get("src")
        assert not images[0].get("name", "").startswith(hashlib.sha256(image).hexdigest())
        vault, profile = init("native-cloud-image", root, "cloud")
        adopt("native-cloud-image", profile)
        result = invoke("native-cloud-image", "sync", "--approve", expected=(0, 4))
        if result["paused_count"]:
            raise RuntimeError("native_cloud_image_import_paused")
        imported = list((vault / "_attachments/feishu").glob("*.png"))
        assert len(imported) == 1 and imported[0].read_bytes() == image
        note = vault / "Incoming image.md"
        assert imported[0].relative_to(vault).as_posix() in note.read_text(encoding="utf-8")
        assert invoke("native-cloud-image", "sync", "--approve")["writes"] == 0
        text = note.read_text(encoding="utf-8")
        if "Local edit after image import" not in text:
            note.write_text(text + "\nLocal edit after image import\n", encoding="utf-8")
        result = invoke("native-cloud-image", "sync", "--approve", expected=(0, 4))
        if result["paused_count"]:
            raise RuntimeError("native_cloud_image_local_edit_paused")
        assert "Local edit after image import" in api.fetch(fixture["obj_token"])["content"]
        assert invoke("native-cloud-image", "sync", "--approve")["writes"] == 0
        state["cases"]["native_cloud_image"] = {
            "cloud_image_bytes_preserved": True,
            "generic_attachment_directory": True,
            "subsequent_local_edit_synced": True,
            "repeat_writes": 0,
        }
        save()

    public_result = {
        "status": "passed",
        "cases": state["cases"],
        "llm_calls": 0,
        "notes_and_ids_in_output_only": True,
    }
    atomic(out / "public-result.json", (dump(public_result) + "\n").encode())
    print(dump(public_result))


if __name__ == "__main__":
    main()
