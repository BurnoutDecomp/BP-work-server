# Automated build → download button

On every push to **`BP-Decomp_Workflow`** `main` (and on a daily schedule), a
**GitHub-hosted `windows-latest`** runner rebuilds the game, bundles the Google
Drive assets next to the exe, zips it, and uploads it to the work server. The
dashboard shows a **Download build** button pointing at the newest zip.

```
 BP-Decomp_Workflow push (main)  ──or──  daily schedule
        │
        ▼
 GitHub-hosted windows-latest runner (MSVC + MSYS2 + Strawberry Perl preinstalled)
   rclone sync Drive (service account) ──► assets mirror  (~1GB+, fresh each run)
   build_ffmpeg / build_lua (cached)   ──► vendored deps  (built once, then cached)
   build_game_exe.bat                  ──► build\game\Burnout_PC.exe (+ DLLs, .cgsmap)
   bundle exe + DLLs + assets           ──► zip
        │  POST /admin/builds  (admin X-Work-Token)
        ▼
 work server (adriwin.fr, Linux)
   stores zip under BP_DOWNLOADS_DIR, records the build
        │
        ▼
 dashboard "Download build" button ──► /download/latest
```

Why GitHub-hosted Windows: the build needs MSVC (`cl`), which the Linux download
server can't run. `windows-latest` ships MSVC (VS2022 Enterprise), MSYS2 at
`C:\msys64`, and Strawberry Perl at `C:\Strawberry` — the exact toolchain the
FFmpeg + game batch builds expect — so no self-hosted machine is needed. The
runner pushes the finished zip to the server over HTTPS.

> **The build is not CMake.** `BP-Decomp_Workflow` has a `b5-decomp/CMakeLists.txt`,
> but the *shipped* exe is produced by the bespoke `cl` response-file driver
> `tools/build/build_game_exe.bat`, which emits `build/game/Burnout_PC.exe` and
> links a prebuilt FFmpeg (movie player, a nested submodule at
> `b5-decomp/vendor/FFmpeg`) + Lua (FSM VM). `publish-build.ps1` drives that batch
> build and builds the two deps first if their outputs are missing; the workflow
> caches those outputs so they only build on the first run (or when the FFmpeg
> submodule / build scripts change). The runtime asset folders (SOUND, VIDEOS,
> LANGUAGE, …) are git-ignored (`build/*`), so they come from the rclone Drive
> sync, not the checkout.

## Cost / performance note

The asset set is large (SOUND alone is ~1 GB). A GitHub-hosted runner is wiped
each run, so every build does a full ~1 GB+ Drive download, then zips and uploads
that to the server. Windows runners bill at **2× minutes**, and the daily
schedule means this recurs. If minutes or latency become a problem, switch
`runs-on:` to a self-hosted Windows runner (which keeps assets/deps on disk and
syncs incrementally) — the `publish-build.ps1` driver is identical either way.

## Files (all live in the **BP-Decomp_Workflow** repo)

| This repo (BP-work-server) | Copy to BP-Decomp_Workflow |
| --- | --- |
| `deploy/ci/build-and-publish.yml` | `.github/workflows/build-and-publish.yml` |
| `deploy/ci/publish-build.ps1` | `ci/publish-build.ps1` |

The server-side pieces (upload endpoint, download routes, button) are already
part of BP-work-server — nothing to install there beyond the config below.

## One-time setup

### 1. Google Drive service account (for headless asset sync)

A GitHub-hosted runner can't do interactive OAuth, so rclone authenticates with a
**service account**:

1. In Google Cloud Console, create a project → enable the **Google Drive API**.
2. Create a **service account**, add a **JSON key**, download it.
3. **Share the asset Drive folder** (`1CgSSjtenfAc_Ps6_JLhtGhTly1K5n_HO`) with the
   service account's email (`...@...iam.gserviceaccount.com`), Viewer access.
4. Store the JSON key as the repo secret `GDRIVE_SA_JSON` (step 3 below).

The workflow builds the rclone remote at runtime from that key and pins it to the
folder ID (`rclone config create gdrive drive service_account_file … root_folder_id …`).

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
| Variable | `RCLONE_REMOTE` | `gdrive:` |
| Secret | `WORK_PUBLISH_TOKEN` | the admin token from step 2 |
| Secret | `GDRIVE_SA_JSON` | the full service-account JSON key from step 1 |

(`WORK_SERVER` and `RCLONE_REMOTE` are already set.)

### 4. nginx upload limit (on the server) — important

The zip (~1 GB) is uploaded *through* nginx to the app. nginx's default
`client_max_body_size` is **1 MB**, which will reject it with `413`. Raise it for
the upload path:

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
  `schedule` rebuilds so asset-only edits get published even when the source is
  quiet. The recorded `asset_manifest_hash` tells you which asset set a given
  build shipped.
- **First run is slow:** the initial build compiles FFmpeg (MSYS2/Perl) and Lua;
  the workflow caches both (keyed on the FFmpeg submodule commit + build scripts),
  so later runs restore them and skip straight to the game build.
- **Large downloads:** builds stream from disk via the app. If traffic grows,
  serve `/download/*` directly from nginx (`X-Accel-Redirect`) so the Python
  process isn't in the byte path.
