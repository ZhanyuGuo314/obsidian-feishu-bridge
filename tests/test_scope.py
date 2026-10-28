import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from obsidian_feishu_bridge.runtime import APIError, Lark


class ScopedAPI(Lark):
    def __init__(self, root):
        super().__init__(
            {
                "cli_path": "unused-fake-executable",
                "identity": "user",
                "mappings": [{"space_id": "space", "root_node": "scope-root"}],
            },
            root,
            True,
        )
        self.nodes = {
            "scope-root": {
                "node_token": "scope-root",
                "space_id": "space",
                "parent_node_token": "space-root",
            },
            "inside": {
                "node_token": "inside",
                "space_id": "space",
                "parent_node_token": "scope-root",
            },
            "outside": {
                "node_token": "outside",
                "space_id": "space",
                "parent_node_token": "space-root",
            },
            "space-root": {
                "node_token": "space-root",
                "space_id": "space",
                "parent_node_token": "",
            },
        }

    def call(self, args, stdin=None):
        if args[:2] == ["wiki", "+node-get"]:
            token = args[args.index("--node-token") + 1].rsplit("/", 1)[-1]
            return self.nodes[token]
        return super().call(args, stdin)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = ScopedAPI(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_parent_never_becomes_space_root_write(self):
        with patch("obsidian_feishu_bridge.runtime.subprocess.run") as process:
            with self.assertRaises(APIError):
                self.api.create("space", "", "Note")
            process.assert_not_called()

    def test_outside_node_cannot_be_updated(self):
        with patch("obsidian_feishu_bridge.runtime.subprocess.run") as process:
            with self.assertRaisesRegex(APIError, "outside_configured_subtree"):
                self.api.update("outside", 1, "block_insert_after", "<p>text</p>")
            process.assert_not_called()

    def test_root_body_is_protected_but_child_creation_is_allowed(self):
        with self.assertRaisesRegex(APIError, "root_body"):
            self.api.assert_scope("scope-root")
        self.api.assert_scope("scope-root", allow_root=True)

    def test_moved_node_does_not_keep_cached_write_permission(self):
        self.api.assert_scope("inside")
        self.api.nodes["inside"]["parent_node_token"] = "space-root"
        with self.assertRaisesRegex(APIError, "outside_configured_subtree"):
            self.api.assert_scope("inside")

    def test_inside_write_reaches_cli_after_scope_validation(self):
        result = SimpleNamespace(
            returncode=0, stdout='{"ok":true,"data":{"result":"success"}}', stderr=""
        )
        with patch("obsidian_feishu_bridge.runtime.subprocess.run", return_value=result) as process:
            self.api.update("inside", 1, "block_insert_after", "<p>text</p>")
            process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
