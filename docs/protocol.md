# BP Work Server Protocol

The server coordinates claims and status for Burnout Paradise decompilation work.
It does not store IDA exports, leaked references, dossiers, or reconstructed code.
Those stay in `BP-Decomp_Workflow` and Git.

## Core Rules

- The server is authoritative for live TU status.
- `POST /claims` is the only way to claim work.
- Claims are leases. Agents must heartbeat or the server returns stale work to `todo`.
- Git remains authoritative for code review and source changes.
- A TU should become `done` only after the local compile/review policy has passed.

## Authentication (worker ids)

Access is gated by a **server-issued worker id (token)**, so the server URL does not need
to be secret — knowing the URL is not enough, you need a valid id. The maintainer mints
ids and hands them out privately.

- Each id maps to a **username**. Clients send the id in the `X-Work-Token` header; the
  server resolves it to the username and records the **username** as the claim owner. The
  id itself is never stored on TU rows or in the event log.
- Enforcement is **on by default** (`BP_WORK_REQUIRE_TOKEN=1`). Every write endpoint
  (`/claims`, `/claims/next`, heartbeat, release, `/tu/*`) requires a valid id; a
  missing/invalid/revoked id returns `401`. Read endpoints and the dashboard stay open.
  Set `BP_WORK_REQUIRE_TOKEN=0` (or `false`/`no`/`off`) to disable it for a fully
  private/trusted deployment, in which case the request body `agent` is used.
- **Admin is a role on a worker id** (`is_admin`), not a separate shared secret — there is
  no `BP_WORK_ADMIN_TOKEN`. The `/admin/*` endpoints (mint/list/revoke ids, import, sync,
  reset, event reconciliation) require an id whose worker has the admin role: a non-admin
  id gets `403`, a
  missing/invalid id `401`. Bootstrap the first admin on the host with the direct-DB CLI
  `bp-work-server worker add <name> --admin` (needs no existing admin).
- Revoking an id immediately blocks it; existing claims are unaffected until their lease
  expires or they are reassigned. Worker ids survive `/admin/sync?reset=true`.

## Statuses

| Status | Meaning |
| --- | --- |
| `todo` | Available to claim. |
| `in_progress` | Claimed by an agent with an active lease. |
| `compiled` | Local compile gate passed; waiting for review or merge policy. |
| `done` | Accepted complete work. |
| `blocked` | Not claimable until manually unblocked. |

## Endpoints

### `GET /health`

Returns server liveness and version.

### `GET /`

Serves the live read-only dashboard for humans. It shows progress counts, active
agents, active work, the next queue, imported goals, blocked TUs, and recent
events.

### `POST /admin/import?workflow_root=...&reset=false`

Imports `progress/tu_index.json`, `progress/status.json`, `progress/tu_deps.json`,
and `progress/goals.json` from the workflow repo.

Use `reset=true` for the initial import or for rebuilding a disposable dev server.
Avoid `reset=true` on a live server unless you intentionally want to discard claims.

All `/admin/*` endpoints require an `X-Work-Token` whose worker has the admin role.

### `POST /admin/workers`  (admin)

Mints a new worker id bound to a username. `is_admin` grants the new id the admin role.
Returns the id once — give it to the user privately; they set it as `WORK_AGENT`.

```json
// request
{ "username": "Adrian", "is_admin": false }
// response (201)
{ "token": "AbC123…urlsafe", "username": "Adrian", "is_admin": false }
```

### `GET /admin/workers`  (admin)

Lists workers (`token`, `username`, `active`, `is_admin`, `created_at`, `last_seen`).

### `DELETE /admin/workers/{token}`  (admin)

Revokes a worker id (`204`; `404` if unknown). Revoked ids can no longer authenticate.

### `GET /next?n=5&goal=boot-trace`

Returns dependency-ranked `todo` TUs. If `goal` is omitted, the imported active goal
is used. Goal ranking counts unresolved dependencies inside the goal only, matching
the existing `work next` behavior.

