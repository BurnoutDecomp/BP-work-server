from __future__ import annotations

import fnmatch
import json
import os
import re
import secrets
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from bp_work_server.models import ClaimResponse, NextTu, StatusCounts, TuRecord
from bp_work_server.schema import (
    DB_BUSY_TIMEOUT_MS,
    DURABLE_IMPORT_STATUSES,
    TU_STATUSES,
    USERS_SCHEMA,
    WORK_SCHEMA,
)


SCHEMA = WORK_SCHEMA

DEFAULT_ACTOR_ALIASES = {
    # Git identities seen in the b5-decomp history. User-maintained
    # worker_alias rows still override these defaults below.
    "Niaz": "Derneuere",
    "tigrexspalterlp@gmail.com": "Derneuere",
    "Nathan V.": "JeBobs",
    "jebcraftserver@gmail.com": "JeBobs",
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def login_from_noreply_email(email: str | None) -> str | None:
    if not email or "users.noreply.github.com" not in email.lower():
        return None
    local = email.split("@", 1)[0]
    return local.split("+", 1)[-1] or None


class WorkStore:
    def __init__(self, db_path: str | Path, users_db_path: str | Path | None = None):
        self.db_path = Path(db_path)
        self.users_db_path = Path(users_db_path) if users_db_path else self._default_users_path()

    def _default_users_path(self) -> Path:
        return self.db_path.with_name(f"{self.db_path.stem}-users{self.db_path.suffix}")

    @contextmanager
    def connect(self, *, ensure_wal: bool = False) -> Iterable[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=DB_BUSY_TIMEOUT_MS / 1000)
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA synchronous = NORMAL")
        if ensure_wal:
            con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def users_connect(self, *, ensure_wal: bool = False) -> Iterable[sqlite3.Connection]:
        self.users_db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.users_db_path, timeout=DB_BUSY_TIMEOUT_MS / 1000)
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA synchronous = NORMAL")
        if ensure_wal:
            con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def migrate(self) -> None:
        with self.connect(ensure_wal=True) as con:
            con.executescript(SCHEMA)
            cols = {r["name"] for r in con.execute("PRAGMA table_info(func)")}
            if "completed_by" not in cols:
                con.execute("ALTER TABLE func ADD COLUMN completed_by TEXT")
            if "completed_at" not in cols:
                con.execute("ALTER TABLE func ADD COLUMN completed_at TEXT")
            self._backfill_missing_dest_paths(con)
        self._migrate_users()

    def _migrate_users(self) -> None:
        with self.users_connect(ensure_wal=True) as con:
            con.executescript(USERS_SCHEMA)
            # additive migration for user DBs created before the admin role existed
            cols = {r["name"] for r in con.execute("PRAGMA table_info(worker)")}
            if "is_admin" not in cols:
                con.execute("ALTER TABLE worker ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            if "github_username" not in cols:
                con.execute("ALTER TABLE worker ADD COLUMN github_username TEXT")
        self._copy_legacy_workers()

    def _copy_legacy_workers(self) -> None:
        if self.users_db_path.resolve() == self.db_path.resolve():
            return
        with self.connect() as con:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker'"
            ).fetchone()
            if not exists:
                return
            cols = {r["name"] for r in con.execute("PRAGMA table_info(worker)")}
            is_admin_expr = "is_admin" if "is_admin" in cols else "0 AS is_admin"
            github_expr = "github_username" if "github_username" in cols else "NULL AS github_username"
            rows = con.execute(
                f"""
                SELECT token, username, active, {is_admin_expr}, {github_expr}, created_at, last_seen
                FROM worker
                """
            ).fetchall()
        if rows:
            with self.users_connect() as con:
                for row in rows:
                    con.execute(
                        """
                        INSERT INTO worker(
                          token, username, active, is_admin, github_username, created_at, last_seen
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(token) DO NOTHING
                        """,
                        (
                            row["token"],
                            row["username"],
                            int(row["active"]),
                            int(row["is_admin"]),
                            row["github_username"],
                            row["created_at"],
                            row["last_seen"],
                        ),
                    )
        with self.connect() as con:
            con.execute("DROP TABLE IF EXISTS worker")

    def import_workflow(self, workflow_root: str | Path, reset: bool = False) -> dict[str, int]:
        progress = Path(workflow_root) / "progress"
        tu_index_path = progress / "tu_index.json"
        status_path = progress / "status.json"
        deps_path = progress / "tu_deps.json"
        goals_path = progress / "goals.json"

        # Real home files for class TUs (resolved from committed sources by
        # tools/work/resolve_class_homes.py). class TUs have no path in tu_index,
        # so without this the synthetic src/classes/<Class>.cpp path is used and
        # Git contribution attribution can never resolve them.
        class_homes_path = progress / "class_homes.json"

        tu_index = json.loads(tu_index_path.read_text(encoding="utf-8"))
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        deps = json.loads(deps_path.read_text(encoding="utf-8")) if deps_path.exists() else []
        goals = json.loads(goals_path.read_text(encoding="utf-8")) if goals_path.exists() else {}
        class_homes = (
            json.loads(class_homes_path.read_text(encoding="utf-8"))
            if class_homes_path.exists()
            else {}
        )

        with self.connect(ensure_wal=True) as con:
            con.executescript(SCHEMA)
            if reset:
                con.executescript(
                    "DELETE FROM event; DELETE FROM goal_tu; DELETE FROM goal; "
                    "DELETE FROM tu_dep; DELETE FROM func; DELETE FROM tu; DELETE FROM meta;"
                )

            for tu_id, row in tu_index.items():
                con.execute(
                    """
                    INSERT INTO tu(id, source, status, n_funcs, n_decfigs, dest_path, updated_at)
                    VALUES(?, ?, 'todo', ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      source=excluded.source,
                      n_funcs=excluded.n_funcs,
                      n_decfigs=excluded.n_decfigs,
                      dest_path=COALESCE(NULLIF(tu.dest_path, ''), excluded.dest_path)
                    """,
                    (
                        tu_id,
                        row.get("source"),
                        int(row.get("n_funcs") or 0),
                        int(row.get("n_decfigs") or 0),
                        class_homes.get(tu_id) or self._dest_for(tu_id, row.get("source")),
                        iso(),
                    ),
                )
                for fn in row.get("functions", []):
                    con.execute(
                        """
                        INSERT INTO func(name, tu_id, status)
                        VALUES(?, ?, 'todo')
                        ON CONFLICT(name) DO UPDATE SET tu_id=excluded.tu_id
                        """,
                        (fn, tu_id),
                    )

            # class TU dest_paths are otherwise the synthetic src/classes/<Class>.cpp
            # (kept by the ON CONFLICT COALESCE above). The resolved home is always the
            # better, real path, so let class_homes win for class TUs. This only moves
            # attribution to the right file -- it never touches TU status.
            for class_tu_id, home in class_homes.items():
                con.execute(
                    "UPDATE tu SET dest_path=? WHERE id=? AND source='class' AND dest_path IS NOT ?",
                    (home, class_tu_id, home),
                )

            status_rows = self._restore_status(con, status)
            dep_count = self._restore_deps(con, deps)
            goal_count = self._restore_goals(con, goals)
            self._log(con, "server", "import", None, {"workflow_root": str(workflow_root)})
            return {
                "tus": len(tu_index),
                "funcs": sum(len(row.get("functions", [])) for row in tu_index.values()),
                "deps": dep_count,
                "goals": goal_count,
                "status_rows": status_rows,
            }

    def next_tus(self, n: int = 1, goal: str | None = None) -> tuple[str | None, list[NextTu]]:
        with self.connect() as con:
            self._expire_leases(con)
            return self._next_tus_from_connection(con, n=n, goal=goal)

    def claim(
        self, tu_id: str, agent: str, lease_seconds: int = 7200, force: bool = False
    ) -> ClaimResponse:
        now = utcnow()
        expiry = now + timedelta(seconds=lease_seconds)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._expire_leases(con, now)
            row = con.execute("SELECT * FROM tu WHERE id=?", (tu_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown TU: {tu_id}")

            if row["status"] == "in_progress" and row["owner"] == agent:
                con.execute(
                    "UPDATE tu SET lease_expires_at=?, updated_at=? WHERE id=?",
                    (iso(expiry), iso(now), tu_id),
                )
                self._log(con, agent, "heartbeat", tu_id, {"lease_seconds": lease_seconds})
                return ClaimResponse(
                    claimed=True,
                    tu=tu_id,
                    status="in_progress",
                    owner=agent,
                    lease_expires_at=expiry,
                    message="renewed existing claim",
                )

            claimable = row["status"] == "todo" or (force and row["status"] != "done")
            if not claimable:
                return ClaimResponse(
                    claimed=False,
                    tu=tu_id,
                    status=row["status"],
                    owner=row["owner"],
                    lease_expires_at=parse_dt(row["lease_expires_at"]),
                    message="not claimable",
                )

            cur = con.execute(
                """
                UPDATE tu
                SET status='in_progress',
                    owner=?,
                    claimed_at=?,
                    lease_expires_at=?,
                    updated_at=?,
                    notes=NULL
                WHERE id=? AND status=?
                """,
                (agent, iso(now), iso(expiry), iso(now), tu_id, row["status"]),
            )
            if cur.rowcount == 0:
                return ClaimResponse(
                    claimed=False,
                    tu=tu_id,
                    status=row["status"],
                    owner=row["owner"],
                    lease_expires_at=parse_dt(row["lease_expires_at"]),
                    message="claim race lost",
                )
            self._log(con, agent, "claim", tu_id, {"lease_seconds": lease_seconds, "force": force})
            return ClaimResponse(
                claimed=True,
                tu=tu_id,
                status="in_progress",
                owner=agent,
                lease_expires_at=expiry,
            )

    def claim_next(
        self, agent: str, n: int = 1, lease_seconds: int = 7200, goal: str | None = None
    ) -> tuple[str | None, list[ClaimResponse]]:
        """Rank the ``todo`` queue and claim the top ``n`` for ``agent`` in one
        transaction, so concurrent agents get distinct work (no rank-then-race
        window). Returns the active goal and the claims that succeeded -- fewer
        than ``n`` if the queue is short or a row was taken between rank and claim.
        """
        now = utcnow()
        expiry = now + timedelta(seconds=lease_seconds)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._expire_leases(con, now)
            active_goal, ranked = self._next_tus_from_connection(con, n=n, goal=goal)
            claimed: list[ClaimResponse] = []
            for item in ranked:
                cur = con.execute(
                    """
                    UPDATE tu
                    SET status='in_progress', owner=?, claimed_at=?,
                        lease_expires_at=?, updated_at=?, notes=NULL
                    WHERE id=? AND status='todo'
                    """,
                    (agent, iso(now), iso(expiry), iso(now), item.id),
                )
                if cur.rowcount == 0:
                    continue
                self._log(
                    con, agent, "claim", item.id,
                    {"lease_seconds": lease_seconds, "via": "claim_next"},
                )
                claimed.append(
                    ClaimResponse(
                        claimed=True,
                        tu=item.id,
                        status="in_progress",
                        owner=agent,
                        lease_expires_at=expiry,
                    )
                )
            return active_goal, claimed

    def heartbeat(self, tu_id: str, agent: str, lease_seconds: int = 7200) -> ClaimResponse:
        return self.claim(tu_id, agent, lease_seconds=lease_seconds)

    def release(self, tu_id: str, agent: str) -> None:
        with self.connect() as con:
            row = self._require_tu(con, tu_id)
            if row["status"] != "in_progress" or row["owner"] != agent:
                raise PermissionError("only the current claim owner can release this TU")
            con.execute(
                """
                UPDATE tu
                SET status='todo', owner=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    notes=NULL, updated_at=?
                WHERE id=?
                """,
                (iso(), tu_id),
            )
            self._log(con, agent, "release", tu_id, {})

    def mark_compiled(
        self, tu_id: str, agent: str, notes: str | None = None, commit: str | None = None
    ) -> None:
        with self.connect() as con:
            row = self._require_tu(con, tu_id)
            self._require_owner(row, agent)
            con.execute(
                """
                UPDATE tu
                SET status='compiled', notes=?, commit_hash=COALESCE(?, commit_hash),
                    lease_expires_at=NULL, updated_at=?
                WHERE id=?
                """,
                (notes, commit, iso(), tu_id),
            )
            now = iso()
            con.execute(
                "UPDATE func SET status='compiles', completed_by=?, completed_at=? WHERE tu_id=?",
                (agent, now, tu_id),
            )
            self._log(con, agent, "compiled", tu_id, {"notes": notes, "commit": commit})

    def review(
        self,
        tu_id: str,
        agent: str,
        verdict: str,
        notes: str | None = None,
        commit: str | None = None,
    ) -> None:
        if verdict not in {"pass", "fail"}:
            raise ValueError("verdict must be pass or fail")
        with self.connect() as con:
            self._require_tu(con, tu_id)
            if verdict == "pass":
                status = "done"
                func_status = "reviewed"
                owner = None
            else:
                status = "in_progress"
                func_status = "recovered"
                owner = agent
            con.execute(
                """
                UPDATE tu
                SET status=?, owner=?, notes=?, commit_hash=COALESCE(?, commit_hash),
                    lease_expires_at=NULL, updated_at=?
                WHERE id=?
                """,
                (status, owner, notes, commit, iso(), tu_id),
            )
            now = iso()
            con.execute(
                "UPDATE func SET status=?, completed_by=?, completed_at=? WHERE tu_id=?",
                (func_status, agent, now, tu_id),
            )
            self._log(con, agent, f"review_{verdict}", tu_id, {"notes": notes, "commit": commit})

    def block(self, tu_id: str, agent: str, reason: str) -> None:
        with self.connect() as con:
            self._require_tu(con, tu_id)
            con.execute(
                """
                UPDATE tu
                SET status='blocked', owner=NULL, notes=?, lease_expires_at=NULL, updated_at=?
                WHERE id=?
                """,
                (reason, iso(), tu_id),
            )
            self._log(con, agent, "block", tu_id, {"reason": reason})

    def unblock(self, tu_id: str, agent: str) -> None:
        with self.connect() as con:
            self._return_tu_to_todo(con, tu_id, notes=None)
            self._log(con, agent, "unblock", tu_id, {})

    def reset_tu(self, tu_id: str, agent: str, notes: str | None = None) -> None:
        with self.connect() as con:
            self._return_tu_to_todo(con, tu_id, notes=notes)
            self._log(con, agent, "reset_tu", tu_id, {"notes": notes})

    # --- workers (server-issued identities) -------------------------------
    def create_worker(
        self, username: str, is_admin: bool = False, github_username: str | None = None
    ) -> dict[str, Any]:
        """Mint a new secret token bound to a human username. `is_admin` grants access to
        the /admin/* endpoints (minting/revoking ids, import/sync/reset). Admin is a role
        on a worker, not a separate shared secret."""
        token = secrets.token_urlsafe(24)
        github_username = self._normalize_github_username(username, github_username)
        with self.users_connect() as con:
            con.execute(
                """
                INSERT INTO worker(token, username, active, is_admin, github_username, created_at)
                VALUES(?, ?, 1, ?, ?, ?)
                """,
                (token, username, 1 if is_admin else 0, github_username, iso()),
            )
        with self.connect() as con:
            self._log(con, username, "worker_create", None, {"is_admin": bool(is_admin)})
        return {
            "token": token,
            "username": username,
            "is_admin": bool(is_admin),
            "github_username": github_username,
        }

    def resolve_worker(self, token: str) -> str | None:
        """Return the username for an active token, or None. Updates last_seen.
        The token itself is never stored in events or on TU rows -- only the username."""
        if not token:
            return None
        with self.users_connect() as con:
            row = con.execute(
                "SELECT username FROM worker WHERE token=? AND active=1", (token,)
            ).fetchone()
            if not row:
                return None
            con.execute("UPDATE worker SET last_seen=? WHERE token=?", (iso(), token))
            return row["username"]

    def resolve_admin(self, token: str) -> str | None:
        """Return the username for an active *admin* token, or None."""
        if not token:
            return None
        with self.users_connect() as con:
            row = con.execute(
                "SELECT username FROM worker WHERE token=? AND active=1 AND is_admin=1",
                (token,),
            ).fetchone()
            return row["username"] if row else None

    def list_workers(self) -> list[dict[str, Any]]:
        with self.users_connect() as con:
            return [
                {
                    "token": r["token"],
                    "username": r["username"],
                    "active": bool(r["active"]),
                    "is_admin": bool(r["is_admin"]),
                    "github_username": r["github_username"],
                    "created_at": r["created_at"],
                    "last_seen": r["last_seen"],
                }
                for r in con.execute("SELECT * FROM worker ORDER BY created_at, username")
            ]

    def _registered_agents(self) -> dict[str, dict[str, Any]]:
        with self.users_connect() as con:
            rows = con.execute(
                """
                SELECT username,
                       MAX(active) AS active,
                       MAX(is_admin) AS is_admin,
                       MAX(github_username) AS github_username,
                       COUNT(*) AS tokens,
                       MIN(created_at) AS created_at,
                       MAX(last_seen) AS last_seen
                FROM worker
                GROUP BY username
                ORDER BY lower(username), username
                """
            ).fetchall()

        by_key: dict[str, dict[str, Any]] = {}
        for row in rows:
            username = (row["username"] or "").strip()
            if not username:
                continue
            key = username.lower()
            current = by_key.get(key)
            if current is None:
                by_key[key] = {
                    "username": username,
                    "active": bool(row["active"]),
                    "is_admin": bool(row["is_admin"]),
                    "github_username": row["github_username"],
                    "tokens": row["tokens"],
                    "created_at": row["created_at"],
                    "last_seen": row["last_seen"],
                }
                continue

            if self._prefer_username(username, current["username"]):
                current["username"] = username
            current["active"] = bool(current["active"] or row["active"])
            current["is_admin"] = bool(current["is_admin"] or row["is_admin"])
            current["github_username"] = current["github_username"] or row["github_username"]
            current["tokens"] += row["tokens"]
            if row["created_at"] and (
                not current["created_at"] or row["created_at"] < current["created_at"]
            ):
                current["created_at"] = row["created_at"]
            if row["last_seen"] and (
                not current["last_seen"] or row["last_seen"] > current["last_seen"]
            ):
                current["last_seen"] = row["last_seen"]

        return {
            data.pop("username"): data
            for data in sorted(by_key.values(), key=lambda item: item["username"].lower())
        }

    def revoke_worker(self, token: str) -> bool:
        with self.users_connect() as con:
            cur = con.execute("UPDATE worker SET active=0 WHERE token=?", (token,))
            revoked = cur.rowcount > 0
        if revoked:
            with self.connect() as con:
                self._log(con, None, "worker_revoke", None, {})
        return revoked

    def set_worker_github_username(
        self, username: str, github_username: str | None
    ) -> int:
        github_username = self._normalize_github_username(username, github_username)
        with self.users_connect() as con:
            cur = con.execute(
                "UPDATE worker SET github_username=? WHERE username=?",
                (github_username, username),
            )
            return cur.rowcount

    def export_status(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Reproduce the durable ``progress/status.json`` shape from the live DB.

        This is the inverse of ``WorkStore.import_workflow``: it emits exactly the
        states the workflow CLI's server-mode ``sync_status`` would commit to git --
        only the DURABLE TU statuses (``done``/``blocked``, the ones tied to committed
        code) plus their notes, and every non-``todo`` func status. The transient live
        layer (``in_progress``/``compiled``, ``owner``, leases) is deliberately omitted:
        it belongs to the server, never to git. Lets a CI job regenerate the committed
        status.json from the server so workers never push it by hand.

        Format mirrors ``sync_status``: nested ``{"tu": {...}, "func": {...}}`` so a
        consumer can ``json.dump(..., indent=1, sort_keys=True)`` it byte-for-byte.
        """
        with self.connect() as con:
            self._expire_leases(con)
            tu: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                "SELECT id, status, notes FROM tu "
                "WHERE status IN ('done','blocked') ORDER BY id"
            ):
                entry: dict[str, Any] = {"status": row["status"]}
                if row["notes"]:
                    entry["notes"] = row["notes"]
                tu[row["id"]] = entry
            fn: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                "SELECT name, status FROM func WHERE status!='todo' ORDER BY name"
            ):
                fn[row["name"]] = {"status": row["status"]}
            return {"tu": tu, "func": fn}

    def snapshot(self, include_tus: bool = True) -> tuple[str | None, StatusCounts, list[TuRecord]]:
        with self.connect() as con:
            self._expire_leases(con)
            counts = {key: 0 for key in TU_STATUSES}
            for row in con.execute("SELECT status, COUNT(*) AS c FROM tu GROUP BY status"):
                counts[row["status"]] = row["c"]
            tus: list[TuRecord] = []
            if include_tus:
                for row in con.execute("SELECT * FROM tu ORDER BY id"):
                    tus.append(self._tu_record(row))
            return self._get_meta(con, "active_goal"), StatusCounts(**counts), tus

    # Sources marking backfilled events: these share one import timestamp and a
    # single meaningless commit SHA, so their real time comes from the file's own
    # last commit instead (see decomp.DecompRepo).
    BACKFILLED_SOURCES = ("workflow commit delta", "legacy pre-server attribution")
    # Events synthesized from b5-decomp git history after the server DB was reset
    # (by the since-removed reconcile-events tool) -- no real server workflow event
    # ever existed for them, and their ts is the git commit author date, not a real
    # review time. They are hidden from all dashboards/metrics/feeds by default so
    # only GitHub-verifiable activity is shown. Any such rows still present are NOT
    # deleted; unhide them with BP_HIDE_RECONSTRUCTED=0.
    RECONSTRUCTED_SOURCES = ("b5-decomp commit reconstruction",)
    # git's placeholder author for lines that are modified but not committed (a dirty
    # working tree in the b5-decomp clone). It is never a real contributor, so it must
    # never surface as an agent. Compared case-insensitively against name and email.
    NON_AUTHOR_IDENTITIES = frozenset({"not committed yet", "not.committed.yet"})
    RELIABLE_EVENT_ACTIONS = (
        "claim",
        "compiled",
        "review_pass",
        "review_fail",
        "block",
        "unblock",
        "reset_tu",
    )
    DASHBOARD_HIDDEN_ACTIONS = ("lease_missing", "lease_expired")

    @staticmethod
    def _hide_reconstructed_enabled() -> bool:
        """Whether git-reconstructed events are hidden from display (default: yes).

        Reversible at runtime via the BP_HIDE_RECONSTRUCTED env var; set it to one
        of 0/false/no/off to surface the reconstructed events again.
        """
        return os.environ.get("BP_HIDE_RECONSTRUCTED", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
            "",
        )

    def _reconstructed_filter(self, column: str = "detail_json") -> str:
        """Return a SQL ``AND`` fragment excluding reconstructed events.

        Empty string when hiding is disabled, so callers can interpolate it
        unconditionally. ``column`` is the (optionally alias-qualified) detail_json
        column for the query, e.g. ``"e.detail_json"``. Operates on a fixed set of
        source literals, so there is no SQL-injection surface.
        """
        if not self._hide_reconstructed_enabled():
            return ""
        quoted = ",".join("'" + s.replace("'", "''") + "'" for s in self.RECONSTRUCTED_SOURCES)
        return f" AND COALESCE(json_extract({column}, '$.source'), '') NOT IN ({quoted})"

    def actor_maps(
        self, registered_agents: dict[str, dict[str, Any]] | None = None
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return ``(alias -> canonical username, canonical username -> GitHub profile)``.

        Worker usernames are the canonical display identity. GitHub usernames and
        case variants are aliases, so Git-derived rows do not split Adriwin from
        adriwin06 or Derneuere from derneuere.
        """
        registered_agents = registered_agents or self._registered_agents()
        aliases: dict[str, str] = {}
        profiles: dict[str, str] = {}
        for username, data in registered_agents.items():
            profile = data.get("github_username") or username
            profiles[username] = profile
            for alias in (username, data.get("github_username"), profile):
                cleaned = (alias or "").strip()
                if cleaned:
                    aliases.setdefault(cleaned.lower(), username)
        for alias, username in DEFAULT_ACTOR_ALIASES.items():
            if username in registered_agents:
                cleaned = alias.strip()
                if cleaned:
                    aliases.setdefault(cleaned.lower(), username)
        with self.users_connect() as con:
            for row in con.execute("SELECT alias, username FROM worker_alias"):
                cleaned = (row["alias"] or "").strip()
                canonical = self.canonical_actor(row["username"], aliases)
                if cleaned and canonical:
                    aliases[cleaned.lower()] = canonical
        return aliases, profiles

    def canonical_actor(self, actor: str | None, aliases: dict[str, str] | None = None) -> str | None:
        if actor is None:
            return None
        cleaned = str(actor).strip()
        if not cleaned:
            return None
        if cleaned.lower() in self.NON_AUTHOR_IDENTITIES:
            return None
        if aliases is None:
            aliases, _profiles = self.actor_maps()
        return aliases.get(cleaned.lower(), cleaned)

    def backfilled_event_targets(self) -> dict[str, str | None]:
        """Map each backfilled event's TU id to its destination file path.

        Deduplicated by TU id; used to date those events from the decomp repo.
        Backfilled placeholders are ignored once the same TU has reliable
        server workflow events, because those rows already carry the normalized
        actor and event time.
        """
        placeholders = ",".join("?" for _ in self.BACKFILLED_SOURCES)
        reliable_placeholders = ",".join("?" for _ in self.RELIABLE_EVENT_ACTIONS)
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT DISTINCT e.tu_id AS tu_id, t.dest_path AS dest_path
                FROM event e
                JOIN tu t ON t.id = e.tu_id
                WHERE e.tu_id IS NOT NULL
                  AND json_extract(e.detail_json, '$.source') IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1
                    FROM event reliable
                    WHERE reliable.tu_id = e.tu_id
                      AND reliable.id != e.id
                      AND reliable.agent IS NOT NULL
                      AND reliable.action IN ({reliable_placeholders})
                      AND COALESCE(json_extract(reliable.detail_json, '$.source'), '')
                        NOT IN ({placeholders})
                  )
                """,
                (*self.BACKFILLED_SOURCES, *self.RELIABLE_EVENT_ACTIONS, *self.BACKFILLED_SOURCES),
            ).fetchall()
            return {row["tu_id"]: row["dest_path"] for row in rows}

    def events(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as con:
            aliases, _profiles = self.actor_maps()
            rows = con.execute(
                f"""
                SELECT id, ts, tu_id, agent, action, detail_json
                FROM event
                WHERE id > ?{self._reconstructed_filter()}
                ORDER BY id
                LIMIT ?
                """,
                (after, limit),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "ts": parse_dt(row["ts"]),
                    "tu_id": row["tu_id"],
                    "agent": self.canonical_actor(row["agent"], aliases),
                    "action": row["action"],
                    "detail": json.loads(row["detail_json"] or "{}"),
                }
                for row in rows
            ]

    def dashboard_state(self, attribution_repo_rev: str | None = None) -> dict[str, Any]:
        with self.connect() as con:
            self._expire_leases(con)
            counts = {key: 0 for key in TU_STATUSES}
            for row in con.execute("SELECT status, COUNT(*) AS c FROM tu GROUP BY status"):
                counts[row["status"]] = row["c"]
            total_tus = sum(counts.values())
            total_funcs = con.execute("SELECT COUNT(*) FROM func").fetchone()[0]
            done_funcs = con.execute(
                """
                SELECT COUNT(*)
                FROM func f
                JOIN tu t ON t.id=f.tu_id
                WHERE t.status='done'
                """
            ).fetchone()[0]

            active_work = [
                self._dashboard_tu(row)
                for row in con.execute(
                    """
                    SELECT *
                    FROM tu
                    WHERE status='in_progress'
                      AND lease_expires_at IS NOT NULL
                    ORDER BY updated_at DESC, id
                    LIMIT 100
                    """
                )
            ]
            self._enrich_tu_items(con, active_work)
            blocked = [
                self._dashboard_tu(row)
                for row in con.execute(
                    """
                    SELECT *
                    FROM tu
                    WHERE status='blocked'
                    ORDER BY updated_at DESC, id
                    """
                )
            ]
            self._enrich_tu_items(con, blocked)
            registered_agents = self._registered_agents()
            aliases, profiles = self.actor_maps(registered_agents)
            self._canonicalize_item_actors(active_work, aliases)
            self._canonicalize_item_actors(blocked, aliases)
            agent_work: dict[str, list[str]] = defaultdict(list)
            for item in active_work:
                if item["owner"]:
                    agent_work[item["owner"]].append(item["id"])
            completed_by_agent: dict[str, int] = defaultdict(int)
            for row in con.execute(
                f"""
                SELECT agent, COUNT(DISTINCT tu_id) AS completed
                FROM event
                WHERE agent IS NOT NULL AND action='review_pass' AND tu_id IS NOT NULL
                  {self._reconstructed_filter()}
                GROUP BY agent
                """
            ):
                actor = self.canonical_actor(row["agent"], aliases)
                if actor:
                    completed_by_agent[actor] += row["completed"]
            completed_funcs_by_agent: dict[str, int] = defaultdict(int)
            for row in con.execute(
                """
                SELECT completed_by, COUNT(*) AS completed
                FROM func
                WHERE completed_by IS NOT NULL AND status!='todo'
                GROUP BY completed_by
                """
            ):
                actor = self.canonical_actor(row["completed_by"], aliases)
                if actor:
                    completed_funcs_by_agent[actor] += row["completed"]
            attribution_cache_coverage = self._attribution_cache_coverage(con, attribution_repo_rev)
            (
                contributed_tus_by_agent,
                contributed_funcs_by_agent,
                primary_tus_by_agent,
                primary_funcs_by_agent,
            ) = self._contribution_counts_from_cache(con, aliases, attribution_repo_rev)
            last_activity_by_agent: dict[str, str] = {}
            for row in con.execute(
                f"""
                SELECT agent, MAX(ts) AS last_activity
                FROM event
                WHERE agent IS NOT NULL{self._reconstructed_filter()}
                GROUP BY agent
                """
            ):
                actor = self.canonical_actor(row["agent"], aliases)
                if actor and (
                    actor not in last_activity_by_agent
                    or row["last_activity"] > last_activity_by_agent[actor]
                ):
                    last_activity_by_agent[actor] = row["last_activity"]
            active_agents: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                """
                SELECT owner,
                       SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                       0 AS compiled,
                       COUNT(*) AS total,
                       MAX(lease_expires_at) AS lease_expires_at,
                       MAX(updated_at) AS last_update
                FROM tu
                WHERE owner IS NOT NULL
                  AND status='in_progress'
                  AND lease_expires_at IS NOT NULL
                GROUP BY owner
                ORDER BY total DESC, owner
                """
            ):
                owner = self.canonical_actor(row["owner"], aliases)
                if not owner:
                    continue
                current = active_agents.setdefault(
                    owner,
                    {
                        "in_progress": 0,
                        "compiled": 0,
                        "total": 0,
                        "lease_expires_at": None,
                        "last_update": None,
                    },
                )
                current["in_progress"] += row["in_progress"]
                current["compiled"] += row["compiled"]
                current["total"] += row["total"]
                if row["lease_expires_at"] and (
                    not current["lease_expires_at"]
                    or row["lease_expires_at"] > current["lease_expires_at"]
                ):
                    current["lease_expires_at"] = row["lease_expires_at"]
                if row["last_update"] and (
                    not current["last_update"] or row["last_update"] > current["last_update"]
                ):
                    current["last_update"] = row["last_update"]
            agent_names = (
                set(registered_agents)
                | set(active_agents)
                | set(completed_by_agent)
                | set(completed_funcs_by_agent)
                | set(contributed_tus_by_agent)
                | set(contributed_funcs_by_agent)
            )
            agents = []
            for name in sorted(
                agent_names,
                key=lambda n: (
                    -(active_agents.get(n, {}).get("total") or 0),
                    n.lower(),
                ),
            ):
                active = active_agents.get(name, {})
                registered = registered_agents.get(name, {})
                agents.append(
                    {
                        "name": name,
                        "registered": bool(registered),
                        "worker_active": bool(registered.get("active", True)),
                        "is_admin": bool(registered.get("is_admin", False)),
                        "github_username": registered.get("github_username"),
                        "worker_tokens": registered.get("tokens", 0),
                        "created_at": registered.get("created_at"),
                        "last_seen": registered.get("last_seen"),
                        "in_progress": active.get("in_progress", 0),
                        "compiled": active.get("compiled", 0),
                        "total": active.get("total", 0),
                        "has_active_work": bool(active.get("total", 0)),
                        "completed": completed_by_agent.get(name, 0),
                        "completed_tus": completed_by_agent.get(name, 0),
                        "completed_funcs": completed_funcs_by_agent.get(name, 0),
                        "contributed_tus": contributed_tus_by_agent.get(name, 0),
                        "contributed_funcs": contributed_funcs_by_agent.get(name, 0),
                        "primary_tus": primary_tus_by_agent.get(name, 0),
                        "primary_funcs": primary_funcs_by_agent.get(name, 0),
                        "lease_expires_at": active.get("lease_expires_at"),
                        "last_update": active.get("last_update"),
                        "last_activity": last_activity_by_agent.get(
                            name, active.get("last_update") or registered.get("last_seen")
                        ),
                        "current_work": agent_work.get(name, [])[:5],
                    }
                )
            # Return the full event log and full ready queue; the dashboard UI
            # filters, searches, and paginates these client-side. The payload is
            # rebuilt only when the cache is invalidated (on writes), not per read.
            recent_events = self._events_from_connection(con, after=0, limit=1_000_000)
            next_goal, next_items = self._next_tus_from_connection(con, n=1_000_000)
            goal_rows = [
                {
                    "name": row["name"],
                    "category": row["category"],
                    "source": row["source"],
                    "description": row["description"],
                    "total": row["total"],
                    "done": row["done"],
                }
                for row in con.execute(
                    """
                    SELECT g.name, g.category, g.source, g.description,
                           COUNT(gt.tu_id) AS total,
                           SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) AS done
                    FROM goal g
                    LEFT JOIN goal_tu gt ON gt.goal_name=g.name
                    LEFT JOIN tu t ON t.id=gt.tu_id
                    GROUP BY g.name
                    ORDER BY g.category, g.name
                    """
                )
            ]
            return {
                "active_goal": self._get_meta(con, "active_goal"),
                "counts": counts,
                "totals": {
                    "tus": total_tus,
                    "funcs": total_funcs,
                    "done_tus": counts["done"],
                    "done_funcs": done_funcs,
                    "tu_percent": self._percent(counts["done"], total_tus),
                    "func_percent": self._percent(done_funcs, total_funcs),
                },
                "agents": agents,
                "attribution_cache": attribution_cache_coverage,
                "actor_profiles": profiles,
                "active_work": active_work,
                "blocked": blocked,
                "recent_events": recent_events,
                "next": {
                    "active_goal": next_goal,
                    "items": [item.model_dump(mode="json") for item in next_items],
                },
                "goals": goal_rows,
                "server_time": iso(),
            }

    def facets(self) -> dict[str, Any]:
        """Filter options for the explorer UI: sources, statuses, goals."""
        with self.connect() as con:
            sources = [
                row["source"]
                for row in con.execute(
                    "SELECT DISTINCT source FROM tu WHERE source IS NOT NULL ORDER BY source"
                )
            ]
            func_statuses = [
                row["status"]
                for row in con.execute(
                    "SELECT DISTINCT status FROM func ORDER BY status"
                )
            ]
            goals = [
                row["name"]
                for row in con.execute("SELECT name FROM goal ORDER BY category, name")
            ]
            return {
                "tu_statuses": sorted(TU_STATUSES),
                "sources": sources,
                "func_statuses": func_statuses,
                "goals": goals,
            }

    def goal_detail(self, name: str) -> dict[str, Any]:
        """Everything still needed to finish a goal, ranked like the next queue."""
        with self.connect() as con:
            self._expire_leases(con)
            goal = con.execute(
                "SELECT name, category, source, description FROM goal WHERE name=?", (name,)
            ).fetchone()
            if goal is None:
                raise KeyError(f"unknown goal: {name}")

            scope = {
                row["tu_id"]
                for row in con.execute("SELECT tu_id FROM goal_tu WHERE goal_name=?", (name,))
            }
            counts = {key: 0 for key in TU_STATUSES}
            rows = con.execute(
                f"""
                SELECT *
                FROM tu
                WHERE id IN ({",".join("?" for _ in scope) if scope else "NULL"})
                """,
                list(scope),
            ).fetchall()
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + 1

            dep_map: dict[str, set[str]] = defaultdict(set)
            for row in con.execute("SELECT tu_id, dep_id FROM tu_dep"):
                dep_map[row["tu_id"]].add(row["dep_id"])
            status_by_tu = {
                row["id"]: row["status"] for row in con.execute("SELECT id, status FROM tu")
            }

            remaining: list[dict[str, Any]] = []
            for row in rows:
                if row["status"] == "done":
                    continue
                item = self._dashboard_tu(row)
                remaining.append(item)

            def rank(item: dict[str, Any]) -> tuple[int, int, int, int, str]:
                status_rank = {
                    "todo": 0,
                    "in_progress": 1,
                    "compiled": 2,
                    "blocked": 3,
                }.get(item["status"], 4)
                return (
                    status_rank,
                    item["unresolved_deps"] if item["status"] == "todo" else 0,
                    item["source"] != "decfigs",
                    item["n_funcs"],
                    item["id"],
                )

            self._enrich_tu_items(con, remaining)
            aliases, _profiles = self.actor_maps()
            self._canonicalize_item_actors(remaining, aliases)
            for item in remaining:
                scoped_deps = {dep_id for dep_id in dep_map.get(item["id"], set()) if dep_id in scope}
                unresolved = [
                    dep_id for dep_id in scoped_deps if status_by_tu.get(dep_id) != "done"
                ]
                item["total_deps"] = len(scoped_deps)
                item["unresolved_deps"] = len(unresolved)
                item["unresolved_dep_ids"] = sorted(unresolved)
            remaining.sort(key=rank)

            ready = [
                item for item in remaining
                if item["status"] == "todo" and item["unresolved_deps"] == 0
            ]
            waiting = [item for item in remaining if item["status"] == "compiled"]
            active = [
                item for item in remaining
                if item["status"] == "in_progress" and item.get("owner")
            ]
            blocked = [item for item in remaining if item["status"] == "blocked"]
            locked = [
                item for item in remaining
                if item["status"] == "todo" and item["unresolved_deps"] > 0
            ]
            total = len(scope)
            done = counts.get("done", 0)
            return {
                "name": goal["name"],
                "category": goal["category"],
                "source": goal["source"],
                "description": goal["description"],
                "total": total,
                "done": done,
                "remaining_count": total - done,
                "counts": counts,
                "ready": ready,
                "active": active,
                "waiting_review": waiting,
                "blocked": blocked,
                "locked": locked,
                "remaining": remaining,
            }

    def search_tus(
        self,
        *,
        q: str | None = None,
        statuses: list[str] | None = None,
        source: str | None = None,
        goal: str | None = None,
        owner: str | None = None,
        sort: str = "id",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filter, sort, and paginate translation units for the explorer.

        Most sorts page directly in SQL. The dependency-aware ``queue`` sort
        still ranks in Python so it stays identical to ``next_tus``.
        """
        with self.connect() as con:
            self._expire_leases(con)

            clauses: list[str] = []
            params: list[Any] = []
            joins = ""
            if goal:
                joins = "JOIN goal_tu gt ON gt.tu_id = t.id AND gt.goal_name = ?"
                params.append(goal)
            if q:
                clauses.append("(t.id LIKE ? OR t.source LIKE ? OR t.dest_path LIKE ?)")
                like = f"%{q}%"
                params.extend([like, like, like])
            if statuses:
                placeholders = ",".join("?" * len(statuses))
                clauses.append(f"t.status IN ({placeholders})")
                params.extend(statuses)
            if source:
                clauses.append("t.source = ?")
                params.append(source)
            if owner:
                clauses.append("t.owner = ?")
                params.append(owner)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

            need_deps = sort == "queue"
            if not need_deps:
                direction = "DESC" if order == "desc" else "ASC"
                order_by = {
                    "funcs": f"t.n_funcs {direction}, t.id {direction}",
                    "updated": f"COALESCE(t.updated_at, '') {direction}, t.id {direction}",
                    "status": f"t.status {direction}, t.id {direction}",
                    "id": f"t.id {direction}",
                }.get(sort, f"t.id {direction}")
                total = con.execute(
                    f"SELECT COUNT(*) FROM tu t {joins}{where}", params
                ).fetchone()[0]
                rows = con.execute(
                    f"""
                    SELECT t.*
                    FROM tu t {joins}{where}
                    ORDER BY {order_by}
                    LIMIT ? OFFSET ?
                    """,
                    [*params, limit, offset],
                ).fetchall()
                items = [self._dashboard_tu(row) for row in rows]
                self._enrich_tu_items(con, items)
                aliases, _profiles = self.actor_maps()
                self._canonicalize_item_actors(items, aliases)
                return {"total": total, "limit": limit, "offset": offset, "items": items}

            rows = con.execute(f"SELECT t.* FROM tu t {joins}{where}", params).fetchall()
            unresolved = self._unresolved_dep_counts(con, [row["id"] for row in rows])

            items = [self._dashboard_tu(row) for row in rows]
            for item in items:
                item["unresolved_deps"] = unresolved.get(item["id"])

            reverse = order == "desc"
            if sort == "funcs":
                items.sort(key=lambda it: (it["n_funcs"], it["id"]), reverse=reverse)
            elif sort == "updated":
                items.sort(key=lambda it: (it["updated_at"] or "", it["id"]), reverse=reverse)
            elif sort == "status":
                items.sort(key=lambda it: (it["status"], it["id"]), reverse=reverse)
            elif sort == "queue":
                items.sort(
                    key=lambda it: (
                        it["unresolved_deps"] if it["unresolved_deps"] is not None else 1 << 30,
                        it["source"] != "decfigs",
                        it["n_funcs"],
                        it["id"],
                    )
                )
            else:  # id
                items.sort(key=lambda it: it["id"], reverse=reverse)

            total = len(items)
            page = items[offset : offset + limit]
            self._enrich_tu_items(con, page)
            aliases, _profiles = self.actor_maps()
            self._canonicalize_item_actors(page, aliases)
            return {"total": total, "limit": limit, "offset": offset, "items": page}

    def tu_detail(self, tu_id: str) -> dict[str, Any]:
        """Everything the server knows about one TU -- the data given to agents."""
        with self.connect() as con:
            self._expire_leases(con)
            row = self._require_tu(con, tu_id)
            detail = self._dashboard_tu(row)
            detail["funcs"] = [
                {
                    "name": r["name"],
                    "status": r["status"],
                    "completed_by": r["completed_by"],
                    "completed_at": r["completed_at"],
                }
                for r in con.execute(
                    "SELECT name, status, completed_by, completed_at FROM func WHERE tu_id=? ORDER BY name",
                    (tu_id,),
                )
            ]
            detail["deps"] = [
                {
                    "id": r["dep_id"],
                    "weight": r["weight"],
                    "status": r["status"],
                    "source": r["source"],
                }
                for r in con.execute(
                    """
                    SELECT d.dep_id, d.weight, t.status, t.source
                    FROM tu_dep d
                    LEFT JOIN tu t ON t.id = d.dep_id
                    WHERE d.tu_id = ?
                    ORDER BY d.weight DESC, d.dep_id
                    """,
                    (tu_id,),
                )
            ]
            detail["dependents"] = [
                {"id": r["tu_id"], "weight": r["weight"], "status": r["status"]}
                for r in con.execute(
                    """
                    SELECT d.tu_id, d.weight, t.status
                    FROM tu_dep d
                    LEFT JOIN tu t ON t.id = d.tu_id
                    WHERE d.dep_id = ?
                    ORDER BY d.weight DESC, d.tu_id
                    """,
                    (tu_id,),
                )
            ]
            detail["goals"] = [
                r["goal_name"]
                for r in con.execute(
                    "SELECT goal_name FROM goal_tu WHERE tu_id=? ORDER BY goal_name", (tu_id,)
                )
            ]
            self._enrich_tu_items(con, [detail])
            aliases, _profiles = self.actor_maps()
            self._canonicalize_item_actors([detail], aliases)
            for func in detail["funcs"]:
                func["completed_by"] = self.canonical_actor(func["completed_by"], aliases)
            return detail

    def search_funcs(
        self,
        *,
        q: str | None = None,
        statuses: list[str] | None = None,
        tu: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filter and paginate functions across all TUs."""
        with self.connect() as con:
            clauses: list[str] = []
            params: list[Any] = []
            if q:
                clauses.append("f.name LIKE ?")
                params.append(f"%{q}%")
            if statuses:
                placeholders = ",".join("?" * len(statuses))
                clauses.append(f"f.status IN ({placeholders})")
                params.extend(statuses)
            if tu:
                clauses.append("f.tu_id = ?")
                params.append(tu)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            total = con.execute(
                f"SELECT COUNT(*) FROM func f{where}", params
            ).fetchone()[0]
            rows = con.execute(
                f"""
                SELECT f.name, f.tu_id, f.status, f.completed_by, f.completed_at,
                       t.dest_path AS tu_dest_path
                FROM func f
                LEFT JOIN tu t ON t.id = f.tu_id
                {where}
                ORDER BY f.name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = [
                {
                    "name": r["name"],
                    "tu_id": r["tu_id"],
                    "status": r["status"],
                    "completed_by": r["completed_by"],
                    "completed_at": r["completed_at"],
                    "tu_dest_path": r["tu_dest_path"],
                }
                for r in rows
            ]
            aliases, _profiles = self.actor_maps()
            for item in items:
                item["completed_by"] = self.canonical_actor(item["completed_by"], aliases)
            self._enrich_func_items_from_events(con, items, aliases)
            return {"total": total, "limit": limit, "offset": offset, "items": items}

    def actor_profile(
        self,
        name: str,
        attribution_repo_rev: str | None = None,
    ) -> dict[str, Any]:
        """Aggregated dashboard profile for one canonical contributor."""
        requested = name.strip()
        if not requested:
            raise KeyError("empty actor name")
        with self.connect() as con:
            self._expire_leases(con)
            registered_agents = self._registered_agents()
            aliases, profiles = self.actor_maps(registered_agents)
            actor = self.canonical_actor(requested, aliases) or requested
            registered = registered_agents.get(actor, {})
            github_username = registered.get("github_username") or profiles.get(actor)
            alias_values = sorted(
                {
                    alias
                    for alias, canonical in aliases.items()
                    if canonical == actor and alias != actor.lower()
                }
            )

            active_work = [
                self._dashboard_tu(row)
                for row in con.execute(
                    """
                    SELECT *
                    FROM tu
                    WHERE owner IS NOT NULL
                      AND status='in_progress'
                      AND lease_expires_at IS NOT NULL
                    ORDER BY updated_at DESC, id
                    """
                )
                if self.canonical_actor(row["owner"], aliases) == actor
            ]
            self._enrich_tu_items(con, active_work)
            self._canonicalize_item_actors(active_work, aliases)

            completed_tus = self._profile_completed_tu_items(con, actor, aliases)
            completed_funcs = self._profile_completed_func_items(con, actor, aliases)
            contributed_tus, contributed_lines = self._profile_contributed_tus(
                con, actor, aliases, attribution_repo_rev
            )
            contributed_funcs, contributed_func_lines = self._profile_contributed_funcs(
                con, actor, aliases, attribution_repo_rev
            )
            profile_tus = self._merge_profile_items(completed_tus, contributed_tus)
            profile_funcs = self._merge_profile_items(completed_funcs, contributed_funcs, key="name")
            recent_events = self._profile_events(con, actor, aliases, limit=80)
            # Activity graph: drive it from the real commit dates of contributed TUs, so
            # it shows a true timeline for everyone (the event log is thin and its
            # reconstructed rows share one backdated date -> a single misleading bar).
            activity_by_day = self._profile_activity_by_day(list(contributed_tus.values()))
            action_counts = self._profile_action_counts(recent_events)
            status_counts = {key: 0 for key in TU_STATUSES}
            for item in profile_tus.values():
                if item.get("status") in status_counts:
                    status_counts[item["status"]] += 1

            source_counts: dict[str, int] = defaultdict(int)
            source_lines: dict[str, int] = defaultdict(int)
            goal_counts: dict[str, int] = defaultdict(int)
            for item in profile_tus.values():
                source = item.get("source") or "unknown"
                source_counts[source] += 1
                source_lines[source] += int(item.get("lines") or 0)
                for goal in item.get("goals") or []:
                    goal_counts[goal] += 1

            last_activity_candidates = [
                value
                for value in [
                    registered.get("last_seen"),
                    *(event.get("ts") for event in recent_events),
                    *(item.get("latest_change_at") for item in contributed_tus.values()),
                ]
                if value
            ]
            last_activity = max(last_activity_candidates) if last_activity_candidates else None

            return {
                "name": actor,
                "github_username": github_username,
                "registered": bool(registered),
                "worker_active": bool(registered.get("active", True)),
                "is_admin": bool(registered.get("is_admin", False)),
                "aliases": alias_values,
                "summary": {
                    "active_tus": len(active_work),
                    "completed_tus": len(completed_tus),
                    "completed_funcs": len(completed_funcs),
                    "contributed_tus": len(profile_tus),
                    "contributed_funcs": len(profile_funcs),
                    "contributed_lines": contributed_lines,
                    "contributed_function_lines": contributed_func_lines,
                    "attributed_tus": len(contributed_tus),
                    "attributed_funcs": len(contributed_funcs),
                    "recent_events": len(recent_events),
                    "last_activity": last_activity,
                },
                "status_counts": status_counts,
                "activity_by_day": activity_by_day,
                "action_counts": action_counts,
                "sources": [
                    {"name": source, "tus": source_counts[source], "lines": source_lines[source]}
                    for source in sorted(
                        source_counts, key=lambda key: (-source_counts[key], -source_lines[key], key)
                    )[:12]
                ],
                "goals": [
                    {"name": goal, "tus": goal_counts[goal]}
                    for goal in sorted(goal_counts, key=lambda key: (-goal_counts[key], key))[:12]
                ],
                "active_work": active_work[:20],
                "top_tus": sorted(
                    profile_tus.values(),
                    key=lambda item: (
                        -(item.get("lines") or 0),
                        item.get("basis") != "surviving_lines",
                        item["id"],
                    ),
                )[:25],
                "top_funcs": sorted(
                    profile_funcs.values(),
                    key=lambda item: (
                        -(item.get("lines") or 0),
                        item.get("basis") != "surviving_lines",
                        item["name"],
                    ),
                )[:25],
                "recent_events": recent_events[:40],
                "attribution_cache": self._attribution_cache_coverage(con, attribution_repo_rev),
            }

    def _next_tus_from_connection(
        self, con: sqlite3.Connection, n: int = 1, goal: str | None = None
    ) -> tuple[str | None, list[NextTu]]:
        active_goal = goal or self._get_meta(con, "active_goal")
        scope = self._goal_scope(con, active_goal) if active_goal else None

        rows = con.execute(
            """
            SELECT id, source, n_funcs, n_decfigs, dest_path
            FROM tu
            WHERE status='todo'
            """
        ).fetchall()

        unresolved_by_tu = self._unresolved_dep_counts(
            con,
            [row["id"] for row in rows],
            scope=scope,
        )
        ranked: list[dict[str, Any]] = []
        for row in rows:
            if scope is not None and row["id"] not in scope:
                continue
            unresolved = unresolved_by_tu.get(row["id"], 0)
            ranked.append({**dict(row), "unresolved_deps": unresolved})

        ranked.sort(
            key=lambda row: (
                row["unresolved_deps"],
                row["source"] != "decfigs",
                row["n_funcs"],
                row["id"],
            )
        )
        return active_goal, [NextTu(**row) for row in ranked[:n]]

    def _unresolved_dep_counts(
        self,
        con: sqlite3.Connection,
        tu_ids: list[str],
        *,
        scope: set[str] | None = None,
    ) -> dict[str, int]:
        """Count unresolved deps for specific TUs without scanning unrelated rows."""
        if not tu_ids:
            return {}
        counts = {tu_id: 0 for tu_id in tu_ids}
        for chunk_start in range(0, len(tu_ids), 500):
            chunk = tu_ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in con.execute(
                f"""
                SELECT d.tu_id, d.dep_id, dep.status AS dep_status
                FROM tu_dep d
                LEFT JOIN tu dep ON dep.id=d.dep_id
                WHERE d.tu_id IN ({placeholders})
                """,
                chunk,
            ):
                if scope is not None and row["dep_id"] not in scope:
                    continue
                if row["dep_status"] != "done":
                    counts[row["tu_id"]] += 1
        return counts

    def _restore_status(self, con: sqlite3.Connection, status: dict[str, Any]) -> int:
        rows = 0
        for tu_id, data in status.get("tu", {}).items():
            tu_status = data.get("status", "todo")
            # Skip todo and the transient in_progress/compiled states: they are owned by
            # the live server, not the snapshot. Importing them is what was filling Active
            # Work with stale, lease-less "claims" carried over from status.json.
            if tu_status not in DURABLE_IMPORT_STATUSES:
                continue
            # Durable status wins, but the owner/lease from the snapshot is dropped: a
            # done/blocked TU holds no live claim. The `status != ?` guard keeps re-syncs
            # from needlessly bumping updated_at on rows that already match.
            cur = con.execute(
                """
                UPDATE tu
                SET status=?, owner=NULL, notes=?, claimed_at=NULL, lease_expires_at=NULL,
                    updated_at=?
                WHERE id=? AND status != ?
                """,
                (tu_status, data.get("notes"), iso(), tu_id, tu_status),
            )
            rows += cur.rowcount
        for name, data in status.get("func", {}).items():
            con.execute(
                "UPDATE func SET status=? WHERE name=?",
                (data.get("status", "todo"), name),
            )
        return rows

    def _restore_deps(self, con: sqlite3.Connection, deps: list[list[Any]]) -> int:
        con.execute("DELETE FROM tu_dep")
        rows = 0
        for tu_id, dep_id, weight in deps:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO tu_dep(tu_id, dep_id, weight)
                VALUES(?, ?, ?)
                """,
                (tu_id, dep_id, int(weight or 1)),
            )
            rows += cur.rowcount
        return rows

    def _restore_goals(self, con: sqlite3.Connection, goals: dict[str, Any]) -> int:
        con.execute("DELETE FROM goal_tu")
        con.execute("DELETE FROM goal")
        active = goals.get("active_goal")
        if active:
            self._set_meta(con, "active_goal", active)
        elif active is None:
            self._set_meta(con, "active_goal", None)

        target_strings: dict[str, list[str]] = defaultdict(list)
        for row in con.execute("SELECT id FROM tu"):
            target_strings[row["id"]].append(row["id"])
        for row in con.execute("SELECT name, tu_id FROM func"):
            target_strings[row["tu_id"]].append(row["name"])

        count = 0
        for category, bucket in goals.get("goals", {}).items():
            if not isinstance(bucket, dict):
                continue
            for name, goal in bucket.items():
                con.execute(
                    """
                    INSERT INTO goal(name, category, description, source)
                    VALUES(?, ?, ?, ?)
                    """,
                    (name, category, goal.get("description"), goal.get("source")),
                )
                selected = self._select_goal_tus(goal, target_strings)
                for tu_id in selected:
                    con.execute(
                        "INSERT OR IGNORE INTO goal_tu(goal_name, tu_id) VALUES(?, ?)",
                        (name, tu_id),
                    )
                count += 1
        return count

    def _select_goal_tus(self, goal: dict[str, Any], targets: dict[str, list[str]]) -> set[str]:
        selected = set(goal.get("include_tus") or [])
        include_globs = goal.get("include") or []
        exclude_globs = goal.get("exclude") or []
        exclude_tus = set(goal.get("exclude_tus") or [])
        if include_globs:
            for tu_id, strings in targets.items():
                if any(
                    fnmatch.fnmatchcase(value, pattern)
                    for value in strings
                    for pattern in include_globs
                ) and not any(
                    fnmatch.fnmatchcase(value, pattern)
                    for value in strings
                    for pattern in exclude_globs
                ):
                    selected.add(tu_id)
        selected -= exclude_tus
        return selected

    def _expire_leases(self, con: sqlite3.Connection, now: datetime | None = None) -> int:
        threshold = iso(now)
        missing_rows = con.execute(
            """
            SELECT id, owner FROM tu
            WHERE status='in_progress'
              AND lease_expires_at IS NULL
            """
        ).fetchall()
        for row in missing_rows:
            con.execute(
                """
                UPDATE tu
                SET status='todo', owner=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    notes='lease missing', updated_at=?
                WHERE id=?
                """,
                (threshold, row["id"]),
            )
            self._log(con, row["owner"], "lease_missing", row["id"], {})

        rows = con.execute(
            """
            SELECT id, owner FROM tu
            WHERE status='in_progress'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < ?
            """,
            (threshold,),
        ).fetchall()
        for row in rows:
            con.execute(
                """
                UPDATE tu
                SET status='todo', owner=NULL, claimed_at=NULL, lease_expires_at=NULL,
                    notes='lease expired', updated_at=?
                WHERE id=?
                """,
                (threshold, row["id"]),
            )
            self._log(con, row["owner"], "lease_expired", row["id"], {})
        return len(missing_rows) + len(rows)

    def _goal_scope(self, con: sqlite3.Connection, goal: str | None) -> set[str] | None:
        if not goal:
            return None
        exists = con.execute("SELECT 1 FROM goal WHERE name=?", (goal,)).fetchone()
        if not exists:
            return set()
        return {row["tu_id"] for row in con.execute("SELECT tu_id FROM goal_tu WHERE goal_name=?", (goal,))}

    def _require_tu(self, con: sqlite3.Connection, tu_id: str) -> sqlite3.Row:
        row = con.execute("SELECT * FROM tu WHERE id=?", (tu_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown TU: {tu_id}")
        return row

    def _require_owner(self, row: sqlite3.Row, agent: str) -> None:
        if row["owner"] and row["owner"] != agent:
            raise PermissionError(f"TU is owned by {row['owner']}")
        if row["status"] not in {"in_progress", "compiled"}:
            raise ValueError(f"TU is {row['status']}, not in progress")

    def _return_tu_to_todo(
        self, con: sqlite3.Connection, tu_id: str, notes: str | None = None
    ) -> None:
        row = self._require_tu(con, tu_id)
        con.execute(
            """
            UPDATE tu
            SET status='todo', owner=NULL, notes=?, claimed_at=NULL,
                lease_expires_at=NULL, updated_at=?
            WHERE id=?
            """,
            (notes, iso(), tu_id),
        )
        con.execute(
            """
            UPDATE func
            SET status='todo', completed_by=NULL, completed_at=NULL
            WHERE tu_id=?
            """,
            (tu_id,),
        )
        if row["dest_path"]:
            con.execute("DELETE FROM attribution_cache WHERE dest_path=?", (row["dest_path"],))

    def _tu_record(self, row: sqlite3.Row) -> TuRecord:
        return TuRecord(
            id=row["id"],
            source=row["source"],
            status=row["status"],
            n_funcs=row["n_funcs"],
            n_decfigs=row["n_decfigs"],
            dest_path=row["dest_path"],
            owner=row["owner"],
            notes=row["notes"],
            updated_at=parse_dt(row["updated_at"]),
            lease_expires_at=parse_dt(row["lease_expires_at"]),
        )

    def _log(
        self,
        con: sqlite3.Connection,
        agent: str | None,
        action: str,
        tu_id: str | None,
        detail: dict[str, Any],
    ) -> None:
        con.execute(
            "INSERT INTO event(ts, tu_id, agent, action, detail_json) VALUES(?, ?, ?, ?, ?)",
            (iso(), tu_id, agent, action, json.dumps(detail, sort_keys=True)),
        )

    def _events_from_connection(
        self, con: sqlite3.Connection, after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        aliases, _profiles = self.actor_maps()
        backfilled_placeholders = ",".join("?" for _ in self.BACKFILLED_SOURCES)
        reliable_placeholders = ",".join("?" for _ in self.RELIABLE_EVENT_ACTIONS)
        hidden_placeholders = ",".join("?" for _ in self.DASHBOARD_HIDDEN_ACTIONS)
        rows = con.execute(
            f"""
            SELECT id, ts, tu_id, agent, action, detail_json
            FROM event
            WHERE id > ?
              AND action NOT IN ({hidden_placeholders}){self._reconstructed_filter()}
              AND NOT (
                tu_id IS NOT NULL
                AND COALESCE(
                  json_extract(detail_json, '$.source') IN ({backfilled_placeholders}), 0
                )
                AND EXISTS (
                  SELECT 1
                  FROM event reliable
                  WHERE reliable.tu_id = event.tu_id
                    AND reliable.id != event.id
                    AND reliable.agent IS NOT NULL
                    AND reliable.action IN ({reliable_placeholders})
                    AND COALESCE(json_extract(reliable.detail_json, '$.source'), '')
                      NOT IN ({backfilled_placeholders})
                )
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                after,
                *self.DASHBOARD_HIDDEN_ACTIONS,
                *self.BACKFILLED_SOURCES,
                *self.RELIABLE_EVENT_ACTIONS,
                *self.BACKFILLED_SOURCES,
                limit,
            ),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "tu_id": row["tu_id"],
                "agent": self.canonical_actor(row["agent"], aliases),
                "action": row["action"],
                "detail": json.loads(row["detail_json"] or "{}"),
            }
            for row in rows
        ]

    def _dashboard_tu(self, row: sqlite3.Row) -> dict[str, Any]:
        has_live_claim = row["status"] == "in_progress" and bool(row["lease_expires_at"])
        return {
            "id": row["id"],
            "source": row["source"],
            "status": row["status"],
            "n_funcs": row["n_funcs"],
            "n_decfigs": row["n_decfigs"],
            "dest_path": row["dest_path"],
            "owner": row["owner"] if has_live_claim else None,
            "notes": row["notes"],
            "updated_at": row["updated_at"],
            "lease_expires_at": row["lease_expires_at"],
            "commit": row["commit_hash"],
        }

    def _enrich_tu_items(self, con: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
        """Attach derived fields used by the dashboard/explorer.

        `owner` remains the live claim owner. Historical actor fields come from the
        server event log only, so imported durable status never pretends to know an
        author it does not actually have.
        """
        ids = [item["id"] for item in items]
        if not ids:
            return
        by_id = {item["id"]: item for item in items}
        placeholders = ",".join("?" for _ in ids)
        hidden_placeholders = ",".join("?" for _ in self.DASHBOARD_HIDDEN_ACTIONS)

        for item in items:
            item.setdefault("total_deps", 0)
            item.setdefault("unresolved_deps", 0)
            item.setdefault("last_actor", None)
            item.setdefault("last_action", None)
            item.setdefault("last_event_at", None)
            item.setdefault("completed_by", None)
            item.setdefault("completed_at", None)

        for row in con.execute(
            f"""
            SELECT d.tu_id,
                   COUNT(*) AS total_deps,
                   SUM(CASE WHEN COALESCE(t.status, 'todo') != 'done' THEN 1 ELSE 0 END)
                     AS unresolved_deps
            FROM tu_dep d
            LEFT JOIN tu t ON t.id=d.dep_id
            WHERE d.tu_id IN ({placeholders})
            GROUP BY d.tu_id
            """,
            ids,
        ):
            item = by_id.get(row["tu_id"])
            if item is not None:
                item["total_deps"] = row["total_deps"] or 0
                item["unresolved_deps"] = row["unresolved_deps"] or 0

        for row in con.execute(
            f"""
            SELECT e.tu_id, e.agent, e.action, e.ts
            FROM event e
            JOIN (
              SELECT tu_id, MAX(id) AS max_id
              FROM event
              WHERE tu_id IN ({placeholders})
                AND agent IS NOT NULL
                AND action NOT IN ({hidden_placeholders}){self._reconstructed_filter()}
              GROUP BY tu_id
            ) latest ON latest.max_id=e.id
            """,
            [*ids, *self.DASHBOARD_HIDDEN_ACTIONS],
        ):
            item = by_id.get(row["tu_id"])
            if item is not None:
                item["last_actor"] = row["agent"]
                item["last_action"] = row["action"]
                item["last_event_at"] = row["ts"]

        for row in con.execute(
            f"""
            SELECT e.tu_id, e.agent, e.ts
            FROM event e
            JOIN (
              SELECT tu_id, MAX(id) AS max_id
              FROM event
              WHERE tu_id IN ({placeholders})
                AND agent IS NOT NULL
                AND action='review_pass'
              GROUP BY tu_id
            ) completed ON completed.max_id=e.id
            """,
            ids,
        ):
            item = by_id.get(row["tu_id"])
            if item is not None and item.get("status") == "done":
                item["completed_by"] = row["agent"]
                item["completed_at"] = row["ts"]

    def _canonicalize_item_actors(self, items: list[dict[str, Any]], aliases: dict[str, str]) -> None:
        for item in items:
            for key in ("owner", "last_actor", "completed_by"):
                item[key] = self.canonical_actor(item.get(key), aliases)

    def _enrich_func_items_from_events(
        self,
        con: sqlite3.Connection,
        items: list[dict[str, Any]],
        aliases: dict[str, str],
    ) -> None:
        tu_ids = sorted({
            item["tu_id"]
            for item in items
            if item.get("tu_id") and item.get("status") != "todo"
        })
        if not tu_ids:
            return
        by_tu = {tu_id: [] for tu_id in tu_ids}
        for item in items:
            if item.get("tu_id") in by_tu:
                by_tu[item["tu_id"]].append(item)
        placeholders = ",".join("?" for _ in tu_ids)
        for row in con.execute(
            f"""
            SELECT e.tu_id, e.agent, e.ts, e.detail_json
            FROM event e
            JOIN (
              SELECT tu_id, MAX(id) AS max_id
              FROM event
              WHERE tu_id IN ({placeholders})
                AND agent IS NOT NULL
                AND action='review_pass'
              GROUP BY tu_id
            ) latest ON latest.max_id=e.id
            """,
            tu_ids,
        ):
            actor = self.canonical_actor(row["agent"], aliases)
            detail = json.loads(row["detail_json"] or "{}")
            login = detail.get("github_login")
            for item in by_tu.get(row["tu_id"], []):
                if item.get("status") == "todo":
                    continue
                item["completed_by"] = actor
                item["completed_at"] = row["ts"]
                if login:
                    item["completed_by_login"] = login

    def _prefer_username(self, candidate: str, current: str) -> bool:
        """Pick a display spelling when worker rows only differ by case."""
        return self._username_score(candidate) > self._username_score(current)

    def _username_score(self, username: str) -> tuple[int, int, int]:
        return (
            int(any(ch.isupper() for ch in username)),
            int(username[:1].isupper()),
            -sum(1 for ch in username if ch.isupper()),
        )

    def _contribution_counts_from_cache(
        self,
        con: sqlite3.Connection,
        aliases: dict[str, str],
        repo_rev: str | None,
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
        # "contributed" = any surviving-line author (a TU/func can count for several
        # people). "primary" = the single dominant author (most surviving lines),
        # credited only when that author maps to a worker -- a subset of contributed.
        contributed_tus: dict[str, set[str]] = defaultdict(set)
        contributed_funcs: dict[str, set[str]] = defaultdict(set)
        primary_tus: dict[str, set[str]] = defaultdict(set)
        primary_funcs: dict[str, set[str]] = defaultdict(set)
        if not repo_rev:
            return {}, {}, {}, {}

        def contributor_actor(contributor: dict[str, Any]) -> str | None:
            email = str(contributor.get("email") or "").strip()
            for candidate in (login_from_noreply_email(email), email, contributor.get("name")):
                cleaned = str(candidate or "").strip()
                if not cleaned:
                    continue
                actor = self.canonical_actor(cleaned, aliases)
                if actor:
                    return actor
            return None

        def primary_actor(contributors: list[dict[str, Any]]) -> str | None:
            best: tuple[int, dict[str, Any]] | None = None
            for contributor in contributors:
                lines = int(contributor.get("lines") or 0)
                if lines <= 0:
                    continue
                if best is None or lines > best[0]:
                    best = (lines, contributor)
            return contributor_actor(best[1]) if best else None

        for row in con.execute(
            """
            SELECT t.id AS tu_id, ac.payload_json
            FROM attribution_cache ac
            JOIN tu t ON t.dest_path=ac.dest_path
            WHERE ac.scope='file'
              AND ac.repo_rev=?
              AND t.status='done'
            """,
            (repo_rev,),
        ):
            payload = json.loads(row["payload_json"] or "{}")
            contributors = (payload.get("contributors") or {}).get("contributors") or []
            for contributor in contributors:
                if int(contributor.get("lines") or 0) <= 0:
                    continue
                actor = contributor_actor(contributor)
                if actor:
                    contributed_tus[actor].add(row["tu_id"])
            primary = primary_actor(contributors)
            if primary:
                primary_tus[primary].add(row["tu_id"])

        for row in con.execute(
            """
            SELECT f.name AS func_name, ac.payload_json
            FROM attribution_cache ac
            JOIN tu t ON t.dest_path=ac.dest_path
            JOIN func f ON f.tu_id=t.id AND f.name=ac.function_name
            WHERE ac.scope='function'
              AND ac.repo_rev=?
              AND f.status!='todo'
            """,
            (repo_rev,),
        ):
            payload = json.loads(row["payload_json"] or "{}")
            contributors = payload.get("contributors") or []
            for contributor in contributors:
                if int(contributor.get("lines") or 0) <= 0:
                    continue
                actor = contributor_actor(contributor)
                if actor:
                    contributed_funcs[actor].add(row["func_name"])
            primary = primary_actor(contributors)
            if primary:
                primary_funcs[primary].add(row["func_name"])

        return (
            {actor: len(tus) for actor, tus in contributed_tus.items()},
            {actor: len(funcs) for actor, funcs in contributed_funcs.items()},
            {actor: len(tus) for actor, tus in primary_tus.items()},
            {actor: len(funcs) for actor, funcs in primary_funcs.items()},
        )

    def _profile_contributor_actor(
        self, contributor: dict[str, Any], aliases: dict[str, str]
    ) -> str | None:
        email = str(contributor.get("email") or "").strip()
        for candidate in (login_from_noreply_email(email), email, contributor.get("name")):
            cleaned = str(candidate or "").strip()
            if not cleaned:
                continue
            actor = self.canonical_actor(cleaned, aliases)
            if actor:
                return actor
        return None

    def _profile_file_contributors(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        contributors = payload.get("contributors") or {}
        if isinstance(contributors, dict):
            return contributors.get("contributors") or []
        if isinstance(contributors, list):
            return contributors
        return []

    def _profile_func_contributors(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        contributors = payload.get("contributors") or []
        return contributors if isinstance(contributors, list) else []

    def _profile_completed_tu_items(
        self, con: sqlite3.Connection, actor: str, aliases: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        goals_by_tu: dict[str, list[str]] = defaultdict(list)
        for goal in con.execute("SELECT goal_name, tu_id FROM goal_tu ORDER BY goal_name"):
            goals_by_tu[goal["tu_id"]].append(goal["goal_name"])
        completed: dict[str, dict[str, Any]] = {}
        for row in con.execute(
            f"""
            SELECT e.agent, e.tu_id, MAX(e.ts) AS completed_at,
                   t.source, t.status, t.n_funcs, t.dest_path
            FROM event e
            JOIN tu t ON t.id=e.tu_id
            WHERE e.agent IS NOT NULL
              AND e.action='review_pass'
              AND e.tu_id IS NOT NULL{self._reconstructed_filter("e.detail_json")}
            GROUP BY e.agent, e.tu_id
            """
        ):
            if self.canonical_actor(row["agent"], aliases) == actor:
                completed[row["tu_id"]] = {
                    "id": row["tu_id"],
                    "source": row["source"],
                    "status": row["status"],
                    "n_funcs": row["n_funcs"],
                    "dest_path": row["dest_path"],
                    "goals": goals_by_tu.get(row["tu_id"], []),
                    "lines": 0,
                    "percent": 0,
                    "basis": "review_pass",
                    "completed_at": row["completed_at"],
                    "latest_change_at": row["completed_at"],
                }
        return completed

    def _profile_completed_func_items(
        self, con: sqlite3.Connection, actor: str, aliases: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        completed: dict[str, dict[str, Any]] = {}
        for row in con.execute(
            """
            SELECT f.name, f.tu_id, f.status, f.completed_by, f.completed_at, t.dest_path
            FROM func f
            LEFT JOIN tu t ON t.id=f.tu_id
            WHERE f.completed_by IS NOT NULL
              AND f.status!='todo'
            """
        ):
            if self.canonical_actor(row["completed_by"], aliases) == actor:
                completed[row["name"]] = {
                    "name": row["name"],
                    "tu_id": row["tu_id"],
                    "status": row["status"],
                    "dest_path": row["dest_path"],
                    "lines": 0,
                    "percent": 0,
                    "basis": "completed_by",
                    "completed_at": row["completed_at"],
                }
        return completed

    def _merge_profile_items(
        self,
        base: dict[str, dict[str, Any]],
        attributed: dict[str, dict[str, Any]],
        *,
        key: str = "id",
    ) -> dict[str, dict[str, Any]]:
        merged = {item_key: dict(item) for item_key, item in base.items()}
        for item_key, item in attributed.items():
            current = merged.get(item_key)
            if not current:
                merged[item_key] = dict(item)
                merged[item_key]["basis"] = "surviving_lines"
                continue
            current.update({k: v for k, v in item.items() if v not in (None, "", [])})
            current["lines"] = int(item.get("lines") or 0)
            current["percent"] = item.get("percent") or current.get("percent") or 0
            current["basis"] = "surviving_lines"
            if key not in current:
                current[key] = item_key
        return merged

    def _profile_contributed_tus(
        self,
        con: sqlite3.Connection,
        actor: str,
        aliases: dict[str, str],
        repo_rev: str | None,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        if not repo_rev:
            return {}, 0
        contributed: dict[str, dict[str, Any]] = {}
        total_lines = 0
        goals_by_tu: dict[str, list[str]] = defaultdict(list)
        for goal in con.execute("SELECT goal_name, tu_id FROM goal_tu ORDER BY goal_name"):
            goals_by_tu[goal["tu_id"]].append(goal["goal_name"])
        for row in con.execute(
            """
            SELECT t.id AS tu_id, t.source, t.status, t.n_funcs, t.dest_path, ac.payload_json
            FROM attribution_cache ac
            JOIN tu t ON t.dest_path=ac.dest_path
            WHERE ac.scope='file'
              AND ac.repo_rev=?
              AND t.status='done'
            """,
            (repo_rev,),
        ):
            payload = json.loads(row["payload_json"] or "{}")
            for contributor in self._profile_file_contributors(payload):
                if self._profile_contributor_actor(contributor, aliases) != actor:
                    continue
                lines = int(contributor.get("lines") or 0)
                if lines <= 0:
                    continue
                latest = payload.get("latest") or {}
                item = contributed.setdefault(
                    row["tu_id"],
                    {
                        "id": row["tu_id"],
                        "source": row["source"],
                        "status": row["status"],
                        "n_funcs": row["n_funcs"],
                        "dest_path": row["dest_path"],
                        "goals": goals_by_tu.get(row["tu_id"], []),
                        "lines": 0,
                        "percent": 0,
                        "basis": "surviving_lines",
                        "latest_change_at": latest.get("date"),
                    },
                )
                item["lines"] += lines
                item["percent"] = max(float(contributor.get("percent") or 0), item["percent"])
                total_lines += lines
        return contributed, total_lines

    def _profile_contributed_funcs(
        self,
        con: sqlite3.Connection,
        actor: str,
        aliases: dict[str, str],
        repo_rev: str | None,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        if not repo_rev:
            return {}, 0
        contributed: dict[str, dict[str, Any]] = {}
        total_lines = 0
        for row in con.execute(
            """
            SELECT f.name, f.tu_id, f.status, t.dest_path, ac.payload_json
            FROM attribution_cache ac
            JOIN tu t ON t.dest_path=ac.dest_path
            JOIN func f ON f.tu_id=t.id AND f.name=ac.function_name
            WHERE ac.scope='function'
              AND ac.repo_rev=?
              AND f.status!='todo'
            """,
            (repo_rev,),
        ):
            payload = json.loads(row["payload_json"] or "{}")
            for contributor in self._profile_func_contributors(payload):
                if self._profile_contributor_actor(contributor, aliases) != actor:
                    continue
                lines = int(contributor.get("lines") or 0)
                if lines <= 0:
                    continue
                item = contributed.setdefault(
                    row["name"],
                    {
                        "name": row["name"],
                        "tu_id": row["tu_id"],
                        "status": row["status"],
                        "dest_path": row["dest_path"],
                        "lines": 0,
                        "percent": 0,
                        "basis": "surviving_lines",
                    },
                )
                item["lines"] += lines
                item["percent"] = max(float(contributor.get("percent") or 0), item["percent"])
                total_lines += lines
        return contributed, total_lines

    def _profile_events(
        self,
        con: sqlite3.Connection,
        actor: str,
        aliases: dict[str, str],
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        hidden_placeholders = ",".join("?" for _ in self.DASHBOARD_HIDDEN_ACTIONS)
        for row in con.execute(
            f"""
            SELECT id, ts, tu_id, agent, action, detail_json
            FROM event
            WHERE agent IS NOT NULL
              AND action NOT IN ({hidden_placeholders}){self._reconstructed_filter()}
            ORDER BY id DESC
            LIMIT 5000
            """,
            [*self.DASHBOARD_HIDDEN_ACTIONS],
        ):
            canonical = self.canonical_actor(row["agent"], aliases)
            if canonical != actor:
                continue
            events.append(
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "tu_id": row["tu_id"],
                    "agent": canonical,
                    "action": row["action"],
                    "detail": json.loads(row["detail_json"] or "{}"),
                }
            )
            if len(events) >= limit:
                break
        return events

    def _profile_activity_by_day(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per-day activity from the real git commit dates of the actor's contributed
        TUs (each item's ``latest_change_at``), not the event log. The event log is thin
        and its reconstructed entries share one backdated timestamp, which collapsed the
        graph into a single bar; committed-file dates spread across the real timeline and
        exist for every contributor.
        """
        counts: dict[str, int] = defaultdict(int)
        for item in items:
            ts = str(item.get("latest_change_at") or "")
            if len(ts) >= 10:
                counts[ts[:10]] += 1
        return [{"date": date, "count": counts[date]} for date in sorted(counts)]

    def _profile_action_counts(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        for event in events:
            counts[event.get("action") or "event"] += 1
        return [
            {"action": action, "count": counts[action]}
            for action in sorted(counts, key=lambda key: (-counts[key], key))
        ]

    def _attribution_cache_coverage(
        self, con: sqlite3.Connection, repo_rev: str | None
    ) -> dict[str, Any]:
        done_tus = con.execute(
            "SELECT COUNT(*) FROM tu WHERE status='done' AND dest_path IS NOT NULL"
        ).fetchone()[0]
        done_funcs = con.execute(
            """
            SELECT COUNT(*)
            FROM func f
            JOIN tu t ON t.id=f.tu_id
            WHERE f.status!='todo'
              AND t.dest_path IS NOT NULL
              AND t.dest_path != ''
            """
        ).fetchone()[0]
        if not repo_rev:
            return {
                "repo_rev": None,
                "file_cached": 0,
                "file_total": done_tus,
                "function_cached": 0,
                "function_total": done_funcs,
                "file_complete": False,
                "function_complete": False,
            }
        file_cached = con.execute(
            """
            SELECT COUNT(DISTINCT t.id)
            FROM attribution_cache ac
            JOIN tu t ON t.dest_path=ac.dest_path
            WHERE ac.scope='file'
              AND ac.repo_rev=?
              AND t.status='done'
            """,
            (repo_rev,),
        ).fetchone()[0]
        function_cached = con.execute(
            """
            SELECT COUNT(DISTINCT f.name)
            FROM attribution_cache ac
            JOIN tu t ON t.dest_path=ac.dest_path
            JOIN func f ON f.tu_id=t.id AND f.name=ac.function_name
            WHERE ac.scope='function'
              AND ac.repo_rev=?
              AND f.status!='todo'
            """,
            (repo_rev,),
        ).fetchone()[0]
        return {
            "repo_rev": repo_rev,
            "file_cached": file_cached,
            "file_total": done_tus,
            "function_cached": function_cached,
            "function_total": done_funcs,
            "file_complete": done_tus > 0 and file_cached >= done_tus,
            "function_complete": done_funcs > 0 and function_cached >= done_funcs,
        }

    def _percent(self, value: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round((value / total) * 100, 2)

    def attribution_cache_get(
        self,
        *,
        scope: str,
        dest_path: str,
        repo_rev: str,
        function_name: str = "",
    ) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT payload_json
                FROM attribution_cache
                WHERE scope=? AND dest_path=? AND function_name=? AND repo_rev=?
                """,
                (scope, dest_path, function_name, repo_rev),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"] or "{}")

    def attribution_cache_set(
        self,
        *,
        scope: str,
        dest_path: str,
        repo_rev: str,
        payload: dict[str, Any],
        function_name: str = "",
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                DELETE FROM attribution_cache
                WHERE scope=? AND dest_path=? AND function_name=? AND repo_rev != ?
                """,
                (scope, dest_path, function_name, repo_rev),
            )
            con.execute(
                """
                INSERT INTO attribution_cache(
                    scope, dest_path, function_name, repo_rev, payload_json, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, dest_path, function_name, repo_rev)
                DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (
                    scope,
                    dest_path,
                    function_name,
                    repo_rev,
                    json.dumps(payload, sort_keys=True),
                    iso(),
                ),
            )

    def _get_meta(self, con: sqlite3.Connection, key: str) -> str | None:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, con: sqlite3.Connection, key: str, value: str | None) -> None:
        if value is None:
            con.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            con.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, value, value),
            )

    def _dest_for(self, tu_id: str, source: str | None) -> str | None:
        if source == "decfigs":
            return "b5-decomp/src/" + self._normalize_path(tu_id)
        if source == "class" or tu_id.startswith("class:"):
            class_name = tu_id.removeprefix("class:")
            parts = [
                self._safe_class_path_part(part)
                for part in class_name.replace("\\", "::").replace("/", "::").split("::")
            ]
            parts = [part for part in parts if part]
            if not parts:
                parts = ["anonymous"]
            return "b5-decomp/src/classes/" + "/".join(parts) + ".cpp"
        return None

    def _backfill_missing_dest_paths(self, con: sqlite3.Connection) -> None:
        for row in con.execute(
            """
            SELECT id, source
            FROM tu
            WHERE dest_path IS NULL OR dest_path=''
            """
        ):
            dest_path = self._dest_for(row["id"], row["source"])
            if dest_path:
                con.execute("UPDATE tu SET dest_path=? WHERE id=?", (dest_path, row["id"]))

    def _safe_class_path_part(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
        return cleaned or "anonymous"

    def _normalize_path(self, value: str) -> str:
        parts: list[str] = []
        for seg in value.replace("\\", "/").split("/"):
            if seg in {"", "."}:
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        return "/".join(parts)

    def _normalize_github_username(self, username: str, github_username: str | None) -> str | None:
        cleaned = (github_username or "").strip()
        if not cleaned or cleaned.lower() == username.strip().lower():
            return None
        return cleaned
