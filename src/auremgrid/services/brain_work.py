from __future__ import annotations
import datetime
import json
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import Actor
from auremgrid.domain.ops import ALLOWED_TRANSITIONS, AccountBrief, ClientBrainPack, DEFINITION_OF_DONE, Playbook, StatusPost, Touchpoint, WorkEvent, WorkItem, default_dod
from auremgrid.services.brain_shared import new_id, utcnow




class BrainWorkMixin:
        def capture_work(
            self,
            workspace_id: str,
            actor_id: str,
            title: str,
            request: str,
            requested_by: str,
            needed_by: str | None = None,
            playbook_id: str | None = None,
            decision_maker: str | None = None,
            work_item_id: str | None = None,
        ) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "capture_work")
            if work_item_id:
                existing = self.store.get_work_item(workspace_id, work_item_id)
                if existing:
                    return existing
            if not title.strip() or not request.strip() or not requested_by.strip():
                raise ValidationError("intake requires title, request, and requested_by")
            now = utcnow()
            item = WorkItem(
                id=work_item_id or new_id("work"),
                workspace_id=workspace_id,
                title=title.strip(),
                request=request.strip(),
                requested_by=requested_by.strip(),
                needed_by=needed_by,
                status="captured",
                assignee_id=None,
                playbook_id=playbook_id,
                decision_maker=decision_maker,
                definition_of_done=default_dod(),
                created_at=now,
                updated_at=now,
            )
            self.store.upsert_work_item(item)
            self._record_work_event(item, actor.id, "captured", None, "captured", "intake recorded")
            self._audit(workspace_id, actor.id, "capture_work", item.id, "created", title)
            return item
    
        def assign_work(
            self,
            workspace_id: str,
            actor_id: str,
            work_item_id: str,
            assignee_id: str,
            decision_maker: str | None = None,
        ) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "assign_work")
            assignee = self._require_actor(workspace_id, assignee_id)
            item = self._require_work_item(workspace_id, work_item_id)
            updated = self._transition(
                item,
                actor.id,
                "assigned",
                assignee_id=assignee.id,
                decision_maker=decision_maker or item.decision_maker or actor.name,
                detail=f"assigned to {assignee.name}",
            )
            self._audit(workspace_id, actor.id, "assign_work", item.id, "ok", assignee.id)
            return updated
    
        def start_work(self, workspace_id: str, actor_id: str, work_item_id: str) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "start_work")
            item = self._require_work_item(workspace_id, work_item_id)
            updated = self._transition(item, actor.id, "in_progress", detail="production started")
            self._audit(workspace_id, actor.id, "start_work", item.id, "ok", updated.status)
            return updated
    
        def mark_dod(
            self,
            workspace_id: str,
            actor_id: str,
            work_item_id: str,
            checks: dict[str, bool],
        ) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "mark_dod")
            item = self._require_work_item(workspace_id, work_item_id)
            dod = dict(item.definition_of_done)
            for key, value in checks.items():
                if key not in DEFINITION_OF_DONE:
                    raise ValidationError(f"unknown definition-of-done check: {key}")
                dod[key] = bool(value)
            updated = WorkItem(**{**item.__dict__, "definition_of_done": dod, "updated_at": utcnow()})
            self.store.upsert_work_item(updated)
            self._record_work_event(updated, actor.id, "dod_updated", item.status, item.status, json.dumps(dod))
            self._audit(workspace_id, actor.id, "mark_dod", item.id, "ok", json.dumps(dod))
            return updated
    
        def submit_review(self, workspace_id: str, actor_id: str, work_item_id: str) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "submit_review")
            item = self._require_work_item(workspace_id, work_item_id)
            if not item.dod_complete:
                missing = [key for key, value in item.definition_of_done.items() if not value]
                raise ValidationError(f"definition of done incomplete: {', '.join(missing)}")
            updated = self._transition(item, actor.id, "review", detail="submitted for internal review")
            self._audit(workspace_id, actor.id, "submit_review", item.id, "ok", updated.status)
            return updated
    
        def close_review(
            self,
            workspace_id: str,
            actor_id: str,
            work_item_id: str,
            approved: bool,
            note: str = "",
        ) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "close_review")
            item = self._require_work_item(workspace_id, work_item_id)
            next_status = "client_review" if approved else "in_progress"
            updated = self._transition(
                item,
                actor.id,
                next_status,
                detail=note or ("approved internally" if approved else "returned to production"),
            )
            self._audit(workspace_id, actor.id, "close_review", item.id, "ok", updated.status)
            return updated
    
        def ship_work(
            self,
            workspace_id: str,
            actor_id: str,
            work_item_id: str,
            note: str = "",
        ) -> WorkItem:
            actor = self._require_writable(workspace_id, actor_id, "ship_work")
            item = self._require_work_item(workspace_id, work_item_id)
            updated = self._transition(item, actor.id, "shipped", detail=note or "shipped to client")
            self.record_status(
                workspace_id,
                actor.id,
                f"{updated.title} shipped. {note}".strip(),
            )
            self._audit(workspace_id, actor.id, "ship_work", item.id, "ok", updated.status)
            return updated
    
        def record_touchpoint(
            self,
            workspace_id: str,
            actor_id: str,
            summary: str,
            kind: str = "client",
            occurred_at: datetime | None = None,
            touchpoint_id: str | None = None,
        ) -> Touchpoint:
            actor = self._require_writable(workspace_id, actor_id, "record_touchpoint")
            if touchpoint_id:
                row = self.store.conn.execute("SELECT * FROM touchpoints WHERE workspace_id=? AND id=?",(workspace_id,touchpoint_id)).fetchone()
                if row:
                    return self.store._touchpoint_from_row(row)
            touchpoint = Touchpoint(
                id=touchpoint_id or new_id("tp"),
                workspace_id=workspace_id,
                actor_id=actor.id,
                kind=kind,
                summary=summary,
                occurred_at=occurred_at or utcnow(),
                recorded_at=utcnow(),
            )
            self.store.create_touchpoint(touchpoint)
            self._audit(workspace_id, actor.id, "record_touchpoint", kind, "created", summary[:120])
            return touchpoint
    
        def record_status(self, workspace_id: str, actor_id: str, body: str) -> StatusPost:
            actor = self._require_writable(workspace_id, actor_id, "record_status")
            post = StatusPost(
                id=new_id("st"),
                workspace_id=workspace_id,
                actor_id=actor.id,
                body=body,
                posted_at=utcnow(),
            )
            self.store.create_status_post(post)
            self._audit(workspace_id, actor.id, "record_status", workspace_id, "created", body[:120])
            return post
    
        def upsert_playbook(
            self,
            actor_id: str,
            slug: str,
            title: str,
            body: str,
            workspace_id: str | None = None,
        ) -> Playbook:
            if workspace_id:
                self._require_writable(workspace_id, actor_id, "upsert_playbook")
                audit_workspace = workspace_id
            else:
                actor = self.store.get_actor_any(actor_id)
                if actor is None:
                    raise NotFoundError(f"actor not found: {actor_id}")
                audit_workspace = actor.workspace_id
            playbook = Playbook(
                id=new_id("pb"),
                workspace_id=workspace_id,
                slug=slug,
                title=title,
                body=body,
                created_at=utcnow(),
            )
            saved = self.store.upsert_playbook(playbook)
            self._audit(audit_workspace, actor_id, "upsert_playbook", slug, "ok", title)
            return saved
    
        def upsert_client_brain(
            self,
            workspace_id: str,
            actor_id: str,
            snapshot: str,
            brand_rules: str,
            landing_pages: str = "",
            ads: str = "",
            design: str = "",
            email: str = "",
            dos: list[str] | None = None,
            donts: list[str] | None = None,
            open_loops: list[str] | None = None,
        ) -> ClientBrainPack:
            actor = self._require_writable(workspace_id, actor_id, "upsert_client_brain")
            brain = ClientBrainPack(
                workspace_id=workspace_id,
                snapshot=snapshot,
                brand_rules=brand_rules,
                landing_pages=landing_pages,
                ads=ads,
                design=design,
                email=email,
                dos=tuple(dos or ()),
                donts=tuple(donts or ()),
                open_loops=tuple(open_loops or ()),
                updated_at=utcnow(),
            )
            saved = self.store.upsert_client_brain(brain)
            self._audit(workspace_id, actor.id, "upsert_client_brain", workspace_id, "ok", snapshot[:120])
            return saved
    
        def account_brief(
            self,
            workspace_id: str,
            actor_id: str,
            query: str | None = None,
        ) -> AccountBrief:
            actor = self._require_actor(workspace_id, actor_id)
            brain = self.store.get_client_brain(workspace_id)
            playbooks = tuple(self.store.list_playbooks(workspace_id))
            open_work = tuple(self.store.list_work_items(workspace_id, open_only=True))
            latest = self.store.latest_touchpoint(workspace_id)
            days = None
            if latest:
                days = max((utcnow() - latest.occurred_at).days, 0)
            evidence = {}
            if query:
                evidence = self.search(workspace_id, actor.id, query).to_dict()
            self._audit(workspace_id, actor.id, "account_brief", workspace_id, "ok", query or "brief")
            return AccountBrief(
                workspace_id=workspace_id,
                brain=brain,
                playbooks=playbooks,
                open_work=open_work,
                latest_touchpoint=latest,
                days_since_touchpoint=days,
                evidence=evidence,
            )
    
        def list_work(self, workspace_id: str, actor_id: str, open_only: bool = False) -> list[WorkItem]:
            self._require_actor(workspace_id, actor_id)
            return self.store.list_work_items(workspace_id, open_only=open_only)
    
        def _require_writable(self, workspace_id: str, actor_id: str, action: str) -> Actor:
            actor = self._require_actor(workspace_id, actor_id)
            if not actor.can_write:
                self._audit(workspace_id, actor_id, action, workspace_id, "denied", "read-only actor")
                raise AuthorizationError(f"actor cannot {action}")
            return actor
    
        def _require_work_item(self, workspace_id: str, work_item_id: str) -> WorkItem:
            item = self.store.get_work_item(workspace_id, work_item_id)
            if item is None:
                raise NotFoundError(f"work item not found: {work_item_id}")
            return item
    
        def _transition(
            self,
            item: WorkItem,
            actor_id: str,
            to_status: str,
            detail: str,
            assignee_id: str | None = None,
            decision_maker: str | None = None,
        ) -> WorkItem:
            allowed = ALLOWED_TRANSITIONS.get(item.status, set())
            if to_status not in allowed:
                raise ValidationError(f"cannot move {item.status} to {to_status}")
            updated = WorkItem(
                **{
                    **item.__dict__,
                    "status": to_status,
                    "assignee_id": assignee_id if assignee_id is not None else item.assignee_id,
                    "decision_maker": decision_maker if decision_maker is not None else item.decision_maker,
                    "updated_at": utcnow(),
                }
            )
            self.store.upsert_work_item(updated)
            self._record_work_event(updated, actor_id, "transition", item.status, to_status, detail)
            return updated
    
        def _record_work_event(
            self,
            item: WorkItem,
            actor_id: str,
            action: str,
            from_status: str | None,
            to_status: str | None,
            detail: str,
        ) -> None:
            self.store.create_work_event(
                WorkEvent(
                    id=new_id("wev"),
                    workspace_id=item.workspace_id,
                    work_item_id=item.id,
                    actor_id=actor_id,
                    action=action,
                    from_status=from_status,
                    to_status=to_status,
                    detail=detail,
                    recorded_at=utcnow(),
                )
            )
