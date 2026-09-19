from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import subprocess
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response

from bp_work_server.dependencies import get_store, invalidate_dashboard_cache, require_admin_worker
from bp_work_server.models import (
    BuildContentsResponse,
    BuildEntry,
    BuildInfo,
    BuildListResponse,
)
from bp_work_server.store import WorkStore

_MAX_CONTENTS_ENTRIES = 20000  # guard payload size for zips with huge file counts

router = APIRouter()
log = logging.getLogger(__name__)

_UPLOAD_CHUNK = 1024 * 1024  # 1 MiB streamed writes so a multi-GB zip never buffers in RAM.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def downloads_dir() -> Path:
    """Directory holding published build zips. Under BP_DOWNLOADS_DIR (default
    ``data/downloads``, which is git-ignored and survives the deploy's git reset)."""
    d = Path(os.environ.get("BP_DOWNLOADS_DIR", "data/downloads"))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- Download quotas + counting ----------------------------------------------
#
# The public counter counts DOWNLOADERS: the first start of a build (per kind) by an
# address on a UTC day. Every start counts against that address's daily quota per kind,
# so one machine cannot pull the 5 GB zip a hundred times. The client address comes from
# Cloudflare (CF-Connecting-IP), else the proxy's X-Forwarded-For / X-Real-IP, else the
# socket -- the app only ever listens behind nginx on localhost, so the headers are
# trusted. Limits: BP_DL_FULL_PER_DAY (default 3), BP_DL_UPDATE_PER_DAY (default 30).
#
# Transfer: with BP_DOWNLOADS_ACCEL set (e.g. "/_dl/"), the response only carries an
# X-Accel-Redirect and nginx streams the file from its internal location -- ranges,
# resumes and the 5 GB body never pass through Python. Unset (dev/tests): FileResponse.
KIND_FULL = "full"
KIND_UPDATE = "update"


def _quota(kind: str) -> int:
    key = "BP_DL_FULL_PER_DAY" if kind == KIND_FULL else "BP_DL_UPDATE_PER_DAY"
    default = 3 if kind == KIND_FULL else 30
    try:
        return max(1, int(os.environ.get(key, str(default))))
    except ValueError:
        return default


def _client_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _utc_day() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _keep_builds() -> int:
    try:
        return max(1, int(os.environ.get("BP_KEEP_BUILDS", "5")))
    except ValueError:
        return 5


# ---- Server-side asset merge -------------------------------------------------
#
# CI (a GitHub-hosted runner) compiles *only* the exe and uploads a small bundle
# (exe + FFmpeg DLLs + .cgsmap). The heavy game assets (~1 GB) live here: the
# server rclone-syncs them from Drive and merges them with the exe into the zip
# the download button serves. This keeps the ~1 GB off CI (no per-run download/
# upload) and lets the sync be incremental on the server's persistent disk.
#
# Opt-in: only when BP_ASSET_RCLONE_REMOTE is set does the server sync+merge.
# Unset (dev/tests) -> the uploaded bundle is stored verbatim.


def _asset_remote() -> str | None:
    """rclone remote to sync assets from, e.g. ``gdrive:``. None disables merge."""
    return os.environ.get("BP_ASSET_RCLONE_REMOTE") or None


