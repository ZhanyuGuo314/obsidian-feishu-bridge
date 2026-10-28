"""Persistent state, process lock, and allowlisted Feishu adapter."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def guarded_local_write(path, data, expected):
    """Windows exclusive writer; caller has durably saved both byte sequences."""
    path = Path(path)
    handle = None
    if expected is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000, 0x1, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "local_file_busy")
    try:
        if path.read_bytes() != expected:
            raise RuntimeError("local_changed_during_write")
        if handle is None:
            atomic(path, data)
        else:
            import ctypes
            from ctypes import wintypes

            kernel.SetFilePointerEx.argtypes = [
                wintypes.HANDLE,
                ctypes.c_longlong,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel.WriteFile.argtypes = [
                wintypes.HANDLE,
                wintypes.LPCVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ]
            kernel.SetEndOfFile.argtypes = [wintypes.HANDLE]
            kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
            written = wintypes.DWORD()
            if not kernel.SetFilePointerEx(handle, 0, None, 0):
                raise OSError(ctypes.get_last_error(), "seek_failed")
            if not kernel.WriteFile(
                handle, data, len(data), ctypes.byref(written), None
            ) or written.value != len(data):
                raise OSError(ctypes.get_last_error(), "write_failed")
            if not kernel.SetEndOfFile(handle) or not kernel.FlushFileBuffers(handle):
                raise OSError(ctypes.get_last_error(), "flush_failed")
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._defer_commits = False
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pairs(id TEXT PRIMARY KEY,path TEXT UNIQUE,doc TEXT UNIQUE,node TEXT,space TEXT,parent TEXT,local BLOB,remote TEXT,meta TEXT,checked REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS objects(token TEXT PRIMARY KEY,node TEXT,kind TEXT,space TEXT,parent TEXT,title TEXT,role TEXT);
        CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY,node TEXT,space TEXT);
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,pair_id TEXT,kind TEXT,status TEXT,payload TEXT,updated TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT,kind TEXT,data TEXT);
        """)
        self.db.commit()

    def pairs(self):
        return [dict(x) for x in self.db.execute("SELECT * FROM pairs ORDER BY path")]

    def commit(self):
        if not self._defer_commits:
            self.db.commit()

    @contextmanager
    def transaction(self):
        if self._defer_commits:
            raise RuntimeError("nested_store_transaction")
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        self._defer_commits = True
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self._defer_commits = False

    def setting(self, k, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, dump(v)))
        self.commit()

    def event(self, kind, data):
        self.db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (now(), kind, dump(data)))
        self.commit()

    def op(self, identifier, pair, kind, status, payload):
        self.db.execute(
            "INSERT OR REPLACE INTO operations VALUES(?,?,?,?,?,?)",
            (identifier, pair, kind, status, dump(payload), now()),
        )
        self.commit()

    def save_pair(self, p, local, remote, meta=None, settings=None):
        self.db.execute(
            """INSERT INTO pairs(id,path,doc,node,space,parent,local,remote,meta,checked) VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET path=excluded.path,doc=excluded.doc,node=excluded.node,space=excluded.space,parent=excluded.parent,local=excluded.local,remote=excluded.remote,meta=excluded.meta,checked=excluded.checked""",
            (
                p["id"],
                p["path"],
                p["doc"],
                p.get("node"),
                p["space"],
                p.get("parent"),
                local,
                dump(remote),
                meta,
                time.time(),
            ),
        )
        if p["doc"]:
            self.db.execute(
                "INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?,?)",
                (
                    p["doc"],
                    p.get("node"),
                    "docx",
                    p["space"],
                    p.get("parent"),
                    Path(p["path"]).stem,
                    "paired",
                ),
            )
        for key, value in (settings or {}).items():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dump(value)))
        self.commit()


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    f.seek(0)
    f.write(b"0")
    f.flush()
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError("another_sync_process_is_running")
    try:
        yield
    finally:
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


class APIError(RuntimeError):
    pass


