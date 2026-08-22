from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


VALID_INSIGHT_TYPES = ("creative_comparison", "channel_comparison", "trend", "anomaly", "client_preference")


class PerformanceOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any]) -> None:
        self.conn, self.new_id, self.authorize = conn, new_id, authorize

    def generate_insights(self, organization_id: str, workspace_id: str, person_id: str,
        insight_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        now = _now().isoformat()
        created = []
        types = [insight_type] if insight_type else ["creative_comparison", "channel_comparison", "anomaly"]
        for t in types:
            if t == "creative_comparison":
                created.extend(self._creative_comparisons(organization_id, workspace_id, now, limit))
            elif t == "channel_comparison":
                created.extend(self._channel_comparisons(organization_id, workspace_id, now, limit))
            elif t == "anomaly":
                created.extend(self._anomalies(organization_id, workspace_id, now, limit))
        self.conn.commit()
        return created

    def _creative_comparisons(self, org: str, ws: str, now: str, limit: int) -> list[dict[str, Any]]:
        results = []
        campaigns = [r["id"] for r in self.conn.execute(
            "SELECT id FROM campaigns WHERE organization_id=? AND workspace_id=? AND status != ?", (org, ws, "draft")).fetchall()]
        for cid in campaigns[:10]:
            rows = self.conn.execute(
                "SELECT ca.id, ca.title, ca.style_tags, AVG(cp.roas) as avg_roas FROM creative_assets ca JOIN creative_performance cp ON cp.asset_id=ca.id WHERE ca.campaign_id=? AND cp.roas IS NOT NULL GROUP BY ca.id HAVING COUNT(*) >= 2 ORDER BY avg_roas DESC",
                (cid,)).fetchall()
            if len(rows) < 2:
                continue
            top = rows[0]
            others = rows[1:]
            avg_others = sum(r["avg_roas"] for r in others) / len(others)
            delta = top["avg_roas"] - avg_others if avg_others else 0
            direction = "positive" if delta > 0 else "negative" if delta < 0 else "neutral"
            confidence = min(abs(delta) / max(abs(avg_others), 0.01), 1.0)
            iid = self.new_id("pi")
            self.conn.execute(
                "INSERT INTO performance_insights (id, organization_id, workspace_id, insight_type, subject_type, subject_id, comparison_subject_id, metric_name, metric_value_a, metric_value_b, delta, direction, confidence, evidence_summary, source_snapshot_ids, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, org, ws, "creative_comparison", "creative", top["id"], others[0]["id"], "roas", top["avg_roas"], avg_others, round(delta, 4), direction, round(confidence, 4), f"{top['title']} outperforms {others[0]['title']} on roas", json.dumps([]), "proposed", now))
            results.append(self.conn.execute("SELECT * FROM performance_insights WHERE id=?", (iid,)).fetchone())
            if len(results) >= limit:
                break
        return [dict(r) for r in results]

    def _channel_comparisons(self, org: str, ws: str, now: str, limit: int) -> list[dict[str, Any]]:
        results = []
        rows = self.conn.execute(
            "SELECT c.platform, AVG(cm.roas) as avg_roas, AVG(cm.ctr) as avg_ctr, COUNT(*) as n FROM campaigns c JOIN campaign_metric_snapshots cm ON cm.campaign_id=c.id WHERE c.organization_id=? AND c.workspace_id=? AND c.status != ? GROUP BY c.platform HAVING n >= 2",
            (org, ws, "draft")).fetchall()
        if len(rows) < 2:
            return results
        best = max(rows, key=lambda r: r["avg_roas"] or 0)
        worst = min(rows, key=lambda r: r["avg_roas"] or 0)
        delta = (best["avg_roas"] or 0) - (worst["avg_roas"] or 0)
        iid = self.new_id("pi")
        direction = "positive" if delta > 0 else "neutral"
        self.conn.execute(
            "INSERT INTO performance_insights (id, organization_id, workspace_id, insight_type, subject_type, subject_id, comparison_subject_id, metric_name, metric_value_a, metric_value_b, delta, direction, confidence, evidence_summary, source_snapshot_ids, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, org, ws, "channel_comparison", "platform", best["platform"], worst["platform"], "roas", best["avg_roas"], worst["avg_roas"], round(delta, 4), direction, 0.6, f"{best['platform']} ROAS ({best['avg_roas']}) vs {worst['platform']} ({worst['avg_roas']})", json.dumps([]), "proposed", now))
        results.append(self.conn.execute("SELECT * FROM performance_insights WHERE id=?", (iid,)).fetchone())
        return [dict(r) for r in results]

    def _anomalies(self, org: str, ws: str, now: str, limit: int) -> list[dict[str, Any]]:
        results = []
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(campaign_metric_snapshots)").fetchall()}
        if {"metric_name", "metric_value"}.issubset(columns):
            snaps = self.conn.execute(
                "SELECT id, campaign_id, metric_name, metric_value, captured_at FROM campaign_metric_snapshots WHERE organization_id=? AND workspace_id=? ORDER BY campaign_id, metric_name, captured_at DESC LIMIT 200",
                (org, ws)).fetchall()
        else:
            # The production migration stores a wide metric snapshot while
            # older service fixtures use a metric_name/metric_value pair.
            # Normalize the wide form here so anomaly detection uses the same
            # canonical performance_insights schema in either database.
            metric_names = [name for name in ("spend", "revenue", "leads", "impressions", "clicks", "cpl", "cac", "ctr", "cvr", "roas") if name in columns]
            rows = self.conn.execute(
                f"SELECT id, campaign_id, captured_at, {', '.join(metric_names)} FROM campaign_metric_snapshots WHERE organization_id=? AND workspace_id=? ORDER BY campaign_id, captured_at DESC LIMIT 200",
                (org, ws)).fetchall()
            snaps = [
                {"id": row["id"], "campaign_id": row["campaign_id"], "metric_name": name, "metric_value": row[name], "captured_at": row["captured_at"]}
                for row in rows for name in metric_names if row[name] is not None
            ]
        grouped: dict[str, list] = {}
        for s in snaps:
            key = f"{s['campaign_id']}:{s['metric_name']}"
            grouped.setdefault(key, []).append(s)
        for key, vals in grouped.items():
            if len(vals) < 3:
                continue
            recent = vals[0]["metric_value"]
            avg = sum(v["metric_value"] for v in vals[1:]) / (len(vals) - 1)
            if avg == 0 or recent is None:
                continue
            pct = (recent - avg) / abs(avg)
            if abs(pct) < 0.3:
                continue
            cid, mname = key.split(":")
            iid = self.new_id("pi")
            direction = "positive" if pct > 0 else "negative"
            self.conn.execute(
                "INSERT INTO performance_insights (id, organization_id, workspace_id, insight_type, subject_type, subject_id, comparison_subject_id, metric_name, metric_value_a, metric_value_b, delta, direction, confidence, evidence_summary, source_snapshot_ids, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, org, ws, "anomaly", "campaign", cid, None, mname, recent, round(avg, 4), round(pct, 4), direction, min(abs(pct), 1.0), f"{mname} deviated {round(pct*100)}% from rolling average", json.dumps([v["id"] for v in vals[:3]]), "proposed", now))
            results.append(self.conn.execute("SELECT * FROM performance_insights WHERE id=?", (iid,)).fetchone())
            if len(results) >= limit:
                break
        return [dict(r) for r in results]

    def list_insights(self, organization_id: str, workspace_id: str, person_id: str,
        status: str | None = None, insight_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM performance_insights WHERE organization_id=? AND workspace_id=?"
        params: list[Any] = [organization_id, workspace_id]
        if status:
            sql += " AND status=?"; params.append(status)
        if insight_type:
            sql += " AND insight_type=?"; params.append(insight_type)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def decide_insight(self, organization_id: str, workspace_id: str, person_id: str,
        insight_id: str, decision: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision must be approved or rejected")
        row = self.conn.execute(
            "SELECT * FROM performance_insights WHERE id=? AND organization_id=? AND workspace_id=?",
            (insight_id, organization_id, workspace_id)).fetchone()
        if row is None:
            raise NotFoundError("insight not found")
        now = _now().isoformat()
        self.conn.execute(
            "UPDATE performance_insights SET status=?, approved_by_person_id=?, approved_at=? WHERE id=?",
            (decision, person_id if decision == "approved" else None, now if decision == "approved" else None, insight_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM performance_insights WHERE id=?", (insight_id,)).fetchone())
