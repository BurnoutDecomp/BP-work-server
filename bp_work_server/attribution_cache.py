from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from bp_work_server.store import WorkStore, iso


ProgressCallback = Callable[[str, int, int, str], None]


@dataclass(frozen=True)
class AttributionCacheWarmResult:
    repo_rev: str
    file_targets: int
    function_targets: int
    files_cached: int
    functions_cached: int


def warm_attribution_cache(
    store: WorkStore,
    decomp: Any,
    *,
    include_files: bool = True,
    include_functions: bool = True,
    progress: ProgressCallback | None = None,
) -> AttributionCacheWarmResult:
    """Precompute local-git surviving-line attribution for cacheable reviewed work."""
    store.migrate()
    repo_rev = decomp.revision() if hasattr(decomp, "revision") else None
    if not repo_rev:
        raise RuntimeError("cannot warm attribution cache without a decomp git revision")

    with store.connect() as con:
        file_targets = [
            row["dest_path"]
            for row in con.execute(
                """
                SELECT DISTINCT dest_path
                FROM tu
                WHERE status='done'
                  AND dest_path IS NOT NULL
                  AND dest_path != ''
                ORDER BY dest_path
                """
            )
        ]
        function_targets = [
            (row["dest_path"], row["name"])
            for row in con.execute(
                """
                SELECT t.dest_path, f.name
                FROM func f
                JOIN tu t ON t.id=f.tu_id
                WHERE f.status!='todo'
                  AND t.dest_path IS NOT NULL
                  AND t.dest_path != ''
                ORDER BY t.dest_path, f.name
                """
            )
        ]

    files_cached = 0
    functions_cached = 0
    now = iso()
    rows: list[tuple[str, str, str, str, str, str]] = []

    if include_files:
        total = len(file_targets)
        for index, dest_path in enumerate(file_targets, start=1):
            history = decomp.history(dest_path)
            contributors = (
                decomp.contributors(dest_path)
                if hasattr(decomp, "contributors")
                else {"contributors": [], "basis": "surviving_lines", "path": None}
            )
            rows.append(
                (
                    "file",
                    dest_path,
                    "",
                    repo_rev,
                    json.dumps(
                        {"latest": history[0] if history else None, "contributors": contributors},
                        sort_keys=True,
                    ),
                    now,
                )
            )
            files_cached += 1
            if progress and (index == total or index % 25 == 0):
                progress("files", index, total, dest_path)

    if include_functions:
        total = len(function_targets)
        for index, (dest_path, function_name) in enumerate(function_targets, start=1):
            if hasattr(decomp, "function_contributors"):
                payload = decomp.function_contributors(dest_path, function_name)
            elif hasattr(decomp, "contributors"):
                payload = decomp.contributors(dest_path)
            else:
                payload = {}
            rows.append(
                (
                    "function",
                    dest_path,
                    function_name,
                    repo_rev,
                    json.dumps(payload, sort_keys=True),
                    now,
                )
            )
            functions_cached += 1
            if progress and (index == total or index % 250 == 0):
                progress("functions", index, total, function_name)

    with store.connect() as con:
        for scope, dest_path, function_name, current_rev, _payload_json, _updated_at in rows:
            con.execute(
                """
                DELETE FROM attribution_cache
                WHERE scope=? AND dest_path=? AND function_name=? AND repo_rev != ?
                """,
                (scope, dest_path, function_name, current_rev),
            )
        con.executemany(
            """
            INSERT INTO attribution_cache(
                scope, dest_path, function_name, repo_rev, payload_json, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, dest_path, function_name, repo_rev)
            DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            rows,
        )
        if include_files and include_functions:
            # A full warm rewrites every cacheable target, so anything left under
            # another revision is a TU or function that no longer qualifies. The
            # per-row DELETE above only reaches keys this warm rewrote, so those
            # orphans accumulated forever -- production carried six superseded
            # revisions, 2,876 dead function rows in the newest one alone. Prune
            # only after a complete warm: a --files-only pass would otherwise
            # throw away the function half of the previous revision.
            con.execute("DELETE FROM attribution_cache WHERE repo_rev != ?", (repo_rev,))

    return AttributionCacheWarmResult(
        repo_rev=repo_rev,
        file_targets=len(file_targets) if include_files else 0,
        function_targets=len(function_targets) if include_functions else 0,
        files_cached=files_cached,
        functions_cached=functions_cached,
    )
