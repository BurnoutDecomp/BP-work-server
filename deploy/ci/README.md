# Automated build → download button

On every push to **`BP-Decomp_Workflow`** `main` (and on a daily schedule), a
self-hosted Windows runner rebuilds the game, bundles the Google Drive assets
next to the exe, zips it, and uploads it to the work server. The dashboard shows
a **Download build** button pointing at the newest zip.

```
 BP-Decomp_Workflow push (main)  ──or──  daily schedule
        │
        ▼
 self-hosted Windows runner (MSVC)
   rclone sync Drive         ──► assets mirror  (only changed files transfer)
   build_lua / build_ffmpeg  ──► vendored deps  (built once, then cached)
   build_game_exe.bat        ──► build\game\Burnout_PC.exe (+ FFmpeg DLLs, .cgsmap)
   bundle exe + DLLs + assets ──► zip
        │  POST /admin/builds  (admin X-Work-Token)
        ▼
 work server (adriwin.fr, Linux)
   stores zip under BP_DOWNLOADS_DIR, records the build
        │
        ▼
 dashboard "Download build" button ──► /download/latest
```

Why a Windows runner: the build needs MSVC (`cl`), which the Linux download
server can't run. So the builder and the download host are different machines;
the runner pushes the finished zip to the server over HTTPS.

> **The build is not CMake.** `BP-Decomp_Workflow` has a `b5-decomp/CMakeLists.txt`,
> but the *shipped* exe is produced by the bespoke `cl` response-file driver
> `tools/build/build_game_exe.bat`, which emits `build/game/Burnout_PC.exe` and
> links a prebuilt FFmpeg (movie player) + Lua (FSM VM). `publish-build.ps1`
> drives that batch build and builds the two deps first if their outputs are
> missing. The runtime asset folders (SOUND, VIDEOS, LANGUAGE, …) are git-ignored
> (`build/*`), so they come from the rclone Drive sync, not the checkout.

## Files (all live in the **BP-Decomp_Workflow** repo)

| This repo (BP-work-server) | Copy to BP-Decomp_Workflow |
| --- | --- |
| `deploy/ci/build-and-publish.yml` | `.github/workflows/build-and-publish.yml` |
| `deploy/ci/publish-build.ps1` | `ci/publish-build.ps1` |

The server-side pieces (upload endpoint, download routes, button) are already
part of BP-work-server — nothing to install there beyond the config below.

## One-time setup

### 1. Self-hosted Windows runner

Register a runner on a Windows box that has the game's build toolchain with the
labels the workflow expects:

```
labels: self-hosted, windows, msvc
```

BP-Decomp_Workflow → Settings → Actions → Runners → New self-hosted runner. Add
`msvc` as a custom label. Keep it running as a service so scheduled builds fire
unattended. The box needs:

- **Visual Studio 2022** (MSVC `cl` — the toolchain `build_game_exe.bat` finds via
  `vcvars64`).
- **MSYS2 + Strawberry Perl** — only required the first time FFmpeg is built (see
  `tools/build/build_ffmpeg.bat`). Once `b5-decomp/vendor/ffmpeg-build/` is
  populated, later runs skip the FFmpeg build. To skip it entirely, prebuild
  FFmpeg on the runner once.
- **`py`** on PATH (optional) — used for the `.cgsmap` step; skipped if absent.

### 2. rclone + the Drive remote (on the runner)

```powershell
winget install Rclone.Rclone           # or scoop install rclone
rclone config                          # n) new remote, type "drive"
```

Pin the remote to the **folder ID**, not the share link, so re-sharing the folder
never breaks the build. In `rclone config` set `root_folder_id` to
`1CgSSjtenfAc_Ps6_JLhtGhTly1K5n_HO` (the folder from the Drive URL). For an
unattended runner use a **service account** (`service_account_file`) instead of the
interactive OAuth token so it never needs a browser re-auth. Name it e.g.
`gdrive` → the workflow passes `gdrive:` as `RCLONE_REMOTE`.

Verify:

```powershell
rclone lsf gdrive:            # lists the asset folder
```

### 3. Mint an admin token (on the server)

The runner authenticates to `/admin/builds` with an admin `X-Work-Token`:

```powershell
bp-work-server --db data\bp-work.sqlite3 worker add ci-build --admin
# prints: WORK_AGENT=<token>   <-- this is WORK_PUBLISH_TOKEN
```

### 4. Secrets & variables (in BP-Decomp_Workflow)

Settings → Secrets and variables → Actions:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `WORK_SERVER` | `https://adriwin.fr` |
| Variable | `RCLONE_REMOTE` | `gdrive:` (or `gdrive:Subfolder`) |
| Secret | `WORK_PUBLISH_TOKEN` | the admin token from step 3 |

### 5. nginx upload limit (on the server) — important

The zip is uploaded *through* nginx to the app. nginx's default
`client_max_body_size` is **1 MB**, which will reject a game bundle with `413`.
Raise it for the upload path (a game download served back out is fine, but the
upload needs headroom):

```nginx
location /admin/builds {
    client_max_body_size 0;      # or e.g. 8g
    proxy_request_buffering off;  # stream to the app instead of buffering to disk
    proxy_read_timeout 3600s;
    proxy_pass http://127.0.0.1:8765;
}
```

Reload nginx afterward.

## Server configuration (optional)

| Env var | Default | Purpose |
| --- | --- | --- |
| `BP_DOWNLOADS_DIR` | `data/downloads` | Where published zips are stored (git-ignored; survives deploys). |
| `BP_KEEP_BUILDS` | `5` | How many recent builds to keep on disk; older zips are pruned. |

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/admin/builds` | admin `X-Work-Token` | CI uploads a build zip. |
| `GET` | `/api/builds` | public | Latest + recent builds (JSON), for the dashboard. |
| `GET` | `/download/latest` | public | Stream the newest build. |
| `GET` | `/download/{id}` | public | Stream a specific build. |

> **Public reminder:** `/download/*` is unauthenticated for now. The bundle ships
> the Drive assets alongside the exe — if those are original game files, gate this
> before making the site public. Password protection is the planned next step.

## Notes

- **Asset changes without a commit:** `rclone sync` always mirrors the current
  Drive state, so each build reflects whatever is in the folder *now*. The daily
  `schedule` in the workflow rebuilds so asset-only edits get published even when
  the source is quiet. The recorded `asset_manifest_hash` tells you which asset
  set a given build shipped.
- **First run is slow:** the initial build compiles FFmpeg and Lua; subsequent
  runs skip both (they're only rebuilt when their outputs are missing, or when
  `publish-build.ps1` is invoked with `-ForceFfmpeg` / `-ForceLua`).
- **Large downloads:** builds stream from disk via the app. If traffic grows,
  serve `/download/*` directly from nginx (`X-Accel-Redirect`) so the Python
  process isn't in the byte path.
