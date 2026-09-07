from __future__ import annotations
from auremgrid.services.brain_ops_shared import *

class BrainOpsKnowledgeStateMixin:
    def knowledge_health(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
            self.os._require_person_access(organization_id,workspace_id,person_id)
            issues=[]
            current=[]
            for fact in self.conn.execute("SELECT * FROM facts WHERE workspace_id=? AND superseded_by IS NULL",(workspace_id,)).fetchall():
                state=self._knowledge_state_row(workspace_id,"fact",fact["id"])
                if state is not None and state["state"] == "stale":
                    continue
                current.append(fact)
            grouped={}
            for fact in current:
                if fact["conflict_group"]:
                    grouped.setdefault(fact["conflict_group"],[]).append(fact)
            for group, rows in grouped.items():
                if len(rows)>1:
                    issues.append(("conflicting_facts","high","fact",group,f"{len(rows)} current facts conflict",group))
            low=[row for row in current if row["confidence"]<0.6]
            for row in low: issues.append(("low_confidence_fact","medium","fact",row["id"],f"Fact confidence is {row['confidence']}",row["id"]))
            decisions=self.conn.execute("SELECT id FROM decisions WHERE workspace_id=? AND source_id IS NULL AND evidence=''",(workspace_id,)).fetchall()
            for row in decisions: issues.append(("unsourced_decision","high","decision",row["id"],"Decision has no source or evidence",row["id"]))
            proposals=self.list_memory_proposals(organization_id,workspace_id,person_id)
            for row in proposals:
                if row["status"] == "pending":
                    issues.append(("pending_proposal","low","proposal",row["id"],"Proposal is waiting for review",row["id"]))
            self.conn.execute("UPDATE knowledge_health_issues SET status='resolved',resolved_at=? WHERE workspace_id=? AND status='open'",(_now().isoformat(),workspace_id))
            for type,severity,entity_type,entity_id,explanation,evidence in issues:
                self.conn.execute("INSERT INTO knowledge_health_issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(self._id("kh"),organization_id,workspace_id,type,severity,entity_type,entity_id,explanation,evidence,"open",_now().isoformat(),None))
            self.conn.commit(); projection_rows=self.conn.execute("SELECT * FROM projection_state WHERE workspace_id=?",(workspace_id,)).fetchall()
            if not projection_rows:
                self.os.rebuild_projections(workspace_id)
                projection_rows=self.conn.execute("SELECT * FROM projection_state WHERE workspace_id=?",(workspace_id,)).fetchall()
            projections=[dict(r) for r in projection_rows]
            return {"issues":[dict(r) for r in self.conn.execute("SELECT * FROM knowledge_health_issues WHERE workspace_id=? AND status='open' ORDER BY severity",(workspace_id,)).fetchall()],"projections":projections}

    def _state_event(self, organization_id: str, workspace_id: str | None, subject_type: str, subject_id: str, state: str, reason: str, evidence_source_id: str | None, actor_id: str, effective_from: datetime | None = None, effective_until: datetime | None = None) -> str:
            prior=self.conn.execute(
                "SELECT id,event_sequence,state FROM knowledge_state_events WHERE organization_id=? AND workspace_id IS ? "
                "AND subject_type=? AND subject_id=? ORDER BY event_sequence DESC LIMIT 1",
                (organization_id,workspace_id,subject_type,subject_id),
            ).fetchone()
            state = validate_knowledge_transition(None if prior is None else prior["state"], state)
            now=effective_from or datetime.now(timezone.utc); event_id=self._id("kstate")
            sequence=1 if prior is None else int(prior["event_sequence"])+1
            self.conn.execute("""INSERT INTO knowledge_state_events(
                id,organization_id,workspace_id,subject_type,subject_id,state,reason,evidence_source_id,actor_id,
                effective_from,effective_until,recorded_at,event_sequence,supersedes_event_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                event_id,organization_id,workspace_id,subject_type,subject_id,state,reason,evidence_source_id,actor_id,
                now.isoformat(),effective_until.isoformat() if effective_until else None,datetime.now(timezone.utc).isoformat(),
                sequence,None if prior is None else prior["id"],
            )); return event_id

    def record_knowledge_state(self, organization_id: str, workspace_id: str | None, subject_type: str, subject_id: str, state: str, reason: str, actor_id: str, evidence_source_id: str | None = None, effective_from: datetime | None = None, effective_until: datetime | None = None) -> dict[str, Any]:
            if not hasattr(actor_id,"person_id"):
                raise AuthorizationError("authenticated identity is required")
            actor_id=self._identity_person(organization_id,workspace_id,actor_id,"brain_promote")
            with self.os.store.atomic(immediate=True):
                event_id=self._state_event(organization_id,workspace_id,subject_type,subject_id,state,reason,evidence_source_id,actor_id,effective_from,effective_until)
            return dict(self.conn.execute("SELECT * FROM knowledge_state_events WHERE id=?",(event_id,)).fetchone())

    def derive_stale_states(self, organization_id: str, workspace_id: str, identity: Any,
            max_age_seconds: float, as_of: datetime | None = None,
            subject_type: str | None = None) -> list[dict[str, Any]]:
            """Append stale events for active states older than the deterministic cutoff."""
            actor_id = self._identity_person(organization_id, workspace_id, identity, "brain_promote")
            if max_age_seconds < 0:
                raise ValidationError("max_age_seconds must be non-negative")
            moment = as_of or datetime.now(timezone.utc)
            cutoff = moment.timestamp() - max_age_seconds
            rows = self.conn.execute(
                "SELECT e.* FROM knowledge_state_events e JOIN ("
                "SELECT subject_type,subject_id,MAX(event_sequence) AS sequence "
                "FROM knowledge_state_events WHERE organization_id=? AND workspace_id=? "
                "GROUP BY subject_type,subject_id) current "
                "ON current.subject_type=e.subject_type AND current.subject_id=e.subject_id "
                "AND current.sequence=e.event_sequence "
                "WHERE e.organization_id=? AND e.workspace_id=? AND e.state IN ('verified','high_confidence','inferred','proposed') "
                "AND e.effective_from<=? ORDER BY e.subject_type,e.subject_id",
                (organization_id, workspace_id, organization_id, workspace_id, moment.isoformat()),
            ).fetchall()
            if subject_type is not None:
                rows = [row for row in rows if row["subject_type"] == subject_type]
            stale: list[dict[str, Any]] = []
            with self.os.store.atomic(immediate=True):
                for row in rows:
                    effective = datetime.fromisoformat(row["effective_from"])
                    if effective.timestamp() > cutoff:
                        continue
                    event_id = self._state_event(
                        organization_id, workspace_id, row["subject_type"], row["subject_id"],
                        "stale", "deterministic freshness threshold exceeded", row["evidence_source_id"],
                        actor_id, effective_from=moment,
                    )
                    stale.append(dict(self.conn.execute("SELECT * FROM knowledge_state_events WHERE id=?", (event_id,)).fetchone()))
            return stale

    def mark_stale_if_older_than(self, organization_id: str, workspace_id: str, identity: Any,
            max_age_seconds: float, as_of: datetime | None = None,
            subject_type: str | None = None) -> list[dict[str, Any]]:
            """Compatibility name for callers that treat stale derivation as a write."""
            return self.derive_stale_states(
                organization_id, workspace_id, identity, max_age_seconds, as_of, subject_type,
            )

    def knowledge_state(self, organization_id: str, workspace_id: str, person_id: str, subject_type: str, subject_id: str, as_of: datetime | None = None) -> dict[str, Any]:
            self._authorize(organization_id,workspace_id,person_id,False)
            row=self._knowledge_state_row(workspace_id,subject_type,subject_id,as_of,organization_id)
            return dict(row) if row is not None else {"state":"unknown","subject_id":subject_id}

    def query_knowledge_state(self, organization_id: str, workspace_id: str, person_id: str,
            subject_type: str, subject_id: str, as_of: datetime | None = None) -> dict[str, Any]:
            return self.knowledge_state(organization_id, workspace_id, person_id, subject_type, subject_id, as_of)

    def transition_knowledge_state(self, organization_id: str, workspace_id: str | None,
            subject_type: str, subject_id: str, state: str, reason: str, actor_id: Any,
            evidence_source_id: str | None = None, effective_from: datetime | None = None,
            effective_until: datetime | None = None) -> dict[str, Any]:
            return self.record_knowledge_state(
                organization_id, workspace_id, subject_type, subject_id, state, reason,
                actor_id, evidence_source_id, effective_from, effective_until,
            )

    def _knowledge_state_row(self, workspace_id: str, subject_type: str, subject_id: str,
            as_of: datetime | None = None, organization_id: str | None = None) -> Any:
            moment=(as_of or datetime.now(timezone.utc)).isoformat()
            parameters: list[Any]=[workspace_id,subject_type,subject_id,moment,moment]
            organization_clause=""
            if organization_id is not None:
                organization_clause=" AND organization_id=?"
                parameters.append(organization_id)
            return self.conn.execute(f"""SELECT * FROM knowledge_state_events
                WHERE workspace_id=? AND subject_type=? AND subject_id=?
                  AND effective_from<=? AND (effective_until IS NULL OR effective_until>?)
                  {organization_clause}
                ORDER BY effective_from DESC,event_sequence DESC,recorded_at DESC,id DESC LIMIT 1""",parameters).fetchone()

    def resolve_fact_conflict(self, identity: Any, conflict_group: str, winner_fact_id: str) -> dict[str, Any]:
            if not hasattr(identity, "person_id"): raise AuthorizationError("authenticated identity is required")
            identity.require("brain_promote")
            rows=self.conn.execute(
                "SELECT * FROM facts WHERE workspace_id=? AND conflict_group=? AND superseded_by IS NULL",
                (identity.workspace_id,conflict_group),
            ).fetchall()
            if not rows or winner_fact_id not in {row["id"] for row in rows}: raise NotFoundError("conflict not found")
            try:
                actor_id=self.os.auth.actor_for_identity(identity,identity.workspace_id)
                actor=self.os._require_actor(identity.workspace_id,actor_id)
                allowed={source.id for source in self.os.store.allowed_sources(identity.workspace_id,actor)}
            except AuthorizationError as exc:
                membership=self.os.company.workspace_membership(identity.workspace_id,identity.person_id)
                if membership is None or membership.role != "admin":
                    raise NotFoundError("conflict not found") from exc
                allowed={str(row["id"]) for row in self.conn.execute("""SELECT DISTINCT s.id FROM sources s
                    JOIN source_lifecycle_intervals l ON l.source_id=s.id AND l.workspace_id=s.workspace_id
                    WHERE s.workspace_id=? AND l.retired_at IS NULL""",(identity.workspace_id,)).fetchall()}
            if any(row["source_id"] not in allowed for row in rows): raise NotFoundError("conflict not found")
            states={
                row["id"]: self._knowledge_state_row(identity.workspace_id,"fact",row["id"])
                for row in rows
            }
            resolved=[row["id"] for row in rows if states[row["id"]] is not None and states[row["id"]]["state"]=="verified"]
            stale=[row["id"] for row in rows if states[row["id"]] is not None and states[row["id"]]["state"]=="stale"]
            if len(resolved)==1 and len(stale)==len(rows)-1:
                if resolved[0] != winner_fact_id: raise ValidationError("conflict already resolved with a different winner")
                return {"conflict_group":conflict_group,"winner_fact_id":winner_fact_id,"resolved":True,"changed":False}
            with self.os.store.atomic(immediate=True):
                for row in rows:
                    self._state_event(identity.organization_id,identity.workspace_id,"fact",row["id"],"verified" if row["id"]==winner_fact_id else "stale","human conflict resolution",row["source_id"],identity.person_id)
            return {"conflict_group":conflict_group,"winner_fact_id":winner_fact_id,"resolved":True,"changed":True}
