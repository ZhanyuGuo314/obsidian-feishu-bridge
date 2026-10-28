import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CLIContractTests(unittest.TestCase):
    def invoke(self, *arguments, cwd=None, env=None):
        return subprocess.run(
            [sys.executable, "-m", "obsidian_feishu_bridge", *arguments],
            cwd=cwd,
            env=env,
            capture_output=True,
            encoding="utf-8",
        )

    def test_help_from_unrelated_directory_without_forced_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ)
            env.pop("PYTHONUTF8", None)
            env.pop("PYTHONIOENCODING", None)
            result = self.invoke("--help", cwd=folder, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("bootstrap", result.stdout)
            self.assertIn("sync", result.stdout)

    def test_missing_profile_is_an_actionable_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.invoke("--data-home", folder, "doctor")
            self.assertEqual(result.returncode, 2)
            message = json.loads(result.stderr)
            self.assertEqual(message["status"], "failed")

    def test_profile_traversal_does_not_touch_arbitrary_directories(self):
        result = self.invoke("--profile", "../outside", "doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid_profile_name", result.stderr)

    def test_profiles_can_be_discovered_without_old_chat_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "profiles/demo"
            root.mkdir(parents=True)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "vault_root": "/synthetic/vault",
                        "mappings": [],
                        "production_writes_enabled": False,
                    }
                ),
                encoding="utf-8",
            )
            result = self.invoke("--data-home", folder, "profiles")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["profiles"][0]["name"], "demo")

    def test_packaged_skill_installs_without_touching_existing_skills(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "skill"
            result = self.invoke("install-skill", "--output", str(destination))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "name: obsidian-feishu-sync", (destination / "SKILL.md").read_text(encoding="utf-8")
            )
            second = self.invoke("install-skill", "--output", str(destination))
            self.assertEqual(second.returncode, 2)
            self.assertIn("skill_destination_exists", second.stderr)


if __name__ == "__main__":
    unittest.main()
