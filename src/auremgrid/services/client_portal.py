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

    def _require_staff_membership(self, organization_id: str, workspace_id: str, person_id: str) -> None:
        scope = self.company.workspace_scope(workspace_id)
        organization_membership = self.company.org_membership(organization_id, person_id)
        membership = self.company.workspace_membership(workspace_id, person_id)
        if (
            scope is None
            or scope["organization_id"] != organization_id
            or organization_membership is None
            or organization_membership.role == "client"
            or membership is None
            or membership.role in {"viewer", "client"}
        ):
            raise AuthorizationError("person is not staff for this workspace")

    def _require_workspace_actor(self, workspace_id: str, actor_id: str | None) -> None:
        if actor_id is None:
            return
        if self.store.get_actor(workspace_id, actor_id) is None:
            raise AuthorizationError("assignee is not an actor in this workspace")

    def _audit(
        self, organization_id: str, workspace_id: str, person_id: str,
        action: str, entity_type: str, entity_id: str, detail: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO ledger_audit(
                id, organization_id, workspace_id, principal_type, principal_id,
                action, entity_type, entity_id, detail, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                self.new_id("audit"), organization_id, workspace_id, "person", person_id,
                action, entity_type, entity_id, detail, _now().isoformat(),
            ),
        )

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
        self._audit(organization_id, workspace_id, person_id, "create", "client_intake_request", item["id"], "submitted")
        self.conn.commit()
        return item

    def list_intake_requests(
        self, organization_id: str, workspace_id: str, person_id: str, status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_client_membership(organization_id, workspace_id, person_id)
        sql = """SELECT * FROM client_intake_requests
                 WHERE organization_id=? AND workspace_id=? AND submitted_by_person_id=?"""
        values: list[Any] = [organization_id, workspace_id, person_id]
        if status:
            sql += " AND status=?"
            values.append(status)
        rows = self.conn.execute(sql + " ORDER BY created_at DESC", values).fetchall()
        return [dict(row) for row in rows]

    def list_intake_queue(self, organization_id: str, workspace_id: str, staff_person_id: str) -> list[dict[str, Any]]:
        """Staff-facing read of the pending intake queue for a client workspace.

        Clients can list their own submitted requests through list_intake_requests;
        this queue is for staff triage only.
        """

        self._require_staff_membership(organization_id, workspace_id, staff_person_id)
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
        on its own.
        """

        self._require_staff_membership(organization_id, workspace_id, staff_person_id)
        self._require_workspace_actor(workspace_id, assignee_id)
        if decision_maker is not None:
            self._require_staff_membership(organization_id, workspace_id, decision_maker)
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
        self._audit(organization_id, workspace_id, staff_person_id, "accept", "client_intake_request", intake_id, item.id)
        self.conn.commit()
        return {"intake_request_id": intake_id, "work_item_id": item.id, "status": "accepted"}

    def decline_intake_request(
        self, organization_id: str, workspace_id: str, staff_person_id: str, intake_id: str, note: str = "",
    ) -> dict[str, Any]:
        self._require_staff_membership(organization_id, workspace_id, staff_person_id)
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
        self._audit(organization_id, workspace_id, staff_person_id, "decline", "client_intake_request", intake_id, note.strip())
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
        comment = self.company.save_review_comment(
            ReviewComment(self.new_id("reviewcomment"), review_id, person_id, body.strip(), None, _now())
        )
        self._audit(organization_id, workspace_id, person_id, "create", "review_comment", comment.id, review_id)
        self.conn.commit()
        return comment

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
        self._audit(organization_id, workspace_id, person_id, "decide", "review", review_id, decision)
        self.conn.commit()
        return updated
