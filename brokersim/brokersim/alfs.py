"""A tiny in-memory stand-in for Alva's file system (ALFS).

Only what the trading guardrails need: the agent reads and writes small files
such as ``~/memory/auto-trade-consent.md``. ``~`` expands to the user's home.
"""

from __future__ import annotations

import posixpath

from .util import iso


class FsError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Alfs:
    def __init__(self, home: str, clock):
        self.home = home.rstrip("/")
        self.clock = clock
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, int] = {}
        self.dirs: set[str] = {"/", "/alva", "/alva/home", self.home}

    def norm(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            path = self.home + path[1:]
        if not path.startswith("/"):
            path = self.home + "/" + path
        p = posixpath.normpath(path)
        return p

    def _parents(self, p: str) -> list[str]:
        out = []
        while p not in ("/", ""):
            p = posixpath.dirname(p)
            out.append(p)
        return out

    def write(self, path: str, data: bytes, append: bool = False, mkdir_parents: bool = True) -> int:
        p = self.norm(path)
        parent = posixpath.dirname(p)
        if parent not in self.dirs:
            if not mkdir_parents:
                raise FsError(404, "NOT_FOUND", f"parent directory {parent} does not exist")
            self.dirs.update(self._parents(p))
        if p in self.dirs:
            raise FsError(400, "INVALID_ARGUMENT", f"{p} is a directory")
        self.files[p] = (self.files.get(p, b"") + data) if append else data
        self.mtimes[p] = self.clock.now_ms()
        return len(data)

    def read(self, path: str, offset: int = 0, size: int | None = None) -> bytes:
        p = self.norm(path)
        if p not in self.files:
            raise FsError(404, "NOT_FOUND", f"{p} not found")
        data = self.files[p][offset:]
        return data if size is None else data[:size]

    def stat(self, path: str) -> dict:
        p = self.norm(path)
        if p in self.files:
            return {"name": posixpath.basename(p), "size": len(self.files[p]), "mode": 0o644,
                    "mod_time": iso(self.mtimes[p]), "is_dir": False}
        if p in self.dirs:
            return {"name": posixpath.basename(p) or "/", "size": 0, "mode": 0o755,
                    "mod_time": iso(self.clock.start_ms), "is_dir": True}
        raise FsError(404, "NOT_FOUND", f"{p} not found")

    def readdir(self, path: str, recursive: bool = False) -> dict:
        p = self.norm(path)
        if p not in self.dirs:
            raise FsError(404, "NOT_FOUND", f"{p} not found")
        prefix = p.rstrip("/") + "/"
        entries = []
        for d in sorted(self.dirs):
            if d.startswith(prefix) and (recursive or "/" not in d[len(prefix):]):
                entries.append({"name": d[len(prefix):], "size": 0, "is_dir": True})
        for f in sorted(self.files):
            if f.startswith(prefix) and (recursive or "/" not in f[len(prefix):]):
                entries.append({"name": f[len(prefix):], "size": len(self.files[f]), "is_dir": False,
                                "mod_time": iso(self.mtimes[f])})
        return {"entries": entries}

    def mkdir(self, path: str) -> None:
        p = self.norm(path)
        self.dirs.add(p)
        self.dirs.update(self._parents(p))

    def remove(self, path: str, recursive: bool = False) -> None:
        p = self.norm(path)
        if p in self.files:
            del self.files[p]
            self.mtimes.pop(p, None)
            return
        if p in self.dirs:
            children = [x for x in list(self.files) + list(self.dirs) if x.startswith(p + "/")]
            if children and not recursive:
                raise FsError(400, "INVALID_ARGUMENT", f"{p} is not empty")
            for c in children:
                self.files.pop(c, None)
                self.dirs.discard(c)
            self.dirs.discard(p)
            return
        raise FsError(404, "NOT_FOUND", f"{p} not found")

    def snapshot(self) -> dict:
        return {k: v.decode("utf-8", "replace") for k, v in sorted(self.files.items())}
