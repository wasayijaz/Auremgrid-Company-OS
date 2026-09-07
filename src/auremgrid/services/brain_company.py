from __future__ import annotations
from typing import Any
from auremgrid.domain.company import Decision, Deliverable, Organization, OrganizationMembership, Person, Project, Review, ReviewComment, WorkspaceMembership
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import Actor, Fact, Workspace
from auremgrid.services.brain_shared import new_id, normalize_text, utcnow




class BrainCompanyMixin:
        def create_organization(self, name: str, organization_id: str | None = None) -> Organization:
            if not name.strip():
                raise ValidationError("organization name is required")
            if organization_id:
                existing = self.company.get_organization(organization_id)
                if existing:
                    return existing
            item = Organization(organization_id or new_id("org"), name.strip(), utcnow())
            return self.company.save_organization(item)
    
        def create_organization_workspace(
            self, organization_id: str, name: str, kind: str = "client", workspace_id: str | None = None
        ) -> Workspace:
            if self.company.get_organization(organization_id) is None:
                raise NotFoundError(f"organization not found: {organization_id}")
            if kind not in {"internal", "client"}:
                raise ValidationError("workspace kind must be internal or client")
            workspace = self.create_workspace(name, workspace_id)
            if self.company.workspace_scope(workspace.id) is None:
                self.company.attach_workspace(organization_id, workspace.id, kind)
            return workspace
    
        def create_person(
            self, organization_id: str, name: str, email: str | None = None, title: str | None = None,
            department: str | None = None, manager_id: str | None = None, role: str = "member",
            person_id: str | None = None,
        ) -> Person:
            if self.company.get_organization(organization_id) is None:
                raise NotFoundError(f"organization not found: {organization_id}")
            if not name.strip() or role not in {"owner", "admin", "member", "client"}:
                raise ValidationError("valid person name and organization role are required")
            now = utcnow()
            person = self.company.save_person(Person(person_id or new_id("person"), organization_id, name.strip(), email,
                title, department, manager_id, "active", now, now))
            self.company.save_org_membership(OrganizationMembership(new_id("om"), organization_id, person.id, role, now))
            return person
    
        def add_person_to_workspace(self, organization_id: str, workspace_id: str, person_id: str, role: str = "operator") -> WorkspaceMembership:
            scope = self.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != organization_id:
                raise NotFoundError("workspace not found in organization")
            if self.company.get_person(organization_id, person_id) is None:
                raise NotFoundError("person not found in organization")
            if role not in {"admin", "operator", "viewer", "client"}:
                raise ValidationError("unsupported workspace role")
            if role == "client" and scope["kind"] != "client":
                raise ValidationError("client portal role requires a client workspace")
            return self.company.save_workspace_membership(WorkspaceMembership(new_id("wm"), workspace_id, person_id, role, utcnow()))
    
        def create_project(self, organization_id: str, workspace_id: str, person_id: str, name: str,
            description: str = "", priority: str = "normal", due_date: str | None = None,
            budget: float | None = None, tags: list[str] | None = None) -> Project:
            self._require_person_access(organization_id, workspace_id, person_id, write=True)
            if not name.strip() or priority not in {"low", "normal", "high", "urgent"}:
                raise ValidationError("valid project name and priority are required")
            now = utcnow()
            return self.company.save_project(Project(new_id("project"), organization_id, workspace_id, name.strip(),
                description, person_id, "planned", priority, None, due_date, budget, tuple(tags or ()), "healthy", 0.0, now, now))
    
        def create_initiative(self, organization_id: str, workspace_id: str, person_id: str, project_id: str,
            name: str, description: str = "") -> dict[str,Any]:
            self._require_person_access(organization_id,workspace_id,person_id,write=True)
            if self.company.get_project(workspace_id,project_id) is None: raise NotFoundError("project not found")
            if not name.strip(): raise ValidationError("initiative name is required")
            now=utcnow().isoformat();item={"id":new_id("initiative"),"organization_id":organization_id,"workspace_id":workspace_id,
                "project_id":project_id,"name":name.strip(),"description":description,"status":"planned","owner_person_id":person_id,"created_at":now,"updated_at":now}
            self.store.conn.execute("INSERT INTO initiatives VALUES (?,?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.store.conn.commit();return item
    
        def list_projects(self, organization_id: str, workspace_id: str, person_id: str) -> list[Project]:
            self._require_person_access(organization_id, workspace_id, person_id)
            return self.company.list_projects(workspace_id)
    
        def create_deliverable(self, organization_id: str, workspace_id: str, person_id: str, project_id: str,
            title: str, type: str, work_item_id: str | None = None) -> Deliverable:
            self._require_person_access(organization_id, workspace_id, person_id, write=True)
            if self.company.get_project(workspace_id, project_id) is None:
                raise NotFoundError("project not found")
            if work_item_id:
                work_item = self.store.get_work_item(workspace_id, work_item_id)
                if work_item is None:
                    raise NotFoundError("work item not found")
                if work_item.project_id and work_item.project_id != project_id:
                    raise ValidationError("work item belongs to a different project")
            allowed = {"design_asset","landing_page","ad_creative","video","report","copy","presentation","website","document","campaign_output"}
            if type not in allowed or not title.strip():
                raise ValidationError("valid deliverable title and type are required")
            return self.company.save_deliverable(Deliverable(new_id("deliverable"),organization_id,workspace_id,project_id,
                work_item_id,title.strip(),type,person_id,1,"draft",None,None,None,None,0,utcnow(),None))
    
        def open_review(self, organization_id: str, workspace_id: str, person_id: str, deliverable_id: str,
            kind: str = "internal", reviewer_person_id: str | None = None) -> Review:
            self._require_person_access(organization_id, workspace_id, person_id, write=True)
            deliverable = self.company.get_deliverable(workspace_id, deliverable_id)
            if deliverable is None:
                raise NotFoundError("deliverable not found")
            if kind not in {"internal", "client"}:
                raise ValidationError("review kind must be internal or client")
            if reviewer_person_id is not None:
                reviewer_membership = self.company.workspace_membership(workspace_id, reviewer_person_id)
                if reviewer_membership is None:
                    raise ValidationError("reviewer must be a workspace member")
            return self.company.save_review(Review(new_id("review"),organization_id,workspace_id,deliverable_id,
                deliverable.current_version,kind,"open",reviewer_person_id,utcnow(),None,None))
    
        def add_deliverable_version(self, organization_id: str, workspace_id: str, person_id: str,
            deliverable_id: str, notes: str, file_url: str | None = None) -> Deliverable:
            self._require_person_access(organization_id,workspace_id,person_id,write=True)
            deliverable=self.company.get_deliverable(workspace_id,deliverable_id)
            if deliverable is None: raise NotFoundError("deliverable not found")
            version=deliverable.current_version+1;now=utcnow()
            self.store.conn.execute("INSERT INTO deliverable_versions VALUES (?,?,?,?,?,?)",(new_id("dversion"),deliverable_id,version,notes,person_id,now.isoformat()))
            if file_url:self.store.conn.execute("INSERT INTO deliverable_files VALUES (?,?,?,?,?,?,?)",(new_id("dfile"),deliverable_id,version,f"Version {version}",file_url,"source",now.isoformat()))
            updated=Deliverable(**{**deliverable.__dict__,"current_version":version,"approval_status":"draft"})
            return self.company.update_deliverable(updated)
    
        def decide_review(self, organization_id: str, workspace_id: str, person_id: str, review_id: str, decision: str) -> Review:
            membership = self._require_person_access(organization_id, workspace_id, person_id, write=True)
            review = self.company.get_review(workspace_id, review_id)
            if review is None:
                raise NotFoundError("review not found")
            if review.status != "open" or decision not in {"approved", "revision_requested", "rejected"}:
                raise ValidationError("open review and valid decision are required")
            if review.reviewer_person_id and review.reviewer_person_id != person_id and membership.role != "admin":
                raise AuthorizationError("only the assigned reviewer or workspace admin may decide this review")
            status = "approved" if decision == "approved" else decision
            updated=self.company.update_review(Review(**{**review.__dict__, "status": status, "decision": decision, "closed_at": utcnow()}))
            deliverable=self.company.get_deliverable(workspace_id,review.deliverable_id)
            if deliverable:
                revisions=deliverable.revision_count+(1 if decision=="revision_requested" else 0)
                self.company.update_deliverable(Deliverable(**{**deliverable.__dict__,"approval_status":decision,"revision_count":revisions}))
            return updated
    
        def add_review_comment(self, organization_id: str, workspace_id: str, person_id: str, review_id: str,
            body: str, timestamp_seconds: float | None = None) -> ReviewComment:
            self._require_person_access(organization_id,workspace_id,person_id,write=True)
            if self.company.get_review(workspace_id,review_id) is None: raise NotFoundError("review not found")
            if not body.strip(): raise ValidationError("review comment body is required")
            return self.company.save_review_comment(ReviewComment(new_id("reviewcomment"),review_id,person_id,body.strip(),timestamp_seconds,utcnow()))
    
        def create_decision(self, organization_id: str, person_id: str, statement: str, rationale: str,
            workspace_id: str | None = None, project_id: str | None = None, source_id: str | None = None,
            evidence: str = "", tags: list[str] | None = None) -> Decision:
            membership = self.company.org_membership(organization_id, person_id)
            if membership is None:
                raise AuthorizationError("person is not an organization member")
            if workspace_id:
                self._require_person_access(organization_id, workspace_id, person_id, write=True)
                if project_id and self.company.get_project(workspace_id, project_id) is None:
                    raise NotFoundError("project not found")
                if source_id and not self.store.conn.execute(
                    "SELECT id FROM sources WHERE workspace_id=? AND id=?", (workspace_id, source_id)
                ).fetchone():
                    raise NotFoundError("source not found")
            elif project_id:
                raise ValidationError("project_id requires workspace_id")
            if not statement.strip() or not rationale.strip():
                raise ValidationError("decision statement and rationale are required")
            now = utcnow()
            return self.company.save_decision(Decision(new_id("decision"),organization_id,workspace_id,project_id,None,
                statement.strip(),rationale.strip(),person_id,(),source_id,None,evidence,now,now,None,None,tuple(tags or ()),()))
    
        def _require_person_access(self, organization_id: str, workspace_id: str, person_id: str, write: bool = False) -> WorkspaceMembership:
            scope = self.company.workspace_scope(workspace_id)
            membership = self.company.workspace_membership(workspace_id, person_id)
            if scope is None or scope["organization_id"] != organization_id or membership is None:
                raise AuthorizationError("person cannot access workspace")
            if write and membership.role in {"viewer", "client"}:
                raise AuthorizationError("person cannot write workspace")
            return membership
    
        def _require_scope_access(self, organization_id: str, *scope: str, write: bool = False) -> object:
            if len(scope) == 1:
                person_id = scope[0]
                membership = self.company.org_membership(organization_id, person_id)
                if membership is None:
                    raise AuthorizationError("person is not an organization member")
                if write and membership.role == "client":
                    raise AuthorizationError("person cannot write organization")
                return membership
            if len(scope) == 2:
                workspace_id, person_id = scope
                return self._require_person_access(organization_id, workspace_id, person_id, write=write)
            raise ValidationError("invalid authorization scope")
    
        def create_workspace(self, name: str, workspace_id: str | None = None) -> Workspace:
            if workspace_id:
                existing = self.store.get_workspace(workspace_id)
                if existing:
                    return existing
            workspace = Workspace(id=workspace_id or new_id("ws"), name=name, created_at=utcnow())
            return self.store.create_workspace(workspace)
    
        def create_actor(
            self,
            workspace_id: str,
            name: str,
            role: str = "operator",
            actor_id: str | None = None,
        ) -> Actor:
            self._require_workspace(workspace_id)
            if role not in {"admin", "operator", "agent"}:
                raise ValidationError(f"unsupported role: {role}")
            if actor_id:
                existing = self.store.get_actor(workspace_id, actor_id)
                if existing:
                    return existing
            actor = Actor(
                id=actor_id or new_id("act"),
                workspace_id=workspace_id,
                name=name,
                role=role,
                created_at=utcnow(),
            )
            return self.store.create_actor(actor)
    
        def _supersede_matching(self, actor: Actor, incoming: Fact) -> None:
            source_ids = [source.id for source in self.store.allowed_sources(incoming.workspace_id, actor)]
            for existing in self.store.list_facts(incoming.workspace_id, source_ids, include_superseded=False):
                same_claim = (
                    normalize_text(existing.subject) == normalize_text(incoming.subject)
                    and normalize_text(existing.predicate) == normalize_text(incoming.predicate)
                )
                if not same_claim:
                    continue
                if normalize_text(existing.object) == normalize_text(incoming.object):
                    continue
                # Incompatible current observations remain append-only and are
                # grouped as a conflict; human resolution is a separate event.
                group = incoming.conflict_group or existing.conflict_group or f"{normalize_text(incoming.subject)}::{normalize_text(incoming.predicate)}"
                if existing.conflict_group is None:
                    self.store.conn.execute("UPDATE facts SET conflict_group=? WHERE id=?", (group, existing.id))
                scope = self.company.workspace_scope(incoming.workspace_id)
                if scope is not None:
                    self.brain_ops._state_event(scope["organization_id"], incoming.workspace_id, "fact", existing.id, "conflicted", "incompatible current observation", existing.source_id, actor.id)
    
        def _conflict_group_for(self, actor: Actor, incoming: Fact) -> str | None:
            source_ids = [source.id for source in self.store.allowed_sources(incoming.workspace_id, actor)]
            for existing in self.store.list_facts(incoming.workspace_id, source_ids, include_superseded=False):
                if normalize_text(existing.subject) == normalize_text(incoming.subject) and normalize_text(existing.predicate) == normalize_text(incoming.predicate) and normalize_text(existing.object) != normalize_text(incoming.object):
                    return existing.conflict_group or f"{normalize_text(incoming.subject)}::{normalize_text(incoming.predicate)}"
            return None
    
        def _require_workspace(self, workspace_id: str) -> Workspace:
            workspace = self.store.get_workspace(workspace_id)
            if workspace is None:
                raise NotFoundError(f"workspace not found: {workspace_id}")
            return workspace
    
        def _require_actor(self, workspace_id: str, actor_id: str) -> Actor:
            self._require_workspace(workspace_id)
            actor = self.store.get_actor(workspace_id, actor_id)
            if actor is None:
                self._audit(workspace_id, actor_id, "auth", workspace_id, "denied", "unknown actor")
                raise NotFoundError(f"actor not found in workspace: {actor_id}")
            return actor
