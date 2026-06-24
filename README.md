# BP Work Server

Coordination server for the Burnout Paradise decompilation workflow.

This service prevents multiple agents from claiming the same translation unit at
the same time. It stores live work status, owners, leases, goals, dependencies,
and an event log. It does **not** store reconstructed code, IDA exports, leaked
references, dossiers, or other decompilation evidence.

## Current State

This repo contains an MVP server:

- FastAPI HTTP API.
- Live web dashboard at `/`.
- SQLite-backed store for local/dev deployment.
- Atomic TU claims.
- Lease expiry for abandoned work.
- Dependency-ranked `next` compatible with the existing `work next` behavior.
- Import from `BP-Decomp_Workflow/progress`.
- Append-only event log.
- Server-sent events stream for real-time dashboard refresh.
- GitHub repo overview (info, recent commits, file tree) on the dashboard.
- Explorer panel to search/filter/sort every TU and function, with a detail
  drawer showing the data handed to agents (deps, dependents, funcs, goals).
- Git-derived contribution attribution per agent: "contributed to" (any
  surviving-line author) and "primary on" (dominant author). `class:` TUs are
  attributed via `progress/class_homes.json`; the dashboard shows only
  GitHub-verifiable data (git-reconstructed events are hidden by default,
  `BP_HIDE_RECONSTRUCTED=0` to reveal).
- File-tree entries and TU destinations link straight to the file on GitHub.
- Burnout Paradise themed dashboard (drop a `logo.png` into the static folder).
- Small stdlib HTTP client for `work.py` integration.

PostgreSQL is the right production database once more people are using it, but
the public protocol should not need to change.

## Quick Start

From the repo root, `launch.ps1` sets up a local `.venv`, refreshes the database
from the workflow checkout, and serves. It resolves every path relative to
itself, so the repo can live anywhere.

```powershell
.\launch.ps1                                  # serve on 127.0.0.1:8765
.\launch.ps1 -HostName 0.0.0.0 -Port 8765     # bind for LAN access
.\launch.ps1 -NoImport                         # serve existing db, skip import
```

By default the workflow checkout is expected as a sibling folder
(`..\BP-Decomp_Workflow`). Override with `-WorkflowRoot <path>` or the
`BP_WORKFLOW_ROOT` environment variable; override the database with `-Db` or
`BP_WORK_DB`.

