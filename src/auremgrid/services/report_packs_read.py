"""Read-only report pack projections for client workspaces.

This service assembles existing Company OS evidence into deterministic report
packs.  It does not register routes, create records, or calculate persisted
snapshots; callers own when and where the returned pack is shown.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timezone
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


OPEN_WORK_STATUSES = {"captured", "assigned", "in_progress", "review", "client_review"}


class ReportPacksReadService:
    """Small read-only aggregator for client report packs."""

    def __init__(self, company_os: Any) -> None:
        self.os = company_os
        self._conn = company_os.store.conn

    def list_available_packs(self, os_scope: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return client report packs visible from the supplied OS scope."""
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=False)
        result = []
        for workspace in self.os.company.list_workspaces(organization_id):
            if workspace.get("kind") != "client":
                continue
            client_id = str(workspace.get("id") or "")
            try:
                self.os._require_person_access(organization_id, client_id, person_id, write=False)
            except AuthorizationError:
                continue
            result.append({
                "id": f"client_report_pack:{client_id}",
                "type": "client_report_pack",
                "client_id": client_id,
                "client_name": str(workspace.get("name") or client_id),
                "sections": ["performance", "finance", "delivery", "risks"],
            })
        return sorted(result, key=lambda item: (item["client_name"].lower(), item["client_id"]))

    def build_report_pack(
        self,
        os_scope: Mapping[str, Any],
        client_id: str,
        period: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build a read-only report pack for one permitted client workspace."""
        organization_id, workspace_id, person_id = self._scope(os_scope)
        client = self._require_client_workspace(organization_id, client_id)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=False)
        if client_id != workspace_id:
            self.os._require_person_access(organization_id, client_id, person_id, write=False)
        period_bounds = self._period_bounds(period)
        return {
            "type": "client_report_pack",
            "scope": {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "person_id": person_id,
                "client_id": client_id,
                "period": period_bounds["label"],
                "period_start": period_bounds["start"],
                "period_end": period_bounds["end"],
            },
            "client": client,
            "performance": self._performance_summary(organization_id, client_id, period_bounds),
            "finance": self.os.agency_ops.finance_status(organization_id, person_id, client_id),
            "delivery": self._delivery_summary(client_id, period_bounds),
            "risks": self.os.client_ops.list_risks(organization_id, client_id, person_id, open_only=True),
        }

    def _scope(self, os_scope: Mapping[str, Any]) -> tuple[str, str, str]:
        if not isinstance(os_scope, Mapping):
            raise ValidationError("OS scope is required")
        organization_id = self._required_text(os_scope.get("organization_id"), "organization is required")
        workspace_id = self._required_text(os_scope.get("workspace_id"), "workspace is required")
        person_id = self._required_text(os_scope.get("person_id"), "person is required")
        return organization_id, workspace_id, person_id

    def _require_client_workspace(self, organization_id: str, client_id: str) -> dict[str, str]:
        client_id = self._required_text(client_id, "client is required")
        scope = self.os.company.workspace_scope(client_id)
        if scope is None:
            raise NotFoundError("client workspace was not found")
        scoped = dict(scope)
        if scoped.get("organization_id") != organization_id:
            raise AuthorizationError("client workspace is outside organization scope")
        if scoped.get("kind") != "client":
            raise ValidationError("client workspace is required")
        for workspace in self.os.company.list_workspaces(organization_id):
            if workspace.get("id") == client_id:
                return {
                    "id": client_id,
                    "name": str(workspace.get("name") or client_id),
                    "kind": str(workspace.get("kind") or "client"),
                }
        return {"id": client_id, "name": client_id, "kind": "client"}

    def _performance_summary(
        self,
        organization_id: str,
        client_id: str,
        period: Mapping[str, str],
    ) -> dict[str, Any]:
        if not self._table_exists("campaigns"):
            return self._empty_performance()
        campaigns = [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM campaigns WHERE organization_id=? AND workspace_id=? ORDER BY created_at,id",
                (organization_id, client_id),
            ).fetchall()
        ]
        if not campaigns or not self._table_exists("campaign_metric_snapshots"):
            return {**self._empty_performance(), "campaign_count": len(campaigns)}

        campaign_summaries = []
        totals = {"spend": 0.0, "revenue": 0.0, "leads": 0.0, "impressions": 0.0, "clicks": 0.0}
        metric_count = 0
        for campaign in campaigns:
            latest = self._conn.execute(
                """SELECT * FROM campaign_metric_snapshots
                   WHERE organization_id=? AND workspace_id=? AND campaign_id=?
                   AND captured_at BETWEEN ? AND ?
                   ORDER BY captured_at DESC,id DESC LIMIT 1""",
                (organization_id, client_id, campaign["id"], period["start"], period["end"]),
            ).fetchone()
            metrics = dict(latest) if latest else None
            if metrics:
                metric_count += 1
                for key in totals:
                    totals[key] += float(metrics.get(key) or 0)
            campaign_summaries.append({
                "id": campaign["id"],
                "name": campaign["name"],
                "platform": campaign["platform"],
                "status": campaign["status"],
                "latest_metrics": metrics,
            })

        return {
            "campaign_count": len(campaigns),
            "active_campaign_count": sum(1 for item in campaigns if item.get("status") in {"scheduled", "active"}),
            "campaigns_with_metrics": metric_count,
            "totals": {
                **{key: round(value, 4) for key, value in totals.items()},
                "ctr": self._ratio(totals["clicks"], totals["impressions"], 100),
                "cvr": self._ratio(totals["leads"], totals["clicks"], 100),
                "roas": self._ratio(totals["revenue"], totals["spend"]),
                "cpl": self._ratio(totals["spend"], totals["leads"]),
            },
            "campaigns": campaign_summaries,
        }

    def _delivery_summary(self, client_id: str, period: Mapping[str, str]) -> dict[str, Any]:
        work_items = [self._work_item_dict(item) for item in self.os.store.list_work_items(client_id, open_only=False)]
        period_start = self._parse_datetime(period["start"])
        by_status: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        by_assignee: dict[str, int] = {}
        open_items = []
        blocked_items = []
        overdue_items = []
        for item in work_items:
            status = str(item.get("status") or "unknown")
            priority = str(item.get("priority") or "normal")
            assignee = str(item.get("assignee_person_id") or item.get("assignee_id") or "unassigned")
            by_status[status] = by_status.get(status, 0) + 1
            by_priority[priority] = by_priority.get(priority, 0) + 1
            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1
            if status in OPEN_WORK_STATUSES:
                open_items.append(item)
                if item.get("blocking_reason"):
                    blocked_items.append(item)
                deadline = item.get("deadline") or item.get("needed_by")
                if status in OPEN_WORK_STATUSES and deadline and self._parse_datetime(deadline) < period_start:
                    overdue_items.append(item)
        return {
            "work_item_count": len(work_items),
            "open_work_count": len(open_items),
            "shipped_work_count": by_status.get("shipped", 0),
            "blocked_work_count": len(blocked_items),
            "overdue_work_count": len(overdue_items),
            "by_status": dict(sorted(by_status.items())),
            "by_priority": dict(sorted(by_priority.items())),
            "by_assignee": dict(sorted(by_assignee.items())),
            "open_items": self._bounded_work_items(open_items),
            "blocked_items": self._bounded_work_items(blocked_items),
            "overdue_items": self._bounded_work_items(overdue_items),
        }

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _period_bounds(period: str | Mapping[str, Any]) -> dict[str, str]:
        if isinstance(period, str):
            text = period.strip()
            try:
                year, month = (int(part) for part in text.split("-", 1))
                start_date = date(year, month, 1)
                end_date = date(year, month, calendar.monthrange(year, month)[1])
            except (TypeError, ValueError) as exc:
                raise ValidationError("period must be YYYY-MM or include start and end") from exc
            return {
                "label": text,
                "start": datetime.combine(start_date, time.min, timezone.utc).isoformat(),
                "end": datetime.combine(end_date, time.max, timezone.utc).isoformat(),
            }
        if isinstance(period, Mapping):
            start = ReportPacksReadService._parse_datetime(period.get("start"))
            end = ReportPacksReadService._parse_datetime(period.get("end"))
            if end < start:
                raise ValidationError("period end must not precede start")
            return {"label": f"{start.date().isoformat()}..{end.date().isoformat()}", "start": start.isoformat(), "end": end.isoformat()}
        raise ValidationError("period must be YYYY-MM or include start and end")

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("timestamp is required")
        try:
            if len(text) == 10:
                parsed = datetime.combine(date.fromisoformat(text), time.min, timezone.utc)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("timestamp is invalid") from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _work_item_dict(item: Any) -> dict[str, Any]:
        if hasattr(item, "to_dict") and callable(item.to_dict):
            return item.to_dict()
        return dict(item)

    @staticmethod
    def _bounded_work_items(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "status": str(item.get("status") or ""),
                "priority": str(item.get("priority") or "normal"),
                "assignee_person_id": item.get("assignee_person_id") or item.get("assignee_id"),
                "deadline": item.get("deadline") or item.get("needed_by"),
                "blocking_reason": item.get("blocking_reason"),
            }
            for item in items[:20]
        ]

    @staticmethod
    def _empty_performance() -> dict[str, Any]:
        return {
            "campaign_count": 0,
            "active_campaign_count": 0,
            "campaigns_with_metrics": 0,
            "totals": {"spend": 0.0, "revenue": 0.0, "leads": 0.0, "impressions": 0.0, "clicks": 0.0, "ctr": None, "cvr": None, "roas": None, "cpl": None},
            "campaigns": [],
        }

    @staticmethod
    def _ratio(numerator: float, denominator: float, multiplier: float = 1.0) -> float | None:
        return round(numerator / denominator * multiplier, 4) if denominator else None

    @staticmethod
    def _required_text(value: Any, message: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValidationError(message)
        return text


ReportPacksReadServiceAlias = ReportPacksReadService
