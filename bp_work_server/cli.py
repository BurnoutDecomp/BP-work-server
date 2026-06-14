from __future__ import annotations

import argparse
import os
from pathlib import Path

from bp_work_server.store import WorkStore


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
    worker_sub.add_parser("list", help="List worker ids.")
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
            f"{result['goals']} goals ({result['status_rows']} status rows)"
        )
        return

    if args.cmd == "worker":
        store.migrate()
        if args.worker_cmd == "add":
            result = store.create_worker(args.username, is_admin=args.admin)
            role = "admin" if result["is_admin"] else "user"
            print(f"created {role} worker for {result['username']!r}")
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
                print(f"  [{state}|{role}] {w['username']:24s} {w['token']}  "
                      f"last_seen={w['last_seen']}")
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
        app = create_app(store)
        uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
        return

    parser.error(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()
