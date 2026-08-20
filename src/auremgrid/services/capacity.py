from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone, timedelta
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, ValidationError


TERMINAL_WORK_STATUSES = {"shipped"}
TERMINAL_WORKFLOW_STATUSES = {"completed", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("week_start must be an ISO Monday date") from exc


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _rounded(value: float) -> float:
    return round(float(value), 4)


def _interval_hours(
    started_at: datetime,
    ended_at: datetime | None,
    duration_hours: Any,
    *,
    lower: datetime | None = None,
    upper: datetime,
) -> float:
    """Return only the portion of a time entry inside the requested window."""
    if ended_at is None and duration_hours is not None:
        ended_at = started_at + timedelta(hours=_float(duration_hours))
    if ended_at is None:
        return 0.0
    interval_start = max(started_at, lower) if lower is not None else started_at
    interval_end = min(ended_at, upper)
    return max((interval_end - interval_start).total_seconds() / 3600, 0.0)


def _weekdays_between(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


class CapacityService:
    """Derived current-week capacity board.

    The service is intentionally read-only. It uses current availability and
    leave configuration, plus append-only work/workflow/time records where
    historical cutoffs are available.
    """

    def __init__(self, conn: sqlite3.Connection, company: Any, authorize: Callable[..., Any]) -> None:
        self.conn = conn
        self.company = company
        self.authorize = authorize

    def weekly_board(
        self,
        organization_id: str,
        person_id: str,
        week_start: str | None = None,
        workspace_id: str | None = None,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any]:
        cutoff = _parse_dt(as_of) or _now()
        start = self._week_start(week_start, cutoff)
        end = start + timedelta(days=7)
        week_start_dt = datetime.combine(start, time.min, timezone.utc)
        week_end_dt = datetime.combine(end, time.min, timezone.utc)

        self._require_org_member(organization_id, person_id)
        workspaces = self._visible_workspaces(organization_id, person_id, workspace_id)
        workspace_ids = [workspace["workspace_id"] for workspace in workspaces]

        people = self._people(organization_id, workspace_ids)
        person_rows = {
            item["person_id"]: {
                **item,
                "available_hours": 0.0,
                "leave_hours": 0.0,
                "net_available_hours": 0.0,
                "booked_hours": 0.0,
                "work_remaining_hours": 0.0,
                "workflow_hours": 0.0,
                "remaining_hours": 0.0,
                "work_unestimated_count": 0,
                "workflow_unestimated_stage_count": 0,
                "total_unestimated_count": 0,
                "overloaded": False,
            }
            for item in people
        }
        account_rows = {
            workspace["workspace_id"]: {
                **workspace,
                "booked_hours": 0.0,
                "work_remaining_hours": 0.0,
                "workflow_hours": 0.0,
                "work_unestimated_count": 0,
                "workflow_unestimated_stage_count": 0,
                "roster": self._active_roster(organization_id, workspace["workspace_id"], cutoff),
            }
            for workspace in workspaces
        }
        wing_rows: dict[str, dict[str, Any]] = {}

        self._apply_availability(organization_id, start, person_rows)
        self._apply_leave(organization_id, start, end, person_rows)
        self._apply_booked_time(
            organization_id, workspace_ids, week_start_dt, week_end_dt, cutoff, person_rows, account_rows
        )
        self._apply_work_demand(organization_id, workspace_ids, cutoff, person_rows, account_rows)
        self._apply_workflow_demand(
            organization_id, workspace_ids, cutoff, person_rows, account_rows, wing_rows
        )

        for row in person_rows.values():
            row["net_available_hours"] = row["available_hours"] - row["leave_hours"]
            row["remaining_hours"] = (
                row["available_hours"]
                - row["leave_hours"]
                - row["booked_hours"]
                - row["work_remaining_hours"]
                - row["workflow_hours"]
            )
            row["total_unestimated_count"] = (
                row["work_unestimated_count"] + row["workflow_unestimated_stage_count"]
            )
            row["overloaded"] = row["remaining_hours"] < 0
            self._normalize_hours(row)

        for row in account_rows.values():
            self._normalize_hours(row)
        for row in wing_rows.values():
            row["assigned_person_ids"] = sorted(row["assigned_person_ids"])
            self._normalize_hours(row)

        return {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "as_of": cutoff.isoformat(),
            "metadata": {
                "historical_inputs": "current_configuration",
                "notes": [
                    "availability and leave use current configuration",
                    "work and workflow channels may overlap because no canonical linkage exists",
                ],
            },
            "people": sorted(person_rows.values(), key=lambda item: (item["name"], item["person_id"])),
            "accounts": sorted(account_rows.values(), key=lambda item: (item["workspace_name"], item["workspace_id"])),
            "wings": sorted(wing_rows.values(), key=lambda item: item["wing"].casefold()),
        }

    def _week_start(self, week_start: str | None, cutoff: datetime) -> date:
        if week_start is None:
            today = cutoff.date()
            return today - timedelta(days=today.weekday())
        result = _parse_date(week_start)
        if result.weekday() != 0:
            raise ValidationError("week_start must be an ISO Monday date")
        return result

    def _require_org_member(self, organization_id: str, person_id: str) -> None:
        row = self.conn.execute(
            """SELECT p.status FROM people p
               JOIN organization_memberships om ON om.person_id=p.id AND om.organization_id=p.organization_id
               WHERE p.organization_id=? AND p.id=?""",
            (organization_id, person_id),
        ).fetchone()
        if row is None:
            raise AuthorizationError("organization membership required")
        if row["status"] != "active":
            raise AuthorizationError("person is disabled")

    def _visible_workspaces(
        self, organization_id: str, person_id: str, workspace_id: str | None
    ) -> list[dict[str, str]]:
        if workspace_id is not None:
            self.authorize(organization_id, workspace_id, person_id)
            rows = self.conn.execute(
                """SELECT w.id AS workspace_id,w.name AS workspace_name,wo.kind
                   FROM workspaces w
                   JOIN workspace_organization wo ON wo.workspace_id=w.id
                   WHERE wo.organization_id=? AND w.id=?
                   ORDER BY w.name""",
                (organization_id, workspace_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT w.id AS workspace_id,w.name AS workspace_name,wo.kind
                   FROM workspaces w
                   JOIN workspace_organization wo ON wo.workspace_id=w.id
                   JOIN workspace_memberships wm ON wm.workspace_id=w.id
                   WHERE wo.organization_id=? AND wm.person_id=?
                   ORDER BY w.name""",
                (organization_id, person_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _people(self, organization_id: str, workspace_ids: list[str]) -> list[dict[str, str]]:
        if not workspace_ids:
            return []
        placeholders = ",".join("?" for _ in workspace_ids)
        rows = self.conn.execute(
            f"""SELECT DISTINCT p.id AS person_id,p.name
                FROM people p
                JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.organization_id=? AND p.status='active'
                  AND wm.workspace_id IN ({placeholders})
                ORDER BY p.name,p.id""",
            (organization_id, *workspace_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def _apply_availability(
        self, organization_id: str, week_start: date, people: dict[str, dict[str, Any]]
    ) -> None:
        rows = self.conn.execute(
            "SELECT person_id,available_hours FROM availability WHERE organization_id=? AND week_start=?",
            (organization_id, week_start.isoformat()),
        ).fetchall()
        for row in rows:
            person = people.get(row["person_id"])
            if person is not None:
                person["available_hours"] = _float(row["available_hours"])

    def _apply_leave(
        self,
        organization_id: str,
        week_start: date,
        week_end: date,
        people: dict[str, dict[str, Any]],
    ) -> None:
        week_last = week_end - timedelta(days=1)
        rows = self.conn.execute(
            """SELECT person_id,start_date,end_date,hours FROM leave_records
               WHERE organization_id=? AND LOWER(status)='approved'
                 AND start_date<=? AND end_date>=?""",
            (organization_id, week_last.isoformat(), week_start.isoformat()),
        ).fetchall()
        for row in rows:
            person = people.get(row["person_id"])
            if person is None:
                continue
            leave_start = date.fromisoformat(row["start_date"])
            leave_end = date.fromisoformat(row["end_date"])
            leave_weekdays = _weekdays_between(leave_start, leave_end)
            if not leave_weekdays:
                continue
            overlap_start = max(leave_start, week_start)
            overlap_end = min(leave_end, week_last)
            overlap_weekdays = _weekdays_between(overlap_start, overlap_end)
            person["leave_hours"] += _float(row["hours"]) * (len(overlap_weekdays) / len(leave_weekdays))

    def _apply_booked_time(
        self,
        organization_id: str,
        workspace_ids: list[str],
        week_start: datetime,
        week_end: datetime,
        cutoff: datetime,
        people: dict[str, dict[str, Any]],
        accounts: dict[str, dict[str, Any]],
    ) -> None:
        if not workspace_ids:
            return
        placeholders = ",".join("?" for _ in workspace_ids)
        rows = self.conn.execute(
            f"""SELECT person_id,workspace_id,started_at,ended_at,duration_hours
                FROM time_entries
                WHERE organization_id=? AND workspace_id IN ({placeholders})""",
            (organization_id, *workspace_ids),
        ).fetchall()
        for row in rows:
            started_at = _parse_dt(row["started_at"])
            if started_at is None or started_at >= week_end or started_at > cutoff:
                continue
            duration = _interval_hours(
                started_at,
                _parse_dt(row["ended_at"]),
                row["duration_hours"],
                lower=week_start,
                upper=min(week_end, cutoff),
            )
            if duration <= 0:
                continue
            person = people.get(row["person_id"])
            if person is not None:
                person["booked_hours"] += duration
            account = accounts.get(row["workspace_id"])
            if account is not None:
                account["booked_hours"] += duration

    def _apply_work_demand(
        self,
        organization_id: str,
        workspace_ids: list[str],
        cutoff: datetime,
        people: dict[str, dict[str, Any]],
        accounts: dict[str, dict[str, Any]],
    ) -> None:
        actual_by_work_item = self._actual_effort_by_work_item(organization_id, workspace_ids, cutoff)
        for item in self._work_rows(organization_id, workspace_ids, cutoff):
            assignee = item.get("assignee_person_id")
            if not assignee or item.get("status") in TERMINAL_WORK_STATUSES:
                continue
            person = people.get(str(assignee))
            account = accounts.get(str(item.get("workspace_id")))
            estimate = item.get("estimate_hours")
            if estimate is None:
                if person is not None:
                    person["work_unestimated_count"] += 1
                if account is not None:
                    account["work_unestimated_count"] += 1
                continue
            actual = actual_by_work_item.get(str(item.get("id")), _float(item.get("actual_effort_hours")))
            remaining = max(float(estimate) - actual, 0.0)
            if person is not None:
                person["work_remaining_hours"] += remaining
            if account is not None:
                account["work_remaining_hours"] += remaining

    def _actual_effort_by_work_item(
        self, organization_id: str, workspace_ids: list[str], cutoff: datetime
    ) -> dict[str, float]:
        if not workspace_ids:
            return {}
        placeholders = ",".join("?" for _ in workspace_ids)
        rows = self.conn.execute(
            f"""SELECT work_item_id,started_at,ended_at,duration_hours
                FROM time_entries
                WHERE organization_id=? AND workspace_id IN ({placeholders})
                  AND started_at<=?""",
            (organization_id, *workspace_ids, cutoff.isoformat()),
        ).fetchall()
        totals: dict[str, float] = defaultdict(float)
        for row in rows:
            started_at = _parse_dt(row["started_at"])
            if started_at is None or started_at > cutoff:
                continue
            totals[row["work_item_id"]] += _interval_hours(
                started_at,
                _parse_dt(row["ended_at"]),
                row["duration_hours"],
                upper=cutoff,
            )
        return dict(totals)

    def _work_rows(
        self, organization_id: str, workspace_ids: list[str], cutoff: datetime
    ) -> list[dict[str, Any]]:
        if not workspace_ids:
            return []
        placeholders = ",".join("?" for _ in workspace_ids)
        rows = self.conn.execute(
            f"""SELECT wv.work_item_id,wv.payload,wv.created_at
                FROM work_versions wv
                JOIN work_items wi ON wi.id=wv.work_item_id
                JOIN workspace_organization wo ON wo.workspace_id=wi.workspace_id
                WHERE wo.organization_id=? AND wi.workspace_id IN ({placeholders})
                  AND wv.created_at<=?
                ORDER BY wv.work_item_id,wv.created_at,wv.rowid""",
            (organization_id, *workspace_ids, cutoff.isoformat()),
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[row["work_item_id"]] = json.loads(row["payload"])
        return list(latest.values())

    def _apply_workflow_demand(
        self,
        organization_id: str,
        workspace_ids: list[str],
        cutoff: datetime,
        people: dict[str, dict[str, Any]],
        accounts: dict[str, dict[str, Any]],
        wings: dict[str, dict[str, Any]],
    ) -> None:
        if not workspace_ids:
            return
        placeholders = ",".join("?" for _ in workspace_ids)
        rows = self.conn.execute(
            f"""SELECT s.*,r.organization_id,r.workspace_id,r.template_snapshot
                FROM workflow_stage_runs s
                JOIN workflow_runs r ON r.id=s.run_id
                WHERE r.organization_id=? AND r.workspace_id IN ({placeholders})
                  AND s.created_at<=?""",
            (organization_id, *workspace_ids, cutoff.isoformat()),
        ).fetchall()
        for row in rows:
            status = self._workflow_status_at(row["id"], row["status"], cutoff)
            assignee = row["assignee_person_id"]
            if status in TERMINAL_WORKFLOW_STATUSES or not assignee:
                continue
            duration = self._workflow_stage_duration(row["template_snapshot"], row["stage_key"])
            person = people.get(assignee)
            account = accounts.get(row["workspace_id"])
            wing = str(row["assignee_wing"]).strip()
            wing_row = wings.setdefault(
                wing,
                {
                    "wing": wing,
                    "workflow_hours": 0.0,
                    "workflow_unestimated_stage_count": 0,
                    "assigned_person_ids": set(),
                },
            )
            wing_row["assigned_person_ids"].add(assignee)
            if duration is None:
                if person is not None:
                    person["workflow_unestimated_stage_count"] += 1
                if account is not None:
                    account["workflow_unestimated_stage_count"] += 1
                wing_row["workflow_unestimated_stage_count"] += 1
            else:
                if person is not None:
                    person["workflow_hours"] += duration
                if account is not None:
                    account["workflow_hours"] += duration
                wing_row["workflow_hours"] += duration

    def _workflow_status_at(self, stage_run_id: str, current_status: str, cutoff: datetime) -> str:
        row = self.conn.execute(
            """SELECT to_status FROM workflow_transition_history
               WHERE stage_run_id=? AND created_at<=?
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (stage_run_id, cutoff.isoformat()),
        ).fetchone()
        if row and row["to_status"]:
            return row["to_status"]
        # When the first transition happened after the requested cutoff, its
        # from_status is the durable state that existed at the cutoff. Falling
        # back to the mutable current row would leak a future completion into
        # historical capacity.
        future = self.conn.execute(
            """SELECT from_status FROM workflow_transition_history
               WHERE stage_run_id=? AND created_at>?
               ORDER BY created_at,rowid LIMIT 1""",
            (stage_run_id, cutoff.isoformat()),
        ).fetchone()
        return future["from_status"] if future and future["from_status"] else current_status

    def _workflow_stage_duration(self, template_snapshot: str, stage_key: str) -> float | None:
        try:
            snapshot = json.loads(template_snapshot)
        except (TypeError, ValueError):
            return None
        stages = snapshot.get("stages") or snapshot.get("steps") or []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            key = stage.get("key") or stage.get("id") or stage.get("slug") or stage.get("stage_key")
            if key != stage_key:
                continue
            value = stage.get("expected_duration_hours")
            if value is None:
                value = stage.get("estimate_hours")
            return None if value is None else float(value)
        return None

    def _active_roster(
        self, organization_id: str, workspace_id: str, cutoff: datetime
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM client_account_rosters
               WHERE organization_id=? AND workspace_id=? AND effective_at<=? AND created_at<=?
               ORDER BY effective_at DESC,created_at DESC,id DESC LIMIT 1""",
            (organization_id, workspace_id, cutoff.isoformat(), cutoff.isoformat()),
        ).fetchone()
        if row is None:
            return None
        roles = [
            dict(role)
            for role in self.conn.execute(
                """SELECT role_key,wing,person_id
                   FROM client_account_roster_roles
                   WHERE roster_id=?
                   ORDER BY role_key,COALESCE(wing,''),person_id""",
                (row["id"],),
            ).fetchall()
        ]
        return {
            "id": row["id"],
            "version": row["version"],
            "effective_at": row["effective_at"],
            "roles": roles,
        }

    def _normalize_hours(self, row: dict[str, Any]) -> None:
        for key in (
            "available_hours",
            "leave_hours",
            "net_available_hours",
            "booked_hours",
            "work_remaining_hours",
            "workflow_hours",
            "remaining_hours",
        ):
            if key in row:
                row[key] = _rounded(row[key])
