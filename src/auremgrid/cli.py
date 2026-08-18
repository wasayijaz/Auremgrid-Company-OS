from __future__ import annotations

import argparse
import json
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter
from auremgrid.services.brain import CompanyOS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auremgrid")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="seed synthetic fixtures and run a sample search")
    demo.add_argument("--db", default=":memory:")

    brief = sub.add_parser("brief", help="print a client brief from the seeded demo")
    brief.add_argument("--db", default=":memory:")
    brief.add_argument("--workspace", default="ws_alpha")
    brief.add_argument("--actor", default="act_alpha_operator")
    brief.add_argument("--query", default="consultation price")

    serve_cmd = sub.add_parser("serve", help="start the local read-mostly HTTP API")
    serve_cmd.add_argument("--db", default="auremgrid.sqlite")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8787)
    serve_cmd.add_argument("--seed", action="store_true")

    sync = sub.add_parser("sync", help="pull connector events into the evidence layer")
    sync.add_argument("--db", default=":memory:")
    sync.add_argument("--actor", default="act_alpha_admin")
    sync.add_argument("--simulated", action="store_true")

    onboard = sub.add_parser("onboard", help="create an isolated workspace for any agency")
    onboard.add_argument("--agency", required=True)
    onboard.add_argument("--workspace", required=True)
    onboard.add_argument("--admin", required=True)
    onboard.add_argument("--operator")
    onboard.add_argument("--source-dir")
    onboard.add_argument("--db", default="auremgrid.sqlite")

    args = parser.parse_args(argv)
    if args.command == "demo":
        os = CompanyOS(args.db)
        os.seed_demo()
        router = McpToolRouter(os)
        result = router.call(
            "search",
            {
                "workspace_id": "ws_alpha",
                "actor_id": "act_alpha_operator",
                "query": "consultation price",
            },
        )
        print(json.dumps(result, indent=2))
        os.close()
        return 0
    if args.command == "brief":
        os = CompanyOS(args.db)
        if args.db == ":memory:":
            os.seed_demo()
        result = os.account_brief(args.workspace, args.actor, query=args.query)
        print(json.dumps(result.to_dict(), indent=2))
        os.close()
        return 0
    if args.command == "serve":
        db_path = Path(args.db)
        os = CompanyOS(db_path)
        if args.seed:
            os.seed_demo()
        server = serve(os, host=args.host, port=args.port)
        print(f"listening on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        finally:
            os.close()
        return 0
    if args.command == "sync":
        os = CompanyOS(args.db)
        if args.db == ":memory:":
            os.seed_demo()
        results = os.sync_connectors(args.actor, include_simulated=args.simulated)
        print(json.dumps([result.to_dict() for result in results], indent=2))
        os.close()
        return 0
    if args.command == "onboard":
        os = CompanyOS(args.db)
        result = os.onboard_agency(
            agency_name=args.agency,
            workspace_id=args.workspace,
            admin_name=args.admin,
            operator_name=args.operator,
            source_dir=args.source_dir,
        )
        print(json.dumps(result, indent=2))
        os.close()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
