from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


UNKNOWN_FALSE_ALERT_REASON = (
    "No explicit false-positive marker column exists on proactive attention lifecycle data; "
    "dismissals are reported separately."
)
UNKNOWN_EVIDENCE_CORRECTNESS_REASON = (
    "No stored evidence correctness verdict or quality label is present in the reporting schema."
)
UNKNOWN_ATTENTION_USEFULNESS_REASON = (
    "No stored attention usefulness score or feedback label is present in the reporting schema."
)
UNKNOWN_RECOMMENDATION_VALUE_REASON = (
    "Recommendation decisions and outcome attribution are countable, but no single stored value "
    "score is present across all recommendations."
)
NO_OPERATOR_VERDICTS_REASON = "No operator verdicts have been recorded for this pilot scope."


def unknown(reason: str) -> dict[str, str]:
    return {"status": "unknown", "reason": reason}


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        resolved = db_path.resolve()
        conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def count_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    workspace_id: str | None = None,
    where: str | None = None,
    params: tuple[Any, ...] = (),
) -> int:
    if not table_exists(conn, table):
        return 0
    clauses: list[str] = []
    query_params: list[Any] = []
    columns = table_columns(conn, table)
    if workspace_id is not None and "workspace_id" in columns:
        clauses.append("workspace_id = ?")
        query_params.append(workspace_id)
    if where:
        clauses.append(f"({where})")
        query_params.extend(params)
    sql = f"SELECT COUNT(*) AS count FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    row = conn.execute(sql, tuple(query_params)).fetchone()
    return int(row["count"])


def group_counts(
    conn: sqlite3.Connection,
    table: str,
    group_field: str,
    *,
    workspace_id: str | None = None,
    where: str | None = None,
    params: tuple[Any, ...] = (),
) -> dict[str, int]:
    if not table_exists(conn, table) or group_field not in table_columns(conn, table):
        return {}
    clauses: list[str] = []
    query_params: list[Any] = []
    columns = table_columns(conn, table)
    if workspace_id is not None and "workspace_id" in columns:
        clauses.append("workspace_id = ?")
        query_params.append(workspace_id)
    if where:
        clauses.append(f"({where})")
        query_params.extend(params)
    sql = f"SELECT {group_field} AS key, COUNT(*) AS count FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" GROUP BY {group_field} ORDER BY {group_field}"
    return {str(row["key"]): int(row["count"]) for row in conn.execute(sql, tuple(query_params))}


