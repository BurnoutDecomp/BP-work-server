from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from bp_work_server.store import WorkStore

# Uvicorn's logging config wires up its own loggers and nothing else, so every
# bp_work_server log call -- request failures, admin actions, the warnings that
# say the attribution clone has stopped advancing -- was written to a logger
# with no handler and silently dropped in production. Configure the root logger
# before uvicorn.run() installs its config: it sets disable_existing_loggers to
# false and defines no root logger, so this survives.
DEFAULT_LOG_LEVEL = "INFO"


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("BP_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        format="%(levelname)s: %(name)s: %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="bp-work-server")
    parser.add_argument(
        "--db",
        default=os.environ.get("BP_WORK_DB", "data/bp-work.sqlite3"),
        help="SQLite database path for work/progress data.",
    )
    parser.add_argument(
        "--users-db",
        default=os.environ.get("BP_WORK_USERS_DB"),
        help="SQLite database path for worker/admin user ids. Defaults to <db-stem>-users.sqlite3.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create or migrate the database schema.")

    import_p = sub.add_parser("import", help="Import ledger metadata from BP-Decomp_Workflow.")
    import_p.add_argument("workflow_root", help="Path to BP-Decomp_Workflow.")
    import_p.add_argument("--reset", action="store_true", help="Clear existing server data first.")

    audits_p = sub.add_parser(
        "import-audits",
        help="Import only the audit reports (funcaudit.json / stubs.json / asmaudit.json) from a directory; "
             "for backfilling the history with runs made against older commits.",
    )
    audits_p.add_argument("progress_dir", help="Directory holding the report JSON files.")
    audits_p.add_argument("--imported-at", help="ISO time to stamp the runs with (default: now).")
    audits_p.add_argument("--no-events", action="store_true", help="Log no delta Live Events for these runs.")
    audits_p.add_argument("--replace-latest", action="store_true",
                          help="Also replace the per-function tables (default: a history point only).")

    hb_p = sub.add_parser(
        "history-backfill",
        help="Rebuild the rings' history from a FULL BP-Decomp_Workflow clone: the last ledger "
             "commit of every day, imported into a throwaway store, one snapshot each -> JSON.",
    )
    hb_p.add_argument("workflow_repo", help="Path to a full (not shallow) BP-Decomp_Workflow clone.")
    hb_p.add_argument("--out", required=True, help="Where to write the snapshots JSON.")
    hb_p.add_argument("--since", help="First day to include (YYYY-MM-DD).")
    hi_p = sub.add_parser("history-import", help="Load a history-backfill JSON into this database.")
    hi_p.add_argument("file", help="The snapshots JSON.")

    warm_p = sub.add_parser(
        "warm-attribution-cache",
        help="Precompute local-git surviving-line attribution for reviewed work.",
    )
    warm_p.add_argument("--decomp-root", required=True, help="Path to a local b5-decomp checkout.")
    warm_p.add_argument("--branch", help="Branch/ref to fetch before scanning.")
    warm_p.add_argument("--files-only", action="store_true", help="Only cache completed TU attribution.")
    warm_p.add_argument(
        "--functions-only", action="store_true", help="Only cache reviewed function attribution."
    )
    warm_p.add_argument(
        "--full",
        action="store_true",
        help="Re-blame every target instead of carrying unchanged files forward.",
    )

    serve_p = sub.add_parser("serve", help="Run the API server.")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument("--reload", action="store_true")

    worker_p = sub.add_parser(
        "worker", help="Manage worker ids directly on the DB (bootstrap admins, no HTTP)."
    )
    worker_sub = worker_p.add_subparsers(dest="worker_cmd", required=True)
    w_add = worker_sub.add_parser("add", help="Mint a worker id for a username.")
    w_add.add_argument("username")
    w_add.add_argument("--admin", action="store_true", help="Grant the admin role.")
    w_add.add_argument(
        "--service",
        action="store_true",
        help="Mark as a bot identity (e.g. CI); hidden from the dashboard agent roster.",
    )
    w_add.add_argument(
        "--github-username",
        help="GitHub username override when it differs from the worker username.",
    )
    worker_sub.add_parser("list", help="List worker ids.")
    w_svc = worker_sub.add_parser(
        "service", help="Mark an existing user as a service (bot) identity, or clear the mark."
    )
    w_svc.add_argument("username")
    w_svc.add_argument(
        "--off", action="store_true", help="Clear the flag and show the user again."
    )
    w_gh = worker_sub.add_parser(
        "github", help="Set or clear a GitHub username override for an existing user."
    )
    w_gh.add_argument("username")
    w_gh.add_argument(
        "github_username",
        nargs="?",
        help="GitHub username override. Omit it, or pass the same value as username, to clear.",
    )
    w_rev = worker_sub.add_parser("revoke", help="Revoke a worker id.")
    w_rev.add_argument("token")

    args = parser.parse_args()
    store = WorkStore(Path(args.db), Path(args.users_db) if args.users_db else None)

    if args.cmd == "init-db":
        store.migrate()
        print(f"database ready: {args.db}")
        return

    if args.cmd == "import":
        result = store.import_workflow(args.workflow_root, reset=args.reset)
        print(
            "imported "
            f"{result['tus']} TUs, {result['funcs']} funcs, {result['deps']} deps, "
            f"{result['goals']} goals ({result['status_rows']} status rows), "
            f"{result['linked']} TUs linked into the exe"
        )
        return

    if args.cmd == "import-audits":
        counts = store.import_audits_only(
            args.progress_dir, imported_at=args.imported_at, events=not args.no_events,
            history_only=not args.replace_latest,
        )
        print(
            f"imported audits from {args.progress_dir}: "
            f"{counts.get('funcaudit', 0)} findings, {counts.get('stubs', 0)} stubs, "
            f"{counts.get('asm', 0)} shape rows (0 = already imported or absent)"
        )
        return

    if args.cmd == "history-backfill":
        from bp_work_server import history

        def report(day: str, sha: str, values: dict) -> None:
            print(f"{day} {sha[:10]}: {values['tu_done']}/{values['tu_total']} TUs done, "
                  f"{values['funcs_done']}/{values['funcs_total']} funcs covered, "
                  f"linked {values['tu_linked']}", flush=True)

        snaps = history.backfill(args.workflow_repo, since=args.since, out_path=args.out, progress=report)
        print(f"{len(snaps)} snapshots -> {args.out}")
        return

    if args.cmd == "history-import":
        n = store.import_history_file(args.file)
        print(f"imported {n} snapshots from {args.file}")
        return

    if args.cmd == "warm-attribution-cache":
        if args.files_only and args.functions_only:
            parser.error("--files-only and --functions-only cannot be used together")
        from bp_work_server.attribution_cache import warm_attribution_cache
        from bp_work_server.decomp import DecompRepo

        def progress(kind: str, current: int, total: int, label: str) -> None:
            print(f"  {kind}: {current}/{total} {label}", flush=True)

        decomp = DecompRepo(root=args.decomp_root, branch=args.branch)
        result = warm_attribution_cache(
            store,
            decomp,
            include_files=not args.functions_only,
            include_functions=not args.files_only,
            progress=progress,
            full=args.full,
        )
        print("attribution cache warmed")
        print(f"  repo rev: {result.repo_rev}")
        print(f"  files cached: {result.files_cached}/{result.file_targets}")
        print(f"  functions cached: {result.functions_cached}/{result.function_targets}")
        if result.base_rev:
            print(
                f"  carried forward from {result.base_rev[:12]}: "
                f"{result.files_reused} files, {result.functions_reused} functions"
            )
        else:
            print("  carried forward: nothing (full pass)")
        return

    if args.cmd == "worker":
        store.migrate()
        if args.worker_cmd == "add":
            result = store.create_worker(
                args.username,
                is_admin=args.admin,
                github_username=args.github_username,
                is_service=args.service,
            )
            role = "admin" if result["is_admin"] else "user"
            if result["is_service"]:
                role += " service"
            print(f"created {role} worker for {result['username']!r}")
            if result["github_username"]:
                print(f"  github={result['github_username']}")
            print(f"  WORK_AGENT={result['token']}")
            print("\nGive this id to the user privately; they set it as WORK_AGENT.")
            return
        if args.worker_cmd == "list":
            workers = store.list_workers()
            if not workers:
                print("no workers registered")
                return
            for w in workers:
                state = "active " if w["active"] else "revoked"
                role = "admin" if w["is_admin"] else "user "
                kind = " service" if w.get("is_service") else ""
                github = f" github={w['github_username']}" if w.get("github_username") else ""
                print(
                    f"  [{state}|{role}] {w['username']:24s} {w['token']}  "
                    f"last_seen={w['last_seen']}{kind}{github}"
                )
            return
        if args.worker_cmd == "service":
            rows = store.set_worker_service(args.username, not args.off)
            if rows:
                what = "cleared on" if args.off else "set on"
                print(f"service flag {what} {rows} worker id(s) for {args.username!r}")
            else:
                print(f"unknown user: {args.username!r}")
            return
        if args.worker_cmd == "github":
            rows = store.set_worker_github_username(args.username, args.github_username)
            if rows:
                value = args.github_username or ""
                print(f"updated {rows} worker id(s) for {args.username!r}; github={value!r}")
            else:
                print(f"unknown user: {args.username!r}")
            return
        if args.worker_cmd == "revoke":
            print("revoked" if store.revoke_worker(args.token) else "unknown token")
            return

    if args.cmd == "serve":
        import uvicorn

        from bp_work_server.api import create_app

        os.environ["BP_WORK_DB"] = str(args.db)
        if args.users_db:
            os.environ["BP_WORK_USERS_DB"] = str(args.users_db)
        configure_logging()
        app = create_app(store)
        # The dashboard keeps an SSE stream (/events/stream) open per viewer;
        # uvicorn's default graceful shutdown waits for every connection to
        # close, so with one browser open a restart hung until systemd's
        # 90 s stop timeout SIGKILLed the process and the site showed the
        # "Deploying" page for the whole wait. Close stragglers after 5 s
        # instead; EventSource reconnects on its own.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            reload=args.reload,
            timeout_graceful_shutdown=5,
        )
        return

    parser.error(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()
