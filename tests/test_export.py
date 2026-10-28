import importlib.util
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_public.py"
spec = importlib.util.spec_from_file_location("ofb_export_check", SCRIPT)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class ExportTests(unittest.TestCase):
    def test_runtime_database_cannot_be_exported(self):
        self.assertIn("runtime_or_private_directory", export.inspect("STATE/sync.sqlite", b"data"))

    def test_cloud_identifier_is_rejected_even_in_documentation(self):
        url = "https://example.feishu.cn/wiki/" + "A" * 27
        self.assertIn("live_cloud_resource_url", export.inspect("README.md", url.encode()))

    def test_credential_assignment_is_rejected(self):
        text = 'access_token = "' + "x" * 30 + '"'
        self.assertIn("assigned_credential", export.inspect("example.py", text.encode()))

    def test_plain_synthetic_source_is_allowed(self):
        self.assertEqual(export.inspect("tests/example.py", b'node = "fake-node"\n'), [])

    def test_personal_paths_and_contacts_are_rejected(self):
        samples = [
            ("windows_user_home", "C:/" + "Users/" + "example/vault"),
            ("posix_user_home", "/home/" + "example/vault"),
            ("posix_user_home", "/Users/" + "example/vault"),
            ("personal_email_address", "person" + "@" + "example.com"),
        ]
        for category, value in samples:
            with self.subTest(category=category):
                self.assertIn(category, export.inspect("notes.md", value.encode()))

    def test_raw_identifiers_are_rejected_without_urls(self):
        token = "A" * 27
        samples = [
            ("assigned_cloud_identifier", 'root_node = "' + token + '"'),
            ("assigned_cloud_identifier", '{"doc_token": "' + token + '"}'),
            ("live_numeric_space_identifier", 'space_id = "' + "1" * 19 + '"'),
        ]
        for category, value in samples:
            with self.subTest(category=category):
                self.assertIn(category, export.inspect("example.json", value.encode()))

    def test_private_key_and_bearer_credentials_are_rejected(self):
        key = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        self.assertIn("private_key", export.inspect("example.txt", key.encode()))
        bearer = "Bearer " + "x" * 30
        self.assertIn("bearer_credential", export.inspect("example.txt", bearer.encode()))

    def test_environment_files_and_windows_archive_paths_are_rejected(self):
        self.assertIn("runtime_configuration", export.inspect(".env.production", b""))
        self.assertIn("unsafe_archive_path", export.inspect("C:/escape.txt", b""))
        self.assertIn("runtime_or_private_directory", export.inspect(".git/config", b""))

    def test_zip_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "release.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr(info, "outside")
            with self.assertRaisesRegex(ValueError, "archive_symlink"):
                list(export.archive_files(archive))


if __name__ == "__main__":
    unittest.main()
