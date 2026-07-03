from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

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


def _build_info(row: dict) -> BuildInfo:
    return BuildInfo(download_url=f"/download/{row['id']}", **row)


def _friendly_name(row: dict) -> str:
    tag = _safe_slug(row.get("commit_short") or row.get("commit_sha"), "build")
    return f"burnout-paradise-{tag}.zip"


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
        tmp.unlink(missing_ok=True)
        source = assembled
    else:
        # No asset merge configured: publish the uploaded bundle as-is.
        sha256, size = bundle_hasher.hexdigest(), bundle_size
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

    # Drop older builds from disk (keep the newest BP_KEEP_BUILDS). A file is only
    # unlinked when no surviving row still points at it (content-addressed sharing).
    pruned = store.prune_builds(_keep_builds())
    if pruned:
        live = {r["filename"] for r in store.list_builds(limit=_keep_builds() + len(pruned))}
        for stale in pruned:
            name = Path(stale["filename"]).name
            if name not in live:
                (dest / name).unlink(missing_ok=True)

    invalidate_dashboard_cache(request)
    log.info(
        "published build id=%s commit=%s size=%s assets=%s",
        row["id"], row["commit_short"], size, asset_manifest_hash,
    )
    return _build_info(row)


@router.get("/api/builds", response_model=BuildListResponse)
def list_builds(store: WorkStore = Depends(get_store)) -> BuildListResponse:
    builds = [_build_info(r) for r in store.list_builds(limit=20)]
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


def _serve(request: Request, store: WorkStore, row: dict | None) -> FileResponse:
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no build available")
    path = downloads_dir() / Path(row["filename"]).name
    if not path.is_file():
        # DB row survived but the file was pruned/lost; treat as gone rather than 500.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "build artifact is no longer on disk")
    if _is_fresh_download(request):
        store.increment_build_downloads(row["id"])
    return FileResponse(
        path,
        media_type="application/zip",
        filename=_friendly_name(row),
    )


@router.get("/download/latest")
def download_latest(request: Request, store: WorkStore = Depends(get_store)) -> FileResponse:
    return _serve(request, store, store.latest_build())


@router.get("/download/{build_id}")
def download_build(
    build_id: int, request: Request, store: WorkStore = Depends(get_store)
) -> FileResponse:
    return _serve(request, store, store.get_build(build_id))
