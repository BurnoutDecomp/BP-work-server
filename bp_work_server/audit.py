"""The evidence layer: the static glue audit and the stub inventory, per commit.

Everything else on the dashboard measures what people DECLARED (a TU reviewed, a function
marked covered). CI regenerates ``progress/status.json`` from this very server, so that
loop can never discover new done-ness on its own. These two reports are the independent
signal: ``tools/re/funcaudit.py`` compares every reconstructed PC body with the console's
own function (case ids, event posts, callees, asserts, cited data) and
``tools/re/stubaudit.py`` lists every body that is still a stand-in. Both run in CI on
every b5-decomp commit and land in ``progress/funcaudit.json`` / ``progress/stubs.json``,
which the ordinary workflow import picks up here.

Storage model:
  * ``audit_run``      -- one row per (kind, commit): the totals. History is kept forever;
                          it is what makes the "verified" ring a trend rather than a number.
  * ``audit_finding``  -- the LATEST funcaudit run only, one row per function with findings.
  * ``audit_file``     -- its per-file rollup (the explorer's Audit tab).
  * ``stub`` / ``stub_file`` -- the latest stub inventory and its per-file rollup.

"High signal" is the weight used everywhere a number has to be ranked: a missing body,
case id, event post, callee or assert. Missing log strings, uncited data symbols and the
parameter-count hint are shown but never counted, because they are the categories the
tool itself documents as noisy.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

HIGH_SIGNAL = (
    "NO_BODY",
    "MISSING_CASE",
    "EXTRA_CASE",
    "MISSING_EVENT",
    "MISSING_CALLEE",
    "MISSING_ASSERT",
)
CATEGORIES = (
    "NO_BODY",
    "MISSING_CASE",
    "EXTRA_CASE",
    "MISSING_EVENT",
    "MISSING_EVENT?",
    "MISSING_CALLEE",
    "MISSING_ASSERT",
    "MISSING_STRING",
    "UNCITED_DATA",
    "FEWER_PARAMS",
)
# category -> audit_file column
FILE_COLUMNS = {
    "NO_BODY": "no_body",
    "MISSING_CASE": "missing_case",
    "EXTRA_CASE": "extra_case",
    "MISSING_EVENT": "missing_event",
    "MISSING_CALLEE": "missing_callee",
    "MISSING_ASSERT": "missing_assert",
    "MISSING_STRING": "missing_string",
    "UNCITED_DATA": "uncited_data",
}
FILE_SORTS = {
    "weight": "weight",
    "functions": "functions",
    "no_body": "no_body",
    "missing_case": "missing_case",
    "missing_event": "missing_event",
    "missing_callee": "missing_callee",
    "missing_assert": "missing_assert",
    "file": "file",
}
STUB_FILE_SORTS = {
    "stubs": "stubs",
    "high": "high",
    "live": "live",
    "console_lines": "console_lines",
    "file": "file",
}
STUB_TIERS = ("HIGH", "MEDIUM", "LOW")
SRC_PREFIX = "b5-decomp/src/"
HISTORY_LIMIT = 120
DELTA_LIST_LIMIT = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_run(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  commit_hash TEXT NOT NULL,
  author TEXT,
  generated_at TEXT,
  imported_at TEXT NOT NULL,
  stats_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(kind, commit_hash)
);

CREATE TABLE IF NOT EXISTS audit_finding(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  addr TEXT,
  file TEXT NOT NULL,
  line INTEGER NOT NULL DEFAULT 0,
  flagged INTEGER NOT NULL DEFAULT 0,
  helpers INTEGER NOT NULL DEFAULT 0,
  weight INTEGER NOT NULL DEFAULT 0,
  findings_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit_file(
  file TEXT PRIMARY KEY,
  functions INTEGER NOT NULL DEFAULT 0,
  weight INTEGER NOT NULL DEFAULT 0,
  no_body INTEGER NOT NULL DEFAULT 0,
  missing_case INTEGER NOT NULL DEFAULT 0,
  extra_case INTEGER NOT NULL DEFAULT 0,
  missing_event INTEGER NOT NULL DEFAULT 0,
  missing_callee INTEGER NOT NULL DEFAULT 0,
  missing_assert INTEGER NOT NULL DEFAULT 0,
  missing_string INTEGER NOT NULL DEFAULT 0,
  uncited_data INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stub(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file TEXT NOT NULL,
  line INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL,
  addr TEXT,
  tier TEXT NOT NULL,
  why TEXT,
  console_lines INTEGER,
  callers INTEGER NOT NULL DEFAULT 0,
  live_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS stub_file(
  file TEXT PRIMARY KEY,
  stubs INTEGER NOT NULL DEFAULT 0,
  high INTEGER NOT NULL DEFAULT 0,
  medium INTEGER NOT NULL DEFAULT 0,
  low INTEGER NOT NULL DEFAULT 0,
  live INTEGER NOT NULL DEFAULT 0,
  console_lines INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_audit_finding_file ON audit_finding(file);
CREATE INDEX IF NOT EXISTS ix_audit_finding_name ON audit_finding(name);
CREATE INDEX IF NOT EXISTS ix_stub_file ON stub(file);
CREATE INDEX IF NOT EXISTS ix_stub_name ON stub(name);
CREATE INDEX IF NOT EXISTS ix_audit_run_kind ON audit_run(kind, imported_at);
"""

