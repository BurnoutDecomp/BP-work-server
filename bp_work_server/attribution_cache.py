from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from bp_work_server.store import WorkStore, iso


ProgressCallback = Callable[[str, int, int, str], None]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttributionCacheWarmResult:
    repo_rev: str
    file_targets: int
    function_targets: int
    files_cached: int
    functions_cached: int
    base_rev: str | None = None
    files_reused: int = 0
    functions_reused: int = 0


def warm_attribution_cache(
    store: WorkStore,
    decomp: Any,
    *,
    include_files: bool = True,
    include_functions: bool = True,
    progress: ProgressCallback | None = None,
    full: bool = False,
) -> AttributionCacheWarmResult:
    """Precompute local-git surviving-line attribution for cacheable reviewed work.

    Incremental by default. `git blame` and `git log` for a file depend only on
    that file's own history, so when the previously warmed revision is an
    ancestor of this one, every destination whose file is untouched between them
    blames identically and its cached payload is carried forward verbatim. Only
    the handful of files a push actually changed -- plus targets the previous
    pass never covered -- are recomputed.

    That is the difference between a pass that finishes in seconds and one that
    takes an hour. b5-decomp is pushed every few minutes while people work, so
    an hour-long pass meant the contribution numbers were permanently an hour
    behind, and any restart killed the run and started it over.
    """
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

    base_rev, reusable = _reusable_payloads(store, decomp, repo_rev, full=full)

    files_cached = 0
    functions_cached = 0
    files_reused = 0
    functions_reused = 0
    now = iso()
    rows: list[tuple[str, str, str, str, str, str]] = []

    if include_files:
        total = len(file_targets)
        for index, dest_path in enumerate(file_targets, start=1):
            payload_json = reusable.get(("file", dest_path, ""))
            if payload_json is None:
                history = decomp.history(dest_path)
                contributors = (
                    decomp.contributors(dest_path)
                    if hasattr(decomp, "contributors")
                    else {"contributors": [], "basis": "surviving_lines", "path": None}
                )
                payload_json = json.dumps(
                    {"latest": history[0] if history else None, "contributors": contributors},
                    sort_keys=True,
                )
            else:
                files_reused += 1
            rows.append(("file", dest_path, "", repo_rev, payload_json, now))
            files_cached += 1
            if progress and (index == total or index % 25 == 0):
                progress("files", index, total, dest_path)

    if include_functions:
        total = len(function_targets)
        for index, (dest_path, function_name) in enumerate(function_targets, start=1):
            payload_json = reusable.get(("function", dest_path, function_name))
            if payload_json is None:
                if hasattr(decomp, "function_contributors"):
                    payload = decomp.function_contributors(dest_path, function_name)
                elif hasattr(decomp, "contributors"):
                    payload = decomp.contributors(dest_path)
                else:
                    payload = {}
                payload_json = json.dumps(payload, sort_keys=True)
            else:
                functions_reused += 1
            rows.append(("function", dest_path, function_name, repo_rev, payload_json, now))
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

    log.info(
        "attribution warm %s: %d/%d files and %d/%d functions carried forward from %s",
        repo_rev[:12],
        files_reused,
        files_cached,
        functions_reused,
        functions_cached,
        (base_rev or "nothing")[:12],
    )
    return AttributionCacheWarmResult(
        repo_rev=repo_rev,
        file_targets=len(file_targets) if include_files else 0,
        function_targets=len(function_targets) if include_functions else 0,
        files_cached=files_cached,
        functions_cached=functions_cached,
        base_rev=base_rev,
        files_reused=files_reused,
        functions_reused=functions_reused,
    )


def _reusable_payloads(
    store: WorkStore,
    decomp: Any,
    repo_rev: str,
    *,
    full: bool,
) -> tuple[str | None, dict[tuple[str, str, str], str]]:
    """Cached payloads from an earlier revision that this one can reuse verbatim.

    An entry survives only if its destination resolves to files untouched
    between the two revisions -- every candidate path, because a *.h that gains
    or loses its .cpp sibling changes which file the blame came from even though
    neither path was edited.
    """
    if full or not hasattr(decomp, "changed_paths"):
        return None, {}
    base_rev = store.attribution_cache_reuse_base(exclude_rev=repo_rev)
    if not base_rev:
        return None, {}
    changed = decomp.changed_paths(base_rev, repo_rev)
    if changed is None:
        # Not an ancestor (a force-push or rewritten branch) or the base commit
        # is gone: nothing from it can be trusted.
        log.info("attribution warm cannot reuse %s; recomputing in full", base_rev[:12])
        return None, {}
    cached = store.attribution_cache_payloads(base_rev)
    if not cached:
        return None, {}
    if not changed:
        return base_rev, cached
    stale_destinations = {
        dest_path
        for _scope, dest_path, _function_name in cached
        if any(candidate in changed for candidate in decomp.candidate_paths(dest_path))
    }
    return base_rev, {
        key: payload
        for key, payload in cached.items()
        if key[1] not in stale_destinations
    }
