from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _months_ago(n: int) -> str:
    return (_now() - timedelta(days=30 * n)).isoformat()


VALID_TYPES = ("client_renewal", "revenue", "capacity", "scope_consumption", "utilization", "delivery_pressure")


class ForecastOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any]) -> None:
        self.conn, self.new_id, self.authorize = conn, new_id, authorize

    def generate_forecasts(self, organization_id: str, person_id: str,
        forecast_type: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id, person_id)
        now = _now()
        created = []
        types = [forecast_type] if forecast_type else ["client_renewal", "revenue", "capacity", "utilization"]
        for t in types:
            if t == "client_renewal":
                created.extend(self._client_renewal(organization_id, now))
            elif t == "revenue":
                created.extend(self._revenue(organization_id, now))
            elif t == "capacity":
                created.extend(self._capacity(organization_id, now))
            elif t == "utilization":
                created.extend(self._utilization(organization_id, now))
        self.conn.commit()
        return created

    def _client_renewal(self, org: str, now: datetime) -> list[dict[str, Any]]:
        results = []
        contracts = self.conn.execute(
            # REAL CompanyOS schema scopes contracts by workspace; it has no
            # client_id column.  Keep renewal forecasts workspace-scoped and
            # derive the client identity from the workspace boundary.
            "SELECT id, workspace_id, end_date, status FROM contracts WHERE organization_id=? AND status='active' AND end_date IS NOT NULL",
            (org,)).fetchall()
        for c in contracts:
            end = datetime.fromisoformat(c["end_date"])
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            days_left = (end - now).days
            if days_left > 90 or days_left < 0:
                continue
            risk = "high" if days_left < 30 else "medium" if days_left < 60 else "low"
            iid = self.new_id("fc")
            self.conn.execute(
                "INSERT INTO forecasts (id, organization_id, forecast_type, subject_id, period_start, period_end, predicted_value, confidence, basis, data_points, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, org, "client_renewal", c["id"], now.isoformat(), c["end_date"], float(days_left), 0.8, json.dumps([{"contract_id": c["id"], "days_left": days_left}]), 1, "active", now.isoformat()))
            results.append(self.conn.execute("SELECT * FROM forecasts WHERE id=?", (iid,)).fetchone())
        return [dict(r) for r in results]

    def _revenue(self, org: str, now: datetime) -> list[dict[str, Any]]:
        results = []
        rows = self.conn.execute(
            "SELECT SUM(amount) as total, strftime('%Y-%m', recognized_at) as month FROM revenues WHERE organization_id=? AND recognized_at > ? GROUP BY month ORDER BY month DESC LIMIT 3",
            (org, _months_ago(4))).fetchall()
        if len(rows) < 2:
            return results
        values = [float(r["total"] or 0) for r in rows]
        avg = sum(values) / len(values)
        trend = (values[0] - values[-1]) / max(abs(values[-1]), 1)
        for i, period in enumerate([30, 60, 90]):
            iid = self.new_id("fc")
            projected = avg * (1 + trend * (period / 30))
            period_start = (now + timedelta(days=1)).isoformat()
            period_end = (now + timedelta(days=period)).isoformat()
            self.conn.execute(
                "INSERT INTO forecasts (id, organization_id, forecast_type, subject_id, period_start, period_end, predicted_value, confidence, basis, data_points, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, org, "revenue", None, period_start, period_end, round(projected, 2), round(0.5 + 0.1 * (3 - i), 2), json.dumps({"monthly_avg": avg, "trend": round(trend, 4), "months_used": len(values)}), len(values), "active", now.isoformat()))
            results.append(self.conn.execute("SELECT * FROM forecasts WHERE id=?", (iid,)).fetchone())
        return [dict(r) for r in results]

    def _capacity(self, org: str, now: datetime) -> list[dict[str, Any]]:
        results = []
        snaps = self.conn.execute(
            "SELECT AVG(available_hours) as avg_avail, AVG(estimated_assigned_hours + booked_hours) as avg_util, calculated_at FROM capacity_snapshots WHERE organization_id=? AND calculated_at > ? GROUP BY calculated_at ORDER BY calculated_at DESC LIMIT 4",
            (org, _months_ago(2))).fetchall()
        if not snaps:
            return results
        avg_avail = float(snaps[0]["avg_avail"] or 0)
        avg_util = float(snaps[0]["avg_util"] or 0)
        pressure = avg_util / max(avg_avail, 1)
        iid = self.new_id("fc")
        self.conn.execute(
            "INSERT INTO forecasts (id, organization_id, forecast_type, subject_id, period_start, period_end, predicted_value, confidence, basis, data_points, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, org, "capacity", None, now.isoformat(), (now + timedelta(days=30)).isoformat(), round(pressure, 4), 0.7, json.dumps({"avg_available": avg_avail, "avg_utilized": avg_util}), len(snaps), "active", now.isoformat()))
        results.append(self.conn.execute("SELECT * FROM forecasts WHERE id=?", (iid,)).fetchone())
        return [dict(r) for r in results]

    def _utilization(self, org: str, now: datetime) -> list[dict[str, Any]]:
        results = []
        snaps = self.conn.execute(
            "SELECT AVG((available_hours - remaining_hours) / CASE WHEN available_hours=0 THEN 1 ELSE available_hours END) as avg_util, calculated_at FROM capacity_snapshots WHERE organization_id=? AND calculated_at > ? GROUP BY calculated_at ORDER BY calculated_at DESC LIMIT 6",
            (org, _months_ago(3))).fetchall()
        if len(snaps) < 2:
            return results
        vals = [float(s["avg_util"] or 0) for s in snaps]
        trend = (vals[0] - vals[-1]) / max(abs(vals[-1]), 1)
        projected = vals[0] * (1 + trend * 2)
        projected = min(max(projected, 0), 1)
        iid = self.new_id("fc")
        self.conn.execute(
            "INSERT INTO forecasts (id, organization_id, forecast_type, subject_id, period_start, period_end, predicted_value, confidence, basis, data_points, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, org, "utilization", None, now.isoformat(), (now + timedelta(days=30)).isoformat(), round(projected, 4), round(0.6, 2), json.dumps({"current": vals[0], "trend": round(trend, 4), "data_points": len(vals)}), len(vals), "active", now.isoformat()))
        results.append(self.conn.execute("SELECT * FROM forecasts WHERE id=?", (iid,)).fetchone())
        return [dict(r) for r in results]

    def list_forecasts(self, organization_id: str, person_id: str,
        forecast_type: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.authorize(organization_id, person_id)
        sql = "SELECT * FROM forecasts WHERE organization_id=?"
        params: list[Any] = [organization_id]
        if forecast_type:
            sql += " AND forecast_type=?"; params.append(forecast_type)
        if status:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
