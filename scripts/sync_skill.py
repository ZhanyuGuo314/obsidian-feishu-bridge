"""Keep the wheel's embedded skill identical to the GitHub skill directory."""

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/obsidian-feishu-sync"
TARGET = ROOT / "src/obsidian_feishu_bridge/skilldata"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = {p.relative_to(SOURCE) for p in SOURCE.rglob("*") if p.is_file()}
    actual = (
        {p.relative_to(TARGET) for p in TARGET.rglob("*") if p.is_file()}
        if TARGET.exists()
        else set()
    )
    if args.check:
        if expected != actual or any(
            (SOURCE / p).read_bytes() != (TARGET / p).read_bytes() for p in expected
        ):
            raise SystemExit("Embedded skill differs; run python scripts/sync_skill.py")
    else:
        TARGET.mkdir(parents=True, exist_ok=True)
        for rel in expected:
            (TARGET / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / rel, TARGET / rel)
    print("Skill files are synchronized.")


if __name__ == "__main__":
    main()
