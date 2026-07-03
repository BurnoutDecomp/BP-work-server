# Automated build → download button

On every push to **`BP-Decomp_Workflow`** `main` (and on a daily schedule), a
**GitHub-hosted `windows-latest`** runner compiles the game exe and uploads a
small bundle to the work server. **The server** rclone-syncs the ~1 GB game
assets, merges them with the exe into a zip, and the dashboard's **Download
build** button serves the newest one.

```
 BP-Decomp_Workflow push (main)  ──or──  daily schedule
        │
        ▼
 GitHub-hosted windows-latest runner (MSVC + MSYS2 + Strawberry Perl preinstalled)
   build_ffmpeg / build_lua (cached) ──► vendored deps (built once, then cached)
   build_game_exe.bat                 ──► build\game\Burnout_PC.exe (+ DLLs, .cgsmap)
   zip JUST exe + DLLs + .cgsmap       ──► small bundle (no assets)
        │  POST /admin/builds  (admin X-Work-Token)
        ▼
 work server (adriwin.fr, Linux)
   rclone sync Drive ──► local asset mirror (~1 GB, incremental)
   merge exe bundle + assets ──► final zip, stored under BP_DOWNLOADS_DIR
        │
        ▼
 dashboard "Download build" button ──► /download/latest
```

**Why the split.** The build needs MSVC (`cl`), which the Linux server can't run,
so GitHub compiles. But the ~1 GB assets shouldn't ride through CI on every run
(download + re-upload = slow + 2× Windows minutes), so the server owns them: it
keeps a persistent, incrementally-synced mirror and assembles the download. CI
uploads only a small exe bundle.

> **The build is not CMake.** `BP-Decomp_Workflow` has a `b5-decomp/CMakeLists.txt`,
> but the *shipped* exe is produced by the bespoke `cl` response-file driver
> `tools/build/build_game_exe.bat`, which emits `build/game/Burnout_PC.exe` and
> links a prebuilt FFmpeg (movie player, a nested submodule at
> `b5-decomp/vendor/FFmpeg`) + Lua (FSM VM). The workflow caches those dep builds
> so they only compile on the first run (or when the FFmpeg submodule / build
> scripts change).

## Files (all live in the **BP-Decomp_Workflow** repo)

| This repo (BP-work-server) | Copy to BP-Decomp_Workflow |
| --- | --- |
| `deploy/ci/build-and-publish.yml` | `.github/workflows/build-and-publish.yml` |
| `deploy/ci/publish-build.ps1` | `ci/publish-build.ps1` |

The server-side pieces (upload/merge endpoint, download routes, button) are
already part of BP-work-server — configure them per below.

## One-time setup

### 1. rclone on the server (asset source)

The server merges assets only when `BP_ASSET_RCLONE_REMOTE` is set; it shells out
to `rclone sync <remote> <BP_ASSETS_DIR>` before each merge. On the server (Linux):

```bash
apt install rclone        # or the official install script
rclone config             # n) new remote, type "drive", name it e.g. gdrive
```

Pin the remote to the **folder ID** so re-sharing never breaks it: set
`root_folder_id` to `1CgSSjtenfAc_Ps6_JLhtGhTly1K5n_HO`. An interactive OAuth
token is fine here (unlike CI) because the server is persistent — it won't need a
browser re-auth. Verify: `rclone lsf gdrive:`.

Then set the server env (see the config table below): `BP_ASSET_RCLONE_REMOTE=gdrive:`.

### 2. Mint an admin token (on the server)

The runner authenticates to `/admin/builds` with an admin `X-Work-Token`:

```powershell
bp-work-server --db data\bp-work.sqlite3 worker add ci-build --admin
# prints: WORK_AGENT=<token>   <-- this is WORK_PUBLISH_TOKEN
```

### 3. Secrets & variables (in BP-Decomp_Workflow)

Settings → Secrets and variables → Actions:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `WORK_SERVER` | `https://adriwin.fr` |
| Secret | `WORK_PUBLISH_TOKEN` | the admin token from step 2 |

(`WORK_SERVER` is already set. No Drive credentials live on GitHub — the server
owns asset sync.)

### 4. nginx upload limit (on the server)

The exe bundle is small (tens of MB), so nginx's default 1 MB `client_max_body_size`
would still reject it. Raise it for the upload path (the server holds the request
open while it syncs assets and assembles the ~1 GB zip, so allow a long timeout):

```nginx
location /admin/builds {
    client_max_body_size 512m;    # exe bundle only; assets are added server-side
    proxy_read_timeout 3600s;     # rclone sync + 1 GB zip assembly happens in-request
    proxy_pass http://127.0.0.1:8765;
}
```

Reload nginx afterward.

## Server configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `BP_ASSET_RCLONE_REMOTE` | *(unset)* | rclone remote to sync assets from, e.g. `gdrive:`. **Unset → the server publishes the uploaded bundle as-is (no assets merged).** |
| `BP_ASSETS_DIR` | `data/assets` | Local asset mirror (rclone target; persistent, git-ignored). |
| `BP_RCLONE_BIN` | `rclone` | Path to the rclone binary. |
| `BP_DOWNLOADS_DIR` | `data/downloads` | Where assembled zips are stored (git-ignored; survives deploys). |
| `BP_KEEP_BUILDS` | `5` | Recent builds kept on disk; older zips pruned. Each is ~1 GB — tune this. |

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/admin/builds` | admin `X-Work-Token` | CI uploads the exe bundle; server merges assets + stores. |
| `GET` | `/api/builds` | public | Latest + recent builds (JSON), for the dashboard. |
| `GET` | `/download/latest` | public | Stream the newest build. |
| `GET` | `/download/{id}` | public | Stream a specific build. |

> **Public reminder:** `/download/*` is unauthenticated for now. The bundle ships
> the Drive assets alongside the exe — if those are original game files, gate this
> before making the site public. Password protection is the planned next step.

## Notes

- **Asset changes without a commit:** the server's `rclone sync` mirrors the
  current Drive state on every publish, so each build reflects whatever is in the
  folder *now*. The daily `schedule` triggers a rebuild+republish so asset-only
  edits reach the download even when the source is quiet. The recorded
  `asset_manifest_hash` (computed server-side) tells you which asset set shipped.
- **First run is slow:** the initial CI run compiles FFmpeg (MSYS2/Perl) and Lua;
  the workflow caches both, so later runs skip straight to the game build.
- **Assembly is in-request:** the server syncs assets and writes the ~1 GB zip
  while handling the upload (off the event loop, in a threadpool). If that ever
  gets too slow, move it to a background task and have `/api/builds` reflect only
  fully-assembled builds.
