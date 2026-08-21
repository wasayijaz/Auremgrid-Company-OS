"""Client-facing portal: bounded intake submission and client-review actions.

External client contacts get a distinct workspace role ("client") with a
narrow "client_portal" capability rather than "workspace_write". This module
is the only place that role is allowed to mutate state, and every mutation
here is scoped to exactly one workspace and validated against that
person's own membership row -- a client identity can never act on another
workspace's data even if it discovers another workspace's id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.company import Deliverable, Review, ReviewComment
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.ops import WorkItem, default_dod


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class ClientPortalOperations:
    def __init__(self, store: Any, company: Any, new_id: Callable[[str], str]) -> None:
        self.store = store
        self.conn = store.conn
        self.company = company
        self.new_id = new_id

    def _require_client_membership(self, organization_id: str, workspace_id: str, person_id: str) -> None:
        scope = self.company.workspace_scope(workspace_id)
        membership = self.company.workspace_membership(workspace_id, person_id)
        if (
            scope is None
            or scope["organization_id"] != organization_id
            or scope["kind"] != "client"
            or membership is None
            or membership.role != "client"
        ):
            raise AuthorizationError("person is not a client-portal member of this workspace")

    def submit_intake_request(
        self, organization_id: str, workspace_id: str, person_id: str,
        title: str, request: str, needed_by: str | None = None,
    ) -> dict[str, Any]:
        self._require_client_membership(organization_id, workspace_id, person_id)
        if not title.strip() or not request.strip():
            raise ValidationError("intake request requires a title and request body")
        now = _now()
        item = {
            "id": self.new_id("intake"), "organization_id": organization_id, "workspace_id": workspace_id,
            "submitted_by_person_id": person_id, "title": title.strip(), "request": request.strip(),
            "needed_by": needed_by, "status": "pending", "work_item_id": None,
            "decided_by_person_id": None, "decision_note": None,
            "created_at": now.isoformat(), "decided_at": None,
        }
        self.conn.execute(
            """INSERT INTO client_intake_requests(
                id,organization_id,workspace_id,submitted_by_person_id,title,request,needed_by,
                status,work_item_id,decided_by_person_id,decision_note,created_at,decided_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    def list_intake_requests(
        self, organization_id: str, workspace_id: str, person_id: str, status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_client_membership(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM client_intake_requests WHERE workspace_id=?"
        values: list[Any] = [workspace_id]
        if status:
            sql += " AND status=?"
            values.append(status)
        rows = self.conn.execute(sql + " ORDER BY created_at DESC", values).fetchall()
        return [dict(row) for row in rows]

    def list_intake_queue(self, organization_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """Staff-facing read of the pending intake queue for a client workspace.

        Callers are responsible for their own staff-capability authorization
        before invoking this; it performs no client-membership check because
        staff, not clients, use it.
        """

        rows = self.conn.execute(
            """SELECT * FROM client_intake_requests
               WHERE organization_id=? AND workspace_id=? AND status='pending'
               ORDER BY created_at ASC""",
            (organization_id, workspace_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def accept_intake_request(
        self, organization_id: str, workspace_id: str, staff_person_id: str, intake_id: str,
        assignee_id: str | None = None, decision_maker: str | None = None,
    ) -> dict[str, Any]:
        """Staff confirms an intake request, creating the canonical WorkItem.

        This is the front-door gate: a client submission never becomes work
        on its own. Callers must have already checked staff write access to
        the workspace (e.g. via CompanyOS.capture_work's own authorization
        path) before calling this; it re-validates the request row and
        workspace scope but does not itself gate on staff capability, since
        that check belongs to the calling actor-authorized surface.
        """

        row = self.conn.execute(
            "SELECT * FROM client_intake_requests WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, intake_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("intake request not found")
        if row["status"] != "pending":
            raise ValidationError("intake request has already been decided")
        now = _now()
        item = WorkItem(
            id=self.new_id("work"), workspace_id=workspace_id, title=row["title"], request=row["request"],
            requested_by=row["submitted_by_person_id"], needed_by=row["needed_by"], status="captured",
            assignee_id=assignee_id, playbook_id=None, decision_maker=decision_maker,
            definition_of_done=default_dod(), created_at=now, updated_at=now,
        )
        self.store.upsert_work_item(item)
        self.conn.execute(
            """UPDATE client_intake_requests SET status='accepted',work_item_id=?,
               decided_by_person_id=?,decided_at=? WHERE id=?""",
            (item.id, staff_person_id, now.isoformat(), intake_id),
        )
        self.conn.commit()
        return {"intake_request_id": intake_id, "work_item_id": item.id, "status": "accepted"}

    def decline_intake_request(
        self, organization_id: str, workspace_id: str, staff_person_id: str, intake_id: str, note: str = "",
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM client_intake_requests WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, intake_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("intake request not found")
        if row["status"] != "pending":
            raise ValidationError("intake request has already been decided")
        now = _now()
        self.conn.execute(
            """UPDATE client_intake_requests SET status='declined',
               decided_by_person_id=?,decision_note=?,decided_at=? WHERE id=?""",
            (staff_person_id, note.strip() or None, now.isoformat(), intake_id),
        )
        self.conn.commit()
        return {"intake_request_id": intake_id, "status": "declined"}

    def list_client_reviews(self, organization_id: str, workspace_id: str, person_id: str) -> list[dict[str, Any]]:
        self._require_client_membership(organization_id, workspace_id, person_id)
        reviews = self.company.list_reviews(workspace_id)
        return [review.to_dict() for review in reviews if review.kind == "client"]

    def _require_client_review(self, workspace_id: str, review_id: str) -> Review:
        review = self.company.get_review(workspace_id, review_id)
        if review is None:
            raise NotFoundError("review not found")
        if review.kind != "client":
            raise AuthorizationError("client portal can only act on client-kind reviews")
        return review

    def add_client_review_comment(
        self, organization_id: str, workspace_id: str, person_id: str, review_id: str, body: str,
    ) -> ReviewComment:
        self._require_client_membership(organization_id, workspace_id, person_id)
        self._require_client_review(workspace_id, review_id)
        if not body.strip():
            raise ValidationError("review comment body is required")
        return self.company.save_review_comment(
            ReviewComment(self.new_id("reviewcomment"), review_id, person_id, body.strip(), None, _now())
        )

    def decide_client_review(
        self, organization_id: str, workspace_id: str, person_id: str, review_id: str, decision: str,
    ) -> Review:
        self._require_client_membership(organization_id, workspace_id, person_id)
        review = self._require_client_review(workspace_id, review_id)
        if review.status != "open" or decision not in {"approved", "revision_requested", "rejected"}:
            raise ValidationError("open client review and valid decision are required")
        status = "approved" if decision == "approved" else decision
        updated = self.company.update_review(
            Review(**{**review.__dict__, "status": status, "decision": decision, "closed_at": _now()})
        )
        deliverable = self.company.get_deliverable(workspace_id, review.deliverable_id)
        if deliverable:
            revisions = deliverable.revision_count + (1 if decision == "revision_requested" else 0)
            self.company.update_deliverable(
                Deliverable(**{**deliverable.__dict__, "approval_status": decision, "revision_count": revisions})
            )
        return updated
