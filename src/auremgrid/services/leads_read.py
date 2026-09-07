"""Offline, organization-scoped agency leads read projection.

Provides role-filterable read-only operational summaries for:
- Ads Lead: Campaign performance, ad spend, ROAS, conversion metrics, platform breakdowns
- Design Lead: Creative pipeline states, approval queues, revision counts, asset formats
- Marketing Lead: Multi-channel status, campaign channel alignment, deliverable pipeline
- Executive Rollup: High-level cross-discipline health indicators, key metrics, and bottlenecks
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class LeadsReadService:
    """Read-only service projecting agency lead operational postures."""

    def __init__(self, company_os: Any) -> None:
        self.os = company_os
        self.conn: sqlite3.Connection = company_os.store.conn

    def get_lead_view(
        self,
        os_scope: Any,
        role: str = "executive",
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch to role-specific lead summary (ads, design, marketing, executive)."""
        clean_role = str(role).strip().lower()
        if clean_role in ("ads", "ads_lead", "paid_media"):
            return self.get_ads_lead_view(os_scope, workspace_id=workspace_id)
        elif clean_role in ("design", "design_lead", "creative"):
            return self.get_design_lead_view(os_scope, workspace_id=workspace_id)
        elif clean_role in ("marketing", "marketing_lead", "channels"):
            return self.get_marketing_lead_view(os_scope, workspace_id=workspace_id)
        elif clean_role in ("executive", "exec", "rollup", "all"):
            return self.get_executive_rollup(os_scope, workspace_id=workspace_id)
        else:
            raise ValidationError(f"unsupported lead role: {role}; expected 'ads', 'design', 'marketing', or 'executive'")

    def get_ads_lead_view(
        self,
        os_scope: Any,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Project campaign and performance summaries for the Ads Lead role."""
        organization_id, target_ws, person_id = self._scope(os_scope, workspace_id)
        self._authorize(organization_id, target_ws, person_id)

        params: list[Any] = [organization_id]
        camp_sql = """
            SELECT id, workspace_id, project_id, name, objective, platform,
                   budget, currency, start_date, end_date, status, owner_person_id
            FROM campaigns
            WHERE organization_id=?
        """
        if target_ws:
            camp_sql += " AND workspace_id=?"
            params.append(target_ws)
        camp_sql += " ORDER BY status ASC, created_at DESC"

        try:
            c_rows = self.conn.execute(camp_sql, params).fetchall()
            campaigns = [dict(r) for r in c_rows]
        except Exception:
            campaigns = []

        total_campaigns = len(campaigns)
        active_campaigns = sum(1 for c in campaigns if str(c.get("status", "")).lower() == "active")
        total_budget = sum(float(c.get("budget") or 0.0) for c in campaigns)

        total_spend = 0.0
        total_revenue = 0.0
        total_leads = 0
        total_impressions = 0
        total_clicks = 0
        platform_metrics: dict[str, dict[str, Any]] = {}
        anomalies: list[dict[str, Any]] = []

        for c in campaigns:
            cid = c["id"]
            plat = str(c.get("platform") or "other").lower()
            if plat not in platform_metrics:
                platform_metrics[plat] = {
                    "platform": plat,
                    "campaigns_count": 0,
                    "budget": 0.0,
                    "spend": 0.0,
                    "revenue": 0.0,
                    "leads": 0,
                }
            platform_metrics[plat]["campaigns_count"] += 1
            platform_metrics[plat]["budget"] += float(c.get("budget") or 0.0)

            try:
                snap_row = self.conn.execute(
                    """
                    SELECT spend, revenue, leads, impressions, clicks, ctr, cvr, roas, captured_at
                    FROM campaign_metric_snapshots
                    WHERE campaign_id=? AND organization_id=?
                    ORDER BY captured_at DESC, id DESC LIMIT 1
                    """,
                    (cid, organization_id),
                ).fetchone()
                if snap_row:
                    c["latest_snapshot"] = dict(snap_row)
                    s_spend = float(snap_row["spend"] or 0.0)
                    s_rev = float(snap_row["revenue"] or 0.0)
                    s_leads = int(snap_row["leads"] or 0)
                    s_impr = int(snap_row["impressions"] or 0)
                    s_clicks = int(snap_row["clicks"] or 0)

                    total_spend += s_spend
                    total_revenue += s_rev
                    total_leads += s_leads
                    total_impressions += s_impr
                    total_clicks += s_clicks

                    platform_metrics[plat]["spend"] += s_spend
                    platform_metrics[plat]["revenue"] += s_rev
                    platform_metrics[plat]["leads"] += s_leads

                    roas = float(snap_row["roas"] or (s_rev / s_spend if s_spend > 0 else 0.0))
                    if s_spend > 500.0 and roas < 1.0:
                        anomalies.append({
                            "campaign_id": cid,
                            "campaign_name": c["name"],
                            "type": "low_roas",
                            "severity": "high",
                            "message": f"ROAS below 1.0x ({roas:.2f}x) with $" + f"{s_spend:.0f} spend",
                        })
                else:
                    c["latest_snapshot"] = None
            except Exception:
                c["latest_snapshot"] = None

        blended_roas = round(total_revenue / total_spend, 2) if total_spend > 0 else 0.0
        blended_ctr = round((total_clicks / total_impressions) * 100, 2) if total_impressions > 0 else 0.0

        for p_data in platform_metrics.values():
            p_spend = p_data["spend"]
            p_rev = p_data["revenue"]
            p_data["roas"] = round(p_rev / p_spend, 2) if p_spend > 0 else 0.0

        return {
            "role": "ads_lead",
            "organization_id": organization_id,
            "workspace_id": target_ws,
            "summary": {
                "total_campaigns": total_campaigns,
                "active_campaigns": active_campaigns,
                "total_budget": round(total_budget, 2),
                "total_spend": round(total_spend, 2),
                "total_revenue": round(total_revenue, 2),
                "blended_roas": blended_roas,
                "total_leads": total_leads,
                "blended_ctr_percent": blended_ctr,
                "anomalies_count": len(anomalies),
            },
            "platforms": sorted(platform_metrics.values(), key=lambda x: x["spend"], reverse=True),
            "campaigns": campaigns,
            "anomalies": anomalies,
        }

    def get_design_lead_view(
        self,
        os_scope: Any,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Project creative asset pipeline state and review bottlenecks for the Design Lead role."""
        organization_id, target_ws, person_id = self._scope(os_scope, workspace_id)
        self._authorize(organization_id, target_ws, person_id)

        params: list[Any] = [organization_id]
        ca_sql = """
            SELECT id, workspace_id, project_id, campaign_id, title, platform, format,
                   approval_state, revision_count, created_at, reviewer_person_id
            FROM creative_assets
            WHERE organization_id=?
        """
        if target_ws:
            ca_sql += " AND workspace_id=?"
            params.append(target_ws)
        ca_sql += " ORDER BY revision_count DESC, created_at DESC"

        try:
            rows = self.conn.execute(ca_sql, params).fetchall()
            assets = [dict(r) for r in rows]
        except Exception:
            assets = []

        total_assets = len(assets)
        state_counts: dict[str, int] = {
            "draft": 0,
            "in_review": 0,
            "approved": 0,
            "rejected": 0,
            "shipped": 0,
        }
        format_counts: dict[str, int] = {}
        high_revision_assets: list[dict[str, Any]] = []
        review_queue: list[dict[str, Any]] = []

        for a in assets:
            st = str(a.get("approval_state") or "draft").lower()
            state_counts[st] = state_counts.get(st, 0) + 1

            fmt = str(a.get("format") or "unspecified").lower()
            format_counts[fmt] = format_counts.get(fmt, 0) + 1

            revs = int(a.get("revision_count") or 0)
            if revs >= 3:
                high_revision_assets.append(a)

            if st in ("in_review", "review", "pending_approval"):
                review_queue.append(a)

        deliv_params: list[Any] = [organization_id]
        deliv_sql = """
            SELECT id, workspace_id, title, type, current_version, approval_status, revision_count, created_at
            FROM deliverables
            WHERE organization_id=? AND type IN ('ad_creative', 'design_asset', 'video', 'landing_page', 'copy')
        """
        if target_ws:
            deliv_sql += " AND workspace_id=?"
            deliv_params.append(target_ws)
        deliv_sql += " ORDER BY created_at DESC LIMIT 20"

        try:
            d_rows = self.conn.execute(deliv_sql, deliv_params).fetchall()
            deliverables = [dict(r) for r in d_rows]
        except Exception:
            deliverables = []

        bottleneck_status = "clear"
        if len(review_queue) >= 5 or len(high_revision_assets) >= 4:
            bottleneck_status = "congested"
        elif len(review_queue) >= 2 or len(high_revision_assets) >= 2:
            bottleneck_status = "moderate"

        return {
            "role": "design_lead",
            "organization_id": organization_id,
            "workspace_id": target_ws,
            "pipeline_state": {
                "total_creative_assets": total_assets,
                "review_queue_count": len(review_queue),
                "high_revision_count": len(high_revision_assets),
                "approval_states": state_counts,
                "formats": format_counts,
                "bottleneck_status": bottleneck_status,
            },
            "review_queue": review_queue,
            "high_revision_assets": high_revision_assets,
            "creative_deliverables": deliverables,
            "recent_assets": assets[:25],
        }

    def get_marketing_lead_view(
        self,
        os_scope: Any,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Project marketing channel statuses and content output for the Marketing Lead role."""
        organization_id, target_ws, person_id = self._scope(os_scope, workspace_id)
        self._authorize(organization_id, target_ws, person_id)

        params: list[Any] = [organization_id]
        ch_sql = """
            SELECT c.platform, c.status, COUNT(c.id) as campaign_count,
                   SUM(COALESCE(c.budget, 0)) as total_budget
            FROM campaigns c
            WHERE c.organization_id=?
        """
        if target_ws:
            ch_sql += " AND c.workspace_id=?"
            params.append(target_ws)
        ch_sql += " GROUP BY c.platform, c.status"

        try:
            ch_rows = self.conn.execute(ch_sql, params).fetchall()
            raw_channels = [dict(r) for r in ch_rows]
        except Exception:
            raw_channels = []

        channels_summary: dict[str, dict[str, Any]] = {}
        for r in raw_channels:
            plat = str(r.get("platform") or "other").lower()
            if plat not in channels_summary:
                channels_summary[plat] = {
                    "platform": plat,
                    "active_campaigns": 0,
                    "paused_campaigns": 0,
                    "total_budget": 0.0,
                }
            if str(r.get("status", "")).lower() == "active":
                channels_summary[plat]["active_campaigns"] += int(r.get("campaign_count") or 0)
            else:
                channels_summary[plat]["paused_campaigns"] += int(r.get("campaign_count") or 0)
            channels_summary[plat]["total_budget"] += float(r.get("total_budget") or 0.0)

        deliv_params: list[Any] = [organization_id]
        deliv_sql = """
            SELECT id, workspace_id, title, type, current_version, approval_status, shipped_at, created_at
            FROM deliverables
            WHERE organization_id=?
        """
        if target_ws:
            deliv_sql += " AND workspace_id=?"
            deliv_params.append(target_ws)
        deliv_sql += " ORDER BY created_at DESC"

        try:
            d_rows = self.conn.execute(deliv_sql, deliv_params).fetchall()
            all_deliverables = [dict(r) for r in d_rows]
        except Exception:
            all_deliverables = []

        total_deliverables = len(all_deliverables)
        shipped_deliverables = sum(1 for d in all_deliverables if d.get("shipped_at") or d.get("approval_status") == "approved")
        pending_deliverables = total_deliverables - shipped_deliverables

        by_type: dict[str, int] = {}
        for d in all_deliverables:
            dtype = str(d.get("type") or "other").lower()
            by_type[dtype] = by_type.get(dtype, 0) + 1

        active_channels_count = sum(1 for ch in channels_summary.values() if ch["active_campaigns"] > 0)

        return {
            "role": "marketing_lead",
            "organization_id": organization_id,
            "workspace_id": target_ws,
            "channels_overview": {
                "active_channels_count": active_channels_count,
                "total_channels_tracked": len(channels_summary),
                "channels": sorted(channels_summary.values(), key=lambda x: x["active_campaigns"], reverse=True),
            },
            "content_pipeline": {
                "total_deliverables": total_deliverables,
                "shipped_deliverables": shipped_deliverables,
                "pending_deliverables": pending_deliverables,
                "by_type": by_type,
            },
            "recent_deliverables": all_deliverables[:20],
        }

    def get_executive_rollup(
        self,
        os_scope: Any,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Project high-level cross-discipline lead rollup for executive decision-making."""
        ads_view = self.get_ads_lead_view(os_scope, workspace_id=workspace_id)
        design_view = self.get_design_lead_view(os_scope, workspace_id=workspace_id)
        marketing_view = self.get_marketing_lead_view(os_scope, workspace_id=workspace_id)

        ads_summary = ads_view.get("summary", {})
        design_pipeline = design_view.get("pipeline_state", {})
        marketing_channels = marketing_view.get("channels_overview", {})
        marketing_content = marketing_view.get("content_pipeline", {})

        roas = float(ads_summary.get("blended_roas") or 0.0)
        anomalies_cnt = int(ads_summary.get("anomalies_count") or 0)
        if anomalies_cnt >= 2 or (roas < 1.0 and float(ads_summary.get("total_spend", 0.0)) > 0.0):
            ads_posture = "critical"
        elif anomalies_cnt >= 1 or (float(ads_summary.get("total_spend", 0.0)) > 0.0 and roas < 1.5):
            ads_posture = "attention"
        else:
            ads_posture = "healthy"

        bottlenecks = str(design_pipeline.get("bottleneck_status") or "clear")
        if bottlenecks == "congested":
            design_posture = "attention"
        else:
            design_posture = "healthy"

        active_channels = int(marketing_channels.get("active_channels_count") or 0)
        if active_channels == 0 and int(ads_summary.get("total_campaigns", 0)) > 0:
            marketing_posture = "attention"
        else:
            marketing_posture = "healthy"

        postures = [ads_posture, design_posture, marketing_posture]
        if "critical" in postures:
            overall_posture = "critical"
        elif "attention" in postures:
            overall_posture = "needs_attention"
        else:
            overall_posture = "healthy"

        alerts: list[dict[str, Any]] = []
        for an in ads_view.get("anomalies", []):
            alerts.append({"discipline": "ads", "severity": an.get("severity", "high"), "message": an.get("message")})
        rev_q = int(design_pipeline.get("review_queue_count", 0))
        if rev_q >= 4:
            alerts.append({"discipline": "design", "severity": "medium", "message": f"{rev_q} creative assets waiting in review queue"})
        pend_deliv = int(marketing_content.get("pending_deliverables", 0))
        if pend_deliv >= 10:
            alerts.append({"discipline": "marketing", "severity": "medium", "message": f"{pend_deliv} deliverables pending completion"})

        return {
            "role": "executive_rollup",
            "organization_id": ads_view["organization_id"],
            "workspace_id": ads_view["workspace_id"],
            "overall_posture": overall_posture,
            "discipline_postures": {
                "ads": ads_posture,
                "design": design_posture,
                "marketing": marketing_posture,
            },
            "key_metrics": {
                "active_campaigns": ads_summary.get("active_campaigns", 0),
                "total_ad_spend": ads_summary.get("total_spend", 0.0),
                "blended_roas": roas,
                "total_leads": ads_summary.get("total_leads", 0),
                "total_creative_assets": design_pipeline.get("total_creative_assets", 0),
                "creative_review_queue": rev_q,
                "active_channels_count": active_channels,
                "shipped_deliverables": marketing_content.get("shipped_deliverables", 0),
            },
            "alerts": alerts,
        }

    def _authorize(self, organization_id: str, workspace_id: str | None, person_id: str | None) -> None:
        """Verify organization and workspace authorization."""
        if person_id and hasattr(self.os, "_require_person_access"):
            if workspace_id:
                self.os._require_person_access(organization_id, workspace_id, person_id, write=False)
            elif hasattr(self.os.company, "org_membership"):
                if self.os.company.org_membership(organization_id, person_id) is None:
                    raise AuthorizationError("caller is not an organization member")
            return

        if workspace_id:
            scope = self.conn.execute(
                """SELECT 1 FROM workspace_organization
                   WHERE organization_id=? AND workspace_id=?""",
                (organization_id, workspace_id),
            ).fetchone()
            if scope is None:
                raise AuthorizationError("workspace scope denied")

    @staticmethod
    def _scope(os_scope: Any, workspace_id: str | None) -> tuple[str, str | None, str | None]:
        def value(key: str, default: Any = None) -> Any:
            if isinstance(os_scope, Mapping):
                return os_scope.get(key, default)
            return getattr(os_scope, key, default)

        organization_id = str(value("organization_id", "") or "").strip()
        scope_workspace = value("workspace_id")
        person_id = value("person_id")
        target_ws = str(workspace_id or scope_workspace or "").strip() or None
        person_id = str(person_id or "").strip() or None

        if not organization_id:
            raise ValidationError("organization is required")
        if workspace_id and scope_workspace and workspace_id != scope_workspace:
            raise AuthorizationError("requested workspace is outside authorized scope")

        return organization_id, target_ws, person_id


LeadsRead = LeadsReadService
