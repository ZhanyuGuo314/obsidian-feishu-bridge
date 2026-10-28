import tempfile
import unittest
import uuid
import xml.etree.ElementTree as E
from copy import deepcopy
from pathlib import Path

from obsidian_feishu_bridge.codec import Unsupported, frontmatter, hash_bytes, render, semantic
from obsidian_feishu_bridge.engine import SyncEngine


class FakeAPI:
    def __init__(self):
        self.docs = {}
        self.nodes = {}
        self.calls = []
        self.writes = False
        self.fail_after_write = False
        self.drop_text = False
        self.history = {}

    def _id(self, xml):
        r = E.fromstring("<r>" + xml + "</r>")
        for n in r.iter():
            if n.tag != "r" and not n.get("id"):
                n.set("id", uuid.uuid4().hex)
        return "".join(E.tostring(n, encoding="unicode") for n in r)

    def children(self, space, parent=""):
        return [dict(n) for n in self.nodes.values() if n["parent_node_token"] == parent]

    def create(self, space, parent, title):
        assert self.writes
        token = uuid.uuid4().hex
        node = "node" + token
        self.nodes[node] = {
            "node_token": node,
            "obj_token": token,
            "obj_type": "docx",
            "parent_node_token": parent,
            "space_id": space,
            "title": title,
        }
        self.docs[token] = {
            "document_id": token,
            "revision_id": 0,
            "content": "",
            "reference_map": {},
        }
        return self.nodes[node]

    def fetch(self, token):
        return deepcopy(self.docs[token])

    def fetch_revision(self, token, revision):
        return deepcopy(self.history[(token, revision)])

    def rename(self, node, title):
        assert self.writes
        self.nodes[node]["title"] = title

    def metas(self, objects):
        return {x["doc_token"]: str(self.docs[x["doc_token"]]["revision_id"]) for x in objects}

    def update(self, token, revision, command, content, block="-1"):
        assert self.writes
        doc = self.docs[token]
        if revision != doc["revision_id"]:
            raise RuntimeError("version conflict")
        root = E.fromstring("<r>" + doc["content"] + "</r>")
        nodes = list(E.fromstring("<r>" + self._id(content) + "</r>"))
        if self.drop_text:
            nodes = []
        if command == "block_replace":
            idx = next(i for i, n in enumerate(root) if n.get("id") == block)
            root.remove(root[idx])
        elif block in ("-1", "0"):
            idx = len(root) if block == "-1" else 0
        else:
            idx = next(i + 1 for i, n in enumerate(root) if n.get("id") == block)
        for i, n in enumerate(nodes):
            root.insert(idx + i, n)
        doc["content"] = "".join(E.tostring(n, encoding="unicode") for n in root)
        doc["revision_id"] += 1
        self.history[(token, doc["revision_id"])] = deepcopy(doc)
        if self.fail_after_write:
            self.fail_after_write = False
            raise OSError("simulated timeout after mutation")
        return {"result": "success"}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        (self.vault / "Sample Notes").mkdir(parents=True)
        self.cfg = {
            "vault_root": str(self.vault),
            "state_db": "state/test.sqlite",
            "spaces": [{"name": "Sample Notes", "space_id": "space", "root_node": "root"}],
            "production_writes_enabled": True,
            "llm_enabled": False,
            "sweep_seconds": 0,
            "discovery_seconds": 999999999,
        }
        self.api = FakeAPI()
        self.api.writes = True
        self.e = SyncEngine(self.root, self.cfg, self.api)
        self.e.store.set("discovered_at", 99999999999)
        self.e.store.db.execute("INSERT INTO dirs VALUES(?,?,?)", ("Sample Notes", "root", "space"))
        self.e.store.db.commit()

    def tearDown(self):
        self.e.close()
        self.temp.cleanup()

    def seed(self, text="# 标题\n\n原文 **bold**\n"):
        node = self.api.create("space", "root", "note")
        token = node["obj_token"]
        self.api.update(token, 0, "block_insert_after", "".join(render(text)))
        p = {
            "id": "pair",
            "path": "Sample Notes/note.md",
            "doc": token,
            "node": node["node_token"],
            "space": "space",
            "parent": "root",
        }
        (self.vault / p["path"]).write_bytes(text.encode())
        self.e.store.save_pair(p, text.encode(), self.api.fetch(token), "1")
        return p

    def run_sync(self):
        self.e.report = {
            "at": "test",
            "synced": [],
            "paused": [],
            "deferred": [],
            "unchanged": 0,
            "reads": 0,
            "writes": 0,
            "llm_calls": 0,
            "pdf_downloads": 0,
        }
        return self.e.run(True)

    def test_new_create_and_no_duplicate(self):
        (self.vault / "Sample Notes/new.md").write_text("新文档\n", encoding="utf-8")
        self.run_sync()
        r = self.run_sync()
        self.assertFalse(r["paused"])
        self.assertEqual(len(self.api.docs), 1)
        self.run_sync()
        self.assertEqual(len(self.api.docs), 1)

    def test_push_replacement_table_and_formula(self):
        base = "正文\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n公式 $x^2$\n"
        p = self.seed(base)
        (self.vault / p["path"]).write_text(
            base.replace("| 1 | 2 |", "| 1 | 9 |"), encoding="utf-8"
        )
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        self.assertIn("9", self.api.fetch(p["doc"])["content"])
        self.assertFalse(self.run_sync()["synced"])

    def test_cloud_pull_and_no_echo(self):
        p = self.seed("原文\n")
        doc = self.api.fetch(p["doc"])
        self.api.update(p["doc"], doc["revision_id"], "block_insert_after", "<p>云端新增</p>")
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        self.assertIn("云端新增", (self.vault / p["path"]).read_text(encoding="utf-8"))
        self.assertFalse(self.run_sync()["synced"])

    def test_conflict_isolated(self):
        p = self.seed()
        (self.vault / p["path"]).write_text("本地冲突\n", encoding="utf-8")
        d = self.api.fetch(p["doc"])
        self.api.update(p["doc"], d["revision_id"], "block_insert_after", "<p>云端冲突</p>")
        (self.vault / "Sample Notes/new.md").write_text("另一篇\n", encoding="utf-8")
        self.run_sync()
        r = self.run_sync()
        self.assertEqual(len(r["paused"]), 1)
        self.assertEqual(len(self.api.docs), 2)
        self.assertTrue((self.root / "state/conflicts").is_dir())

    def test_delete_never_propagated(self):
        p = self.seed()
        (self.vault / p["path"]).unlink()
        r = self.run_sync()
        self.assertEqual(len(self.api.docs), 1)
        self.assertIn("no_delete", r["paused"][0]["reason"])

    def test_unknown_native_preserved(self):
        p = self.seed()
        d = self.api.docs[p["doc"]]
        d["content"] += '<whiteboard token="board"/>'
        d["revision_id"] += 1
        r = self.run_sync()
        self.assertTrue(r["paused"])
        self.assertIn("whiteboard", self.api.docs[p["doc"]]["content"])

    def test_lost_response_recovery_no_repeat(self):
        p = self.seed("原文\n")
        (self.vault / p["path"]).write_text("原文\n\n新增一次\n", encoding="utf-8")
        self.api.fail_after_write = True
        r = self.run_sync()
        self.assertTrue(r["paused"])
        self.run_sync()
        content = self.api.fetch(p["doc"])["content"]
        self.assertEqual(content.count("新增一次"), 1)

    def test_bad_readback_not_committed(self):
        p = self.seed("原文\n")
        (self.vault / p["path"]).write_text("原文\n\n新增\n", encoding="utf-8")
        self.api.drop_text = True
        r = self.run_sync()
        self.assertTrue(r["paused"])
        pair = self.e.store.pairs()[0]
        self.assertEqual(frontmatter(pair["local"].decode())[1], "原文\n")

    def test_local_change_between_plan_and_write_stops(self):
        p = self.seed("原文\n")
        local = "原文\n\n新增\n".encode()
        (self.vault / p["path"]).write_bytes(local)
        p = self.e.store.pairs()[0]
        d = self.api.fetch(p["doc"])
        patch = self.e.patch_plan(p, local, d)[0]
        (self.vault / p["path"]).write_text("编辑器又保存\n", encoding="utf-8")
        with self.assertRaises(Unsupported):
            self.e.execute_patch(p, local, d, patch, "1")
        self.assertEqual(self.api.fetch(p["doc"])["revision_id"], 1)

    def test_codec_keeps_format_and_fails_on_loss(self):
        a = "".join(render("段落 **粗体** 与 ` a  b `\n\n$$x^2 + y$$\n\n```python\nx = 1\n```\n"))
        self.assertIn("<p>", a)
        self.assertNotIn("<paragraph>", a)
        self.assertIn("<b>粗体</b>", a)
        self.assertNotEqual(semantic(a), semantic(a.replace("粗体", "损失")))

    def test_multiple_hunks_all_commit_once(self):
        old = "第一段\n\n中间段\n\n最后段\n"
        p = self.seed(old)
        (self.vault / p["path"]).write_text(
            old.replace("第一段", "改第一段").replace("最后段", "改最后段"), encoding="utf-8"
        )
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        self.assertEqual(r["writes"], 2)
        self.assertFalse(self.run_sync()["synced"])

    def test_check_does_not_acknowledge_pending_cloud_change(self):
        p = self.seed("原文\n")
        d = self.api.fetch(p["doc"])
        self.api.update(p["doc"], d["revision_id"], "block_insert_after", "<p>远端变化</p>")
        self.e.apply_enabled = False
        self.e.run(False)
        self.assertEqual((self.vault / p["path"]).read_text(encoding="utf-8"), "原文\n")
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        self.assertIn("远端变化", (self.vault / p["path"]).read_text(encoding="utf-8"))

    def test_headerless_rows_preserve_literal_not_guess_table(self):
        p = self.seed("原文\n")
        new = "原文\n\n| a | **1** |\n| b | 2 |\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n"
        (self.vault / p["path"]).write_text(new, encoding="utf-8")
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        doc = self.api.fetch(p["doc"])
        self.assertEqual(doc["content"].count("<table"), 1)
        self.assertIn("| a |", doc["content"])

    def test_new_directory_created_once(self):
        (self.vault / "Sample Notes/新目录").mkdir()
        (self.vault / "Sample Notes/新目录/new.md").write_text("正文\n", encoding="utf-8")
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        count = len(self.api.docs)
        self.run_sync()
        self.assertEqual(count, len(self.api.docs))

    def test_recover_local_write_after_crash_before_baseline(self):
        from obsidian_feishu_bridge.runtime import atomic, dump

        p = self.seed("原文\n")
        pair = self.e.store.pairs()[0]
        doc = self.api.fetch(p["doc"])
        doc["content"] += "<p>云端新段</p>"
        doc["revision_id"] += 1
        self.api.docs[p["doc"]] = doc
        data = "原文\n\n云端新段\n".encode()
        folder = self.root / "state/operations/crashpull"
        folder.mkdir(parents=True)
        atomic(folder / "cloud.json", dump(doc).encode())
        self.e.store.op(
            "crashpull",
            p["id"],
            "pull",
            "prepared",
            {
                "path": p["path"],
                "before_hash": hash_bytes(pair["local"]),
                "after_hash": hash_bytes(data),
            },
        )
        (self.vault / p["path"]).write_bytes(data)
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        self.assertEqual(frontmatter(self.e.store.pairs()[0]["local"].decode())[1], data.decode())

    def test_same_directory_rename_keeps_pair_and_no_duplicate(self):
        p = self.seed("原文\n")
        self.run_sync()
        old = self.vault / p["path"]
        new = old.with_name("改名.md")
        old.rename(new)
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        self.assertEqual(len(self.api.docs), 1)
        self.assertEqual(self.e.store.pairs()[0]["path"], "Sample Notes/改名.md")

    def test_duplicate_identity_preserves_both(self):
        p = self.seed("原文\n")
        self.run_sync()
        old = self.vault / p["path"]
        (old.parent / "副本.md").write_bytes(old.read_bytes())
        r = self.run_sync()
        self.assertTrue(r["paused"])
        self.assertEqual(len(self.api.docs), 1)
        self.assertTrue((old.parent / "副本.md").exists())

    def test_server_accepts_stale_replace_but_history_is_preserved(self):
        p = self.seed("原文A\n")
        (self.vault / p["path"]).write_text("本地C\n", encoding="utf-8")
        real = self.api.update

        def race(token, revision, command, content, block="-1"):
            d = self.api.docs[token]
            d["content"] = d["content"].replace("原文A", "并发B")
            d["revision_id"] += 1
            self.api.history[(token, d["revision_id"])] = deepcopy(d)
            return real(token, d["revision_id"], command, content, block)

        self.api.update = race
        r = self.run_sync()
        self.assertTrue(r["paused"])
        self.assertIn("version_gap", r["paused"][0]["reason"])
        files = list((self.root / "state/operations").rglob("intervening-revision-*.json"))
        self.assertTrue(any("并发B" in f.read_text(encoding="utf-8") for f in files))
        self.assertIn("原文A", self.e.store.pairs()[0]["local"].decode())

    def test_cloud_pull_preserves_local_removed_source_links(self):
        p = self.seed("正文\n")
        remote = self.api.docs[p["doc"]]
        remote["content"] += '<p>来源 <a href="https://example.invalid/old">原文</a></p>'
        remote["revision_id"] += 1
        pair = self.e.store.pairs()[0]
        self.e.store.save_pair(pair, pair["local"], self.api.fetch(p["doc"]))
        remote["content"] += "<p>云端新增正文</p>"
        remote["revision_id"] += 1
        r = self.run_sync()
        self.assertFalse(r["paused"], r)
        text = (self.vault / p["path"]).read_text(encoding="utf-8")
        self.assertIn("云端新增正文", text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("来源", text)

    def test_native_resource_changes_remain_pending(self):
        n = self.api.create("space", "root", "原生资源")
        self.e.store.db.execute(
            "INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
            (n["obj_token"], n["node_token"], "docx", "space", "root", "原生资源", "retained"),
        )
        self.e.store.db.commit()
        self.run_sync()
        self.api.update(n["obj_token"], 0, "block_insert_after", "<p>原生改动</p>")
        r = self.run_sync()
        self.assertTrue(r["paused"])
        self.assertTrue(self.run_sync()["paused"])

    def test_binary_asset_change_not_silently_ignored(self):
        p = self.vault / "Sample Notes/image.png"
        p.write_bytes(b"original")
        self.e.store.set(
            "local_assets", [{"path": "Sample Notes/image.png", "sha256": hash_bytes(b"original")}]
        )
        p.write_bytes(b"changed")
        r = self.run_sync()
        self.assertTrue(r["paused"])
        self.assertEqual(p.read_bytes(), b"changed")

    def test_large_version_gap_always_saves_latest_intervening_state(self):
        p = self.seed("原文\n")
        folder = self.root / "state/operations/large-gap"
        folder.mkdir(parents=True)
        for revision in range(2, 32):
            self.api.history[(p["doc"], revision)] = {
                "revision_id": revision,
                "content": "<p>" + ("最后并发版本" if revision == 31 else "中间") + "</p>",
            }
        with self.assertRaises(Unsupported):
            self.e.preserve_version_gap(p, "large-gap", {}, 1, 32)
        self.assertIn(
            "最后并发版本", (folder / "intervening-revision-31.json").read_text(encoding="utf-8")
        )

    def test_both_sides_converged_content_commits_without_write(self):
        p = self.seed("Original\n")
        (self.vault / p["path"]).write_text("Same new content\n", encoding="utf-8")
        d = self.api.docs[p["doc"]]
        d["content"] = d["content"].replace("Original", "Same new content")
        d["revision_id"] += 1
        result = self.run_sync()
        self.assertFalse(result["paused"], result)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["synced"][0]["action"], "converged_without_write")

    def test_one_metadata_denial_does_not_stop_other_notes(self):
        p = self.seed("Unavailable note\n")
        healthy = self.vault / "Sample Notes/Healthy.md"
        healthy.write_text("Healthy\n", encoding="utf-8")
        self.run_sync()
        healthy.write_text("Healthy\n\nNew healthy text\n", encoding="utf-8")
        original = self.api.metas

        def partial(objects):
            values = original(objects)
            self.api.meta_failures = {p["doc"]: "denied"}
            return {k: v for k, v in values.items() if k != p["doc"]}

        self.api.metas = partial
        result = self.run_sync()
        self.assertEqual(len(result["paused"]), 1)
        self.assertTrue(any(x["path"].endswith("Healthy.md") for x in result["synced"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