Logger = Callable[[sqlite3.Connection, str | None, str, str | None, dict[str, Any]], None]


def audit_file_for_dest(dest_path: str | None) -> str | None:
    """A TU destination ("b5-decomp/src/GameSource/X.cpp") as the audit spells the file."""
    if not dest_path:
        return None
    path = dest_path.replace("\\", "/")
    if path.startswith(SRC_PREFIX):
        return path[len(SRC_PREFIX) :]
    return path


def finding_weight(findings: dict[str, Any]) -> int:
    return sum(len(items) for cat, items in findings.items() if cat in HIGH_SIGNAL)


def _run_key(meta: dict[str, Any]) -> str:
    return str(meta.get("b5_commit") or meta.get("generated_at") or "")


def _last_run(con: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM audit_run WHERE kind=? ORDER BY imported_at DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()


# ------------------------------------------------------------------ import
def import_audits(
    con: sqlite3.Connection, progress: Path, now: str, log: Logger
) -> dict[str, int]:
    """Import ``progress/funcaudit.json`` and ``progress/stubs.json`` when present.

    Idempotent per (kind, commit): a re-sync of the same snapshot changes nothing, so the
    history never carries duplicate points and no phantom delta is ever logged.
    """
    counts = {"funcaudit": 0, "stubs": 0}
    funcaudit_path = progress / "funcaudit.json"
    stubs_path = progress / "stubs.json"
    if funcaudit_path.exists():
        data = json.loads(funcaudit_path.read_text(encoding="utf-8"))
        counts["funcaudit"] = import_funcaudit(con, data, now, log)
    if stubs_path.exists():
        data = json.loads(stubs_path.read_text(encoding="utf-8"))
        counts["stubs"] = import_stubs(con, data, now, log)
    return counts


def import_funcaudit(
    con: sqlite3.Connection, data: dict[str, Any], now: str, log: Logger
) -> int:
    meta = data.get("meta") or {}
    key = _run_key(meta)
    if not key:
        return 0
    if con.execute(
        "SELECT 1 FROM audit_run WHERE kind='funcaudit' AND commit_hash=?", (key,)
    ).fetchone():
        return 0
    results = data.get("results") or []
    previous_run = _last_run(con, "funcaudit")
    previous_files = {
        row["file"]: row["weight"] for row in con.execute("SELECT file, weight FROM audit_file")
    }

    con.execute("DELETE FROM audit_finding")
    con.execute("DELETE FROM audit_file")
    rollup: dict[str, dict[str, int]] = {}
    total_weight = 0
    rows = []
    for item in results:
        findings = item.get("findings") or {}
        if not isinstance(findings, dict):
            continue
        file = item.get("file") or item.get("tu") or ""
        weight = finding_weight(findings)
        total_weight += weight
        rows.append(
            (
                item.get("name") or "",
                item.get("addr"),
                file,
                int(item.get("line") or 0),
                1 if item.get("flagged") else 0,
                int(item.get("helpers") or 0),
                weight,
                json.dumps(findings, separators=(",", ":")),
            )
        )
        bucket = rollup.setdefault(
            file, {"functions": 0, "weight": 0, **{col: 0 for col in FILE_COLUMNS.values()}}
        )
        bucket["functions"] += 1
        bucket["weight"] += weight
        for cat, items in findings.items():
            col = FILE_COLUMNS.get(cat)
            if col:
                bucket[col] += len(items)
    con.executemany(
        """
        INSERT INTO audit_finding(name, addr, file, line, flagged, helpers, weight, findings_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.executemany(
        """
        INSERT INTO audit_file(file, functions, weight, no_body, missing_case, extra_case,
                               missing_event, missing_callee, missing_assert, missing_string,
                               uncited_data)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                file,
                b["functions"],
                b["weight"],
                b["no_body"],
                b["missing_case"],
                b["extra_case"],
                b["missing_event"],
                b["missing_callee"],
                b["missing_assert"],
                b["missing_string"],
                b["uncited_data"],
            )
            for file, b in rollup.items()
        ],
    )
    stats = dict(data.get("stats") or {})
    stats["weight"] = total_weight
    stats["files"] = len(rollup)
    stats["categories"] = data.get("categories") or {}
    author = meta.get("b5_author") or None
    con.execute(
        """
        INSERT INTO audit_run(kind, commit_hash, author, generated_at, imported_at, stats_json)
        VALUES('funcaudit', ?, ?, ?, ?, ?)
        """,
        (key, author, meta.get("generated_at"), now, json.dumps(stats, sort_keys=True)),
    )
    if previous_run is not None:
        _log_delta(
            con,
            log,
            action="audit",
            author=author,
            key=key,
            before=json.loads(previous_run["stats_json"] or "{}"),
            after=stats,
            measure="weight",
            label="high-signal findings",
            previous_files=previous_files,
            current_files={file: b["weight"] for file, b in rollup.items()},
            extra={
                "clean": stats.get("clean"),
                "clean_before": json.loads(previous_run["stats_json"] or "{}").get("clean"),
                "no_body": stats.get("no_body"),
            },
        )
    return len(rows)


