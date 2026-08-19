from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import (
    Actor,
    AuditEvent,
    Citation,
    Document,
    EvidenceBundle,
    EvidenceItem,
    Fact,
    IngestResult,
    Memory,
    Relation,
    SourceArtifact,
    Workspace,
)
from auremgrid.domain.ops import (
    ALLOWED_TRANSITIONS,
    DEFINITION_OF_DONE,
    AccountBrief,
    ClientBrainPack,
    Playbook,
    StatusPost,
    Touchpoint,
    WorkEvent,
    WorkItem,
    default_dod,
)
from auremgrid.extract.deterministic import extract_claims
from auremgrid.storage.sqlite import SqliteStore
from auremgrid.storage.company import CompanyRepository
from auremgrid.domain.company import (
    Decision, Deliverable, Organization, OrganizationMembership, Person, Project,
    Review, ReviewComment, WorkspaceMembership,
)
from auremgrid.adapters.graphiti_local import LocalTemporalGraph
from auremgrid.adapters.hybrid import HybridRanker, RankedHit, cosine, hashed_embedding
from auremgrid.adapters.stack import OpenSourceStack
from auremgrid.services.client_ops import ClientOperations
from auremgrid.services.agency_ops import AgencyOperations
from auremgrid.services.agent_ops import AgentOperations
from auremgrid.services.brain_ops import BrainOperations
from auremgrid.services.dashboard import DashboardService
from auremgrid.services.work_ops import WorkOperations
from auremgrid.services.workflow_catalog import load_workflow_catalog
from auremgrid.services.workflow_ops import WorkflowOperations
from auremgrid.services.auth import AuthService
from auremgrid.services.job_ops import JobOperations
from auremgrid.services.secrets import EnvironmentSecretStore, SecretBindingService
from auremgrid.adapters.semantic import DeterministicFallbackEmbeddingProvider, LocalVectorIndex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