class Lark:
    READ = {
        ("docs", "+fetch"),
        ("wiki", "+node-list"),
        ("wiki", "+node-get"),
        ("drive", "metas", "batch_query"),
        ("docs", "+media-download"),
    }
    WRITE = {
        ("docs", "+update"),
        ("wiki", "+node-create"),
        ("drive", "+update-title"),
        ("drive", "+upload"),
    }

    def __init__(self, cfg, root, writes=False):
        self.cfg = cfg
        self.root = Path(root)
        self.writes = writes
        self.calls = []
        self.meta_failures = {}

    def call(self, args, stdin=None):
        verb = tuple(args[:3]) if args[:2] == ["drive", "metas"] else tuple(args[:2])
        if verb not in self.READ | self.WRITE:
            raise APIError("command_not_allowed:" + str(verb))
        if verb in self.WRITE and not self.writes:
            raise APIError("writes_not_authorized")
        if verb in self.WRITE and self.cfg.get("mappings"):

            def value(flag):
                return args[args.index(flag) + 1] if flag in args else None

            if verb == ("wiki", "+node-create"):
                self.assert_scope(
                    value("--parent-node-token"), value("--space-id"), allow_root=True
                )
            elif verb == ("docs", "+update"):
                self.assert_scope(value("--doc"))
            elif verb == ("drive", "+upload"):
                self.assert_scope(value("--wiki-token"), allow_root=True)
            elif verb == ("drive", "+update-title"):
                target = value("--url") or value("--token")
                self.assert_scope(target)
        self.calls.append({"verb": verb, "write": verb in self.WRITE})
        env = os.environ.copy()
        env["LARK_CLI_SKIP_UPDATE_CHECK"] = "1"
        for attempt in range(3 if verb in self.READ else 1):
            try:
                binary = Path(self.cfg["cli_path"])
                binary = binary if binary.is_absolute() else self.root / binary
                p = subprocess.run(
                    [str(binary)]
                    + args
                    + ["--as", self.cfg.get("identity", "user"), "--format", "json"],
                    input=stdin,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    cwd=self.root,
                    env=env,
                    timeout=self.cfg.get("timeout_seconds", 90),
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                raise APIError(type(e).__name__) from e
            try:
                r = json.loads(p.stdout if p.returncode == 0 else p.stderr)
            except ValueError:
                raise APIError("invalid_cli_response_exit_" + str(p.returncode))
            if not p.returncode and r.get("ok") is True:
                return r.get("data", {})
            err = r.get("error", {})
            code = err.get("code")
            typ = err.get("type")
            if ("rate" in str(typ).lower() or code in (99991400, 99991401)) and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise APIError(
                dump({"type": typ, "code": code, "message": str(err.get("message", ""))[:240]})
            )

    def assert_scope(self, token, space=None, allow_root=False):
        """Validate current ancestry; cached parents do not authorize a write."""
        if not token:
            raise APIError("profile_write_requires_explicit_target_parent")
        data = self.call(["wiki", "+node-get", "--node-token", token])
        node = data.get("node", data)
        current_space = str(node.get("space_id", ""))
        if space is not None and str(space) != current_space:
            raise APIError("target_parent_space_mismatch")
        roots = {
            m["root_node"] for m in self.cfg["mappings"] if str(m["space_id"]) == current_space
        }
        if not roots:
            raise APIError("write_outside_configured_space")
        cursor = node.get("node_token")
        if cursor in roots:
            if allow_root:
                return
            raise APIError("selected_root_body_is_not_a_sync_target")
        visited = set()
        for _ in range(128):
            parent = node.get("parent_node_token")
            if parent in roots:
                return
            if not parent or parent in visited:
                raise APIError("write_outside_configured_subtree")
            visited.add(parent)
            data = self.call(["wiki", "+node-get", "--node-token", parent])
            node = data.get("node", data)
            if str(node.get("space_id", "")) != current_space:
                raise APIError("ancestry_space_mismatch")
        raise APIError("ancestry_depth_limit")

    def fetch(self, token):
        return self.call(
            ["docs", "+fetch", "--doc", token, "--detail", "full", "--doc-format", "xml"]
        )["document"]

    def fetch_revision(self, token, revision):
        return self.call(
            [
                "docs",
                "+fetch",
                "--doc",
                token,
                "--revision-id",
                str(revision),
                "--detail",
                "full",
                "--doc-format",
                "xml",
            ]
        )["document"]

    def children(self, space, parent=""):
        rows = []
        cursor = ""
        cursors = set()
        while True:
            args = ["wiki", "+node-list", "--space-id", space, "--page-size", "50"]
            if parent:
                args += ["--parent-node-token", parent]
            if cursor:
                args += ["--page-token", cursor]
            data = self.call(args)
            rows.extend(data.get("nodes", []))
            if not data.get("has_more"):
                return rows
            cursor = data.get("page_token")
            if not cursor or cursor in cursors:
                raise APIError("incomplete_pagination")
            cursors.add(cursor)

    def metas(self, objects):
        self.meta_failures = {}
        result = {}
        for start in range(0, len(objects), 200):
            batch = objects[start : start + 200]
            d = self.call(
                ["drive", "metas", "batch_query", "--data", "-"],
                dump({"request_docs": batch, "with_url": False}),
            )
            for failure in d.get("failed_list", []):
                self.meta_failures[failure["token"]] = failure.get("code", "unavailable")
            for m in d.get("metas", []):
                result[m["doc_token"]] = dump(
                    {k: m.get(k) for k in ("title", "latest_modify_time", "doc_type")}
                )
            for item in batch:
                if item["doc_token"] not in result:
                    self.meta_failures.setdefault(item["doc_token"], "missing_from_response")
        return result

    def update(self, token, revision, command, content, block="-1"):
        data = self.call(
            [
                "docs",
                "+update",
                "--doc",
                token,
                "--revision-id",
                str(revision),
                "--command",
                command,
                "--block-id",
                block,
                "--doc-format",
                "xml",
                "--content",
                "-",
            ],
            content,
        )
        if data.get("result") not in (None, "success") or data.get("warnings"):
            raise APIError("update_partial_or_warning:" + dump(data))
        return data

    def create(self, space, parent, title):
        if self.cfg.get("mappings") and not parent:
            raise APIError("profile_creation_requires_explicit_parent")
        args = ["wiki", "+node-create", "--space-id", space, "--title", title]
        if parent:
            args += ["--parent-node-token", parent]
        d = self.call(args)
        return d.get("node", d)

    def rename(self, node, title):
        return self.call(
            [
                "drive",
                "+update-title",
                "--url",
                "https://my.feishu.cn/wiki/" + node,
                "--title",
                title,
            ]
        )
