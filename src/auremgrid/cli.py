from __future__ import annotations

import argparse
import json
import re
import sys
from os import environ

from auremgrid.adapters.semantic import embedding_provider_from_config
from auremgrid.adapters.graphiti_upstream import graph_projection_from_environment
from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.backup import check_integrity, create_backup, list_backup_points, restore_backup, rotate_backups, verify_backup
from auremgrid.services.worker import run_one_job
from auremgrid.services.intelligence_evaluation import run_intelligence_evaluations
from auremgrid.demo_agency import seed_realistic_agency_demo


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


def _setup_id(prefix: str, value: str) -> str:
    """Create a readable stable identifier for first-run agency setup."""

    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not slug:
        raise ValueError(f"{prefix} identifier requires at least one letter or number")
    return f"{prefix}_{slug}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auremgrid")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="seed synthetic fixtures and run a sample search")
    demo.add_argument("--db", default=":memory:")

    agency_demo = sub.add_parser("demo-agency", help="seed the realistic synthetic agency scenario")
    agency_demo.add_argument("--db", default=":memory:")
    agency_demo.add_argument("--organization", default="org_realistic_agency_demo")
    agency_demo.add_argument("--owner", default="person_realistic_owner")

    brief = sub.add_parser("brief", help="print a client brief from the seeded demo")
    brief.add_argument("--db", default=":memory:")
    brief.add_argument("--workspace", default="ws_alpha")
    brief.add_argument("--actor", default="act_alpha_operator")
    brief.add_argument("--query", default="consultation price")

    serve_cmd = sub.add_parser("serve", help="start the local read-mostly HTTP API")
    serve_cmd.add_argument("--db", default="auremgrid.sqlite")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8787)
    serve_cmd.add_argument("--storage", choices=["sqlite", "postgres"], default="sqlite")
    serve_cmd.add_argument("--postgres-url", help="PostgreSQL connection URL (required when --storage postgres)")
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
    onboard.add_argument("--db", default="auremgrid.sqlite")

    import_templates = sub.add_parser("import-templates", help="print CSV templates for first-run imports")
    import_templates.add_argument("--db", default="auremgrid.sqlite")

    import_preview = sub.add_parser("import-preview", help="validate CSV import data and record a dry-run preview")
    import_preview.add_argument("--db", default="auremgrid.sqlite")
    import_preview.add_argument("--organization", required=True)
    import_preview.add_argument("--workspace")
    import_preview.add_argument("--person", required=True)
    import_preview.add_argument("--type", required=True, choices=["client_workspaces", "campaigns", "campaign_metrics"])
    import_preview.add_argument("--idempotency-key", required=True)

    import_commit = sub.add_parser("import-commit", help="commit a previously previewed CSV import batch")
    import_commit.add_argument("--db", default="auremgrid.sqlite")
    import_commit.add_argument("--organization", required=True)
    import_commit.add_argument("--batch", required=True)
    import_commit.add_argument("--person", required=True)
    import_commit.add_argument("--idempotency-key", required=True)

    setup_agency = sub.add_parser(
        "setup-agency",
        help="create an agency, owner login, workspace, and first dashboard session in one step",
    )
    setup_agency.add_argument("--agency", required=True, help="agency or company name")
    setup_agency.add_argument("--admin-name", required=True, help="first agency owner name")
    setup_agency.add_argument("--admin-email", required=True, help="first agency owner email")
    setup_agency.add_argument("--db", default="auremgrid.sqlite")
    setup_agency.add_argument("--organization", help="optional organization id")
    setup_agency.add_argument("--workspace", help="optional first workspace id")
    setup_agency.add_argument("--person", help="optional owner person id")
    setup_agency.add_argument("--operator", help="optional operator display name")
    setup_agency.add_argument(
        "--dashboard-url", default="http://127.0.0.1:8787/", help="dashboard address shown in next steps"
    )

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

    backup_rotate = sub.add_parser("backup-rotate", help="delete old backups beyond retention policy")
    backup_rotate.add_argument("--keep-weekly", type=int, default=4)
    backup_rotate.add_argument("--keep-daily", type=int, default=7)
    backup_rotate.add_argument("--dir", required=True, help="backup directory")
    check_int = sub.add_parser("check-integrity", help="run database integrity checks")
    check_int.add_argument("--db", required=True)
    worker = sub.add_parser("worker-once", help="claim and execute one durable job, then exit")
    worker.add_argument("--db", required=True)
    worker.add_argument("--organization", required=True)
    worker.add_argument("--workspace")
    worker.add_argument("--worker-id", required=True)
    worker_loop = sub.add_parser("worker-loop", help="run the durable worker loop until interrupted")
    worker_loop.add_argument("--db", required=True)
    worker_loop.add_argument("--organization", required=True)
    worker_loop.add_argument("--workspace")
    worker_loop.add_argument("--worker-id", required=True)
    worker_loop.add_argument("--poll-seconds", type=float, default=1.0)
    sub.add_parser(
        "evaluate-intelligence",
        help="run the offline Intelligence contract evaluation scenarios",
    )

    for command in (demo, agency_demo, brief, serve_cmd, sync, onboard, import_templates, import_preview, import_commit, setup_agency, backup, bootstrap_auth, worker, worker_loop):
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
        )
        print(json.dumps(result, indent=2))
        os.close()
        return 0
    if args.command == "import-templates":
        os = _company_os(args)
        print(json.dumps(os.onboarding.templates(), indent=2))
        os.close()
        return 0
    if args.command == "import-preview":
        os = _company_os(args)
        result = os.onboarding.preview_csv_import(
            args.organization,
            args.workspace,
            args.person,
            args.type,
            sys.stdin.read(),
            args.idempotency_key,
        )
        print(json.dumps(result, indent=2))
        os.close()
        return 0
    if args.command == "import-commit":
        os = _company_os(args)
        result = os.onboarding.commit_csv_import(
            args.organization,
            args.batch,
            args.person,
            args.idempotency_key,
        )
        print(json.dumps(result, indent=2))
        os.close()
        return 0
    if args.command == "setup-agency":
        organization_id = args.organization or _setup_id("org", args.agency)
        workspace_id = args.workspace or _setup_id("ws", args.agency)
        person_id = args.person or _setup_id("person", args.admin_email.split("@", 1)[0])
        os = _company_os(args)
        try:
            if os.company.get_organization(organization_id) is not None:
                parser.error(
                    f"organization already exists: {organization_id}; use bootstrap-auth to issue a session for an existing person"
                )
            organization = os.create_organization(args.agency, organization_id)
            onboarded = os.onboard_agency(
                agency_name=args.agency,
                workspace_id=workspace_id,
                admin_name=args.admin_name,
                operator_name=args.operator,
            )
            os.create_organization_workspace(organization.id, args.agency, "internal", workspace_id)
            owner = os.create_person(
                organization.id,
                args.admin_name,
                args.admin_email,
                title="Agency Owner",
                role="owner",
                person_id=person_id,
            )
            os.add_person_to_workspace(organization.id, workspace_id, owner.id, "admin")
            principal = os.auth.create_principal(organization.id, owner.id, args.admin_email)
            session = os.auth.create_session(principal["id"])
            identity = os.auth.authenticate_session(session["token"])
            binding = os.auth.bind_actor(identity, workspace_id, onboarded["admin"]["id"])
            print(json.dumps({
                "status": "ready",
                "agency": {"id": organization.id, "name": organization.name},
                "workspace": onboarded["workspace"],
                "owner": {"id": owner.id, "name": owner.name, "email": owner.email, "role": "owner"},
                "actor_binding": binding,
                "session": {
                    "token": session["token"],
                    "expires_at": session["expires_at"],
                    "shown_once": True,
                },
                "dashboard_url": args.dashboard_url,
                "next_steps": [
                    "Start the server with the same --db file.",
                    "Open dashboard_url in a browser.",
                    "Paste session.token into Connect to Auremgrid.",
                    "Use import-templates, import-preview, and import-commit for CSV-first setup data.",
                    "Keep the token private; use Sign out on shared devices.",
                ],
            }, indent=2))
        finally:
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
    if args.command == "check-integrity":
        import sqlite3
        conn = sqlite3.connect(args.db)
        try:
            result = check_integrity(conn)
        finally:
            conn.close()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "worker-loop":
        os = _company_os(args)
        try:
            scheduler = os.scheduler(args.organization, args.workspace, args.worker_id, args.poll_seconds)
            scheduler.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            os.close()
        return 0
    if args.command == "demo-agency":
        os = _company_os(args)
        try:
            print(json.dumps(seed_realistic_agency_demo(os, args.organization, args.owner), indent=2))
        finally:
            os.close()
        return 0
    if args.command == "backup-rotate":
        result = rotate_backups(args.dir, keep_daily=args.keep_daily, keep_weekly=args.keep_weekly)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "evaluate-intelligence":
        result = run_intelligence_evaluations()
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
