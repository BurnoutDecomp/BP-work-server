"""The byte-match layer: the native Xbox 360 tree against the console, function by function.

The workflow repo's `bytematch.yml` publishes ``progress/bytematch.json`` after every push
to b5-decomp's ``x360`` branch and every push of byte-match records: each ARTIST function
with its verdict (exact, nonmatching, missing, sdk, unnamed), the per-area and per-file
rollups, and when each exact function first matched. An import replaces the latest-state
tables wholesale and appends one ``bytematch_run`` row per distinct summary, which is the
history the dashboard draws. A summary already imported (same content, any timestamp) is
skipped, so a re-sync never adds a history point.

Kept free of store/schema imports (schema.py concatenates SCHEMA, like audit.SCHEMA).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS bytematch_run(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision TEXT NOT NULL UNIQUE,
  b5_commit TEXT,
  generated_at TEXT,
  imported_at TEXT NOT NULL,
  stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_bytematch_run_imported ON bytematch_run(imported_at);
CREATE TABLE IF NOT EXISTS bytematch_func(
  artist TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  source TEXT,
  stale INTEGER NOT NULL DEFAULT 0,
  area TEXT,
  tu TEXT,
  first_exact TEXT
);
CREATE INDEX IF NOT EXISTS ix_bytematch_func_status ON bytematch_func(status);
CREATE INDEX IF NOT EXISTS ix_bytematch_func_area ON bytematch_func(area);
CREATE INDEX IF NOT EXISTS ix_bytematch_func_source ON bytematch_func(source);
CREATE TABLE IF NOT EXISTS bytematch_file(
  source TEXT PRIMARY KEY,
  row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bytematch_area(
  area TEXT PRIMARY KEY,
  row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bytematch_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SUMMARY_FILE = "bytematch.json"
STATUSES = ("exact", "nonmatching", "missing", "sdk", "unnamed")
FUNCTION_SORTS = {"size": "size", "score": "score", "name": "name", "artist": "artist",
                  "status": "status", "source": "source", "first_exact": "first_exact"}
FILE_SORTS = ("source", "functions", "exact", "nonmatching", "bytes", "exact_bytes", "fuzzy", "measured")


def _revision(doc: dict[str, Any]) -> str:
    """Content identity: the summary without its own timestamp."""
    meta = {k: v for k, v in doc.get("meta", {}).items() if k != "generated_at"}
    return hashlib.sha256(json.dumps({**doc, "meta": meta}, sort_keys=True).encode()).hexdigest()


def _stats(totals: dict[str, Any]) -> dict[str, Any]:
    game = totals.get("game", {})
    return {
        "functions": int(game.get("functions") or 0),
        "bytes": int(game.get("bytes") or 0),
        "exact": int(game.get("exact") or 0),
        "exact_bytes": int(game.get("exact_bytes") or 0),
        "nonmatching": int(game.get("nonmatching") or 0),
        "missing": int(game.get("missing") or 0),
        "fuzzy_bytes": float(game.get("fuzzy_bytes") or 0.0),
        "sdk": int(totals.get("sdk", {}).get("functions") or 0),
        "unnamed": int(totals.get("unnamed", {}).get("functions") or 0),
        "stale": int(totals.get("objects", {}).get("stale") or 0),
        "sources": int(totals.get("objects", {}).get("sources") or 0),
    }


def _upgrade(con: sqlite3.Connection) -> None:
    """Additive upgrades for stores created before a column existed (CREATE IF NOT EXISTS
    never adds columns to a table that is already there). Plain statements only: this runs
    inside the import's transaction, which executescript() would commit."""
    columns = {r[1] for r in con.execute("PRAGMA table_info(bytematch_func)")}
    if columns and "tu" not in columns:
        con.execute("ALTER TABLE bytematch_func ADD COLUMN tu TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS ix_bytematch_func_tu ON bytematch_func(tu)")


