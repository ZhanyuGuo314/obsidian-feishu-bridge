import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_engine import FakeAPI

from obsidian_feishu_bridge.bootstrap import apply_plan, make_plan
from obsidian_feishu_bridge.bridge import Bridge
from obsidian_feishu_bridge.codec import render
from obsidian_feishu_bridge.config import create_config, engine_config, profile_path
from obsidian_feishu_bridge.paths import child_paths, component
from obsidian_feishu_bridge.resolve import keep_both
from obsidian_feishu_bridge.runtime import Store


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.profile = self.base / "profile"
        self.profile.mkdir()
        self.api = FakeAPI()
        self.api.writes = True

    def tearDown(self):
        self.temp.cleanup()

    def cfg(self, mode="both", prefix=""):
        return create_config(
            self.vault,
            sys.executable,
            [{"local_prefix": prefix, "space_id": "fake-space", "root_node": "fake-root"}],
            mode,
        )

    def cloud(self, title, text="cloud note\n", kind="docx", parent="fake-root"):
        n = self.api.create("fake-space", parent, title)
        n["obj_type"] = kind
        if kind == "docx":
            self.api.update(n["obj_token"], 0, "block_insert_after", "".join(render(text)))
        return n

    def test_local_only_from_empty_state(self):
        (self.vault / "Note.md").write_text("local note\n", encoding="utf-8")
        cfg = self.cfg("local")
        plan = make_plan(self.profile, cfg, self.api)
        self.assertFalse(plan["matches"])
        self.assertFalse(self.api.docs)
        accepted = apply_plan(self.profile, cfg, self.api)
        self.assertEqual(accepted["content_writes"], 0)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            self.assertEqual(len(self.api.docs), 1)
        finally:
            e.close()

    def test_cloud_only_creates_local_without_overwriting_unrelated_note(self):
        self.cloud("Remote", "remote body\n")
        (self.vault / "Unrelated.md").write_text("keep this\n", encoding="utf-8")
        cfg = self.cfg("cloud")
        make_plan(self.profile, cfg, self.api)
        apply_plan(self.profile, cfg, self.api)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            self.assertIn("remote body", (self.vault / "Remote.md").read_text(encoding="utf-8"))
            self.assertEqual(
                (self.vault / "Unrelated.md").read_text(encoding="utf-8"), "keep this\n"
            )
            self.assertEqual(len(self.api.docs), 1)
        finally:
            e.close()

    def test_unique_content_matches_even_when_titles_differ(self):
        self.cloud("Cloud title", "same **content**\n")
        (self.vault / "Local title.md").write_text("same **content**\n", encoding="utf-8")
        plan = make_plan(self.profile, self.cfg(), self.api)
        self.assertEqual(len(plan["matches"]), 1)
        accepted = apply_plan(self.profile, self.cfg(), self.api)
        self.assertEqual(accepted["pairs"], 1)
        self.assertEqual(len(self.api.docs), 1)

    def test_same_name_different_content_pauses_without_overwrite(self):
        self.cloud("Note", "cloud version\n")
        (self.vault / "Note.md").write_text("local version\n", encoding="utf-8")
        cfg = self.cfg()
        plan = make_plan(self.profile, cfg, self.api)
        self.assertEqual(len(plan["conflicts"]), 1)
        accepted = apply_plan(self.profile, cfg, self.api)
        self.assertTrue(accepted["paused"])
        self.assertEqual(accepted["pairs"], 0)
        self.assertEqual((self.vault / "Note.md").read_text(encoding="utf-8"), "local version\n")

    def test_keep_both_is_explicit_and_creates_distinct_local_paths(self):
        n = self.cloud("Note", "cloud version\n")
        (self.vault / "Note.md").write_text("local version\n", encoding="utf-8")
        cfg = self.cfg()
        make_plan(self.profile, cfg, self.api)
        r = apply_plan(self.profile, cfg, self.api, {n["obj_token"]: "keep_both"})
        self.assertEqual(r["pairs"], 2)
        s = Store(self.profile / cfg["state_db"])
        try:
            paths = [p["path"] for p in s.pairs()]
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(any("~cloud-" in x for x in paths))
        finally:
            s.db.close()

    def test_changed_source_after_plan_rejected(self):
        (self.vault / "Note.md").write_text("first\n", encoding="utf-8")
        cfg = self.cfg()
        make_plan(self.profile, cfg, self.api)
        (self.vault / "Note.md").write_text("later\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "local_changed"):
            apply_plan(self.profile, cfg, self.api)

    def test_duplicate_equal_notes_are_not_auto_deduplicated(self):
        self.cloud("One", "same\n")
        self.cloud("Two", "same\n")
        (self.vault / "Note.md").write_text("same\n", encoding="utf-8")
        p = make_plan(self.profile, self.cfg(), self.api)
        self.assertFalse(p["matches"])

    def test_native_resource_link_never_downloads_payload(self):
        self.cloud("Paper.pdf", kind="file")
        cfg = self.cfg("cloud")
        make_plan(self.profile, cfg, self.api)
        accepted = apply_plan(self.profile, cfg, self.api)
        self.assertEqual(accepted["resource_projections"], 1)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertEqual(r["pdf_downloads"], 0)
            note = next(self.vault.rglob("*.md"))
            self.assertIn("Open in Feishu", note.read_text(encoding="utf-8"))
            self.assertEqual(len(self.api.docs), 1)
        finally:
            e.close()

    def test_container_document_maps_to_index_note(self):
        n = self.cloud("Parent", "parent body\n")
        n["has_child"] = True
        self.cloud("Child", "child body\n", parent=n["node_token"])
        plan = make_plan(self.profile, self.cfg("cloud"), self.api)
        self.assertEqual(
            {r["path"] for r in plan["remote"]}, {"Parent/_index.md", "Parent/Child.md"}
        )

    def test_ignored_obsidian_settings_are_never_notes(self):
        folder = self.vault / ".obsidian"
        folder.mkdir()
        (folder / "settings.md").write_text("private setting\n", encoding="utf-8")
        plan = make_plan(self.profile, self.cfg(), self.api)
        self.assertFalse(plan["local"])

    def test_profiles_and_mounts_cannot_escape_or_overlap(self):
        with self.assertRaises(ValueError):
            profile_path("../escape", self.base)
        for prefix in ("../escape", "/absolute", "C:/absolute"):
            with self.assertRaises(ValueError):
                self.cfg(prefix=prefix)
        with self.assertRaises(ValueError):
            create_config(
                self.vault,
                sys.executable,
                [
                    {"local_prefix": "A", "space_id": "s", "root_node": "r"},
                    {"local_prefix": "A/B", "space_id": "s", "root_node": "r2"},
                ],
            )

    def test_windows_reserved_names_and_duplicates(self):
        self.assertEqual(component("CON"), "_CON")
        paths = child_paths(
            "",
            [
                {"title": "Same", "node_token": "fake-one"},
                {"title": "same", "node_token": "fake-two"},
            ],
        )
        self.assertEqual(len({p["path"].casefold() for p in paths.values()}), 2)

    def test_new_notes_can_link_to_each_other_without_context(self):
        (self.vault / "A.md").write_text("See [[B.md]]\n", encoding="utf-8")
        (self.vault / "B.md").write_text("See [[A.md]]\n", encoding="utf-8")
        cfg = self.cfg("local")
        make_plan(self.profile, cfg, self.api)
        apply_plan(self.profile, cfg, self.api)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            self.assertEqual(len(self.api.docs), 2)
            self.assertTrue(all("<a href=" in d["content"] for d in self.api.docs.values()))
        finally:
            e.close()

    def test_root_vault_nested_folder_stays_in_selected_subtree(self):
        (self.vault / "Sub").mkdir()
        (self.vault / "Sub/Note.md").write_text("nested body\n", encoding="utf-8")
        cfg = self.cfg("local")
        make_plan(self.profile, cfg, self.api)
        apply_plan(self.profile, cfg, self.api)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            self.assertTrue(all(n["parent_node_token"] for n in self.api.nodes.values()))
            self.assertTrue(
                any(
                    n["parent_node_token"] == "fake-root" and n["title"] == "Sub"
                    for n in self.api.nodes.values()
                )
            )
        finally:
            e.close()

    def test_native_whiteboard_projects_to_link_without_downgrading_cloud(self):
        n = self.cloud("Board", "intro\n")
        self.api.docs[n["obj_token"]]["content"] += (
            '<whiteboard id="fake-board-block" token="fake-board-token"/>'
        )
        cfg = self.cfg("cloud")
        make_plan(self.profile, cfg, self.api)
        apply_plan(self.profile, cfg, self.api)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            self.assertIn("fake-board-block", (self.vault / "Board.md").read_text(encoding="utf-8"))
            self.assertIn("<whiteboard", self.api.docs[n["obj_token"]]["content"])
        finally:
            e.close()

    def test_large_new_note_uses_bounded_steps_in_correct_order(self):
        text = "\n\n".join(f"Paragraph {i:03d}" for i in range(130)) + "\n"
        (self.vault / "Large.md").write_text(text, encoding="utf-8")
        cfg = self.cfg("local")
        make_plan(self.profile, cfg, self.api)
        apply_plan(self.profile, cfg, self.api)
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            content = next(iter(self.api.docs.values()))["content"]
            positions = [content.index(f"Paragraph {i:03d}") for i in range(130)]
            self.assertEqual(positions, sorted(positions))
        finally:
            e.close()

    def test_interrupted_adoption_does_not_publish_partial_baselines(self):
        (self.vault / "A.md").write_text("A\n", encoding="utf-8")
        (self.vault / "B.md").write_text("B\n", encoding="utf-8")
        cfg = self.cfg("local")
        make_plan(self.profile, cfg, self.api)
        original = Store.save_pair
        count = 0

        def fail_second(store, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic interrupted adoption")
            return original(store, *args, **kwargs)

        with patch.object(Store, "save_pair", fail_second):
            with self.assertRaises(OSError):
                apply_plan(self.profile, cfg, self.api)
        self.assertFalse((self.profile / cfg["state_db"]).exists())
        self.assertEqual(apply_plan(self.profile, cfg, self.api)["pairs"], 2)

    def test_paused_initial_collision_can_be_resolved_without_reset(self):
        n = self.cloud("Note", "Cloud version\n")
        (self.vault / "Note.md").write_text("Local version\n", encoding="utf-8")
        cfg = self.cfg("both")
        make_plan(self.profile, cfg, self.api)
        apply_plan(self.profile, cfg, self.api)
        self.api.assert_scope = lambda token: self.assertEqual(token, n["node_token"])
        result = keep_both(self.profile, cfg, self.api, n["obj_token"])
        self.assertEqual(result["content_writes"], 0)
        self.assertTrue(keep_both(self.profile, cfg, self.api, n["obj_token"])["already_resolved"])
        cfg["production_writes_enabled"] = True
        e = Bridge(self.profile, engine_config(cfg), self.api)
        try:
            r = e.run(True)
            self.assertFalse(r["paused"], r)
            self.assertIn("Local version", (self.vault / "Note.md").read_text(encoding="utf-8"))
            self.assertIn(
                "Cloud version", (self.vault / result["local_alias"]).read_text(encoding="utf-8")
            )
        finally:
            e.close()


if __name__ == "__main__":
    unittest.main()