def assets_dir() -> Path:
    """Local mirror of the Drive assets (persistent; incremental rclone target)."""
    d = Path(os.environ.get("BP_ASSETS_DIR", "data/assets"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_rclone(remote: str, dest: Path) -> None:
    """Mirror ``remote`` into ``dest`` (adds/edits/deletes). Raises on failure.

    Isolated in its own function so tests can monkeypatch it with a fake that
    populates ``dest`` without a real rclone/Drive.
    """
    rclone = os.environ.get("BP_RCLONE_BIN", "rclone")
    subprocess.run(  # noqa: S603 - args are server-config, not user input
        [rclone, "sync", remote, str(dest), "--fast-list", "--transfers", "8", "--checkers", "16"],
        check=True,
    )


def _asset_manifest_hash(root: Path) -> str | None:
    """Fingerprint the synced asset set (sha256 over sorted ``relpath:md5`` lines),
    so each build records exactly which assets it shipped. None if empty."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        return None
    h = hashlib.sha256()
    for f in files:
        rel = f.relative_to(root).as_posix()
        digest = hashlib.md5(f.read_bytes()).hexdigest()  # noqa: S324 - fingerprint, not security
        h.update(f"{rel}:{digest}\n".encode())
    return h.hexdigest()


def _assemble_build_zip(bundle: Path, assets: Path | None, dest: Path) -> tuple[str, int]:
    """Write ``dest`` = the uploaded exe bundle's files + the asset tree at the root.
    Returns ``(sha256, size_bytes)`` of the finished zip.

    Assets are STORED (they're already-compressed game data; DEFLATE would burn CPU
    for almost no gain); the small exe/DLL entries are DEFLATED.
    """
    with zipfile.ZipFile(dest, "w") as out:
        with zipfile.ZipFile(bundle) as src:
            for info in src.infolist():
                if info.is_dir():
                    continue
                out.writestr(info.filename, src.read(info.filename), compress_type=zipfile.ZIP_DEFLATED)
        if assets is not None:
            for f in sorted(p for p in assets.rglob("*") if p.is_file()):
                out.write(f, f.relative_to(assets).as_posix(), compress_type=zipfile.ZIP_STORED)
    hasher = hashlib.sha256()
    with dest.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_UPLOAD_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest(), dest.stat().st_size


def _safe_slug(value: str | None, fallback: str) -> str:
    slug = _SAFE.sub("-", (value or "").strip()).strip("-")
    return slug or fallback


def _build_info(row: dict, previous: dict | None = None) -> BuildInfo:
    """``previous`` is the build published just before this one: ``assets_changed`` says
    whether its asset manifest differs, i.e. whether the exe-only update is enough."""
    changed = None
    if previous is not None:
        changed = (previous.get("asset_manifest_hash") or "") != (row.get("asset_manifest_hash") or "")
    return BuildInfo(
        download_url=f"/download/{row['id']}",
        update_url=f"/download/{row['id']}/update" if row.get("bundle_filename") else None,
        assets_changed=changed,
        **row,
    )


def _friendly_name(row: dict, kind: str = KIND_FULL) -> str:
    tag = _safe_slug(row.get("commit_short") or row.get("commit_sha"), "build")
    return f"burnout-paradise-{tag}{'-update' if kind == KIND_UPDATE else ''}.zip"


@router.post("/admin/builds", response_model=BuildInfo, status_code=status.HTTP_201_CREATED)
async def upload_build(
    request: Request,
    file: UploadFile = File(..., description="The compiled exe bundle (exe + DLLs + .cgsmap), zipped"),
    commit_sha: str = Form(..., description="b5-decomp revision the exe was built from"),
    commit_short: str | None = Form(None),
    branch: str | None = Form(None),
    asset_manifest_hash: str | None = Form(None, description="Ignored when the server merges assets; used verbatim otherwise"),
    built_at: str | None = Form(None, description="ISO time CI produced the artifact"),
    notes: str | None = Form(None),
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> BuildInfo:
    """Receive a compiled exe bundle from CI and publish the game as the latest download.

    CI uploads only the exe bundle (exe + FFmpeg DLLs + .cgsmap). When
    ``BP_ASSET_RCLONE_REMOTE`` is set, the server rclone-syncs the ~1 GB game assets
    and merges them with the exe into the served zip; otherwise the bundle is stored
    verbatim. Names the artifact by its content hash, records it, then prunes older
    builds off disk. Called with an admin ``X-Work-Token``.
    """
    dest = downloads_dir()
    previous = store.latest_build()
    tmp = dest / f".incoming-{_safe_slug(commit_short or commit_sha, 'build')}.part"
    bundle_hasher = hashlib.sha256()
    bundle_size = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                bundle_hasher.update(chunk)
                bundle_size += len(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if bundle_size == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "uploaded build is empty")

    remote = _asset_remote()
    if remote:
        # Sync assets + merge exe+assets into the final zip. Both are blocking
        # (subprocess + a ~1 GB zip write), so run off the event loop.
        assembled = dest / f".assembled-{_safe_slug(commit_short or commit_sha, 'build')}.part"
        try:
            assets = assets_dir()
            await run_in_threadpool(_run_rclone, remote, assets)
            asset_manifest_hash = await run_in_threadpool(_asset_manifest_hash, assets)
            sha256, size = await run_in_threadpool(_assemble_build_zip, tmp, assets, assembled)
        except subprocess.CalledProcessError as exc:
            tmp.unlink(missing_ok=True)
            assembled.unlink(missing_ok=True)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "asset sync (rclone) failed") from exc
        except Exception:
            tmp.unlink(missing_ok=True)
            assembled.unlink(missing_ok=True)
            raise
        # keep the exe-only bundle too: the "update" download for players who have the assets
        bundle_sha256 = bundle_hasher.hexdigest()
        bundle_filename = f"exe-{_safe_slug(commit_short or commit_sha, 'build')}-{bundle_sha256[:12]}.zip"
        tmp.replace(dest / bundle_filename)
        source = assembled
    else:
        # No asset merge configured: publish the uploaded bundle as-is; it IS the update too.
        sha256, size = bundle_hasher.hexdigest(), bundle_size
        bundle_sha256, bundle_filename = sha256, None
        source = tmp

    # Content-addressed name: identical bytes reuse the same file; a re-publish of the
    # same commit with changed assets gets a distinct name via its hash.
    filename = f"burnout-{_safe_slug(commit_short or commit_sha, 'build')}-{sha256[:12]}.zip"
    final = dest / filename
    source.replace(final)

    row = store.record_build(
        commit_sha=commit_sha,
        commit_short=commit_short,
        branch=branch,
        asset_manifest_hash=asset_manifest_hash,
        filename=filename,
        size_bytes=size,
        sha256=sha256,
        built_at=built_at,
        notes=notes,
    )
    store.set_build_bundle(row["id"], bundle_filename or filename, bundle_size, bundle_sha256)
    row = store.get_build(row["id"]) or row

    # Drop older builds from disk (keep the newest BP_KEEP_BUILDS). A file is only
    # unlinked when no surviving row still points at it (content-addressed sharing).
    pruned = store.prune_builds(_keep_builds())
    if pruned:
        survivors = store.list_builds(limit=_keep_builds() + len(pruned))
        live = {r["filename"] for r in survivors} | {r.get("bundle_filename") for r in survivors}
        for stale in pruned:
            for name in (stale.get("filename"), stale.get("bundle_filename")):
                if name and Path(name).name not in live:
                    (dest / Path(name).name).unlink(missing_ok=True)

    invalidate_dashboard_cache(request)
    log.info(
        "published build id=%s commit=%s size=%s assets=%s",
        row["id"], row["commit_short"], size, asset_manifest_hash,
    )
    return _build_info(row, previous)


@router.get("/api/builds", response_model=BuildListResponse)
def list_builds(store: WorkStore = Depends(get_store)) -> BuildListResponse:
    rows = store.list_builds(limit=21)
    builds = [
        _build_info(r, rows[i + 1] if i + 1 < len(rows) else None) for i, r in enumerate(rows[:20])
    ]
    return BuildListResponse(latest=builds[0] if builds else None, builds=builds)


@router.get("/api/builds/{build_id}/contents", response_model=BuildContentsResponse)
def build_contents(build_id: int, store: WorkStore = Depends(get_store)) -> BuildContentsResponse:
    """List the files inside a build's zip (from its central directory, no extraction)."""
    row = store.get_build(build_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such build")
    path = downloads_dir() / Path(row["filename"]).name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "build artifact is no longer on disk")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "not a readable zip") from exc

    entries: list[BuildEntry] = []
    total_size = 0
    total_files = 0
    for info in infos:
        is_dir = info.is_dir()
        if not is_dir:
            total_files += 1
            total_size += info.file_size
        if len(entries) < _MAX_CONTENTS_ENTRIES:
            entries.append(
                BuildEntry(path=info.filename, size=info.file_size, is_dir=is_dir)
            )
    entries.sort(key=lambda e: e.path.lower())
    return BuildContentsResponse(
        id=build_id,
        filename=_friendly_name(row),
        total_files=total_files,
        total_size=total_size,
        truncated=len(infos) > _MAX_CONTENTS_ENTRIES,
        entries=entries,
    )


def _is_fresh_download(request: Request) -> bool:
    """True when a request starts a download (vs. a resume/segment fetch of the same
    file). Browsers issue a click as a Range-less GET or a ``bytes=0-`` GET; segmented
    downloaders re-request later byte ranges, which we don't want to double-count."""
    rng = request.headers.get("range")
    return not rng or rng.replace(" ", "").startswith("bytes=0-")


def _serve(request: Request, store: WorkStore, row: dict | None, kind: str = KIND_FULL) -> Response:
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no build available")
    name = row.get("bundle_filename") if kind == KIND_UPDATE else row["filename"]
    if not name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this build has no exe-only bundle")
    path = downloads_dir() / Path(name).name
    if not path.is_file():
        # DB row survived but the file was pruned/lost; treat as gone rather than 500.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "build artifact is no longer on disk")
    # HEAD is the page asking "may I?" before it navigates: never counted, but refused
    # the same way once the quota is spent.
    if request.method != "HEAD" and _is_fresh_download(request):
        _first, today = store.record_download(
            ip=_client_ip(request), day=_utc_day(), build_id=row["id"], kind=kind
        )
        if today > _quota(kind):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"download limit reached: {_quota(kind)} {kind} download(s) per address per day; "
                + ("the exe-only update has its own, larger allowance" if kind == KIND_FULL
                   else "try again tomorrow"),
                headers={"Retry-After": "86400"},
            )
    elif request.method == "HEAD":
        if _quota_spent(store, request, kind):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "download limit reached",
                                headers={"Retry-After": "86400"})
    friendly = _friendly_name(row, kind)
    accel = os.environ.get("BP_DOWNLOADS_ACCEL")
    if accel:
        # nginx streams it from its internal location (ranges/resumes included); Python
        # only answers with the headers.
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": accel.rstrip("/") + "/" + path.name,
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{friendly}"',
                "Cache-Control": "no-store",
            },
        )
    return FileResponse(path, media_type="application/zip", filename=friendly)