### `POST /claims`

Atomically claims a TU.

Request:

```json
{
  "tu": "GameSource/Foo/Bar.cpp",
  "agent": "adrian-codex-1",
  "lease_seconds": 7200,
  "force": false
}
```

Responses:

- `201`: claim succeeded.
- `409`: TU is already claimed, compiled, done, or blocked.
- `404`: unknown TU.

### `POST /claims/next`

Atomically ranks the `todo` queue and claims the top `n` TUs for one agent in a
single transaction. This is the "checkout" path: concurrent agents calling it get
**distinct** work, with no rank-then-claim race window. Leases auto-expire, so
over-claiming self-heals (claim 5, finish 1, the rest return to `todo`).

Request:

```json
{
  "agent": "adrian-codex-1",
  "n": 5,
  "lease_seconds": 7200,
  "goal": "boot-trace"
}
```

`goal` is optional; when omitted the imported active goal is used (same ranking as
`GET /next`). Response returns the claims that succeeded — possibly fewer than `n`
if the queue is short:

```json
{
  "active_goal": "boot-trace",
  "count": 5,
  "claimed": [
    {"claimed": true, "tu": "GameSource/Foo/Bar.cpp", "status": "in_progress",
     "owner": "adrian-codex-1", "lease_expires_at": "2026-06-14T12:00:00+00:00"}
  ]
}
```

### `POST /claims/{tu}/heartbeat`

Renews the current owner's lease.

Request:

```json
{
  "agent": "adrian-codex-1",
  "lease_seconds": 7200
}
```

### `DELETE /claims/{tu}`

Releases the current owner's claim and returns the TU to `todo`.

Request body:

```json
{
  "agent": "adrian-codex-1"
}
```

### `POST /tu/{tu}/compiled`

Marks a claimed TU as `compiled`.

Request:

```json
{
  "agent": "adrian-codex-1",
  "notes": "compile gate passed",
  "commit": "optional-git-commit",
  "files": ["b5-decomp/src/GameSource/Foo/Bar.cpp"]
}
```

### `POST /tu/{tu}/review`

Records the reviewer verdict. `pass` marks the TU `done`; `fail` returns it to
`in_progress` for the reporting agent.

Request:

```json
{
  "agent": "reviewer-or-owner",
  "verdict": "pass",
  "notes": "trivial; gate-only",
  "commit": "optional-git-commit"
}
```

### `POST /tu/{tu}/block`

Marks a TU `blocked`.

Request:

```json
{
  "agent": "adrian-codex-1",
  "reason": "Vendor code; exists in PC lib or vendor source."
}
```

### `POST /tu/{tu}/unblock`

Returns a blocked TU to `todo`.

### `POST /tu/{tu}/reset`

Returns a TU to `todo`, clears its owner/lease, resets every function in that TU to
`todo`, clears stored `completed_by`/`completed_at` attribution, and drops cached
file/function attribution for the TU destination path.

Request:

```json
{
  "agent": "adrian-codex-1",
  "notes": "returned to queue for rework"
}
```

### `GET /export/status`

Regenerates the committed `progress/status.json` from the live DB and returns it as
JSON. It is the inverse of `/admin/import`: it emits only the **durable** states the
workflow CLI would commit to git — `done`/`blocked` TUs (with `notes`) plus every
non-`todo` func status — and never the transient live layer (`in_progress`/`compiled`,
owners, leases).

Open read (the durable subset it returns is already visible in `/snapshot`). A CI job
in `BP-Decomp_Workflow` fetches this, writes it to `progress/status.json`, bumps the
`b5-decomp` submodule pointer, and commits — so decomp workers push only to `b5-decomp`
and never need write access to the workflow repo. Because `blocked` TUs leave no
distinguishing file, the server is the **only** authority that can fully reconstruct
status.json; a files-only reconcile can recover `done` but not `blocked`.