def import_stubs(
    con: sqlite3.Connection, data: dict[str, Any], now: str, log: Logger
) -> int:
    meta = data.get("meta") or {}
    key = _run_key(meta)
    if not key:
        return 0
    if con.execute(
        "SELECT 1 FROM audit_run WHERE kind='stubs' AND commit_hash=?", (key,)
    ).fetchone():
        return 0
    rows_in = data.get("rows") or []
    previous_run = _last_run(con, "stubs")
    previous_files = {
        row["file"]: row["stubs"] for row in con.execute("SELECT file, stubs FROM stub_file")
    }
    con.execute("DELETE FROM stub")
    con.execute("DELETE FROM stub_file")
    rollup: dict[str, dict[str, int]] = {}
    rows = []
    for item in rows_in:
        file = item.get("file") or ""
        tier = str(item.get("tier") or "LOW").upper()
        live = item.get("live_callers") or []
        console_lines = item.get("console_lines")
        rows.append(
            (
                file,
                int(item.get("line") or 0),
                item.get("name") or "",
                item.get("addr"),
                tier,
                item.get("why"),
                console_lines,
                int(item.get("callers") or 0),
                json.dumps(live, separators=(",", ":")),
            )
        )
        b = rollup.setdefault(
            file, {"stubs": 0, "high": 0, "medium": 0, "low": 0, "live": 0, "console_lines": 0}
        )
        b["stubs"] += 1
        b[tier.lower() if tier.lower() in ("high", "medium", "low") else "low"] += 1
        if live:
            b["live"] += 1
        b["console_lines"] += int(console_lines or 0)
    con.executemany(
        """
        INSERT INTO stub(file, line, name, addr, tier, why, console_lines, callers, live_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.executemany(
        """
        INSERT INTO stub_file(file, stubs, high, medium, low, live, console_lines)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (file, b["stubs"], b["high"], b["medium"], b["low"], b["live"], b["console_lines"])
            for file, b in rollup.items()
        ],
    )
    stats = dict(data.get("stats") or {})
    stats["stubs"] = len(rows)
    stats["files"] = len(rollup)
    stats["live"] = sum(b["live"] for b in rollup.values())
    stats["high"] = sum(b["high"] for b in rollup.values())
    author = meta.get("b5_author") or None
    con.execute(
        """
        INSERT INTO audit_run(kind, commit_hash, author, generated_at, imported_at, stats_json)
        VALUES('stubs', ?, ?, ?, ?, ?)
        """,
        (key, author, meta.get("generated_at"), now, json.dumps(stats, sort_keys=True)),
    )
    if previous_run is not None:
        _log_delta(
            con,
            log,
            action="stubs",
            author=author,
            key=key,
            before=json.loads(previous_run["stats_json"] or "{}"),
            after=stats,
            measure="stubs",
            label="stub bodies",
            previous_files=previous_files,
            current_files={file: b["stubs"] for file, b in rollup.items()},
            extra={"live": stats.get("live"), "live_before": json.loads(previous_run["stats_json"] or "{}").get("live")},
        )
    return len(rows)


