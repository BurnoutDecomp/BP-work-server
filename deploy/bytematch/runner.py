#!/usr/bin/env python3
"""Byte-match runner: measures the native commits the work server queued, in a sandbox.

systemd starts it as root when the queue is not empty (bp-bytematch.path, with a timer as a
backstop); the web app only queues requests and serves the results (bytematch_jobs.py), it never
runs Docker. For each queued commit the runner:

  1. updates its own clones: BP-Decomp_Workflow at origin/main (the measuring code, the
     committed records, the ARTIST name pack) and b5-decomp at the commit, which must be the
     tip of x360 (an older commit is "superseded": CI would not publish it);
  2. checks the commit out with symlinks off and the three vendor submodules from fixed URLs;
  3. builds the measurer image from the workflow's tools/bytematch/docker when it changed;
  4. runs tools/bytematch/wine.py refresh in a container: no network, no capabilities, no new
     privileges, limited CPU, memory and processes, an unprivileged uid; the clones and the
     XDK/ARTIST inputs read-only, only the cache and the run's output writable;
  5. hands status.json and bundle.json.gz to the web app.

Private state, root only (deploy/bytematch/README.md sets it up):
  /var/lib/bp-bytematch/inputs/{xdk,image}   the XDK compiler + headers, ARTIST's bytes
  /var/lib/bp-bytematch/{workflow,b5-decomp} the runner's clones
  /var/lib/bp-bytematch/{cache,runs}         the measurer's cache, each run's output
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

QUEUE_ROOT = Path(os.environ.get("BP_BYTEMATCH_DIR", "/var/lib/bp-work-server/bytematch"))
PRIVATE = Path(os.environ.get("BP_BYTEMATCH_PRIVATE", "/var/lib/bp-bytematch"))
B5_REPO = "https://github.com/BurnoutDecomp/b5-decomp.git"
WORKFLOW_REPO = "https://github.com/BurnoutDecomp/BP-Decomp_Workflow.git"
BRANCH = "x360"
SUBMODULES = {
    "vendor/EABase": "https://github.com/BurnoutDecomp/EABase.git",
    "vendor/EASTL": "https://github.com/BurnoutDecomp/EASTL.git",
    "vendor/EAThread": "https://github.com/BPDecomp/EAThread",
}
SPARSE = ["/tools/bytematch/", "/progress/bytematch/", "/progress/asmaudit_artist.json.gz",
          "/progress/unidentified.json", "/progress/source_aliases.json"]
UID = int(os.environ.get("BP_BYTEMATCH_UID", "1500"))       # the image's unprivileged user
CPUS = os.environ.get("BP_BYTEMATCH_CPUS", "3")
MEMORY = os.environ.get("BP_BYTEMATCH_MEMORY", "6g")
JOBS = os.environ.get("BP_BYTEMATCH_JOBS", "3")
TIMEOUT = int(os.environ.get("BP_BYTEMATCH_TIMEOUT", str(5 * 3600)))
KEEP_JOBS_DAYS, KEEP_RUNS_DAYS = 14, 2
SHA = re.compile(r"[0-9a-f]{40}")
# Only https leaves this machine, no hook or symlink is honoured, no submodule recurses.
GIT = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.symlinks=false",
       "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false", "-c", "advice.detachedHead=false"]


def log(message):
    print(message, flush=True)


def git(repo, *args, check=True, timeout=1800):
    result = subprocess.run(GIT + ["-C", str(repo), *args], capture_output=True, text=True, errors="replace",
                            timeout=timeout, stdin=subprocess.DEVNULL)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {result.stderr.strip()[-800:]}")
    return result.stdout


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def as_owner_of(folder, action):
    """Run action() in a child process with the uid and gid that own folder (the web app's).
    Root never writes where the web app could have planted a symlink."""
    owner = os.lstat(folder)
    if not stat.S_ISDIR(owner.st_mode) or owner.st_uid == 0:
        raise RuntimeError(f"{folder} must be a real folder owned by the web app's user")
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([])
            os.setgid(owner.st_gid)
            os.setuid(owner.st_uid)
            action()
            os._exit(0)
        except BaseException:   # noqa: BLE001
            os._exit(1)
    if os.waitpid(pid, 0)[1]:
        raise RuntimeError(f"could not write under {folder}")


def read_output(path, limit=256 << 20):
    """A file the container wrote: regular, not a symlink, bounded."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RuntimeError(f"{path.name} is not a plain file of at most {limit} bytes")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(limit + 1)
    finally:
        os.close(fd)


class Job:
    """A request's status and result, written into the web app's folder as the web app."""

    def __init__(self, sha):
        self.sha = sha
        self.folder = QUEUE_ROOT / "jobs" / sha
        self.status = {"sha": sha}
        self.beat = 0.0

    def _write(self, files):
        folder = self.folder

        def action():
            folder.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                tmp = folder / (name + ".tmp")
                tmp.unlink(missing_ok=True)
                with open(tmp, "xb") as fh:
                    fh.write(data)
                os.replace(tmp, folder / name)

        as_owner_of(QUEUE_ROOT, action)

    def update(self, **fields):
        self.status.update(fields, sha=self.sha, updated_at=now())
        self._write({"status.json": (json.dumps(self.status, indent=1, sort_keys=True) + "\n").encode()})
        self.beat = time.time()

    def deliver(self, bundle: bytes, **fields):
        self.status.update(fields, sha=self.sha, updated_at=now())
        self._write({"bundle.json.gz": bundle,
                     "status.json": (json.dumps(self.status, indent=1, sort_keys=True) + "\n").encode()})


# ------------------------------------------------------------------------------ clones
def workflow_clone():
    repo = PRIVATE / "workflow"
    if not (repo / ".git").is_dir():
        subprocess.run(GIT + ["clone", "--quiet", "--filter=blob:none", "--no-checkout", "--single-branch",
                              "--branch", "main", WORKFLOW_REPO, str(repo)], check=True, timeout=1800)
        git(repo, "sparse-checkout", "set", "--no-cone", *SPARSE)
    git(repo, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
    git(repo, "checkout", "--quiet", "--force", "--detach", "refs/remotes/origin/main")
    git(repo, "clean", "-ffdxq")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def native_clone(sha):
    """b5-decomp checked out at sha with its three vendor submodules; None when superseded."""
    repo = PRIVATE / "b5-decomp"
    if not (repo / ".git").is_dir():
        subprocess.run(GIT + ["clone", "--quiet", "--filter=blob:none", "--no-checkout", "--single-branch",
                              "--branch", BRANCH, B5_REPO, str(repo)], check=True, timeout=3600)
    git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    tip = git(repo, "rev-parse", f"refs/remotes/origin/{BRANCH}").strip()
    if sha != tip:
        git(repo, "fetch", "--quiet", "origin", sha, check=False)
        older = subprocess.run(GIT + ["-C", str(repo), "merge-base", "--is-ancestor", sha, tip]).returncode == 0
        if older:
            return None, tip
        raise RuntimeError(f"{sha} is not on {BRANCH} (tip {tip})")
    git(repo, "checkout", "--quiet", "--force", "--detach", sha)
    git(repo, "clean", "-ffdxq")
    for path, url in SUBMODULES.items():
        git(repo, "config", f"submodule.{path}.url", url)       # never the URL the tree names
        git(repo, "config", f"submodule.{path}.update", "checkout")
        git(repo, "config", f"submodule.{path}.active", "true")
    git(repo, "submodule", "update", "--init", "--force", "--checkout", "--", *SUBMODULES, timeout=3600)
    links = {line.split("\t", 1)[1]: line.split()[2]
             for line in git(repo, "ls-tree", sha, "--", *SUBMODULES).splitlines()}
    for path in SUBMODULES:
        head = git(repo / path, "rev-parse", "HEAD").strip()
        if links.get(path) != head:
            raise RuntimeError(f"{path} is at {head}, the tree records {links.get(path)}")
        git(repo / path, "clean", "-ffdxq")
    return repo, tip


def image(workflow):
    """The measurer image for this workflow commit's Dockerfile, built when it changed."""
    folder = workflow / "tools" / "bytematch" / "docker"
    digest = hashlib.sha256()
    for path in sorted(folder.iterdir()):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    tag = "bp-bytematch:" + digest.hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode:
        log(f"building {tag}")
        subprocess.run(["docker", "build", "--quiet", "--pull", "-t", tag, str(folder)], check=True, timeout=3600,
                       stdin=subprocess.DEVNULL)
    return tag


def owned(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, UID, UID)
    return path


# ------------------------------------------------------------------------------ a job
def measure(job):
    sha = job.sha
    job.update(state="running", started_at=now(), progress="updating the clones", error=None)
    workflow, workflow_commit = workflow_clone()
    tree, tip = native_clone(sha)
    if tree is None:
        job.update(state="superseded", finished_at=now(), progress=f"the tip of {BRANCH} is now {tip}")
        return
    tag = image(workflow)
    run = PRIVATE / "runs" / sha
    if run.exists():
        shutil.rmtree(run)
    out = owned(run / "out")
    (out / "ls-tree.txt").write_text(git(tree, "ls-tree", "-r", "--full-tree", sha), encoding="utf-8")
    os.chown(out / "ls-tree.txt", UID, UID)
    # Sources include files the measurer makes from ARTIST (wine.py generated_includes) under
    # build/x360/generated/: a writable folder is mounted there over the read-only tree.
    generated = owned(run / "generated")
    (tree / "build" / "x360" / "generated").mkdir(parents=True, exist_ok=True)
    cache = owned(PRIVATE / "cache")
    inputs = PRIVATE / "inputs"
    name = f"bytematch-{sha[:12]}"
    command = ["docker", "run", "--rm", "--init", "--name", name, "--network", "none", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--pids-limit", "1024", "--memory", MEMORY,
               "--memory-swap", MEMORY, "--cpus", CPUS, "--cpu-shares", "256", "--user", f"{UID}:{UID}",
               "-v", f"{workflow}:/work/repo:ro", "-v", f"{tree}:/work/tree:ro",
               "-v", f"{generated}:/work/tree/build/x360/generated",
               "-v", f"{inputs / 'xdk'}:/work/xdk:ro", "-v", f"{inputs / 'image'}:/work/image:ro",
               "-v", f"{cache}:/work/cache", "-v", f"{out}:/work/out",
               tag, "python3", "/work/repo/tools/bytematch/wine.py", "refresh", "--head", sha, "--jobs", JOBS]
    job.update(progress="compiling", workflow_commit=workflow_commit, image=tag)
    started = time.time()
    with open(run / "run.log", "wb") as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        while process.poll() is None:
            time.sleep(15)
            if time.time() - started > TIMEOUT:
                subprocess.run(["docker", "kill", name], capture_output=True)
                process.wait()
                raise RuntimeError(f"the measurement took longer than {TIMEOUT} s")
            lines = (run / "run.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
            line = lines[-1][:200] if lines else "starting the compiler"
            if line != job.status.get("progress") or time.time() - job.beat > 300:   # a heartbeat
                job.update(progress=line)
    text = (run / "run.log").read_text(encoding="utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(f"the measurer exited with {process.returncode}:\n" + "\n".join(text.splitlines()[-25:]))
    result = json.loads(read_output(out / "result.json", 64 << 20))
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}
    job.deliver(read_output(out / "bundle.json.gz"), state="done", finished_at=now(),
                progress=text.strip().splitlines()[-1][:200], result=summary)
    log(f"{sha[:12]}: {job.status['progress']}")


def prune():
    """Drop old runs (root's) and old job folders (the web app's, removed as the web app)."""
    def expired(folder, days):
        return [p for p in folder.iterdir() if not p.is_symlink() and p.is_dir()
                and time.time() - p.stat().st_mtime > days * 86400] if folder.is_dir() else []

    for run in expired(PRIVATE / "runs", KEEP_RUNS_DAYS):
        shutil.rmtree(run, ignore_errors=True)
    old = expired(QUEUE_ROOT / "jobs", KEEP_JOBS_DAYS)
    if old:
        as_owner_of(QUEUE_ROOT, lambda: [shutil.rmtree(p, ignore_errors=True) for p in old])


def main():
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(PRIVATE / "runner.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another runner is working; it drains the queue")
            return 0
        queue = QUEUE_ROOT / "queue"
        while True:
            requests = sorted(queue.glob("*.json"), key=lambda p: p.stat().st_mtime) if queue.is_dir() else []
            if not requests:
                break
            request = requests[0]
            sha = request.stem
            request.unlink(missing_ok=True)
            if not SHA.fullmatch(sha):
                log(f"ignoring a request named {sha!r}")
                continue
            job = Job(sha)
            try:
                measure(job)
            except Exception as exc:   # noqa: BLE001 -- every failure is reported to CI
                job.update(state="failed", finished_at=now(), error=str(exc)[-4000:])
                log(f"{sha[:12]} failed: {exc}")
        prune()
    return 0


if __name__ == "__main__":
    sys.exit(main())