def operator_verdict_report(conn: sqlite3.Connection, workspace_id: str | None) -> dict[str, Any]:
    table = "pilot_operator_verdicts"
    if not table_exists(conn, table):
        return {
            "total": 0,
            "status": "none_recorded",
            "message": NO_OPERATOR_VERDICTS_REASON,
            "scenario_counts": {},
            "verdict_counts": {},
            "latest_by_scenario": {},
        }
    clauses: list[str] = []
    params: list[Any] = []
    if workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(workspace_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = list(
        conn.execute(
            "SELECT scenario_id, verdict, notes, recorded_by_person_id, created_at "
            f"FROM {table}{where} ORDER BY created_at, id",
            tuple(params),
        )
    )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[str(row["scenario_id"])] = {
            "verdict": row["verdict"],
            "notes": row["notes"],
            "recorded_by_person_id": row["recorded_by_person_id"],
            "created_at": row["created_at"],
        }
    return {
        "total": len(rows),
        "status": "recorded" if rows else "none_recorded",
        "message": None if rows else NO_OPERATOR_VERDICTS_REASON,
        "scenario_counts": group_counts(conn, table, "scenario_id", workspace_id=workspace_id),
        "verdict_counts": group_counts(conn, table, "verdict", workspace_id=workspace_id),
        "latest_by_scenario": latest,
    }


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                pass
    return None


def median_hours(rows: list[sqlite3.Row], start_field: str, end_field: str) -> float | None:
    durations: list[float] = []
    for row in rows:
        started = parse_time(row[start_field])
        ended = parse_time(row[end_field])
        if started is None or ended is None:
            continue
        durations.append((ended - started).total_seconds() / 3600)
    if not durations:
        return None
    return round(float(statistics.median(durations)), 2)


def attention_report(conn: sqlite3.Connection, workspace_id: str | None) -> dict[str, Any]:
    table = "proactive_intelligence_attention_lifecycle"
    status_counts = group_counts(conn, table, "status", workspace_id=workspace_id)
    actionable = ("acknowledged", "acted_on", "resolved", "dismissed")
    rows: list[sqlite3.Row] = []
    if table_exists(conn, table):
        params: list[Any] = []
        clauses = ["status IN ('acknowledged','acted_on','resolved','dismissed')"]
        if workspace_id is not None:
            clauses.insert(0, "workspace_id = ?")
            params.append(workspace_id)
        rows = list(
            conn.execute(
                "SELECT created_at, updated_at FROM proactive_intelligence_attention_lifecycle "
                f"WHERE {' AND '.join(clauses)}",
                tuple(params),
            )
        )
    return {
        "items_created": count_rows(conn, table, workspace_id=workspace_id),
        "status_counts": status_counts,
        "acknowledged": status_counts.get("acknowledged", 0),
        "closed": status_counts.get("resolved", 0) + status_counts.get("dismissed", 0),
        "dismissed": status_counts.get("dismissed", 0),
        "median_time_to_action_hours": median_hours(rows, "created_at", "updated_at"),
        "attention_usefulness": unknown(UNKNOWN_ATTENTION_USEFULNESS_REASON),
        "false_alert_rate": unknown(UNKNOWN_FALSE_ALERT_REASON),
    }


def recommendation_report(conn: sqlite3.Connection, workspace_id: str | None) -> dict[str, Any]:
    lifecycle_counts = group_counts(
        conn,
        "intelligence_recommendation_lifecycle",
        "event_type",
        workspace_id=workspace_id,
    )
    handoff_counts = group_counts(
        conn,
        "intelligence_recommendation_handoffs",
        "review_status",
        workspace_id=workspace_id,
    )
    plan_status_counts = group_counts(
        conn,
        "outcome_attribution_plans",
        "status",
        workspace_id=workspace_id,
    )
    snapshot_counts = group_counts(
        conn,
        "outcome_attribution_snapshots",
        "snapshot_kind",
        workspace_id=workspace_id,
    )
    return {
        "recommendations_created": count_rows(
            conn,
            "intelligence_recommendations",
            workspace_id=workspace_id,
        ),
        "lifecycle_event_counts": lifecycle_counts,
        "accepted": lifecycle_counts.get("accepted", 0),
        "rejected": lifecycle_counts.get("rejected", 0),
        "chosen": lifecycle_counts.get("chosen", 0),
        "evaluated": lifecycle_counts.get("evaluated", 0),
        "handoff_review_status_counts": handoff_counts,
        "outcome_attribution_plans": {
            "total": count_rows(conn, "outcome_attribution_plans", workspace_id=workspace_id),
            "status_counts": plan_status_counts,
            "evaluated": plan_status_counts.get("evaluated", 0),
        },
        "outcome_attribution_snapshots": {
            "total": count_rows(conn, "outcome_attribution_snapshots", workspace_id=workspace_id),
            "kind_counts": snapshot_counts,
            "baseline": snapshot_counts.get("baseline", 0),
            "outcome": snapshot_counts.get("outcome", 0),
        },
        "recommendation_value": unknown(UNKNOWN_RECOMMENDATION_VALUE_REASON),
    }


def connector_report(conn: sqlite3.Connection, workspace_id: str | None) -> dict[str, Any]:
    providers = set(group_counts(conn, "provider_import_cursors", "provider", workspace_id=workspace_id))
    providers.update(group_counts(conn, "provider_import_records", "provider", workspace_id=workspace_id))
    providers.update(group_counts(conn, "provider_import_quarantines", "provider"))

    sync_connectors = group_counts(conn, "provider_sync_tasks", "connector", workspace_id=workspace_id)
    providers.update(sync_connectors)

    per_provider: dict[str, dict[str, Any]] = {}
    for provider in sorted(providers):
        cursor_status = group_counts(
            conn,
            "provider_import_cursors",
            "status",
            workspace_id=workspace_id,
            where="provider = ?",
            params=(provider,),
        )
        sync_status = group_counts(
            conn,
            "provider_sync_tasks",
            "status",
            workspace_id=workspace_id,
            where="connector = ?",
            params=(provider,),
        )
        reconcile_total = count_rows(
            conn,
            "provider_sync_tasks",
            workspace_id=workspace_id,
            where="connector = ? AND task_type = 'reconcile'",
            params=(provider,),
        )
        provider_columns = table_columns(conn, "provider_import_quarantines")
        quarantine_workspace_note = None
        if workspace_id is not None and provider_columns and "workspace_id" not in provider_columns:
            quarantine_workspace_note = "provider_import_quarantines has no workspace_id column"
        per_provider[provider] = {
            "cursor_status_counts": cursor_status,
            "synced_cursors": cursor_status.get("configured", 0),
            "degraded_cursors": cursor_status.get("degraded", 0),
            "imported_records": count_rows(
                conn,
                "provider_import_records",
                workspace_id=workspace_id,
                where="provider = ?",
                params=(provider,),
            ),
            "quarantines": count_rows(
                conn,
                "provider_import_quarantines",
                workspace_id=workspace_id,
                where="provider = ?",
                params=(provider,),
            ),
            "quarantine_scope_note": quarantine_workspace_note,
            "sync_task_status_counts": sync_status,
            "reconcile_tasks": reconcile_total,
        }

    return {
        "provider_count": len(per_provider),
        "providers": per_provider,
        "provider_sync_generations": {
            "status_counts": group_counts(
                conn,
                "provider_sync_generations",
                "status",
                workspace_id=workspace_id,
            ),
            "total": count_rows(conn, "provider_sync_generations", workspace_id=workspace_id),
        },
    }


def review_report(conn: sqlite3.Connection, workspace_id: str | None) -> dict[str, Any]:
    rows: list[sqlite3.Row] = []
    if table_exists(conn, "reviews"):
        params: list[Any] = []
        clauses = ["closed_at IS NOT NULL"]
        if workspace_id is not None:
            clauses.insert(0, "workspace_id = ?")
            params.append(workspace_id)
        rows = list(
            conn.execute(
                "SELECT opened_at, closed_at FROM reviews WHERE " + " AND ".join(clauses),
                tuple(params),
            )
        )
    return {
        "opened": count_rows(conn, "reviews", workspace_id=workspace_id),
        "closed": count_rows(
            conn,
            "reviews",
            workspace_id=workspace_id,
            where="closed_at IS NOT NULL",
        ),
        "open": count_rows(
            conn,
            "reviews",
            workspace_id=workspace_id,
            where="closed_at IS NULL",
        ),
        "status_counts": group_counts(conn, "reviews", "status", workspace_id=workspace_id),
        "median_turnaround_hours": median_hours(rows, "opened_at", "closed_at"),
    }


def evidence_report(conn: sqlite3.Connection, workspace_id: str | None) -> dict[str, Any]:
    evidence_tables = {
        "proactive_attention_items": "proactive_intelligence_attention_items",
        "proactive_snapshots": "proactive_intelligence_snapshots",
        "intelligence_recommendations": "intelligence_recommendations",
        "intelligence_recommendation_lifecycle": "intelligence_recommendation_lifecycle",
        "memory_proposals": "memory_proposals",
        "understanding_proposals": "understanding_proposals",
        "review_annotations": "review_annotations",
        "workflow_evidence": "workflow_evidence",
        "decisions": "decisions",
        "risks": "risks",
        "opportunities": "opportunities",
    }
    counts = {
        label: count_rows(conn, table, workspace_id=workspace_id)
        for label, table in evidence_tables.items()
        if table_exists(conn, table)
    }
    proposal_tables = {
        "memory_proposals": "memory_proposals",
        "understanding_proposals": "understanding_proposals",
    }
    proposal_counts = {
        label: count_rows(conn, table, workspace_id=workspace_id)
        for label, table in proposal_tables.items()
        if table_exists(conn, table)
    }
    return {
        "evidence_bearing_counts": counts,
        "proposal_counts": proposal_counts,
        "total_evidence_bearing_rows": sum(counts.values()),
        "total_proposals": sum(proposal_counts.values()),
        "evidence_correctness": unknown(UNKNOWN_EVIDENCE_CORRECTNESS_REASON),
    }


def build_report(conn: sqlite3.Connection, workspace_id: str | None = None) -> dict[str, Any]:
    operator_verdicts = operator_verdict_report(conn, workspace_id)
    attention = attention_report(conn, workspace_id)
    recommendations = recommendation_report(conn, workspace_id)
    evidence = evidence_report(conn, workspace_id)
    latest = operator_verdicts["latest_by_scenario"]
    if "attention.usefulness" in latest:
        attention["attention_usefulness"] = latest["attention.usefulness"]
    if "attention.false_alert_rate" in latest:
        attention["false_alert_rate"] = latest["attention.false_alert_rate"]
    if "evidence.correctness" in latest:
        evidence["evidence_correctness"] = latest["evidence.correctness"]
    if "recommendation.value" in latest:
        recommendations["recommendation_value"] = latest["recommendation.value"]
    return {
        "scope": {"workspace_id": workspace_id},
        "metrics": {
            "attention": attention,
            "recommendations": recommendations,
            "connectors": connector_report(conn, workspace_id),
            "reviews": review_report(conn, workspace_id),
            "evidence": evidence,
            "operator_verdicts": operator_verdicts,
        },
    }


def format_text(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "Pilot metrics summary",
        f"Workspace: {report['scope']['workspace_id'] or 'all'}",
        "",
        "Attention",
        f"- Items created: {metrics['attention']['items_created']}",
        f"- Acknowledged: {metrics['attention']['acknowledged']}",
        f"- Closed: {metrics['attention']['closed']}",
        f"- Dismissed: {metrics['attention']['dismissed']}",
        f"- Median time to action hours: {metrics['attention']['median_time_to_action_hours']}",
        "",
        "Recommendations",
        f"- Created: {metrics['recommendations']['recommendations_created']}",
        f"- Accepted: {metrics['recommendations']['accepted']}",
        f"- Rejected: {metrics['recommendations']['rejected']}",
        f"- Evaluated: {metrics['recommendations']['evaluated']}",
        f"- Outcome plans: {metrics['recommendations']['outcome_attribution_plans']['total']}",
        f"- Outcome snapshots: {metrics['recommendations']['outcome_attribution_snapshots']['total']}",
        "",
        "Connectors",
        f"- Providers: {metrics['connectors']['provider_count']}",
        "",
        "Reviews",
        f"- Opened: {metrics['reviews']['opened']}",
        f"- Closed: {metrics['reviews']['closed']}",
        f"- Open: {metrics['reviews']['open']}",
        f"- Median turnaround hours: {metrics['reviews']['median_turnaround_hours']}",
        "",
        "Evidence",
        f"- Evidence-bearing rows: {metrics['evidence']['total_evidence_bearing_rows']}",
        f"- Proposals: {metrics['evidence']['total_proposals']}",
        f"- Evidence correctness: {metrics['evidence']['evidence_correctness'].get('verdict', 'unknown')}",
        f"- False alert rate: {metrics['attention']['false_alert_rate'].get('verdict', 'unknown')}",
        "",
        "Operator verdicts",
        f"- Total recorded: {metrics['operator_verdicts']['total']}",
    ]
    if metrics["operator_verdicts"]["message"]:
        lines.append(f"- {metrics['operator_verdicts']['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report read-only Release D pilot metrics.")
    parser.add_argument("--db", required=True, type=Path, help="Path to the SQLite ledger database.")
    parser.add_argument("--workspace", help="Optional workspace_id to scope metrics.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)

    with connect_read_only(args.db) as conn:
        report = build_report(conn, args.workspace)
    if args.format == "text":
        print(format_text(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
