from __future__ import annotations

TU_STATUSES = {"todo", "in_progress", "compiled", "done", "blocked"}
# Only these durable statuses are seeded from the workflow snapshot. The transient
# work states (in_progress/compiled), owners, and leases are born on the server and
# must never be clobbered or resurrected by a re-import/sync.
DURABLE_IMPORT_STATUSES = {"done", "blocked"}
DB_BUSY_TIMEOUT_MS = 30_000

# Functions IDA found in the shipped binary that carry no name, so neither the
# DWARF file nor the RTTI class that defines every other TU can claim them. They
# are real code -- ~6% of the executable -- and were absent from the totals
# entirely: not done, not todo, simply outside the denominator. They live under
# one synthetic TU so `func.tu_id` still resolves, and that TU is excluded from
# every *translation unit* count: an unnamed function is not a translation unit,
# and inflating the TU total with one row per function would be a second lie in
# the opposite direction. Its functions do count toward the function totals,
# because the binary is the binary.
UNIDENTIFIED_SOURCE = "unidentified"
UNIDENTIFIED_TU_PREFIX = "unidentified:"


WORK_SCHEMA = """
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
  -- 1 when this TU's destination file is on the game build's compile line
  -- (tools/build/build_game_exe.bat); refreshed on every workflow import.
  linked INTEGER NOT NULL DEFAULT 0,
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
  status TEXT NOT NULL DEFAULT 'todo',
  completed_by TEXT,
  completed_at TEXT
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

CREATE TABLE IF NOT EXISTS attribution_cache(
  scope TEXT NOT NULL,
  dest_path TEXT NOT NULL,
  function_name TEXT NOT NULL DEFAULT '',
  repo_rev TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope, dest_path, function_name, repo_rev)
);

-- Published game builds. One row per zip a CI runner uploads via POST /admin/builds.
-- The zip itself lives on disk under BP_DOWNLOADS_DIR (never in the DB); `filename`
-- is the on-disk name relative to that dir. `commit_sha` is the b5-decomp revision
-- the exe was built from; `asset_manifest_hash` fingerprints the Drive asset set that
-- was bundled at the exe's root, so a build is uniquely identified by the pair.
CREATE TABLE IF NOT EXISTS build(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  commit_sha TEXT NOT NULL,
  commit_short TEXT,
  branch TEXT,
  asset_manifest_hash TEXT,
  filename TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  built_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  downloads INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS ix_tu_status ON tu(status);
CREATE INDEX IF NOT EXISTS ix_tu_owner ON tu(owner);
CREATE INDEX IF NOT EXISTS ix_tu_source ON tu(source);
CREATE INDEX IF NOT EXISTS ix_tu_updated ON tu(updated_at);
CREATE INDEX IF NOT EXISTS ix_func_tu ON func(tu_id);
CREATE INDEX IF NOT EXISTS ix_func_status ON func(status);
CREATE INDEX IF NOT EXISTS ix_dep_tu ON tu_dep(tu_id);
CREATE INDEX IF NOT EXISTS ix_dep_dep ON tu_dep(dep_id);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event(ts);
CREATE INDEX IF NOT EXISTS ix_event_tu_action ON event(tu_id, action, id);
CREATE INDEX IF NOT EXISTS ix_attribution_cache_lookup
  ON attribution_cache(scope, dest_path, function_name);
CREATE INDEX IF NOT EXISTS ix_build_created ON build(created_at);
"""


USERS_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS worker(
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_service INTEGER NOT NULL DEFAULT 0,
  github_username TEXT,
  created_at TEXT,
  last_seen TEXT
);

CREATE INDEX IF NOT EXISTS ix_worker_username ON worker(username);
CREATE INDEX IF NOT EXISTS ix_worker_active ON worker(active);

CREATE TABLE IF NOT EXISTS worker_alias(
  alias TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS ix_worker_alias_username ON worker_alias(username);
"""
