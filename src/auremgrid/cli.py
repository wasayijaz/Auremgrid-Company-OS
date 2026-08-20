from __future__ import annotations

import argparse
import json
from os import environ

from auremgrid.adapters.semantic import embedding_provider_from_config
from auremgrid.adapters.graphiti_upstream import graph_projection_from_environment
from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.backup import create_backup, restore_backup, verify_backup
from auremgrid.services.worker import run_one_job


def _add_semantic_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--semantic-model-path",
        help="existing local SentenceTransformers model directory; never downloaded",
    )
    command.add_argument(
        "--semantic-model",
        help="stable model identity stored with rebuilt projections",
    )
    command.add_argument(
        "--semantic-version",
        help="explicit local model/provider version stored with rebuilt projections",
    )


def _embedding_provider(args: argparse.Namespace):
    return embedding_provider_from_config(
        model_path=args.semantic_model_path or environ.get("AUREMGRID_SEMANTIC_MODEL_PATH"),
        model=args.semantic_model or environ.get("AUREMGRID_SEMANTIC_MODEL"),
        version=args.semantic_version or environ.get("AUREMGRID_SEMANTIC_VERSION"),
    )


def _company_os(args: argparse.Namespace) -> CompanyOS:
    provider = _embedding_provider(args)
    graph = graph_projection_from_environment()
    return CompanyOS(args.db, embedding_provider=provider, graph_projection=graph)


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

    backup = sub.add_parser("backup", help="create and verify an online SQLite backup")
    backup.add_argument("--db", required=True)
    backup.add_argument("--output", required=True)

    verify = sub.add_parser("verify-backup", help="verify a backup checksum and SQLite integrity")
    verify.add_argument("--backup", required=True)

    restore = sub.add_parser("restore", help="restore a verified backup while the service is offline")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--db", required=True)
    restore.add_argument("--overwrite", action="store_true")

    bootstrap_auth = sub.add_parser("bootstrap-auth", help="create the first local principal and session token")
    bootstrap_auth.add_argument("--db", required=True)
    bootstrap_auth.add_argument("--organization", required=True)
    bootstrap_auth.add_argument("--person", required=True)
    bootstrap_auth.add_argument("--email", required=True)
    bootstrap_auth.add_argument("--workspace")
    bootstrap_auth.add_argument("--actor")

    worker = sub.add_parser("worker-once", help="claim and execute one durable job, then exit")
    worker.add_argument("--db", required=True)
    worker.add_argument("--organization", required=True)
    worker.add_argument("--workspace")
    worker.add_argument("--worker-id", required=True)

    for command in (demo, brief, serve_cmd, sync, onboard, backup, bootstrap_auth, worker):
        _add_semantic_options(command)

    args = parser.parse_args(argv)
    if hasattr(args, "semantic_model_path"):
        try:
            _embedding_provider(args)
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "demo":
        os = _company_os(args)
        os.seed_demo()
        result = os.search("ws_alpha", "act_alpha_operator", "consultation price").to_dict()
        print(json.dumps(result, indent=2))
        os.close()
        return 0
    if args.command == "brief":
        os = _company_os(args)
        if args.db == ":memory:":
            os.seed_demo()
        result = os.account_brief(args.workspace, args.actor, query=args.query)
        print(json.dumps(result.to_dict(), indent=2))
        os.close()
        return 0
    if args.command == "serve":
        os = _company_os(args)
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
        os = _company_os(args)
        if args.db == ":memory:":
            os.seed_demo()
        results = os.sync_connectors(args.actor, include_simulated=args.simulated)
        print(json.dumps([result.to_dict() for result in results], indent=2))
        os.close()
        return 0
    if args.command == "onboard":
        os = _company_os(args)
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
    if args.command == "backup":
        os = _company_os(args)
        try:
            result = create_backup(os.store.conn, args.output)
        finally:
            os.close()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "verify-backup":
        print(json.dumps(verify_backup(args.backup), indent=2))
        return 0
    if args.command == "restore":
        print(json.dumps(restore_backup(args.backup, args.db, overwrite=args.overwrite), indent=2))
        return 0
    if args.command == "bootstrap-auth":
        if bool(args.workspace) != bool(args.actor):
            parser.error("--workspace and --actor must be provided together")
        os = _company_os(args)
        try:
            principal = os.auth.create_principal(args.organization, args.person, args.email)
            session = os.auth.create_session(principal["id"])
            identity = os.auth.authenticate_session(session["token"])
            identity.require("auth_manage")
            binding = os.auth.bind_actor(identity, args.workspace, args.actor) if args.workspace else None
            print(json.dumps({"principal":principal,"session":{"id":session["id"],"token":session["token"],"expires_at":session["expires_at"]},"actor_binding":binding}, indent=2))
        finally:
            os.close()
        return 0
    if args.command == "worker-once":
        os = _company_os(args)
        try:
            result = run_one_job(os, args.organization, args.workspace, args.worker_id)
            print(json.dumps(result, indent=2))
        finally:
            os.close()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
