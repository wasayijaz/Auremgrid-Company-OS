from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError


class DashboardReviewCenterMixin:
    def review_center(self, organization_id: str, person_id: str) -> dict[str, Any]:
        """Cross-workspace review queue for the caller's own accessible workspaces.

        This never trusts a caller-supplied workspace_id: membership rows are
        the only source of which workspaces the caller may see review rows
        from, matching the read-access pattern used by command().
        """

        if self.os.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
        workspace_rows = self.conn.execute(
            """SELECT w.id,w.name,wm.role FROM workspaces w
               JOIN workspace_organization wo ON wo.workspace_id=w.id
               JOIN workspace_memberships wm ON wm.workspace_id=w.id
               WHERE wo.organization_id=? AND wm.person_id=?""",
            (organization_id, person_id),
        ).fetchall()
        ids = [row["id"] for row in workspace_rows]
        names = {row["id"]: row["name"] for row in workspace_rows}
        roles = {row["id"]: row["role"] for row in workspace_rows}
        if not ids:
            return {
                "waiting_for_me": [], "waiting_for_team": [], "waiting_for_client": [],
                "revision_requested": [], "stalled": [], "approved_today": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT rv.id,rv.workspace_id,rv.deliverable_id,rv.version,rv.kind,rv.status,
                       rv.reviewer_person_id,rv.opened_at,rv.closed_at,rv.decision,
                       d.title AS deliverable_title, d.type AS deliverable_type
                FROM reviews rv
                JOIN deliverables d ON d.id=rv.deliverable_id
                WHERE rv.workspace_id IN ({placeholders})
                ORDER BY rv.opened_at ASC""",
            ids,
        ).fetchall()
        now = datetime.now(timezone.utc)

        def row_view(row: Any) -> dict[str, Any]:
            deliverable = self.os.company.get_deliverable(row["workspace_id"], row["deliverable_id"])
            annotation_capabilities = self.os.annotation_capabilities_for_deliverable(row["workspace_id"], deliverable)
            source_locator = (deliverable.final_url or deliverable.preview_url) if deliverable else None
            if deliverable and not source_locator:
                source_row = self.conn.execute("SELECT url FROM deliverable_files WHERE deliverable_id=? ORDER BY version DESC,created_at DESC LIMIT 1", (deliverable.id,)).fetchone()
                source_locator = source_row[0] if source_row else None
            allowed_actions = [{"action": "add_annotation", "method": "POST", "route": "/reviews/annotations",
                                "payload": {"review_id": row["id"]}, "required_fields": ["annotation_type", "body"]}]
            can_decide = row["status"] == "open" and (
                not row["reviewer_person_id"] or row["reviewer_person_id"] == person_id or roles.get(row["workspace_id"]) == "admin"
            )
            if can_decide:
                for decision in ("approved", "revision_requested", "rejected"):
                    allowed_actions.append({
                        "action": f"review_{decision}", "label": decision.replace("_", " ").title(),
                        "method": "POST", "route": "/reviews/decide",
                        "payload": {"workspace_id": row["workspace_id"], "review_id": row["id"], "decision": decision},
                        "required_fields": [],
                    })
            return {
                "id": row["id"], "workspace_id": row["workspace_id"], "client": names.get(row["workspace_id"]),
                "deliverable_id": row["deliverable_id"], "deliverable_title": row["deliverable_title"],
                "deliverable_type": row["deliverable_type"], "version": row["version"], "kind": row["kind"],
                "status": row["status"], "reviewer_person_id": row["reviewer_person_id"],
                "opened_at": row["opened_at"], "closed_at": row["closed_at"], "decision": row["decision"],
                "annotation_capabilities": annotation_capabilities,
                "source_locator": source_locator,
                "allowed_actions": allowed_actions,
            }

        waiting_for_me: list[dict[str, Any]] = []
        waiting_for_team: list[dict[str, Any]] = []
        waiting_for_client: list[dict[str, Any]] = []
        revision_requested: list[dict[str, Any]] = []
        stalled: list[dict[str, Any]] = []
        approved_today: list[dict[str, Any]] = []
        for row in rows:
            view = row_view(row)
            if row["status"] == "open":
                opened_at = row["opened_at"]
                try:
                    stalled_hours = (now - datetime.fromisoformat(opened_at.replace("Z", "+00:00"))).total_seconds() / 3600
                except (TypeError, ValueError):
                    stalled_hours = 0.0
                view["stalled_hours"] = round(stalled_hours, 1)
                if stalled_hours >= 48:
                    stalled.append(view)
                if row["kind"] == "client":
                    waiting_for_client.append(view)
                elif row["reviewer_person_id"] == person_id:
                    waiting_for_me.append(view)
                else:
                    waiting_for_team.append(view)
            elif row["status"] == "revision_requested":
                revision_requested.append(view)
            elif row["status"] == "approved" and row["closed_at"] and row["closed_at"][:10] == now.date().isoformat():
                approved_today.append(view)
        return {
            "waiting_for_me": waiting_for_me, "waiting_for_team": waiting_for_team,
            "waiting_for_client": waiting_for_client, "revision_requested": revision_requested,
            "stalled": stalled, "approved_today": approved_today,
            "generated_at": now.isoformat(),
        }
