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
        help="SQLite database path for the MVP server.",
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

    args = parser.parse_args()
    store = WorkStore(Path(args.db))

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

    if args.cmd == "serve":
        import uvicorn

        from bp_work_server.api import create_app

        os.environ["BP_WORK_DB"] = str(args.db)
        app = create_app(store)
        uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
        return

    parser.error(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()