```json
{
  "tu": {
    "GameSource/Foo/Bar.cpp": {"status": "done"},
    "GameSource/Vendor/Baz.cpp": {"status": "blocked", "notes": "Vendor code; in PC lib"}
  },
  "func": {
    "Foo::Bar::Run": {"status": "reviewed"}
  }
}
```

Consumers should write it with `json.dump(..., indent=1, sort_keys=True)` to match the
byte layout the workflow's `sync_status` produces.

### `GET /snapshot?include_tus=true`

Returns status counts and, optionally, the TU table.

### `GET /events?after=0&limit=200`

Returns the append-only event log for polling dashboards or syncing local caches.

### `GET /events/stream?after=0`

Server-sent events stream used by the dashboard. It emits `work-event` messages
when new events are available and periodic `tick` messages so browsers refresh
even when a lease expiry changes state without a user action.

### `GET /dashboard/state`

Dashboard-optimized summary. It returns aggregate progress, active agents,
active work, blocked work, recent events, imported goals, and the next ranked
TUs without requiring the browser to pull the full TU table.

If local-git attribution is incomplete for the current `b5-decomp` revision, the
response includes `attribution_cache_warming: true` and schedules cache warming
in the background. A later request will pick up the warmed attribution data.

## `work.py` Integration

The server is **optional and invite-only**: the local workflow runs fully standalone and
is only coordinated when `WORK_SERVER` is set. Config lives in a repo-root `.env` (copy
`.env.example`), which `work` auto-loads — not shell exports. Only people given the URL
turn it on:

```
# .env (git-ignored; copy from .env.example). Leave WORK_SERVER unset to work locally.
WORK_SERVER=http://your-server:8765   # only if you were given a URL
WORK_AGENT=adrian-codex-1
```

```
work claim -n 1                 # checkout the next ready TU (atomic when a server is set)
work start "GameSource/Foo/Bar.cpp"   # or claim one specific TU by id
```

Mapping:

| Local command | Server call |
| --- | --- |
| `work next` | `GET /next` (preview only — reserves nothing) |
| `work claim [-n N]` | `POST /claims/next` (atomic checkout of the next N) |
| `work start <tu>` | `POST /claims` |
| `work submit <tu>` pass | local compile, then `POST /tu/{tu}/compiled` |
| `work review <tu> --verdict pass` | `POST /tu/{tu}/review` |
| `work block <tu>` | `POST /tu/{tu}/block` |
| `work unblock <tu>` | `POST /tu/{tu}/unblock` |
| `work reset-tu <tu>` | `POST /tu/{tu}/reset` |
| `work server-sync [--branch B]` | `POST /admin/sync` (reset=false) |
| `work server-update [--reconcile]` | refresh `class_homes.json` (+status) & push, then `POST /admin/sync` (reset=false) |
| `work server-reset [--to REF]` | `POST /admin/sync` with `reset=true` |

The local `ledger.sqlite` can remain a cache for dossiers, dependencies, and
offline work. The server exists to prevent duplicate claims.

## Two-store model and reverting

The server is **derived from** the workflow repo, not parallel to it. `progress/status.json`
is the durable record (the `done`/`blocked` states tied to committed code) and the seed
for both `/admin/sync` and a fresh `work bootstrap`. Live claims, leases, `owner`, the
transient `in_progress`/`compiled` states, and the event log are born on the server.

When `WORK_SERVER` is set, the workflow CLI writes only durable statuses into
`status.json` (no `owner`, no `in_progress`) so concurrent agents don't collide on git.

To revert everything to a known-good commit, run `work server-reset --to <ref>`: it
`git reset`s the workflow repo + `b5-decomp`, drops the local `ledger.sqlite` cache, and
re-seeds the server via `POST /admin/sync` with `reset=true`. The reset discards live
claims and the event log (claims are ephemeral; event history is not recoverable), so it
is the deliberate clean-slate path.
