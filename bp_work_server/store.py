from __future__ import annotations

import fnmatch
import json
import secrets
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from bp_work_server.models import ClaimResponse, NextTu, StatusCounts, TuRecord


TU_STATUSES = {"todo", "in_progress", "compiled", "done", "blocked"}
DB_BUSY_TIMEOUT_MS = 30_000


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS tu(
  id TEXT PRIMARY KEY,
  source TEXT,
  status TEXT NOT NULL DEFAULT 'todo',
  n_funcs INTEGER NOT NULL DEFAULT 0,
  n_decfigs INTEGER NOT NULL DEFAULT 0,
  dest_path TEXT,
  owner TEXT,
  notes TEXT,
  updated_at TEXT,
  claimed_at TEXT,
  lease_expires_at TEXT,
  commit_hash TEXT,
  CHECK(status IN ('todo','in_progress','compiled','done','blocked'))
);

CREATE TABLE IF NOT EXISTS func(
  name TEXT PRIMARY KEY,
  tu_id TEXT NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'todo'
);

CREATE TABLE IF NOT EXISTS tu_dep(
  tu_id TEXT NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
  dep_id TEXT NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
  weight INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(tu_id, dep_id)
);

CREATE TABLE IF NOT EXISTS goal(
  name TEXT PRIMARY KEY,
  category TEXT,
  description TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS goal_tu(
  goal_name TEXT NOT NULL REFERENCES goal(name) ON DELETE CASCADE,
  tu_id TEXT NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
  PRIMARY KEY(goal_name, tu_id)
);

CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  tu_id TEXT,
  agent TEXT,
  action TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_tu_status ON tu(status);
CREATE INDEX IF NOT EXISTS ix_tu_owner ON tu(owner);
CREATE INDEX IF NOT EXISTS ix_func_tu ON func(tu_id);
CREATE INDEX IF NOT EXISTS ix_dep_tu ON tu_dep(tu_id);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event(ts);
"""


USERS_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS worker(
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  last_seen TEXT
);

CREATE INDEX IF NOT EXISTS ix_worker_username ON worker(username);
CREATE INDEX IF NOT EXISTS ix_worker_active ON worker(active);
"""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


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
        self._migrate_users()

    def _migrate_users(self) -> None:
        with self.users_connect(ensure_wal=True) as con:
            con.executescript(USERS_SCHEMA)
            # additive migration for user DBs created before the admin role existed
            cols = {r["name"] for r in con.execute("PRAGMA table_info(worker)")}
            if "is_admin" not in cols:
                con.execute("ALTER TABLE worker ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
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
            rows = con.execute(
                f"""
                SELECT token, username, active, {is_admin_expr}, created_at, last_seen
                FROM worker
                """
            ).fetchall()
        if rows:
            with self.users_connect() as con:
                for row in rows:
                    con.execute(
                        """
                        INSERT INTO worker(token, username, active, is_admin, created_at, last_seen)
                        VALUES(?, ?, ?, ?, ?, ?)
                        ON CONFLICT(token) DO NOTHING
                        """,
                        (
                            row["token"],
                            row["username"],
                            int(row["active"]),
                            int(row["is_admin"]),
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

        tu_index = json.loads(tu_index_path.read_text(encoding="utf-8"))
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        deps = json.loads(deps_path.read_text(encoding="utf-8")) if deps_path.exists() else []
        goals = json.loads(goals_path.read_text(encoding="utf-8")) if goals_path.exists() else {}

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
                      dest_path=COALESCE(tu.dest_path, excluded.dest_path)
                    """,
                    (
                        tu_id,
                        row.get("source"),
                        int(row.get("n_funcs") or 0),
                        int(row.get("n_decfigs") or 0),
                        self._dest_for(tu_id, row.get("source")),
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
            con.execute("UPDATE func SET status='compiles' WHERE tu_id=?", (tu_id,))
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
            con.execute("UPDATE func SET status=? WHERE tu_id=?", (func_status, tu_id))
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
            self._require_tu(con, tu_id)
            con.execute(
                """
                UPDATE tu
                SET status='todo', owner=NULL, notes=NULL, lease_expires_at=NULL, updated_at=?
                WHERE id=?
                """,
                (iso(), tu_id),
            )
            self._log(con, agent, "unblock", tu_id, {})

    # --- workers (server-issued identities) -------------------------------
    def create_worker(self, username: str, is_admin: bool = False) -> dict[str, Any]:
        """Mint a new secret token bound to a human username. `is_admin` grants access to
        the /admin/* endpoints (minting/revoking ids, import/sync/reset). Admin is a role
        on a worker, not a separate shared secret."""
        token = secrets.token_urlsafe(24)
        with self.users_connect() as con:
            con.execute(
                "INSERT INTO worker(token, username, active, is_admin, created_at) "
                "VALUES(?, ?, 1, ?, ?)",
                (token, username, 1 if is_admin else 0, iso()),
            )
        with self.connect() as con:
            self._log(con, username, "worker_create", None, {"is_admin": bool(is_admin)})
        return {"token": token, "username": username, "is_admin": bool(is_admin)}

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
                    "created_at": r["created_at"],
                    "last_seen": r["last_seen"],
                }
                for r in con.execute("SELECT * FROM worker ORDER BY created_at, username")
            ]

    def revoke_worker(self, token: str) -> bool:
        with self.users_connect() as con:
            cur = con.execute("UPDATE worker SET active=0 WHERE token=?", (token,))
            revoked = cur.rowcount > 0
        if revoked:
            with self.connect() as con:
                self._log(con, None, "worker_revoke", None, {})
        return revoked

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

    def events(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT id, ts, tu_id, agent, action, detail_json
                FROM event
                WHERE id > ?
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
                    "agent": row["agent"],
                    "action": row["action"],
                    "detail": json.loads(row["detail_json"] or "{}"),
                }
                for row in rows
            ]

    def dashboard_state(self) -> dict[str, Any]:
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
                    WHERE status IN ('in_progress', 'compiled')
                    ORDER BY updated_at DESC, id
                    LIMIT 100
                    """
                )
            ]
            blocked = [
                self._dashboard_tu(row)
                for row in con.execute(
                    """
                    SELECT *
                    FROM tu
                    WHERE status='blocked'
                    ORDER BY updated_at DESC, id
                    LIMIT 50
                    """
                )
            ]
            agents = [
                {
                    "name": row["owner"],
                    "in_progress": row["in_progress"],
                    "compiled": row["compiled"],
                    "total": row["total"],
                    "lease_expires_at": row["lease_expires_at"],
                    "last_update": row["last_update"],
                }
                for row in con.execute(
                    """
                    SELECT owner,
                           SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                           SUM(CASE WHEN status='compiled' THEN 1 ELSE 0 END) AS compiled,
                           COUNT(*) AS total,
                           MAX(lease_expires_at) AS lease_expires_at,
                           MAX(updated_at) AS last_update
                    FROM tu
                    WHERE owner IS NOT NULL AND status IN ('in_progress', 'compiled')
                    GROUP BY owner
                    ORDER BY total DESC, owner
                    """
                )
            ]
            recent_events = self._events_from_connection(con, after=0, limit=25)
            next_goal, next_items = self._next_tus_from_connection(con, n=20)
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
                for item in items:
                    item["unresolved_deps"] = None
                return {"total": total, "limit": limit, "offset": offset, "items": items}

            rows = con.execute(
                f"SELECT t.* FROM tu t {joins}{where}", params
            ).fetchall()
            unresolved: dict[str, int] = {}
            dep_map: dict[str, set[str]] = defaultdict(set)
            for dep in con.execute("SELECT tu_id, dep_id FROM tu_dep"):
                dep_map[dep["tu_id"]].add(dep["dep_id"])
            status_by_tu = {
                r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tu")
            }
            for row in rows:
                unresolved[row["id"]] = sum(
                    1
                    for dep_id in dep_map.get(row["id"], ())
                    if status_by_tu.get(dep_id) != "done"
                )

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
            return {"total": total, "limit": limit, "offset": offset, "items": page}

    def tu_detail(self, tu_id: str) -> dict[str, Any]:
        """Everything the server knows about one TU -- the data given to agents."""
        with self.connect() as con:
            self._expire_leases(con)
            row = self._require_tu(con, tu_id)
            detail = self._dashboard_tu(row)
            detail["funcs"] = [
                {"name": r["name"], "status": r["status"]}
                for r in con.execute(
                    "SELECT name, status FROM func WHERE tu_id=? ORDER BY name", (tu_id,)
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
                clauses.append("name LIKE ?")
                params.append(f"%{q}%")
            if statuses:
                placeholders = ",".join("?" * len(statuses))
                clauses.append(f"status IN ({placeholders})")
                params.extend(statuses)
            if tu:
                clauses.append("tu_id = ?")
                params.append(tu)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            total = con.execute(
                f"SELECT COUNT(*) FROM func{where}", params
            ).fetchone()[0]
            rows = con.execute(
                f"SELECT name, tu_id, status FROM func{where} ORDER BY name LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = [
                {"name": r["name"], "tu_id": r["tu_id"], "status": r["status"]} for r in rows
            ]
            return {"total": total, "limit": limit, "offset": offset, "items": items}

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

        dep_map: dict[str, set[str]] = defaultdict(set)
        for row in con.execute("SELECT tu_id, dep_id FROM tu_dep"):
            dep_map[row["tu_id"]].add(row["dep_id"])
        status_by_tu = {
            row["id"]: row["status"] for row in con.execute("SELECT id, status FROM tu")
        }

        ranked: list[dict[str, Any]] = []
        for row in rows:
            if scope is not None and row["id"] not in scope:
                continue
            unresolved = 0
            for dep_id in dep_map.get(row["id"], set()):
                if scope is not None and dep_id not in scope:
                    continue
                if status_by_tu.get(dep_id) != "done":
                    unresolved += 1
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

    def _restore_status(self, con: sqlite3.Connection, status: dict[str, Any]) -> int:
        rows = 0
        for tu_id, data in status.get("tu", {}).items():
            tu_status = data.get("status", "todo")
            if tu_status not in TU_STATUSES:
                continue
            cur = con.execute(
                """
                UPDATE tu
                SET status=?, owner=?, notes=?, updated_at=?
                WHERE id=?
                """,
                (tu_status, data.get("owner"), data.get("notes"), iso(), tu_id),
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
        return len(rows)

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
        rows = con.execute(
            """
            SELECT id, ts, tu_id, agent, action, detail_json
            FROM event
            WHERE id > ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (after, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "tu_id": row["tu_id"],
                "agent": row["agent"],
                "action": row["action"],
                "detail": json.loads(row["detail_json"] or "{}"),
            }
            for row in rows
        ]

    def _dashboard_tu(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source": row["source"],
            "status": row["status"],
            "n_funcs": row["n_funcs"],
            "n_decfigs": row["n_decfigs"],
            "dest_path": row["dest_path"],
            "owner": row["owner"],
            "notes": row["notes"],
            "updated_at": row["updated_at"],
            "lease_expires_at": row["lease_expires_at"],
            "commit": row["commit_hash"],
        }

    def _percent(self, value: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round((value / total) * 100, 2)

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
        return None

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