def _log_delta(
    con: sqlite3.Connection,
    log: Logger,
    *,
    action: str,
    author: str | None,
    key: str,
    before: dict[str, Any],
    after: dict[str, Any],
    measure: str,
    label: str,
    previous_files: dict[str, int],
    current_files: dict[str, int],
    extra: dict[str, Any],
) -> None:
    """One dashboard event per commit that moved the number, blamed on the commit's author.

    The per-file lists name where it moved, which is what turns "8,224 findings" into
    "this commit closed 12 in BrnRaceCarEntityModule.cpp".
    """
    b = int(before.get(measure) or 0)
    a = int(after.get(measure) or 0)
    changes: list[tuple[str, int]] = []
    for file in set(previous_files) | set(current_files):
        diff = int(current_files.get(file, 0)) - int(previous_files.get(file, 0))
        if diff:
            changes.append((file, diff))
    if a == b and not changes:
        return
    improved = sorted((c for c in changes if c[1] < 0), key=lambda c: c[1])[:DELTA_LIST_LIMIT]
    regressed = sorted((c for c in changes if c[1] > 0), key=lambda c: -c[1])[:DELTA_LIST_LIMIT]
    detail: dict[str, Any] = {
        "b5_commit": key[:12],
        label: f"{b} -> {a} ({a - b:+d})",
    }
    for k, v in extra.items():
        if v is not None:
            detail[k] = v
    if improved:
        detail["closed"] = [f"{file} ({diff:+d})" for file, diff in improved]
    if regressed:
        detail["opened"] = [f"{file} ({diff:+d})" for file, diff in regressed]
    log(con, author, action, None, detail)


