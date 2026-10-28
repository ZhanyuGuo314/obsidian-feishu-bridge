"""Conservative public-export check for a checkout, wheel or source archive.

This is an export guard, not proof that arbitrary text has no personal data.
Runtime profiles must stay outside the repository. Review the file manifest too.
"""

from __future__ import annotations

import argparse
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "windows_user_home": re.compile(r"(?i)[a-z]:[\\/]+Users[\\/]+[^\\/\s\"']+"),
    "posix_user_home": re.compile(r"/(?:Users|home)/[^/\s\"']+"),
    "personal_email_address": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    "bearer_credential": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{24,}"),
    "github_credential": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b"
    ),
    "model_api_credential": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "assigned_credential": re.compile(
        r"""(?i)(?:access_token|refresh_token|app_secret|client_secret)\s*[=:]\s*["']([^"']{12,})["']"""
    ),
    "live_cloud_resource_url": re.compile(
        r"https?://[^\s\"'<>]+/(?:wiki|docx|docs|sheets|file|base|bitable|slides|mindnotes|board|whiteboard)/[A-Za-z0-9]{20,}"
    ),
    "assigned_cloud_identifier": re.compile(
        r"""(?i)\b(?:node_token|obj_token|doc_token|file_token|wiki_token|parent_node_token|root_node|document_id)["']?\s*[:=]\s*["'][A-Za-z0-9]{20,}["']"""
    ),
    "live_numeric_space_identifier": re.compile(
        r"""\b(?:space_id|target_space_id)["']?\s*[:=]\s*["']\d{16,}["']"""
    ),
}
PRIVATE_PARTS = {
    "state",
    "profiles",
    "private",
    "local-runs",
    ".ofb",
    ".obsidian",
    ".obsidian-feishu-bridge",
    ".git",
    "__pycache__",
}
FORBIDDEN_SUFFIXES = {".sqlite", ".db", ".exe", ".dll", ".pdf", ".png", ".jpg", ".jpeg", ".pyc"}


def inspect(name, data):
    path = PurePosixPath(name.replace("\\", "/"))
    issues = []
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", str(path)):
        issues.append("unsafe_archive_path")
    if PRIVATE_PARTS.intersection(part.lower() for part in path.parts):
        issues.append("runtime_or_private_directory")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or any(
        x in path.name for x in (".sqlite-", ".db-")
    ):
        issues.append("runtime_binary_or_attachment")
    if (
        path.name.lower().startswith(".env.") and path.name.lower() != ".env.example"
    ) or path.name.lower() in {
        "config.json",
        "config.local.json",
        ".env",
        "setup-plan.json",
        "live-control.json",
    }:
        issues.append("runtime_configuration")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        issues.append("unexpected_binary_file")
        return issues
    for label, regex in PATTERNS.items():
        if regex.search(text):
            issues.append(label)
    return issues


def checkout_files(root):
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
    )
    if result.returncode:
        raise ValueError("run checkout export checks from a Git working tree")
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8")
        path = root / name
        if path.is_symlink():
            raise ValueError("public_export_contains_symlink:" + name)
        yield name, path.read_bytes()


def archive_files(path):
    if path.suffix == ".whl" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for item in z.infolist():
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("archive_symlink_is_not_allowed")
                if not item.is_dir():
                    yield item.filename, z.read(item)
    else:
        with tarfile.open(path) as tar:
            for item in tar.getmembers():
                if item.issym() or item.islnk():
                    raise ValueError("archive_symlink_is_not_allowed")
                if item.isfile():
                    yield item.name, tar.extractfile(item).read()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--archive", type=Path)
    args = p.parse_args()
    entries = archive_files(args.archive) if args.archive else checkout_files(ROOT)
    errors = []
    count = 0
    for name, data in entries:
        count += 1
        labels = inspect(name, data)
        if labels:
            errors.append((name, labels))
    if errors:
        for name, labels in errors:
            print(name + ": " + ", ".join(labels))
        raise SystemExit(1)
    print(
        f"Public export check passed for {count} files; manual manifest review is still required."
    )


if __name__ == "__main__":
    main()