def migrate(con: sqlite3.Connection) -> None:
    """Create the tables and apply the additive upgrades (outside any transaction)."""
    con.executescript(SCHEMA)
    _upgrade(con)


def import_bytematch(con: sqlite3.Connection, progress: Path, now: str,
                     log: Callable[..., None] | None = None) -> int:
    """Import progress/bytematch.json; returns the functions imported (0 = absent/unchanged)."""
    path = Path(progress) / SUMMARY_FILE
    if not path.is_file():
        return 0
    _upgrade(con)   # the import already ran the schema; never executescript() mid-transaction
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("meta", {}).get("schema") != 1:
        raise ValueError(f"{path}: unsupported byte-match schema {doc.get('meta', {}).get('schema')!r}")
    revision = _revision(doc)
    if con.execute("SELECT 1 FROM bytematch_run WHERE revision = ?", (revision,)).fetchone():
        return 0
    meta, totals = doc["meta"], doc["totals"]
    columns = doc["columns"]
    at = {name: i for i, name in enumerate(columns)}
    sources, areas, tus = doc.get("sources", []), doc.get("area_names", []), doc.get("tu_names", [])
    first_exact = doc.get("first_exact", {})
    rows = []
    for r in doc["functions"]:
        source_i = r[at["source"]]
        area_i = r[at["area"]] if "area" in at else -1
        tu_i = r[at["tu"]] if "tu" in at else -1
        artist = r[at["artist"]]
        fe = first_exact.get(artist)
        rows.append((artist, r[at["name"]], int(r[at["size"]]), r[at["status"]], float(r[at["score"]]),
                     sources[source_i] if source_i >= 0 else None, int(r[at["stale"]]),
                     areas[area_i] if 0 <= area_i < len(areas) else None,
                     tus[tu_i] if 0 <= tu_i < len(tus) else None, fe[1] if fe else None))
    previous = con.execute("SELECT stats_json FROM bytematch_run ORDER BY imported_at DESC, id DESC LIMIT 1").fetchone()
    con.execute("DELETE FROM bytematch_func")
    con.executemany("INSERT INTO bytematch_func(artist, name, size, status, score, source, stale, area, tu, first_exact) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("DELETE FROM bytematch_file")
    con.executemany("INSERT INTO bytematch_file(source, row_json) VALUES(?, ?)",
                    [(f["source"], json.dumps(f, sort_keys=True)) for f in doc.get("files", [])])
    con.execute("DELETE FROM bytematch_area")
    con.executemany("INSERT INTO bytematch_area(area, row_json) VALUES(?, ?)",
                    [(a["area"], json.dumps(a, sort_keys=True)) for a in doc.get("areas", [])])
    for key, value in (("meta", meta), ("totals", totals), ("recent", doc.get("recent", []))):
        con.execute("INSERT INTO bytematch_meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value, sort_keys=True)))
    stats = _stats(totals)
    con.execute("INSERT INTO bytematch_run(revision, b5_commit, generated_at, imported_at, stats_json) "
                "VALUES(?, ?, ?, ?, ?)", (revision, meta.get("b5_commit"), meta.get("generated_at"), now,
                                         json.dumps(stats, sort_keys=True)))
    if log is not None:
        before = json.loads(previous["stats_json"]) if previous else {}
        log(con, "server", "bytematch", None, {
            "b5_commit": meta.get("b5_commit"), "exact": stats["exact"], "functions": stats["functions"],
            "delta_exact": stats["exact"] - int(before.get("exact") or 0),
            "exact_bytes": stats["exact_bytes"], "stale": stats["stale"],
        })
    return len(rows)


def _meta(con: sqlite3.Connection, key: str, default: Any) -> Any:
    try:
        row = con.execute("SELECT value FROM bytematch_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return json.loads(row["value"]) if row else default


def history(con: sqlite3.Connection, limit: int = 365) -> list[dict[str, Any]]:
    try:
        runs = con.execute("SELECT b5_commit, generated_at, imported_at, stats_json FROM bytematch_run "
                           "ORDER BY imported_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"commit": r["b5_commit"], "generated_at": r["generated_at"], "imported_at": r["imported_at"],
             **json.loads(r["stats_json"] or "{}")} for r in reversed(runs)]


def summary(con: sqlite3.Connection) -> dict[str, Any]:
    meta, totals = _meta(con, "meta", {}), _meta(con, "totals", {})
    if not meta:
        return {"available": False, "meta": {}, "totals": {}, "areas": [], "recent": [], "history": [], "status_counts": {}}
    areas = [json.loads(r["row_json"]) for r in con.execute("SELECT row_json FROM bytematch_area")]
    areas.sort(key=lambda a: (-int(a.get("bytes") or 0), a.get("area") or ""))
    counts = {r["status"]: {"functions": r["n"], "bytes": r["b"]} for r in con.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM bytematch_func GROUP BY status")}
    game = totals.get("game", {})
    all_bytes = sum(int(v["bytes"]) for v in counts.values())
    return {
        "available": True,
        "meta": meta,
        "totals": totals,
        "status_counts": counts,
        "percent": {
            "exact_functions": round(100.0 * game.get("exact", 0) / game["functions"], 2) if game.get("functions") else 0.0,
            "exact_bytes": round(100.0 * game.get("exact_bytes", 0) / game["bytes"], 2) if game.get("bytes") else 0.0,
            "fuzzy_bytes": round(100.0 * game.get("fuzzy_bytes", 0) / game["bytes"], 2) if game.get("bytes") else 0.0,
            "image_bytes": round(100.0 * (game.get("exact_bytes", 0) + counts.get("sdk", {}).get("bytes", 0)) / all_bytes, 2)
            if all_bytes else 0.0,
        },
        "areas": areas,
        "recent": _meta(con, "recent", []),
        "history": history(con),
    }


def functions(con: sqlite3.Connection, *, q: str | None = None, status: str | None = None,
              area: str | None = None, source: str | None = None, tu: str | None = None,
              stale: bool | None = None,
              sort: str = "size", order: str = "desc", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    where, args = [], []
    if q:
        where.append("(name LIKE ? OR artist LIKE ? OR source LIKE ?)")
        like = f"%{q.strip()}%"
        args += [like, like.replace("0x", "").replace("0X", ""), like]
    if status:
        where.append("status = ?")
        args.append(status)
    if area:
        where.append("area = ?")
        args.append(area)
    if source:
        where.append("source = ?")
        args.append(source)
    if tu:
        where.append("tu = ?")
        args.append(tu)
    if stale is not None:
        where.append("stale = ?")
        args.append(1 if stale else 0)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    column = FUNCTION_SORTS.get(sort, "size")
    direction = "ASC" if order == "asc" else "DESC"
    try:
        total = con.execute(f"SELECT COUNT(*) FROM bytematch_func{clause}", args).fetchone()[0]
        items = [dict(r) for r in con.execute(
            f"SELECT artist, name, size, status, score, source, stale, area, tu, first_exact FROM bytematch_func{clause} "
            f"ORDER BY {column} {direction}, artist ASC LIMIT ? OFFSET ?", [*args, limit, offset])]
    except sqlite3.OperationalError:
        total, items = 0, []
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def files(con: sqlite3.Connection, *, q: str | None = None, stale: str | None = None, sort: str = "bytes",
          order: str = "desc", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    try:
        rows = [json.loads(r["row_json"]) for r in con.execute("SELECT row_json FROM bytematch_file")]
    except sqlite3.OperationalError:
        rows = []
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r.get("source", "").lower()]
    if stale:
        rows = [r for r in rows if r.get("stale") == stale]
    for r in rows:
        r["fuzzy"] = round(100.0 * float(r.get("fuzzy_bytes") or 0) / r["bytes"], 1) if r.get("bytes") else 0.0
    key = sort if sort in FILE_SORTS else "bytes"
    rows.sort(key=lambda r: (r.get(key) is None, r.get(key) or 0) if key != "source" else (False, r.get("source", "")),
              reverse=(order != "asc"))
    return {"total": len(rows), "limit": limit, "offset": offset, "items": rows[offset:offset + limit]}


UNIDENTIFIED_UNIT = "unidentified:BURNOUT_X360_ARTIST.XEX"


def unit_state(n: int, exact: int, compiled: int, sdk: int, unidentified: bool) -> str:
    """One TU's byte-match state, for the X360 treemap's unit view."""
    if unidentified:
        return "unnamed"
    if n and sdk == n:
        return "sdk"
    if n and exact + sdk == n:
        return "matched"
    if exact:
        return "partial"
    if compiled:
        return "compiled"
    return "missing"


def map_snapshot(con: sqlite3.Connection, include_functions: bool = False) -> dict[str, Any]:
    """The X360 treemap: ledger TUs with byte-match states, the shape /api/progress-map has.

    Units are the ledger's TUs (the X360 universe files each named function under its TU),
    so the tree groups exactly like the PC progress map. Unnamed functions share one
    unidentified bucket; named functions the ledger files nowhere share "unassigned".
    """
    units: dict[str, dict[str, Any]] = {}
    try:
        rows = con.execute("SELECT artist, name, size, status, score, source, tu FROM bytematch_func "
                           "ORDER BY name, artist").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        key = r["tu"] or (UNIDENTIFIED_UNIT if r["status"] == "unnamed" else "unassigned")
        unit = units.get(key)
        if unit is None:
            unit = units[key] = {"id": key, "n": 0, "exact": 0, "compiled": 0, "sdk": 0,
                                 "bytes": 0, "exact_bytes": 0, "functions": []}
        unit["n"] += 1
        unit["bytes"] += int(r["size"] or 0)
        if r["status"] == "exact":
            unit["exact"] += 1
            unit["exact_bytes"] += int(r["size"] or 0)
        if r["status"] in ("exact", "nonmatching"):
            unit["compiled"] += 1
        if r["status"] == "sdk":
            unit["sdk"] += 1
        if include_functions:
            unit["functions"].append({"name": r["name"], "status": r["status"], "score": r["score"],
                                      "size": r["size"], "artist": r["artist"], "source": r["source"]})
    try:
        ledger = {row["id"]: row for row in con.execute("SELECT id, source, dest_path FROM tu")}
    except sqlite3.OperationalError:
        ledger = {}
    out = []
    totals = {"tus": 0, "funcs": 0, "exact_funcs": 0, "matched_tus": 0, "bytes": 0, "exact_bytes": 0}
    for key in sorted(units):
        u = units[key]
        unidentified = key == UNIDENTIFIED_UNIT
        state = unit_state(u["n"], u["exact"], u["compiled"], u["sdk"], unidentified)
        row = ledger.get(key)
        out.append({
            "id": key, "source": row["source"] if row else ("unidentified" if unidentified else None),
            "dest_path": row["dest_path"] if row else None, "status": state, "linked": False,
            "unidentified": unidentified, "function_count": u["n"], "recorded_funcs": u["exact"],
            "compiled_funcs": u["compiled"], "sdk_funcs": u["sdk"], "bytes": u["bytes"],
            "exact_bytes": u["exact_bytes"], "goals": [],
            "functions": u["functions"] if include_functions else None,
        })
        totals["funcs"] += u["n"]
        totals["exact_funcs"] += u["exact"]
        totals["bytes"] += u["bytes"]
        totals["exact_bytes"] += u["exact_bytes"]
        if not unidentified:
            totals["tus"] += 1
            totals["matched_tus"] += state == "matched"
    return {"units": out, "totals": totals, "include_functions": include_functions,
            "meta": _meta(con, "meta", {})}
