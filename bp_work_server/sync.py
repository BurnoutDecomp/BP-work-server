from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from bp_work_server.store import WorkStore


DEFAULT_WORKFLOW_REPO = "https://github.com/Adriwin06/BP-Decomp_Workflow.git"
DEFAULT_WORKFLOW_ROOT = "/var/lib/bp-work-server/BP-Decomp_Workflow"
DEFAULT_WORKFLOW_BRANCH = "main"


def sync_workflow_repo(
    store: WorkStore,
    repo_url: str | None = None,
    workflow_root: str | Path | None = None,
    branch: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    repo_url = repo_url or os.environ.get("BP_WORKFLOW_REPO", DEFAULT_WORKFLOW_REPO)
    workflow_root = Path(
        workflow_root or os.environ.get("BP_WORKFLOW_ROOT", DEFAULT_WORKFLOW_ROOT)
    )
    branch = branch or os.environ.get("BP_WORKFLOW_BRANCH", DEFAULT_WORKFLOW_BRANCH)

    workflow_root.parent.mkdir(parents=True, exist_ok=True)
    if (workflow_root / ".git").exists():
        _run(["git", "-C", str(workflow_root), "fetch", "--prune", "origin", branch])
        _run(["git", "-C", str(workflow_root), "reset", "--hard", f"origin/{branch}"])
    else:
        _run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                repo_url,
                str(workflow_root),
            ]
        )

    commit = _run(["git", "-C", str(workflow_root), "rev-parse", "HEAD"]).strip()
    result = store.import_workflow(workflow_root, reset=reset)
    return {
        **result,
        "repo_url": repo_url,
        "workflow_root": str(workflow_root),
        "branch": branch,
        "commit": commit,
    }


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"{' '.join(args)} failed: {detail}")
    return proc.stdout