# ------------------------------------------------------------------ queries
def _stats(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    stats = json.loads(row["stats_json"] or "{}")
    stats["commit"] = row["commit_hash"]
    stats["author"] = row["author"]
    stats["generated_at"] = row["generated_at"]
    stats["imported_at"] = row["imported_at"]
    return stats


def summary(con: sqlite3.Connection, history_limit: int = HISTORY_LIMIT) -> dict[str, Any]:
    """Latest totals per kind plus the per-commit history behind the verified ring."""
    funcaudit = _stats(_last_run(con, "funcaudit"))
    stubs = _stats(_last_run(con, "stubs"))
    paired = int(funcaudit.get("paired") or 0)
    clean = int(funcaudit.get("clean") or 0)
    funcaudit["verified_percent"] = round(clean * 100.0 / paired, 1) if paired else 0.0
    points: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in con.execute(
        """
        SELECT * FROM (
          SELECT * FROM audit_run ORDER BY imported_at DESC, id DESC LIMIT ?
        ) ORDER BY imported_at ASC, id ASC
        """,
        (history_limit * 2,),
    ):
        stats = json.loads(row["stats_json"] or "{}")
        point = points.get(row["commit_hash"])
        if point is None:
            point = {"commit": row["commit_hash"], "imported_at": row["imported_at"]}
            points[row["commit_hash"]] = point
            order.append(row["commit_hash"])
        if row["kind"] == "funcaudit":
            p = int(stats.get("paired") or 0)
            c = int(stats.get("clean") or 0)
            point.update(
                {
                    "paired": p,
                    "clean": c,
                    "no_body": int(stats.get("no_body") or 0),
                    "weight": int(stats.get("weight") or 0),
                    "verified_percent": round(c * 100.0 / p, 1) if p else 0.0,
                }
            )
        else:
            point.update(
                {
                    "stubs": int(stats.get("stubs") or 0),
                    "stubs_live": int(stats.get("live") or 0),
                    "stubs_high": int(stats.get("high") or 0),
                }
            )
    history = [points[k] for k in order][-history_limit:]
    return {
        "funcaudit": funcaudit,
        "stubs": stubs,
        "history": history,
        "segments": segments(con, funcaudit),
    }


def segments(con: sqlite3.Connection, funcaudit: dict[str, Any]) -> dict[str, int]:
    """The states that make up the verified ring, over every console function the audit
    selected: clean, high-signal findings, soft-only findings (log strings / uncited data /
    the parameter hint), named-with-no-body, and not audited (no PC file known)."""
    row = con.execute(
        """
        SELECT
          SUM(CASE WHEN json_extract(findings_json, '$.NO_BODY') IS NOT NULL THEN 1 ELSE 0 END) AS no_body,
          SUM(CASE WHEN json_extract(findings_json, '$.NO_BODY') IS NULL AND weight > 0 THEN 1 ELSE 0 END) AS high,
          SUM(CASE WHEN weight = 0 THEN 1 ELSE 0 END) AS soft
        FROM audit_finding
        """
    ).fetchone()
    return {
        "clean": int(funcaudit.get("clean") or 0),
        "high": int((row["high"] if row else 0) or 0),
        "soft": int((row["soft"] if row else 0) or 0),
        "no_body": int((row["no_body"] if row else 0) or 0),
        "unpaired": int(funcaudit.get("unpaired_no_file") or 0),
    }


def top_functions(con: sqlite3.Connection, category: str, limit: int = 12) -> list[dict[str, Any]]:
    """The functions with the most items of one category -- the largest switch gaps, the
    most missing event posts -- straight from the latest run."""
    if category not in CATEGORIES:
        return []
    path = "$." + category.replace("?", "")   # JSON1 paths cannot carry the '?'
    if category.endswith("?"):
        path = '$."' + category + '"'
    rows = con.execute(
        """
        SELECT name, addr, file, line, weight,
               json_array_length(json_extract(findings_json, ?)) AS n,
               json_extract(findings_json, ?) AS items
        FROM audit_finding
        WHERE json_extract(findings_json, ?) IS NOT NULL
        ORDER BY n DESC, weight DESC, name ASC
        LIMIT ?
        """,
        (path, path, path, limit),
    ).fetchall()
    out = []
    for row in rows:
        items = json.loads(row["items"] or "[]")
        out.append(
            {
                "name": row["name"],
                "addr": row["addr"],
                "file": row["file"],
                "line": row["line"],
                "weight": row["weight"],
                "count": row["n"],
                # a MISSING_CASE item is one comma-joined list: count the ids for the label
                "ids": len(items[0].split("[")[0].split(",")) if items and category in ("MISSING_CASE", "EXTRA_CASE") else None,
                "preview": (items[0] if items else "")[:90],
            }
        )
    return out


def top_stubs(con: sqlite3.Connection, live_only: bool = True, limit: int = 12) -> list[dict[str, Any]]:
    where = "WHERE live_json != '[]'" if live_only else ""
    return [
        _stub_row(row)
        for row in con.execute(
            f"""
            SELECT * FROM stub {where}
            ORDER BY COALESCE(console_lines, 0) DESC, file ASC, line ASC
            LIMIT ?
            """,
            (limit,),
        )
    ]


def files(
    con: sqlite3.Connection,
    *,
    q: str | None = None,
    category: str | None = None,
    sort: str = "weight",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if q:
        clauses.append("file LIKE ?")
        params.append(f"%{q}%")
    col = FILE_COLUMNS.get(category or "")
    if col:
        clauses.append(f"{col} > 0")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sort_col = FILE_SORTS.get(sort, "weight")
    if col and sort == "weight":
        sort_col = col
    direction = "ASC" if order == "asc" else "DESC"
    total = con.execute(f"SELECT COUNT(*) FROM audit_file{where}", params).fetchone()[0]
    items = [
        dict(row)
        for row in con.execute(
            f"SELECT * FROM audit_file{where} ORDER BY {sort_col} {direction}, file ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def functions(con: sqlite3.Connection, file: str) -> dict[str, Any]:
    rollup = con.execute("SELECT * FROM audit_file WHERE file=?", (file,)).fetchone()
    items = [
        {
            "name": row["name"],
            "addr": row["addr"],
            "file": row["file"],
            "line": row["line"],
            "flagged": bool(row["flagged"]),
            "helpers": row["helpers"],
            "weight": row["weight"],
            "findings": json.loads(row["findings_json"] or "{}"),
        }
        for row in con.execute(
            "SELECT * FROM audit_finding WHERE file=? ORDER BY weight DESC, line ASC", (file,)
        )
    ]
    tu = con.execute(
        "SELECT id FROM tu WHERE dest_path=? OR dest_path=? ORDER BY id LIMIT 1",
        (SRC_PREFIX + file, file),
    ).fetchone()
    return {
        "file": file,
        "rollup": dict(rollup) if rollup else None,
        "tu_id": tu["id"] if tu else None,
        "items": items,
    }


def stub_files(
    con: sqlite3.Connection,
    *,
    q: str | None = None,
    tier: str | None = None,
    live_only: bool = False,
    sort: str = "stubs",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if q:
        clauses.append("file LIKE ?")
        params.append(f"%{q}%")
    tier_col = (tier or "").lower()
    if tier_col in ("high", "medium", "low"):
        clauses.append(f"{tier_col} > 0")
    if live_only:
        clauses.append("live > 0")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sort_col = STUB_FILE_SORTS.get(sort, "stubs")
    direction = "ASC" if order == "asc" else "DESC"
    total = con.execute(f"SELECT COUNT(*) FROM stub_file{where}", params).fetchone()[0]
    items = [
        dict(row)
        for row in con.execute(
            f"SELECT * FROM stub_file{where} ORDER BY {sort_col} {direction}, file ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _stub_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "file": row["file"],
        "line": row["line"],
        "name": row["name"],
        "addr": row["addr"],
        "tier": row["tier"],
        "why": row["why"],
        "console_lines": row["console_lines"],
        "callers": row["callers"],
        "live_callers": json.loads(row["live_json"] or "[]"),
    }


def stubs(con: sqlite3.Connection, file: str) -> dict[str, Any]:
    rollup = con.execute("SELECT * FROM stub_file WHERE file=?", (file,)).fetchone()
    items = [
        _stub_row(row)
        for row in con.execute(
            """
            SELECT * FROM stub WHERE file=?
            ORDER BY CASE tier WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, line ASC
            """,
            (file,),
        )
    ]
    tu = con.execute(
        "SELECT id FROM tu WHERE dest_path=? OR dest_path=? ORDER BY id LIMIT 1",
        (SRC_PREFIX + file, file),
    ).fetchone()
    return {
        "file": file,
        "rollup": dict(rollup) if rollup else None,
        "tu_id": tu["id"] if tu else None,
        "items": items,
    }


def tu_audit(
    con: sqlite3.Connection, dest_path: str | None, func_names: list[str]
) -> dict[str, Any]:
    """What the drawer shows for one TU: its file's rollup, each function's findings, the
    stubs in its file. Functions are joined by canonical name, so a TU whose functions
    live in a file the ledger does not name (class TUs) still gets its own findings."""
    file = audit_file_for_dest(dest_path)
    out: dict[str, Any] = {"file": file, "rollup": None, "funcs": {}, "stubs": [], "stub_rollup": None}
    if file:
        rollup = con.execute("SELECT * FROM audit_file WHERE file=?", (file,)).fetchone()
        out["rollup"] = dict(rollup) if rollup else None
        stub_rollup = con.execute("SELECT * FROM stub_file WHERE file=?", (file,)).fetchone()
        out["stub_rollup"] = dict(stub_rollup) if stub_rollup else None
        out["stubs"] = [
            _stub_row(row)
            for row in con.execute(
                """
                SELECT * FROM stub WHERE file=?
                ORDER BY CASE tier WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, line ASC
                """,
                (file,),
            )
        ]
    if func_names:
        for chunk_start in range(0, len(func_names), 400):
            chunk = func_names[chunk_start : chunk_start + 400]
            placeholders = ",".join("?" * len(chunk))
            for row in con.execute(
                f"SELECT * FROM audit_finding WHERE name IN ({placeholders})", chunk
            ):
                out["funcs"][row["name"]] = {
                    "addr": row["addr"],
                    "file": row["file"],
                    "line": row["line"],
                    "flagged": bool(row["flagged"]),
                    "weight": row["weight"],
                    "findings": json.loads(row["findings_json"] or "{}"),
                }
    return out