class CompanyOS:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.store = SqliteStore(db_path)
        self.company = CompanyRepository(self.store.conn)
        self.client_ops = ClientOperations(self.store.conn, new_id, self._require_person_access)
        self.agency_ops = AgencyOperations(self.store.conn, new_id, self._require_person_access, self.company)
        self.agent_ops = AgentOperations(self.store.conn, new_id, self.company, self.agency_ops, self.client_ops)
        self.graph = LocalTemporalGraph()
        self.ranker = HybridRanker()
        self._embeddings: dict[str, tuple[float, ...]] = {}
        self.embedding_provider = DeterministicFallbackEmbeddingProvider()
        self.vector_index = LocalVectorIndex()
        self.stack = OpenSourceStack()
        self.brain_ops = BrainOperations(self)
        self.dashboard = DashboardService(self)
        self.work_ops = WorkOperations(self.store,self.company,new_id,self._require_person_access)
        self.workflow_catalog = load_workflow_catalog()
        self.workflow_ops = WorkflowOperations(self.store.conn, new_id, self._require_person_access)
        self.auth = AuthService(self.store.conn, new_id)
        self.jobs = JobOperations(self.store.conn, new_id)
        self.secrets = SecretBindingService(self.store.conn, new_id, EnvironmentSecretStore())
        self.rebuild_projections()

    def close(self) -> None:
        self.store.close()

    def rebuild_projections(self, workspace_id: str | None = None) -> dict[str, Any]:
        """Rebuild every disposable local projection from the canonical SQLite ledger."""
        self.graph = LocalTemporalGraph()
        self.stack = OpenSourceStack()
        self._embeddings.clear()
        self.vector_index = LocalVectorIndex()
        workspaces = self.store.conn.execute(
            "SELECT id FROM workspaces" + (" WHERE id=?" if workspace_id else ""),
            (workspace_id,) if workspace_id else (),
        ).fetchall()
        total_documents = total_facts = 0
        for ws_row in workspaces:
            ws = ws_row["id"]
            documents = self.store.conn.execute("SELECT * FROM documents WHERE workspace_id=?", (ws,)).fetchall()
            for row in documents:
                document = self.store._document_from_row(row)
                self.graph.upsert_episode(ws, document.source_id, document.content, document.observed_at.isoformat())
                self.stack.ingest_document(document, document.content, document.observed_at)
                vector = self.embedding_provider.embed([document.content])[0]
                self._embeddings[document.id] = vector
                self.vector_index.upsert(ws, document.id, vector)
            source_ids = [row["id"] for row in self.store.conn.execute("SELECT id FROM sources WHERE workspace_id=?", (ws,)).fetchall()]
            facts = self.store.list_facts(ws, source_ids, include_superseded=True) if source_ids else []
            for fact in facts: self.stack.ingest_fact(fact)
            memories = self.store.conn.execute("SELECT * FROM memories WHERE workspace_id=?",(ws,)).fetchall()
            for memory in memories: self.stack.remember(ws,memory["actor_id"],memory["content"],memory["kind"])
            self.store.conn.execute("""INSERT INTO projection_state VALUES ('local_projections',?,?,?,?,?,?)
                ON CONFLICT(name,workspace_id) DO UPDATE SET status=excluded.status,document_count=excluded.document_count,
                fact_count=excluded.fact_count,last_rebuilt_at=excluded.last_rebuilt_at,last_error=NULL""",
                (ws,"healthy",len(documents),len(facts),utcnow().isoformat(),None))
            total_documents += len(documents); total_facts += len(facts)
        self.store.conn.commit()
        return {"status":"healthy","workspaces":len(workspaces),"documents":total_documents,"facts":total_facts,
            "embedding_provider":self.embedding_provider.name}

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
        if not name.strip() or role not in {"owner", "admin", "member"}:
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
        if role not in {"admin", "operator", "viewer"}:
            raise ValidationError("unsupported workspace role")
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
        now=utcnow().isoformat();item={"id":new_id("initiative"),"organization_id":organization_id,"workspace_id":workspace_id,
            "project_id":project_id,"name":name,"description":description,"status":"planned","owner_person_id":person_id,"created_at":now,"updated_at":now}
        self.store.conn.execute("INSERT INTO initiatives VALUES (?,?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.store.conn.commit();return item

    def list_projects(self, organization_id: str, workspace_id: str, person_id: str) -> list[Project]:
        self._require_person_access(organization_id, workspace_id, person_id)
        return self.company.list_projects(workspace_id)

    def create_deliverable(self, organization_id: str, workspace_id: str, person_id: str, project_id: str,
        title: str, type: str, work_item_id: str | None = None) -> Deliverable:
        self._require_person_access(organization_id, workspace_id, person_id, write=True)
        if self.company.get_project(workspace_id, project_id) is None:
            raise NotFoundError("project not found")
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
        self._require_person_access(organization_id, workspace_id, person_id, write=True)
        review = self.company.get_review(workspace_id, review_id)
        if review is None:
            raise NotFoundError("review not found")
        if review.status != "open" or decision not in {"approved", "revision_requested", "rejected"}:
            raise ValidationError("open review and valid decision are required")
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
        if write and membership.role == "viewer":
            raise AuthorizationError("person cannot write workspace")
        return membership

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

    def ingest_text(
        self,
        workspace_id: str,
        actor_id: str,
        source_key: str,
        content: str,
        locator: str,
        allowed_actor_ids: list[str] | None = None,
        observed_at: datetime | None = None,
        media_type: str = "text/markdown",
        trust_level: str = "internal",
    ) -> IngestResult:
        actor = self._require_actor(workspace_id, actor_id)
        if not actor.can_write:
            self._audit(workspace_id, actor_id, "ingest", source_key, "denied", "read-only actor")
            raise AuthorizationError("actor cannot ingest sources")
        if not source_key.strip():
            raise ValidationError("source_key is required")
        observed = observed_at or utcnow()
        digest = content_hash(content)
        existing = self.store.find_source(workspace_id, source_key, digest)
        if existing:
            self._audit(workspace_id, actor_id, "ingest", source_key, "noop", "identical content hash")
            return IngestResult(
                created=False,
                source=existing,
                document_id=None,
                message="idempotent no-op",
            )
        latest = self.store.latest_source(workspace_id, source_key)
        version = 1 if latest is None else latest.version + 1
        source = SourceArtifact(
            id=new_id("src"),
            workspace_id=workspace_id,
            source_key=source_key,
            locator=locator,
            content_hash=digest,
            media_type=media_type,
            trust_level=trust_level,
            allowed_actor_ids=tuple(allowed_actor_ids or ()),
            observed_at=observed,
            recorded_at=utcnow(),
            version=version,
        )
        self.store.create_source(source)
        document = Document(
            id=new_id("doc"),
            workspace_id=workspace_id,
            source_id=source.id,
            content=content,
            content_hash=digest,
            observed_at=observed,
            recorded_at=utcnow(),
        )
        self.store.create_document(document)
        extraction = extract_claims(content, observed)
        self.graph.upsert_episode(
            workspace_id,
            source.id,
            content,
            observed.isoformat(),
        )
        vector = self.embedding_provider.embed([content])[0]
        self._embeddings[document.id] = vector
        self.vector_index.upsert(workspace_id, document.id, vector)
        self.stack.ingest_document(document, content, observed)
        fact_ids: list[str] = []
        relation_ids: list[str] = []
        for extracted in extraction.facts:
            fact = Fact(
                id=new_id("fact"),
                workspace_id=workspace_id,
                source_id=source.id,
                document_id=document.id,
                subject=extracted.subject,
                predicate=extracted.predicate,
                object=extracted.object,
                valid_from=extracted.valid_from,
                valid_until=extracted.valid_until,
                observed_at=observed,
                recorded_at=utcnow(),
                confidence=extracted.confidence,
                superseded_by=None,
                conflict_group=extracted.conflict_group,
                citation=Citation(
                    source_id=source.id,
                    source_key=source.source_key,
                    locator=source.locator,
                    content_hash=source.content_hash,
                    evidence_span=extracted.evidence_span,
                    observed_at=observed,
                    valid_from=extracted.valid_from,
                    valid_until=extracted.valid_until,
                    confidence=extracted.confidence,
                ),
            )
            self._supersede_matching(actor, fact)
            self.store.create_fact(fact)
            fact_ids.append(fact.id)
            self.stack.ingest_fact(fact)
        for extracted in extraction.relations:
            relation = Relation(
                id=new_id("rel"),
                workspace_id=workspace_id,
                source_id=source.id,
                document_id=document.id,
                from_entity=extracted.from_entity,
                relation=extracted.relation,
                to_entity=extracted.to_entity,
                valid_from=extracted.valid_from,
                valid_until=extracted.valid_until,
                observed_at=observed,
                recorded_at=utcnow(),
                confidence=extracted.confidence,
                citation=Citation(
                    source_id=source.id,
                    source_key=source.source_key,
                    locator=source.locator,
                    content_hash=source.content_hash,
                    evidence_span=extracted.evidence_span,
                    observed_at=observed,
                    valid_from=extracted.valid_from,
                    valid_until=extracted.valid_until,
                    confidence=extracted.confidence,
                ),
            )
            self.store.create_relation(relation)
            relation_ids.append(relation.id)
        self._audit(
            workspace_id,
            actor_id,
            "ingest",
            source_key,
            "created",
            f"facts={len(fact_ids)} relations={len(relation_ids)}",
        )
        return IngestResult(
            created=True,
            source=source,
            document_id=document.id,
            fact_ids=tuple(fact_ids),
            relation_ids=tuple(relation_ids),
            message="ingested",
        )

    def ingest_path(
        self,
        workspace_id: str,
        actor_id: str,
        path: str | Path,
        source_key: str | None = None,
        allowed_actor_ids: list[str] | None = None,
        observed_at: datetime | None = None,
    ) -> IngestResult:
        file_path = Path(path)
        content = file_path.read_text(encoding="utf-8")
        return self.ingest_text(
            workspace_id=workspace_id,
            actor_id=actor_id,
            source_key=source_key or file_path.name,
            content=content,
            locator=str(file_path),
            allowed_actor_ids=allowed_actor_ids,
            observed_at=observed_at,
        )

    def search(
        self,
        workspace_id: str,
        actor_id: str,
        query: str,
        as_of: datetime | None = None,
        limit: int = 8,
    ) -> EvidenceBundle:
        actor = self._require_actor(workspace_id, actor_id)
        as_of = as_of or utcnow()
        sources = self.store.allowed_sources(workspace_id, actor)
        source_ids = [source.id for source in sources]
        if not query.strip():
            raise ValidationError("query is required")
        query_norm = normalize_text(query)
        query_embedding = hashed_embedding(query)
        fused_hits: list[RankedHit] = []
        documents_by_id: dict[str, tuple[Document, SourceArtifact]] = {}
        facts_by_id: dict[str, Fact] = {}
        for document, source, score in self.store.search_documents(workspace_id, source_ids, query, limit):
            documents_by_id[document.id] = (document, source)
            fused_hits.append(RankedHit("document", document.id, score, ("keyword",)))
            embedding = self._embeddings.get(document.id) or hashed_embedding(document.content)
            vector_score = cosine(query_embedding, embedding)
            if vector_score > 0:
                fused_hits.append(RankedHit("document", document.id, vector_score, ("vector",)))
        for fact in self.store.list_facts(workspace_id, source_ids, as_of=as_of, include_superseded=True):
            haystack = normalize_text(f"{fact.subject} {fact.predicate} {fact.object}")
            keyword_hit = _token_overlap(query_norm, haystack)
            graph_boost = self.graph.related_fact_boost(fact, query)
            if not keyword_hit and graph_boost <= 0:
                continue
            facts_by_id[fact.id] = fact
            score = (0.7 + (0.3 * fact.confidence)) if keyword_hit else 0.0
            if fact.superseded_by:
                score -= 0.4
            if keyword_hit:
                fused_hits.append(RankedHit("fact", fact.id, score, ("keyword",)))
            if graph_boost:
                fused_hits.append(RankedHit("fact", fact.id, graph_boost, ("graph",)))
        items: list[EvidenceItem] = []
        for hit in self.ranker.fuse(fused_hits, limit=limit):
            if hit.kind == "document":
                document, source = documents_by_id[hit.key]
                items.append(
                    EvidenceItem(
                        kind="document",
                        score=round(hit.score, 4),
                        payload={
                            "document_id": document.id,
                            "source_key": source.source_key,
                            "channels": list(hit.channels),
                        },
                        citation=Citation(
                            source_id=source.id,
                            source_key=source.source_key,
                            locator=source.locator,
                            content_hash=source.content_hash,
                            evidence_span=_best_span(document.content, query),
                            observed_at=document.observed_at,
                        ),
                    )
                )
            else:
                fact = facts_by_id[hit.key]
                payload = fact.to_dict()
                payload["channels"] = list(hit.channels)
                items.append(
                    EvidenceItem(
                        kind="fact",
                        score=round(hit.score, 4),
                        payload=payload,
                        citation=fact.citation,
                    )
                )
        bounded = tuple(items[:limit])
        unknown = len(bounded) == 0
        message = "insufficient evidence" if unknown else "evidence retrieved"
        self._audit(workspace_id, actor_id, "search", query, "ok" if not unknown else "unknown", message)
        return EvidenceBundle(
            workspace_id=workspace_id,
            query=query,
            as_of=as_of,
            unknown=unknown,
            message=message,
            items=bounded,
        )

    def entity(
        self,
        workspace_id: str,
        actor_id: str,
        name: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        actor = self._require_actor(workspace_id, actor_id)
        as_of = as_of or utcnow()
        source_ids = [source.id for source in self.store.allowed_sources(workspace_id, actor)]
        target = normalize_text(name)
        facts = [
            fact
            for fact in self.store.list_facts(workspace_id, source_ids, as_of=as_of)
            if target in {normalize_text(fact.subject), normalize_text(fact.object)}
        ]
        relations = [
            relation
            for relation in self.store.list_relations(workspace_id, source_ids, as_of=as_of)
            if target in {normalize_text(relation.from_entity), normalize_text(relation.to_entity)}
        ]
        self._audit(workspace_id, actor_id, "entity", name, "ok", f"facts={len(facts)}")
        return {
            "entity": name,
            "as_of": as_of.isoformat(),
            "facts": [fact.to_dict() for fact in facts],
            "relations": [relation.to_dict() for relation in relations],
        }

    def history(
        self,
        workspace_id: str,
        actor_id: str,
        subject: str,
        predicate: str | None = None,
    ) -> dict[str, Any]:
        actor = self._require_actor(workspace_id, actor_id)
        source_ids = [source.id for source in self.store.allowed_sources(workspace_id, actor)]
        subject_norm = normalize_text(subject)
        facts = []
        for fact in self.store.list_facts(workspace_id, source_ids, include_superseded=True):
            if normalize_text(fact.subject) != subject_norm:
                continue
            if predicate and normalize_text(fact.predicate) != normalize_text(predicate):
                continue
            facts.append(fact)
        self._audit(workspace_id, actor_id, "history", subject, "ok", f"versions={len(facts)}")
        return {
            "subject": subject,
            "predicate": predicate,
            "facts": [fact.to_dict() for fact in facts],
        }

    def neighbors(
        self,
        workspace_id: str,
        actor_id: str,
        entity: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        actor = self._require_actor(workspace_id, actor_id)
        as_of = as_of or utcnow()
        source_ids = [source.id for source in self.store.allowed_sources(workspace_id, actor)]
        target = normalize_text(entity)
        relations = [
            relation
            for relation in self.store.list_relations(workspace_id, source_ids, as_of=as_of)
            if target in {normalize_text(relation.from_entity), normalize_text(relation.to_entity)}
        ]
        self._audit(workspace_id, actor_id, "neighbors", entity, "ok", f"edges={len(relations)}")
        return {"entity": entity, "relations": [relation.to_dict() for relation in relations]}

    def sources(self, workspace_id: str, actor_id: str) -> dict[str, Any]:
        actor = self._require_actor(workspace_id, actor_id)
        sources = self.store.allowed_sources(workspace_id, actor)
        self._audit(workspace_id, actor_id, "sources", workspace_id, "ok", f"count={len(sources)}")
        return {"sources": [source.to_dict() for source in sources]}

    def recent(self, workspace_id: str, actor_id: str, limit: int = 5) -> dict[str, Any]:
        actor = self._require_actor(workspace_id, actor_id)
        source_ids = [source.id for source in self.store.allowed_sources(workspace_id, actor)]
        rows = self.store.list_recent_documents(workspace_id, source_ids, limit)
        self._audit(workspace_id, actor_id, "recent", workspace_id, "ok", f"count={len(rows)}")
        return {
            "documents": [
                {
                    "document": document.to_dict(),
                    "source": source.to_dict(),
                }
                for document, source in rows
            ]
        }

    def remember(
        self,
        workspace_id: str,
        actor_id: str,
        content: str,
        kind: str = "preference",
    ) -> Memory:
        actor = self._require_actor(workspace_id, actor_id)
        if not actor.can_write:
            self._audit(workspace_id, actor_id, "remember", kind, "denied", "read-only actor")
            raise AuthorizationError("actor cannot write memory")
        memory = Memory(
            id=new_id("mem"),
            workspace_id=workspace_id,
            actor_id=actor.id,
            kind=kind,
            content=content,
            observed_at=utcnow(),
            recorded_at=utcnow(),
        )
        self.store.create_memory(memory)
        self.stack.remember(workspace_id, actor.id, content, kind)
        self._audit(workspace_id, actor_id, "remember", kind, "created", content[:120])
        return memory

    def memories(self, workspace_id: str, actor_id: str) -> list[Memory]:
        actor = self._require_actor(workspace_id, actor_id)
        return self.store.list_memories(workspace_id, actor.id)

    def audit_log(self, workspace_id: str, actor_id: str) -> list[AuditEvent]:
        actor = self._require_actor(workspace_id, actor_id)
        if not actor.is_admin:
            raise AuthorizationError("only admins can read the audit log")
        return self.store.list_audit(workspace_id)

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

    def onboard_agency(
        self,
        agency_name: str,
        workspace_id: str,
        admin_name: str,
        operator_name: str | None = None,
        source_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        workspace = self.create_workspace(agency_name, workspace_id=workspace_id)
        admin = self.create_actor(workspace.id, admin_name, "admin", f"act_{workspace.id}_admin")
        operator = self.create_actor(
            workspace.id,
            operator_name or f"{agency_name} Operator",
            "operator",
            f"act_{workspace.id}_operator",
        )
        agent = self.create_actor(workspace.id, f"{agency_name} Agent", "agent", f"act_{workspace.id}_agent")
        self.stack.bind_agent(workspace.id, agent.id)
        ingested = 0
        if source_dir:
            root = Path(source_dir)
            for path in sorted(root.glob("*.md")):
                self.ingest_path(workspace.id, admin.id, path)
                ingested += 1
        self.upsert_client_brain(
            workspace.id,
            admin.id,
            snapshot=f"{agency_name} workspace. Fill this brain before starting client work.",
            brand_rules="Add approved visual and voice rules here.",
            dos=["Cite current approved facts", "Capture work before producing"],
            donts=["Do not invent prices or brand rules"],
            open_loops=["Complete the first client brain"],
        )
        return {
            "workspace": workspace.to_dict(),
            "admin": admin.to_dict(),
            "operator": operator.to_dict(),
            "agent": agent.to_dict(),
            "ingested_sources": ingested,
            "engines": [item["name"] for item in self.stack.contributions(workspace.id, agency_name, agent.id)],
        }

    def engine_status(self, workspace_id: str, actor_id: str, query: str) -> dict[str, Any]:
        self._require_actor(workspace_id, actor_id)
        self.stack.bind_agent(workspace_id, actor_id)
        return {"workspace_id": workspace_id, "query": query, "engines": self.stack.contributions(workspace_id, query, actor_id)}

    def sync_connectors(self, actor_id: str, include_simulated: bool = False) -> list[IngestResult]:
        from auremgrid.connectors.bus import ConnectorBus
        from auremgrid.connectors.local import LocalMarkdownConnector
        from auremgrid.connectors.simulated import SimulatedWorkspaceConnector

        bus = ConnectorBus(self, actor_id)
        root = Path(__file__).resolve().parents[3] / "fixtures"
        if (root / "client_alpha").exists():
            bus.register(LocalMarkdownConnector("ws_alpha", root / "client_alpha"))
        if include_simulated:
            self._require_actor("ws_alpha", actor_id)
            bus.register(SimulatedWorkspaceConnector.slack("ws_alpha"))
            bus.register(SimulatedWorkspaceConnector.drive("ws_alpha"))
            bus.register(SimulatedWorkspaceConnector.clickup("ws_alpha"))
            bus.register(SimulatedWorkspaceConnector.figma("ws_alpha"))
        return bus.sync()

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

    def seed_demo(self, fixtures_root: str | Path | None = None) -> dict[str, Any]:
        root = Path(fixtures_root or Path(__file__).resolve().parents[3] / "fixtures")
        organization = self.create_organization("Auremgrid Demo Agency", "org_demo")
        alpha = self.create_workspace("Client Alpha", workspace_id="ws_alpha")
        beta = self.create_workspace("Client Beta", workspace_id="ws_beta")
        if self.company.workspace_scope(alpha.id) is None:
            self.company.attach_workspace(organization.id, alpha.id, "client")
        if self.company.workspace_scope(beta.id) is None:
            self.company.attach_workspace(organization.id, beta.id, "client")
        owner = self.company.get_person(organization.id, "person_demo_owner")
        if owner is None:
            owner = self.create_person(organization.id, "Demo Owner", "owner@demo.invalid", role="owner", person_id="person_demo_owner")
        for workspace in (alpha, beta):
            if self.company.workspace_membership(workspace.id, owner.id) is None:
                self.add_person_to_workspace(organization.id, workspace.id, owner.id, "admin")
        if self.store.conn.execute("SELECT COUNT(*) FROM agents WHERE organization_id=?",(organization.id,)).fetchone()[0] == 0:
            seeded_agents=self.agent_ops.seed_primary_agents(organization.id,owner.id)
            for agent in seeded_agents:
                self.agent_ops.configure_agent(organization.id,owner.id,agent["id"],"unconfigured",
                    ["brain.search","work.list","projects.list"],[alpha.id,beta.id],json.loads(agent["write_permissions"]))
        alpha_admin = self.create_actor(alpha.id, "Alpha Admin", "admin", "act_alpha_admin")
        alpha_operator = self.create_actor(alpha.id, "Alpha Operator", "operator", "act_alpha_operator")
        alpha_agent = self.create_actor(alpha.id, "Alpha Agent", "agent", "act_alpha_agent")
        beta_admin = self.create_actor(beta.id, "Beta Admin", "admin", "act_beta_admin")
        for path in sorted((root / "client_alpha").glob("*.md")):
            allowed = ["act_alpha_admin"] if "restricted" in path.name else None
            self.ingest_path(alpha.id, alpha_admin.id, path, allowed_actor_ids=allowed)
        for path in sorted((root / "client_beta").glob("*.md")):
            self.ingest_path(beta.id, beta_admin.id, path)
        self.upsert_playbook(
            alpha_admin.id,
            "ads",
            "Ads playbook",
            "Instrument first. Do not launch claims without a current approved fact and a named decision-maker.",
        )
        self.upsert_playbook(
            alpha_admin.id,
            "landing-pages",
            "Landing page playbook",
            "Read the client brain, then apply the current offer, visual rules, and Definition of Done before review.",
        )
        self.upsert_client_brain(
            alpha.id,
            alpha_admin.id,
            snapshot="Clinic-services retainer. Success is booked consultations, not vanity reach.",
            brand_rules="Navy and cream only. Calm, clinical tone. No gradients.",
            landing_pages="Lead with the current consultation offer and keep pricing claims citation-backed.",
            ads="Local-service and search first. Healthcare claims require current approved copy.",
            design="Export to shared assets and keep creative inside the safe zone.",
            email="Lifecycle copy stays clinical and specific.",
            dos=["Cite the current consultation price", "Name a decision-maker on every revision loop"],
            donts=["Do not invent pricing", "Do not skip Definition of Done"],
            open_loops=["Consultation landing page needs a current price pass"],
        )
        self.upsert_client_brain(
            beta.id,
            beta_admin.id,
            snapshot="Fitness studio retainer. Success is intro-week conversions.",
            brand_rules="Charcoal and lime. Energetic, short copy.",
            ads="Always pair the intro week with a clear next step.",
            dos=["Keep the intro-week offer current"],
            donts=["Do not reuse clinic visual rules"],
            open_loops=["Intro-week creative needs review"],
        )
        work = self.capture_work(
            alpha.id,
            alpha_admin.id,
            title="Consultation landing page",
            request="Update the consultation landing page to the current approved offer.",
            requested_by="Channel Lead",
            needed_by="2026-04-10",
            playbook_id="landing-pages",
            decision_maker="Alpha Operator",
            work_item_id="work_demo_consultation_page",
        )
        if work.status != "shipped":
            work = self.assign_work(alpha.id, alpha_admin.id, work.id, alpha_operator.id)
            work = self.start_work(alpha.id, alpha_operator.id, work.id)
            self.mark_dod(
                alpha.id,
                alpha_operator.id,
                work.id,
                {
                    "mobile_responsive": True,
                    "assets_exported": True,
                    "creative_safe_zone": True,
                    "copy_spellchecked": True,
                    "handoff_notes": True,
                },
            )
            work = self.submit_review(alpha.id, alpha_operator.id, work.id)
            work = self.close_review(alpha.id, alpha_admin.id, work.id, approved=True, note="Internal review closed")
            self.ship_work(alpha.id, alpha_admin.id, work.id, note="Shipped current consultation page")
        self.record_touchpoint(
            alpha.id,
            alpha_admin.id,
            "Shared the shipped consultation page and confirmed the current price.",
            occurred_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
            touchpoint_id="tp_demo_consultation_shipped",
        )
        open_work = self.capture_work(
            alpha.id,
            alpha_admin.id,
            title="Retargeting ad set",
            request="Build a retargeting set for people who viewed the consultation page.",
            requested_by="Channel Lead",
            needed_by="2026-04-20",
            playbook_id="ads",
            decision_maker="Alpha Operator",
            work_item_id="work_demo_retargeting_ads",
        )
        if open_work.status == "captured":
            self.assign_work(alpha.id, alpha_admin.id, open_work.id, alpha_operator.id)
        for workspace in (alpha, beta):
            if self.store.conn.execute("SELECT COUNT(*) FROM client_health_snapshots WHERE workspace_id=?",(workspace.id,)).fetchone()[0] == 0:
                self.client_ops.calculate_health(organization.id,workspace.id,owner.id)
        return {
            "workspaces": [alpha.to_dict(), beta.to_dict()],
            "actors": [
                alpha_admin.to_dict(),
                alpha_operator.to_dict(),
                alpha_agent.to_dict(),
                beta_admin.to_dict(),
            ],
        }

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
            if incoming.conflict_group:
                continue
            if incoming.valid_from >= existing.valid_from:
                self.store.mark_fact_superseded(incoming.workspace_id, existing.id, incoming.id)

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

    def _audit(
        self,
        workspace_id: str,
        actor_id: str,
        action: str,
        target: str,
        outcome: str,
        detail: str,
    ) -> None:
        self.store.create_audit(
            AuditEvent(
                id=new_id("aud"),
                workspace_id=workspace_id,
                actor_id=actor_id,
                action=action,
                target=target,
                outcome=outcome,
                detail=detail,
                recorded_at=utcnow(),
            )
        )


STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "only",
}


def _token_overlap(query: str, haystack: str) -> bool:
    query_tokens = {
        token for token in query.split() if len(token) > 2 and token not in STOPWORDS
    }
    if not query_tokens:
        return query in haystack
    return any(token in haystack for token in query_tokens)


def _best_span(content: str, query: str) -> str:
    tokens = [token for token in normalize_text(query).split() if token]
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(token in lowered for token in tokens):
            return line[:240]
    return (lines[0] if lines else content)[:240]
