from __future__ import annotations

import json
import sqlite3

from auremgrid.domain.company import (
    Decision,
    Deliverable,
    Organization,
    OrganizationMembership,
    Person,
    Project,
    Review,
    ReviewComment,
    WorkspaceMembership,
)
from auremgrid.storage.sqlite import parse_dt


class CompanyRepository:
    """Organization-scoped records stored in Auremgrid's canonical SQLite ledger."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save_organization(self, item: Organization) -> Organization:
        self.conn.execute(
            "INSERT INTO organizations(id,name,created_at) VALUES (?,?,?)",
            (item.id, item.name, item.created_at.isoformat()),
        )
        self.conn.commit()
        return item

    def get_organization(self, organization_id: str) -> Organization | None:
        row = self.conn.execute("SELECT * FROM organizations WHERE id=?", (organization_id,)).fetchone()
        return Organization(row["id"], row["name"], parse_dt(row["created_at"])) if row else None

    def list_organizations_for_person(self, person_id: str) -> list[Organization]:
        rows = self.conn.execute(
            """SELECT o.* FROM organizations o JOIN organization_memberships m
               ON m.organization_id=o.id WHERE m.person_id=? ORDER BY o.name""",
            (person_id,),
        ).fetchall()
        return [Organization(row["id"], row["name"], parse_dt(row["created_at"])) for row in rows]

    def attach_workspace(self, organization_id: str, workspace_id: str, kind: str) -> None:
        self.conn.execute(
            "INSERT INTO workspace_organization(workspace_id,organization_id,kind) VALUES (?,?,?)",
            (workspace_id, organization_id, kind),
        )
        self.conn.commit()

    def workspace_scope(self, workspace_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM workspace_organization WHERE workspace_id=?", (workspace_id,)
        ).fetchone()

    def list_workspaces(self, organization_id: str) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """SELECT w.id,w.name,m.kind FROM workspaces w JOIN workspace_organization m
               ON m.workspace_id=w.id WHERE m.organization_id=? ORDER BY m.kind,w.name""",
            (organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_person(self, item: Person) -> Person:
        self.conn.execute(
            """INSERT INTO people(id,organization_id,name,email,title,department,manager_id,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (item.id, item.organization_id, item.name, item.email, item.title, item.department,
             item.manager_id, item.status, item.created_at.isoformat(), item.updated_at.isoformat()),
        )
        self.conn.commit()
        return item

    def get_person(self, organization_id: str, person_id: str) -> Person | None:
        row = self.conn.execute(
            "SELECT * FROM people WHERE organization_id=? AND id=?", (organization_id, person_id)
        ).fetchone()
        return self._person(row) if row else None

    def list_people(self, organization_id: str) -> list[Person]:
        rows = self.conn.execute(
            "SELECT * FROM people WHERE organization_id=? ORDER BY name", (organization_id,)
        ).fetchall()
        return [self._person(row) for row in rows]

    def save_org_membership(self, item: OrganizationMembership) -> OrganizationMembership:
        self.conn.execute(
            "INSERT INTO organization_memberships(id,organization_id,person_id,role,created_at) VALUES (?,?,?,?,?)",
            (item.id, item.organization_id, item.person_id, item.role, item.created_at.isoformat()),
        )
        self.conn.commit()
        return item

    def org_membership(self, organization_id: str, person_id: str) -> OrganizationMembership | None:
        row = self.conn.execute(
            "SELECT * FROM organization_memberships WHERE organization_id=? AND person_id=?",
            (organization_id, person_id),
        ).fetchone()
        return OrganizationMembership(row["id"], row["organization_id"], row["person_id"], row["role"], parse_dt(row["created_at"])) if row else None

    def save_workspace_membership(self, item: WorkspaceMembership) -> WorkspaceMembership:
        self.conn.execute(
            "INSERT INTO workspace_memberships(id,workspace_id,person_id,role,created_at) VALUES (?,?,?,?,?)",
            (item.id, item.workspace_id, item.person_id, item.role, item.created_at.isoformat()),
        )
        self.conn.commit()
        return item

    def workspace_membership(self, workspace_id: str, person_id: str) -> WorkspaceMembership | None:
        row = self.conn.execute(
            "SELECT * FROM workspace_memberships WHERE workspace_id=? AND person_id=?",
            (workspace_id, person_id),
        ).fetchone()
        return WorkspaceMembership(row["id"], row["workspace_id"], row["person_id"], row["role"], parse_dt(row["created_at"])) if row else None

    def save_project(self, item: Project) -> Project:
        self.conn.execute(
            """INSERT INTO projects(id,organization_id,workspace_id,name,description,owner_person_id,status,
               priority,start_date,due_date,budget,tags,health,progress,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.id,item.organization_id,item.workspace_id,item.name,item.description,item.owner_person_id,
             item.status,item.priority,item.start_date,item.due_date,item.budget,json.dumps(item.tags),item.health,
             item.progress,item.created_at.isoformat(),item.updated_at.isoformat()),
        )
        self.conn.commit()
        return item

    def get_project(self, workspace_id: str, project_id: str) -> Project | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE workspace_id=? AND id=?", (workspace_id, project_id)
        ).fetchone()
        return self._project(row) if row else None

    def list_projects(self, workspace_id: str) -> list[Project]:
        rows = self.conn.execute(
            "SELECT * FROM projects WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)
        ).fetchall()
        return [self._project(row) for row in rows]

    def save_deliverable(self, item: Deliverable) -> Deliverable:
        self.conn.execute(
            """INSERT INTO deliverables(id,organization_id,workspace_id,project_id,work_item_id,title,type,
               owner_person_id,current_version,approval_status,preview_url,final_url,reviewer_person_id,
               client_approver_contact_id,revision_count,created_at,shipped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.id,item.organization_id,item.workspace_id,item.project_id,item.work_item_id,item.title,item.type,
             item.owner_person_id,item.current_version,item.approval_status,item.preview_url,item.final_url,
             item.reviewer_person_id,item.client_approver_contact_id,item.revision_count,item.created_at.isoformat(),
             item.shipped_at.isoformat() if item.shipped_at else None),
        )
        self.conn.commit()
        return item

    def update_deliverable(self, item: Deliverable) -> Deliverable:
        self.conn.execute("""UPDATE deliverables SET current_version=?,approval_status=?,preview_url=?,final_url=?,
            reviewer_person_id=?,client_approver_contact_id=?,revision_count=?,shipped_at=? WHERE workspace_id=? AND id=?""",
            (item.current_version,item.approval_status,item.preview_url,item.final_url,item.reviewer_person_id,
             item.client_approver_contact_id,item.revision_count,item.shipped_at.isoformat() if item.shipped_at else None,item.workspace_id,item.id))
        self.conn.commit();return item

    def get_deliverable(self, workspace_id: str, deliverable_id: str) -> Deliverable | None:
        row = self.conn.execute(
            "SELECT * FROM deliverables WHERE workspace_id=? AND id=?", (workspace_id, deliverable_id)
        ).fetchone()
        return self._deliverable(row) if row else None

    def list_deliverables(self, workspace_id: str, project_id: str | None = None) -> list[Deliverable]:
        sql, values = "SELECT * FROM deliverables WHERE workspace_id=?", [workspace_id]
        if project_id:
            sql, values = sql + " AND project_id=?", [workspace_id, project_id]
        rows = self.conn.execute(sql + " ORDER BY created_at DESC", values).fetchall()
        return [self._deliverable(row) for row in rows]

    def save_review(self, item: Review) -> Review:
        self.conn.execute(
            """INSERT INTO reviews(id,organization_id,workspace_id,deliverable_id,version,kind,status,
               reviewer_person_id,opened_at,closed_at,decision) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (item.id,item.organization_id,item.workspace_id,item.deliverable_id,item.version,item.kind,item.status,
             item.reviewer_person_id,item.opened_at.isoformat(),item.closed_at.isoformat() if item.closed_at else None,item.decision),
        )
        self.conn.commit()
        return item

    def update_review(self, item: Review) -> Review:
        self.conn.execute(
            "UPDATE reviews SET status=?,closed_at=?,decision=? WHERE workspace_id=? AND id=?",
            (item.status,item.closed_at.isoformat() if item.closed_at else None,item.decision,item.workspace_id,item.id),
        )
        self.conn.commit()
        return item

    def get_review(self, workspace_id: str, review_id: str) -> Review | None:
        row = self.conn.execute("SELECT * FROM reviews WHERE workspace_id=? AND id=?", (workspace_id, review_id)).fetchone()
        return self._review(row) if row else None

    def list_reviews(self, workspace_id: str, status: str | None = None) -> list[Review]:
        sql, values = "SELECT * FROM reviews WHERE workspace_id=?", [workspace_id]
        if status:
            sql, values = sql + " AND status=?", [workspace_id, status]
        rows = self.conn.execute(sql + " ORDER BY opened_at ASC", values).fetchall()
        return [self._review(row) for row in rows]

    def save_review_comment(self, item: ReviewComment) -> ReviewComment:
        self.conn.execute(
            "INSERT INTO review_comments(id,review_id,author_person_id,body,timestamp_seconds,created_at) VALUES (?,?,?,?,?,?)",
            (item.id,item.review_id,item.author_person_id,item.body,item.timestamp_seconds,item.created_at.isoformat()),
        )
        self.conn.commit()
        return item

    def save_decision(self, item: Decision) -> Decision:
        self.conn.execute(
            """INSERT INTO decisions(id,organization_id,workspace_id,project_id,campaign_id,statement,rationale,
               decided_by_person_id,participant_person_ids,source_id,source_locator,evidence,created_at,effective_from,
               effective_until,superseded_by,tags,affected_entities) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.id,item.organization_id,item.workspace_id,item.project_id,item.campaign_id,item.statement,item.rationale,
             item.decided_by_person_id,json.dumps(item.participant_person_ids),item.source_id,item.source_locator,item.evidence,
             item.created_at.isoformat(),item.effective_from.isoformat(),item.effective_until.isoformat() if item.effective_until else None,
             item.superseded_by,json.dumps(item.tags),json.dumps(item.affected_entities)),
        )
        self.conn.commit()
        return item

    def list_decisions(self, organization_id: str, workspace_id: str | None = None) -> list[Decision]:
        sql, values = "SELECT * FROM decisions WHERE organization_id=?", [organization_id]
        if workspace_id:
            sql, values = sql + " AND workspace_id=?", [organization_id, workspace_id]
        rows = self.conn.execute(sql + " ORDER BY effective_from DESC", values).fetchall()
        return [self._decision(row) for row in rows]

    @staticmethod
    def _person(row: sqlite3.Row) -> Person:
        return Person(row["id"],row["organization_id"],row["name"],row["email"],row["title"],row["department"],row["manager_id"],row["status"],parse_dt(row["created_at"]),parse_dt(row["updated_at"]))

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(row["id"],row["organization_id"],row["workspace_id"],row["name"],row["description"],row["owner_person_id"],row["status"],row["priority"],row["start_date"],row["due_date"],row["budget"],tuple(json.loads(row["tags"])),row["health"],row["progress"],parse_dt(row["created_at"]),parse_dt(row["updated_at"]))

    @staticmethod
    def _deliverable(row: sqlite3.Row) -> Deliverable:
        return Deliverable(row["id"],row["organization_id"],row["workspace_id"],row["project_id"],row["work_item_id"],row["title"],row["type"],row["owner_person_id"],row["current_version"],row["approval_status"],row["preview_url"],row["final_url"],row["reviewer_person_id"],row["client_approver_contact_id"],row["revision_count"],parse_dt(row["created_at"]),parse_dt(row["shipped_at"]))

    @staticmethod
    def _review(row: sqlite3.Row) -> Review:
        return Review(row["id"],row["organization_id"],row["workspace_id"],row["deliverable_id"],row["version"],row["kind"],row["status"],row["reviewer_person_id"],parse_dt(row["opened_at"]),parse_dt(row["closed_at"]),row["decision"])

    @staticmethod
    def _decision(row: sqlite3.Row) -> Decision:
        return Decision(row["id"],row["organization_id"],row["workspace_id"],row["project_id"],row["campaign_id"],row["statement"],row["rationale"],row["decided_by_person_id"],tuple(json.loads(row["participant_person_ids"])),row["source_id"],row["source_locator"],row["evidence"],parse_dt(row["created_at"]),parse_dt(row["effective_from"]),parse_dt(row["effective_until"]),row["superseded_by"],tuple(json.loads(row["tags"])),tuple(json.loads(row["affected_entities"])))