def _quota_spent(store: WorkStore, request: Request, kind: str) -> bool:
    with store.connect() as con:
        total = con.execute(
            "SELECT COALESCE(SUM(hits), 0) FROM download_hit WHERE ip=? AND day=? AND kind=?",
            (_client_ip(request), _utc_day(), kind),
        ).fetchone()[0]
    return int(total) >= _quota(kind)


@router.api_route("/download/latest", methods=["GET", "HEAD"])  # HEAD: the page asks before it navigates
def download_latest(request: Request, store: WorkStore = Depends(get_store)) -> Response:
    return _serve(request, store, store.latest_build())


@router.api_route("/download/latest/update", methods=["GET", "HEAD"])  # HEAD: the page asks before it navigates
def download_latest_update(request: Request, store: WorkStore = Depends(get_store)) -> Response:
    return _serve(request, store, store.latest_build(), KIND_UPDATE)


@router.api_route("/download/{build_id}/update", methods=["GET", "HEAD"])  # HEAD: the page asks before it navigates
def download_build_update(
    build_id: int, request: Request, store: WorkStore = Depends(get_store)
) -> Response:
    return _serve(request, store, store.get_build(build_id), KIND_UPDATE)


@router.api_route("/download/{build_id}", methods=["GET", "HEAD"])  # HEAD: the page asks before it navigates
def download_build(
    build_id: int, request: Request, store: WorkStore = Depends(get_store)
) -> Response:
    return _serve(request, store, store.get_build(build_id))
