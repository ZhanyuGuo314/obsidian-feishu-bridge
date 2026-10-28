import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as E
import zlib
from copy import deepcopy
from pathlib import Path

from test_engine import FakeAPI

from obsidian_feishu_bridge.bootstrap import apply_plan, make_plan
from obsidian_feishu_bridge.bridge import Bridge
from obsidian_feishu_bridge.codec import hash_bytes
from obsidian_feishu_bridge.config import create_config, engine_config


def tiny_png(red):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
        + chunk(b"IEND", b"")
    )


class AssetAPI(FakeAPI):
    def __init__(self, root):
        super().__init__()
        self.root = root
        self.uploads = 0
        self.downloads = 0
        self.media = {}
        self.two_phase = False
        self.concurrent_placeholder_text = False

    def update(self, token, revision, command, content, block="-1"):
        before_tokens = {
            n.get("src")
            for n in E.fromstring("<r>" + self.docs[token]["content"] + "</r>").iter("img")
        }
        result = super().update(token, revision, command, content, block)
        if self.two_phase and "<img" in content:
            final = deepcopy(self.docs[token])
            placeholder = E.fromstring("<r>" + final["content"] + "</r>")
            for n in placeholder.iter("img"):
                if n.get("src") not in before_tokens:
                    n.attrib.pop("src", None)
            if self.concurrent_placeholder_text:
                E.SubElement(placeholder, "p").text = "Unrelated concurrent content"
            interim = dict(
                final, content="".join(E.tostring(n, encoding="unicode") for n in placeholder)
            )
            self.history[(token, final["revision_id"])] = interim
            final["revision_id"] += 1
            self.docs[token] = final
            self.history[(token, final["revision_id"])] = deepcopy(final)
        return result

    def _id(self, xml):
        root = E.fromstring("<r>" + super()._id(xml) + "</r>")
        for n in root.iter("img"):
            if n.get("path"):
                data = (self.root / n.attrib.pop("path").removeprefix("@./")).read_bytes()
                token = "fake-media-" + hash_bytes(data)
                self.media[token] = data
                n.set("src", token)
        return "".join(E.tostring(n, encoding="unicode") for n in root)

    def call(self, args, stdin=None):
        if args[:2] == ["drive", "+upload"]:
            self.uploads += 1
            data = (self.root / args[args.index("--file") + 1]).read_bytes()
            parent = args[args.index("--wiki-token") + 1]
            title = args[args.index("--name") + 1]
            node = self.create("fake-space", parent, title)
            node["obj_type"] = "file"
            self.media[node["obj_token"]] = data
            return {"file_token": node["obj_token"]}
        if args[:2] == ["docs", "+media-download"]:
            self.downloads += 1
            token = args[args.index("--token") + 1]
            path = self.root / args[args.index("--output") + 1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.media[token])
            return {"complete": True}
        raise AssertionError("unexpected fake API command: " + repr(args))


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.profile = self.base / "profile"
        self.profile.mkdir()
        self.api = AssetAPI(self.profile)
        self.api.writes = True
        self.cfg = create_config(
            self.vault,
            sys.executable,
            [{"local_prefix": "", "space_id": "fake-space", "root_node": "fake-root"}],
            "local",
        )

    def tearDown(self):
        self.temp.cleanup()

    def adopt(self):
        make_plan(self.profile, self.cfg, self.api)
        apply_plan(self.profile, self.cfg, self.api)
        self.cfg["production_writes_enabled"] = True

    def sync(self):
        e = Bridge(self.profile, engine_config(self.cfg), self.api)
        try:
            result = e.run(True)
            self.assertFalse(result["paused"], result)
            return result
        finally:
            e.close()

    def test_pdf_uploaded_once_and_never_downloaded(self):
        (self.vault / "Paper.pdf").write_bytes(b"%PDF-1.4 synthetic payload")
        (self.vault / "Note.md").write_text("[Paper](Paper.pdf)\n", encoding="utf-8")
        self.adopt()
        self.sync()
        self.assertEqual(self.api.uploads, 1)
        self.assertEqual(self.api.downloads, 0)
        self.assertEqual(self.sync()["writes"], 0)
        self.assertEqual(self.api.uploads, 1)

    def test_shared_pdf_change_updates_both_owners_with_one_new_upload(self):
        source = self.vault / "Paper.pdf"
        source.write_bytes(b"%PDF-1.4 first version")
        for name in ["A.md", "B.md"]:
            (self.vault / name).write_text("[Paper](Paper.pdf)\n", encoding="utf-8")
        self.adopt()
        self.sync()
        self.assertEqual(self.api.uploads, 1)
        source.write_bytes(b"%PDF-1.4 second version")
        self.sync()
        self.assertEqual(self.api.uploads, 2)
        self.assertEqual(self.sync()["writes"], 0)

    def test_image_binary_change_does_not_require_editing_markdown(self):
        source = self.vault / "pixel.png"
        source.write_bytes(tiny_png(1))
        (self.vault / "Note.md").write_text("![Pixel](pixel.png)\n", encoding="utf-8")
        self.adopt()
        self.sync()
        self.assertGreater(self.api.downloads, 0)
        source.write_bytes(tiny_png(2))
        self.sync()
        self.assertEqual(self.sync()["writes"], 0)

    def test_cloud_image_uses_generic_vault_attachment_directory(self):
        self.cfg["bootstrap_mode"] = "cloud"
        node = self.api.create("fake-space", "fake-root", "Incoming")
        image = tiny_png(4)
        self.api.media["fake-cloud-media"] = image
        self.api.update(node["obj_token"], 0, "block_insert_after", '<img src="fake-cloud-media"/>')
        self.adopt()
        self.sync()
        relative = "_attachments/feishu/" + hash_bytes(image) + ".png"
        self.assertEqual((self.vault / relative).read_bytes(), image)
        self.assertIn(relative, (self.vault / "Incoming.md").read_text(encoding="utf-8"))
        self.assertEqual(self.sync()["writes"], 0)

    def test_cli_image_placeholder_revision_is_verified_as_own_step(self):
        (self.vault / "pixel.png").write_bytes(tiny_png(3))
        (self.vault / "Note.md").write_text("![Pixel](pixel.png)\n", encoding="utf-8")
        self.adopt()
        self.api.two_phase = True
        self.sync()
        self.assertEqual(self.sync()["writes"], 0)

    def test_image_revision_does_not_hide_unrelated_concurrent_content(self):
        (self.vault / "pixel.png").write_bytes(tiny_png(3))
        (self.vault / "Note.md").write_text("![Pixel](pixel.png)\n", encoding="utf-8")
        self.adopt()
        self.api.two_phase = True
        self.api.concurrent_placeholder_text = True
        e = Bridge(self.profile, engine_config(self.cfg), self.api)
        try:
            result = e.run(True)
            self.assertTrue(result["paused"])
            self.assertTrue(any("version_gap" in r["reason"] for r in result["paused"]))
            files = list((self.profile / "state/operations").rglob("intervening-revision-*.json"))
            self.assertTrue(
                any("Unrelated concurrent content" in p.read_text(encoding="utf-8") for p in files)
            )
        finally:
            e.close()


if __name__ == "__main__":
    unittest.main()
