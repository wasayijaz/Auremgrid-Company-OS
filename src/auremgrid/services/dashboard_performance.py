from __future__ import annotations

from typing import Any


class DashboardPerformanceMixin:
    def performance_surface(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        campaigns = [dict(row) for row in self.conn.execute("""SELECT c.id,c.name,c.platform,c.status,c.budget,c.currency,m.spend,m.revenue,m.leads,m.impressions,m.clicks,m.ctr,m.cvr,m.roas,m.source,m.captured_at FROM campaigns c LEFT JOIN campaign_metric_snapshots m ON m.id=(SELECT m2.id FROM campaign_metric_snapshots m2 WHERE m2.campaign_id=c.id ORDER BY m2.captured_at DESC LIMIT 1) WHERE c.organization_id=? AND c.workspace_id=? ORDER BY c.updated_at DESC""", (organization_id, workspace_id)).fetchall()]
        insights = [dict(row) for row in self.conn.execute("SELECT * FROM performance_insights WHERE organization_id=? AND workspace_id=? ORDER BY created_at DESC", (organization_id, workspace_id)).fetchall()]
        creative = [dict(row) for row in self.conn.execute("""SELECT ca.id,ca.title,ca.platform,ca.format,ca.approval_state,AVG(cp.ctr) ctr,AVG(cp.cvr) cvr,AVG(cp.roas) roas,COUNT(cp.id) samples FROM creative_assets ca LEFT JOIN creative_performance cp ON cp.asset_id=ca.id WHERE ca.organization_id=? AND ca.workspace_id=? GROUP BY ca.id ORDER BY roas DESC""", (organization_id, workspace_id)).fetchall()]
        attention = [row for row in campaigns if row.get("roas") is None or row.get("source") in {None, "", "not_connected"} or (row.get("roas") is not None and float(row["roas"]) < 1)]
        return {"campaigns": campaigns, "creative_comparison": creative, "insights": insights, "attention": attention, "evidence_status": "sourced" if campaigns else "unknown"}
