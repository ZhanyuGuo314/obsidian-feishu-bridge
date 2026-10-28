import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from obsidian_feishu_bridge.cli import execute, parser
from obsidian_feishu_bridge.runtime import APIError


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.root = self.home / "profiles/default"
        (self.root / "state").mkdir(parents=True)
        self.args = parser().parse_args(
            ["--data-home", str(self.home), "watch", "--approve", "--interval", "15"]
        )

    def tearDown(self):
        self.temp.cleanup()

    def noop(self, *args, **kwargs):
        (self.root / "state/latest.json").write_text(
            json.dumps({"writes": 0, "synced": [], "paused": []}), encoding="utf-8"
        )
        return 0

    def test_unchanged_polling_is_quiet(self):
        output = io.StringIO()
        with (
            patch("obsidian_feishu_bridge.cli.load_profile", return_value={}),
            patch("obsidian_feishu_bridge.cli.run_once", side_effect=self.noop),
            patch("obsidian_feishu_bridge.cli.time.sleep", side_effect=[None, KeyboardInterrupt]),
            redirect_stdout(output),
        ):
            with self.assertRaises(KeyboardInterrupt):
                execute(self.args)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["status"], "watching")

    def test_network_failure_retries_and_recovers(self):
        count = 0

        def step(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise APIError("TimeoutExpired")
            return self.noop()

        with (
            patch("obsidian_feishu_bridge.cli.load_profile", return_value={}),
            patch("obsidian_feishu_bridge.cli.run_once", side_effect=step),
            patch(
                "obsidian_feishu_bridge.cli.time.sleep", side_effect=[None, KeyboardInterrupt]
            ) as sleep,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                execute(self.args)
        self.assertEqual(count, 2)
        self.assertEqual(sleep.call_args_list[0].args, (15,))

    def test_authorization_failure_stops_without_repeated_login(self):
        with (
            patch("obsidian_feishu_bridge.cli.load_profile", return_value={}),
            patch(
                "obsidian_feishu_bridge.cli.run_once", side_effect=APIError("authorization failed")
            ),
            patch("obsidian_feishu_bridge.cli.time.sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(APIError):
                execute(self.args)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
