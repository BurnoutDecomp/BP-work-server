"""Per-file commit dating from a local clone of the decomp source repo.

Backfilled Live Events ("workflow commit delta" / "legacy pre-server
attribution") all share a single import timestamp and a single, meaningless
``detail.commit`` SHA, so neither tells us when the work actually happened. The
honest signal is the *file itself*: the last commit that touched a TU's
destination file in the ``b5-decomp`` repo. This module reads that from a local
clone with ``git log`` -- free and unmetered, unlike per-path GitHub API calls,
which would blow the rate limit across hundreds of files.

Headers are decompiled inline into their ``.cpp``, so a ``*.h`` destination has
no file of its own in the repo; we transparently fall back to the ``.cpp``
sibling, which also fixes the destination links that used to 404 on ``.h`` TUs.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The decomp source lives next to the workflow checkout by default; both sit in
# the persistent data dir so they survive code deploys.
DEFAULT_DECOMP_REPO = "https://github.com/BurnoutDecomp/b5-decomp.git"
DEFAULT_DECOMP_ROOT = "/var/lib/bp-work-server/b5-decomp"
DEFAULT_DECOMP_BRANCH = os.environ.get("BP_GITHUB_REF", "dev")

# How long the local clone is trusted before we fetch the branch again. New
# decomp commits only add to a file's history, so staleness here is cosmetic; a
# generous TTL keeps git fetch out of the hot path.
REFRESH_TTL = 900

# Only commits from this year onward count: the workflow (and this server's
# attribution) began in 2026, so earlier commits belong to the abandoned
# pre-workflow decomp and must not be reconstructed as Live Events.
MIN_COMMIT_YEAR = int(os.environ.get("BP_DECOMP_MIN_YEAR", "2026"))

# Field separator for ``git log`` output; ASCII unit separator never appears in
# hashes, author names, or ISO dates, so it parses unambiguously.
_FIELD_SEP = "\x1f"

# Destination paths are stored as "b5-decomp/src/...": the repo-name prefix is
# stripped to get the path relative to the clone root.
_REPO_PREFIX = "b5-decomp/"


class DecompRepo:
    """Reads per-file commit history from a local clone of the decomp source.

    A TU's destination file is the honest record of who did the work and when:
    each commit that touched it (from MIN_COMMIT_YEAR on) is one unit of work by
    its author on its date. ``history`` exposes that list; ``resolve`` and the
    thin wrappers expose the existing path and the latest commit for callers that
    only need a single date or link.

    All public methods are safe to call from FastAPI's threadpool: results are
    memoised under a lock and ``git`` is shelled out synchronously. Everything
    degrades to empty/None when the clone is missing or git fails, so the
    dashboard simply falls back to stored data.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        branch: str | None = None,
    ) -> None:
        self.root = Path(root or os.environ.get("BP_DECOMP_ROOT", DEFAULT_DECOMP_ROOT))
        self.branch = branch or os.environ.get("BP_DECOMP_BRANCH", DEFAULT_DECOMP_BRANCH)
        self._lock = threading.Lock()
        # path-relative-to-root -> {"path": existing_path|None, "history": [...]}
        self._cache: dict[str, dict] = {}
        self._blame_cache: dict[str, list[dict[str, Any]]] = {}
        self._source_cache: dict[str, str] = {}
        self._function_range_cache: dict[tuple[str, str], tuple[int, int] | None] = {}
        # Memoised HEAD sha. Only a refresh (fetch + reset) can move HEAD, and
        # that path clears this, so revision() need not spawn `git rev-parse` on
        # every call -- it is hit once per dashboard poll as the cache key.
        self._revision: str | None = None
        self._refreshed_at = 0.0

    @property
    def available(self) -> bool:
        return (self.root / ".git").exists()

    def _git(self, *args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def _maybe_refresh(self) -> None:
        """Fetch the branch at most once per TTL; clears the memo on update."""
        if not self.available:
            return
        now = time.time()
        if now - self._refreshed_at < REFRESH_TTL:
            return
        with self._lock:
            if time.time() - self._refreshed_at < REFRESH_TTL:
                return
            self._refresh_locked()

    def force_refresh(self) -> None:
        """Fetch + hard-reset the branch now, ignoring the TTL.

        Called after an admin sync so attribution is computed against the same
        b5-decomp tip the workflow just advanced to, instead of a clone that can
        be up to REFRESH_TTL (15 min) stale.
        """
        if not self.available:
            return
        with self._lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        """Fetch + hard-reset to the branch tip and clear memos. Caller holds the lock."""
        ok = self._git("fetch", "--quiet", "origin", self.branch)
        if ok is not None:
            self._git("reset", "--hard", f"origin/{self.branch}")
            self._cache.clear()
            self._blame_cache.clear()
            self._source_cache.clear()
            self._function_range_cache.clear()
            self._revision = None
        # Record the attempt regardless so a flaky network does not make
        # every request pay the fetch cost.
        self._refreshed_at = time.time()

    @staticmethod
    def _repo_relative(dest_path: str) -> str:
        path = dest_path.removeprefix(_REPO_PREFIX)
        return path.lstrip("/")

    def _existing_path(self, rel: str) -> str | None:
        """The file that exists for ``rel`` (a missing *.h maps to its .cpp)."""
        candidates = [rel]
        # Headers are inlined into the .cpp, so a missing *.h has no file of its own.
        if rel.endswith(".h"):
            candidates.append(rel[:-2] + ".cpp")
        for candidate in candidates:
            if (self.root / candidate).is_file():
                return candidate
        return None

    def _resolve_uncached(self, rel: str) -> dict:
        path = self._existing_path(rel)
        if not path:
            return {"path": None, "history": []}
        out = self._git(
            "log",
            f"--format=%H{_FIELD_SEP}%aI{_FIELD_SEP}%an{_FIELD_SEP}%ae",
            "--",
            path,
        )
        history: list[dict[str, str | None]] = []
        for line in (out or "").splitlines():
            commit, _, rest = line.strip().partition(_FIELD_SEP)
            date, _, rest = rest.partition(_FIELD_SEP)
            name, _, email = rest.partition(_FIELD_SEP)
            if len(date) < 4 or not date[:4].isdigit():
                continue
            if int(date[:4]) < MIN_COMMIT_YEAR:
                continue
            history.append(
                {
                    "commit": commit.strip() or None,
                    "date": date,
                    "name": name.strip() or None,
                    "email": email.strip() or None,
                }
            )
        # git log is newest-first, which is the order we want.
        return {"path": path, "history": history}

    def _record(self, dest_path: str | None) -> dict:
        if not dest_path:
            return {"path": None, "history": []}
        self._maybe_refresh()
        rel = self._repo_relative(dest_path)
        with self._lock:
            hit = self._cache.get(rel)
        if hit is not None:
            return hit
        record = self._resolve_uncached(rel)
        with self._lock:
            self._cache[rel] = record
        return record

    def revision(self) -> str | None:
        """Current checked-out commit for cache keys."""
        self._maybe_refresh()
        with self._lock:
            if self._revision is not None:
                return self._revision
        out = self._git("rev-parse", "HEAD")
        rev = out.strip() if out else None
        if rev:
            with self._lock:
                self._revision = rev
        return rev

    def _source(self, path: str | None) -> str:
        if not path:
            return ""
        with self._lock:
            hit = self._source_cache.get(path)
        if hit is not None:
            return hit
        try:
            text = (self.root / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        with self._lock:
            self._source_cache[path] = text
        return text

    def _blame(self, path: str | None) -> list[dict[str, Any]]:
        if not path:
            return []
        self._maybe_refresh()
        with self._lock:
            hit = self._blame_cache.get(path)
        if hit is not None:
            return hit
        out = self._git("blame", "--line-porcelain", "--", path)
        records: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in (out or "").splitlines():
            if line.startswith("author "):
                current["name"] = line.removeprefix("author ").strip() or None
            elif line.startswith("author-mail "):
                email = line.removeprefix("author-mail ").strip()
                current["email"] = email.strip("<>") or None
            elif line.startswith("author-time "):
                value = line.removeprefix("author-time ").strip()
                if value.isdigit():
                    current["time"] = int(value)
            elif line.startswith("\t"):
                records.append(dict(current))
                current.clear()
        with self._lock:
            self._blame_cache[path] = records
        return records

    @staticmethod
    def _line_year(record: dict[str, Any]) -> int | None:
        ts = record.get("time")
        if not isinstance(ts, int):
            return None
        return datetime.fromtimestamp(ts, UTC).year

    @staticmethod
    def _contributors_from_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str | None, str | None], int] = Counter()
        total = 0
        for line in lines:
            year = DecompRepo._line_year(line)
            if year is not None and year < MIN_COMMIT_YEAR:
                continue
            name = line.get("name")
            email = line.get("email")
            if not name and not email:
                continue
            grouped[(name, email)] += 1
            total += 1
        if total <= 0:
            return []
        contributors = [
            {
                "name": name,
                "email": email,
                "lines": lines,
                "percent": round((lines / total) * 100, 1),
            }
            for (name, email), lines in grouped.items()
        ]
        contributors.sort(key=lambda item: (-item["lines"], item.get("name") or "", item.get("email") or ""))
        return contributors

    def contributors(
        self, dest_path: str | None, line_range: tuple[int, int] | None = None
    ) -> dict[str, Any]:
        """Surviving-line contributors for a destination file or line range."""
        record = self._record(dest_path)
        path = record["path"]
        blame = self._blame(path)
        if line_range and blame:
            start, end = line_range
            start = max(start, 1)
            end = min(end, len(blame))
            blame = blame[start - 1 : end] if start <= end else []
        contributors = self._contributors_from_lines(blame)
        return {
            "path": path,
            "line_range": list(line_range) if line_range else None,
            "basis": "surviving_lines",
            "contributors": contributors,
        }

    @staticmethod
    def _sanitize_cpp(text: str) -> str:
        chars = list(text)
        i = 0
        state = "code"
        while i < len(chars):
            ch = chars[i]
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if state == "code":
                if ch == "/" and nxt == "/":
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    state = "line_comment"
                    continue
                if ch == "/" and nxt == "*":
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    state = "block_comment"
                    continue
                if ch == '"':
                    chars[i] = " "
                    i += 1
                    state = "string"
                    continue
                if ch == "'":
                    chars[i] = " "
                    i += 1
                    state = "char"
                    continue
            elif state == "line_comment":
                if ch == "\n":
                    state = "code"
                else:
                    chars[i] = " "
            elif state == "block_comment":
                if ch == "*" and nxt == "/":
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    state = "code"
                    continue
                if ch != "\n":
                    chars[i] = " "
            elif state in {"string", "char"}:
                quote = '"' if state == "string" else "'"
                if ch == "\\" and i + 1 < len(chars):
                    chars[i] = " "
                    if chars[i + 1] != "\n":
                        chars[i + 1] = " "
                    i += 2
                    continue
                if ch == quote:
                    state = "code"
                if ch != "\n":
                    chars[i] = " "
            i += 1
        return "".join(chars)

    @staticmethod
    def _line_number_at(text: str, index: int) -> int:
        return text.count("\n", 0, index) + 1

    @staticmethod
    def _find_matching(text: str, start: int, open_ch: str, close_ch: str) -> int | None:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        return None

    @staticmethod
    def _function_patterns(function_name: str) -> list[str]:
        names = [function_name.strip()]
        patterns: list[str] = []
        for name in names:
            if not name:
                continue
            if "::" in name:
                parts = [re.escape(part) for part in name.split("::") if part]
                patterns.append(r"(?<![\w:])" + r"\s*::\s*".join(parts) + r"(?![\w:])")
            else:
                patterns.append(r"(?<![\w:])" + re.escape(name) + r"(?![\w:])")
        return list(dict.fromkeys(patterns))

    def function_range(self, dest_path: str | None, function_name: str) -> tuple[int, int] | None:
        """Best-effort C++ function body range, 1-based inclusive line numbers."""
        record = self._record(dest_path)
        path = record["path"]
        if not path or not function_name:
            return None
        key = (path, function_name)
        with self._lock:
            if key in self._function_range_cache:
                return self._function_range_cache[key]
        text = self._source(path)
        clean = self._sanitize_cpp(text)
        found: tuple[int, int] | None = None
        for pattern in self._function_patterns(function_name):
            for match in re.finditer(pattern, clean):
                i = match.end()
                while i < len(clean) and clean[i].isspace():
                    i += 1
                if i >= len(clean) or clean[i] != "(":
                    continue
                close_paren = self._find_matching(clean, i, "(", ")")
                if close_paren is None:
                    continue
                j = close_paren + 1
                while j < len(clean):
                    ch = clean[j]
                    if ch == "{":
                        close_brace = self._find_matching(clean, j, "{", "}")
                        if close_brace is None:
                            break
                        found = (self._line_number_at(clean, match.start()), self._line_number_at(clean, close_brace))
                        break
                    if ch in ";":
                        break
                    j += 1
                if found:
                    break
            if found:
                break
        with self._lock:
            self._function_range_cache[key] = found
        return found

    def function_contributors(self, dest_path: str | None, function_name: str) -> dict[str, Any]:
        line_range = self.function_range(dest_path, function_name)
        result = self.contributors(dest_path, line_range=line_range)
        result["function_range_found"] = line_range is not None
        return result

    def history(self, dest_path: str | None) -> list[dict[str, str | None]]:
        """Commits (newest-first) that touched the file, from MIN_COMMIT_YEAR on.

        Each entry is ``{"commit": sha, "date": iso, "name": git_author,
        "email": git_email}``; empty when the file is absent or has no
        qualifying commits. The caller maps email -> GitHub login for display.
        """
        return self._record(dest_path)["history"]

    def resolve(self, dest_path: str | None) -> tuple[str | None, str | None]:
        """Map a TU destination to ``(existing_repo_path, latest_commit_date)``."""
        record = self._record(dest_path)
        history = record["history"]
        return record["path"], (history[0]["date"] if history else None)

    def commit_date(self, dest_path: str | None) -> str | None:
        return self.resolve(dest_path)[1]

    def repo_path(self, dest_path: str | None) -> str | None:
        return self._record(dest_path)["path"]
