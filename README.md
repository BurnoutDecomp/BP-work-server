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
- File-tree entries and TU destinations link straight to the file on GitHub.
- Burnout Paradise themed dashboard (drop a `logo.png` into the static folder).
- Small stdlib HTTP client for `work.py` integration.

PostgreSQL is the right production database once more people are using it, but
the public protocol should not need to change.

## Setup

```powershell
cd E:\Reverse_Engineering\Burnout\BP-work-server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
```

## Import Workflow Progress

```powershell
bp-work-server --db data\bp-work.sqlite3 import E:\Reverse_Engineering\Burnout\BP-Decomp_Workflow --reset
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