## Manual Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
```

## Import Workflow Progress

```powershell
bp-work-server --db data\bp-work.sqlite3 import ..\BP-Decomp_Workflow --reset
```

Expected scale for the current workflow snapshot:

```text
4319 TUs
27549 funcs
21548 dependency edges
3 goals
```

## Run

```powershell
bp-work-server --db data\bp-work.sqlite3 serve --host 0.0.0.0 --port 8765
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8765/health
```

Dashboard:

```text
http://localhost:8765/
```

Ask for the next ranked work item:

```powershell
Invoke-RestMethod "http://localhost:8765/next?n=5"
```

Claim work:

```powershell
Invoke-RestMethod http://localhost:8765/claims `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"tu":"GameSource/Foo/Bar.cpp","agent":"adrian-codex-1","lease_seconds":7200}'
```

## GitHub Dashboard Panel

The dashboard mirrors a GitHub repository (default `Adriwin06/b5-decomp` on the
`dev` branch): description, stars/forks/issues, recent commits, and the file
tree. The browser only talks to this server's `/github/overview` endpoint; the
server proxies and caches GitHub so every viewer shares one upstream request.

Rate limits are handled in two layers: a per-resource TTL plus conditional
`ETag` requests (GitHub does **not** count `304 Not Modified` responses against
the limit). Unauthenticated access allows 60 requests/hour; set a token to raise
it to 5000/hour.

```powershell
$env:GITHUB_TOKEN = "ghp_xxx"          # optional, raises the rate limit
$env:BP_GITHUB_OWNER = "Adriwin06"     # optional overrides
$env:BP_GITHUB_REPO  = "b5-decomp"
$env:BP_GITHUB_REF   = "dev"
```

## Explorer API

The dashboard's Explorer is backed by read-only JSON endpoints:

```text
GET /api/facets           # filter options: sources, statuses, goals
GET /api/tus              # search/filter/sort TUs (q, status, source, goal, owner, sort, order, limit, offset)
GET /api/tu?id=<tu>       # full detail for one TU (funcs, deps, dependents, goals)
GET /api/funcs            # search functions (q, status, tu, limit, offset)
```

`sort` accepts `id`, `funcs`, `updated`, `status`, or `queue` (dependency-ranked,
matching `next`).

## Syncing the Server

When commits reach `b5-decomp` (or the workflow's `progress/` files) outside the
server's normal claim/submit flow, refresh the server's derived state: re-resolve
class homes, re-import the progress files, then re-warm Git attribution.

**The easy way — one command.**

- **Local server** (this repo's dev DB) — from the server repo:

  ```powershell
  .\sync.ps1                 # backup -> resolve class homes -> import (no reset) -> warm
  .\sync.ps1 -Reconcile      # also reconcile status.json from committed files (promote-only)
  ```

- **Remote server** (over HTTP) — from the workflow repo:

  ```powershell
  work server-update                 # refresh class homes, push, re-import on the server
  work server-update --reconcile     # also reconcile status.json (promote-only)
  ```

Both preserve live claims + the event log (no `--reset`), and `sync.ps1` backs up the
DB first (timestamped, never clobbered). The remote path re-warms attribution lazily on
the next dashboard view.

**The manual steps** (what those commands wrap):

```powershell
# 1) Workflow repo: refresh the derived inputs from the new commits
cd ..\BP-Decomp_Workflow
git pull                                          # new commits + reconciled status.json
git -C b5-decomp fetch origin dev                 # local clone needs them for git-blame
python tools\work\resolve_class_homes.py --apply  # refresh class TU -> real home-file map

# If the new commits also changed which TUs are done and status.json is not
# already reconciled, regenerate it from the committed files first:
python tools\work\reconcile_from_files.py --apply --no-demote

# 2) Server repo: re-import progress, then re-warm attribution
cd ..\BP-work-server
bp-work-server --db data\bp-work.sqlite3 import ..\BP-Decomp_Workflow
bp-work-server --db data\bp-work.sqlite3 warm-attribution-cache `
    --decomp-root ..\BP-Decomp_Workflow\b5-decomp --branch dev
```

- `import` (without `--reset`) updates TU status/metadata and reads
  `progress/class_homes.json`, preserving existing data.
- `warm-attribution-cache` recomputes Git surviving-line attribution for the new
  revision. The dashboard also auto-warms when it sees a new repo revision, so the
  explicit warm is optional.
- `resolve_class_homes.py` maps each `class:` TU to the committed file that holds
  its code, so class work attributes to its authors instead of a synthetic
  `src/classes/<Class>.cpp` path; ambiguous classes are left unmapped, never guessed.

> There is intentionally **no event-reconstruction command**. Synthesizing
> `review_pass` events from git history (a removed `reconcile-events` tool) produced
> fabricated events stamped with commit dates; per-person credit is now derived
> purely from Git attribution. Any reconstructed events left from that era are
> hidden by default (`BP_HIDE_RECONSTRUCTED=0` to reveal them).

## Branding

The header shows `/static/logo.png` if present and falls back to a `B5` mark
otherwise. Drop a Burnout Paradise logo at
`bp_work_server/static/logo.png` to brand the dashboard; the rest of the theme
adapts around it.

## Tests

```powershell
python -m pytest -q
python -m compileall bp_work_server
```

## Protocol

See [docs/protocol.md](docs/protocol.md).

The `BP-Decomp_Workflow` integration is opt-in:

```powershell
$env:WORK_SERVER = "http://your-server:8765"
$env:WORK_AGENT = "adrian-codex-1"
work next
work start "GameSource/Foo/Bar.cpp"
```

When `WORK_SERVER` is unset, the original local-only `work` behavior is unchanged.
