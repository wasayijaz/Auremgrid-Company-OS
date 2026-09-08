from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import replace
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
from auremgrid.adapters.ports import GraphProjectionPort
from auremgrid.adapters.hybrid import HybridRanker, RankedHit
from auremgrid.adapters.stack import OpenSourceStack
from auremgrid.services.client_ops import ClientOperations
from auremgrid.services.agency_ops import AgencyOperations
from auremgrid.services.agent_ops import AgentOperations
from auremgrid.services.capacity import CapacityService
from auremgrid.services.brain_ops import BrainOperations
from auremgrid.services.brain_customization import BrainCustomizationService
from auremgrid.services.dashboard import DashboardService
from auremgrid.services.work_ops import WorkOperations
from auremgrid.services.workflow_catalog import load_workflow_catalog
from auremgrid.services.workflow_ops import WorkflowOperations
from auremgrid.services.auth import AuthService
from auremgrid.services.job_ops import JobOperations
from auremgrid.services.secrets import EnvironmentSecretStore, SecretBindingService
from auremgrid.services.integration_security import OAuthConnectorService, OutboundSendService
from auremgrid.services.provider_imports import ProviderImportService
from auremgrid.services.scheduler import DurableScheduler
from auremgrid.services.integration_ops import IntegrationOperations
from auremgrid.services.client_portal import ClientPortalOperations
from auremgrid.services.feedback_ops import FeedbackOperations
from auremgrid.services.understanding_ops import UnderstandingService
from auremgrid.services.attribution_ops import AttributionService
from auremgrid.services.performance_ops import PerformanceOperations
from auremgrid.services.forecast_ops import ForecastOperations
from auremgrid.services.revenue_ops import RevenueOperations
from auremgrid.services.retention_ops import RetentionOperations
from auremgrid.services.intelligence_service import IntelligenceService
from auremgrid.services.intelligence_contracts import IntelligenceContractService
from auremgrid.services.intelligence_learning import IntelligenceLearningService
from auremgrid.services.intelligence_orchestrator import IntelligenceOrchestrator
from auremgrid.services.intelligence_evaluation_safety import IntelligenceEvaluationSafety
from auremgrid.services.proactive_intelligence import ProactiveIntelligenceService
from auremgrid.services.onboarding import OnboardingService
from auremgrid.services.report_delivery import ReportDeliveryService
from auremgrid.services.asset_recovery import AssetRecoveryService
from auremgrid.services.operator_readiness import OperatorReadinessService
from auremgrid.adapters.semantic import (
    DeterministicFallbackEmbeddingProvider,
    EmbeddingProvider,
    EmbeddingProviderError,
    SqliteVectorIndex,
)
from auremgrid.adapters.reasoning import (
    StrategicReasoningProvider,
    strategic_reasoning_provider_from_config,
)



from auremgrid.services.brain_shared import (
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_QUERY_CHARS,
    STOPWORDS,
    _best_span,
    _freshness_descriptor,
    _temporal_read_moment,
    _token_overlap,
    content_hash,
    new_id,
    normalize_text,
    utcnow,
)
from auremgrid.services.brain_annotations import BrainAnnotationsMixin
from auremgrid.services.brain_company import BrainCompanyMixin
from auremgrid.services.brain_ingestion import BrainIngestionMixin
from auremgrid.services.brain_operations import BrainOperationsMixin
from auremgrid.services.brain_projection import BrainProjectionMixin
from auremgrid.services.brain_work import BrainWorkMixin

AUREMGRID_OAUTH_REDIRECT_URIS = "AUREMGRID_OAUTH_REDIRECT_URIS"

# Extraction can establish a strong but still non-human claim.  Promotion and
# conflict resolution remain the only paths to ``verified``.


class CompanyOS(
    BrainAnnotationsMixin,
    BrainCompanyMixin,
    BrainIngestionMixin,
    BrainOperationsMixin,
    BrainProjectionMixin,
    BrainWorkMixin,
):

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        embedding_provider: EmbeddingProvider | None = None,
        vector_index: Any | None = None,
        graph_projection: GraphProjectionPort | None = None,
        strategic_reasoning_provider: StrategicReasoningProvider | None = None,
    ) -> None:
        if strategic_reasoning_provider is None:
            strategic_reasoning_provider = strategic_reasoning_provider_from_config()
        self.store = SqliteStore(db_path)
        self.company = CompanyRepository(self.store.conn)
        self.client_ops = ClientOperations(self.store.conn, new_id, self._require_person_access)
        self.agency_ops = AgencyOperations(self.store.conn, new_id, self._require_person_access, self.company)
        self.capacity = CapacityService(self.store.conn, self.company, self._require_person_access)
        self.agent_ops = AgentOperations(self.store.conn, new_id, self.company, self.agency_ops, self.client_ops, self.capacity)
        self.graph: GraphProjectionPort = graph_projection or LocalTemporalGraph()
        bind_store = getattr(self.graph, "bind_store", None)
        if bind_store is not None:
            bind_store(self.store)
        self.graph_health = self.graph.health()
        self.ranker = HybridRanker()
        self._embeddings: dict[str, tuple[float, ...]] = {}
        self.embedding_provider = embedding_provider or DeterministicFallbackEmbeddingProvider()
        self.vector_index = vector_index or SqliteVectorIndex(self.store, self.embedding_provider)
        self.embedding_health = self.embedding_provider.health().to_dict()
        # Deliberation is opt-in.  With no provider the deterministic
        # evidence review remains the complete, offline behavior.
        self.strategic_reasoning_provider = strategic_reasoning_provider
        self.stack = OpenSourceStack()
        self.brain_ops = BrainOperations(self)
        self.brain_customizations = BrainCustomizationService(self, new_id)
        self.dashboard = DashboardService(self)
        self.work_ops = WorkOperations(self.store,self.company,new_id,self._require_person_access)
        self.work_ops.company_os = self
        self.workflow_catalog = load_workflow_catalog()
        self.workflow_ops = WorkflowOperations(self.store.conn, new_id, self._require_person_access)
        self.auth = AuthService(self.store.conn, new_id)
        self.jobs = JobOperations(self.store.conn, new_id)
        self.secrets = SecretBindingService(self.store.conn, new_id, EnvironmentSecretStore())
        # OAuth vault is constructed lazily so manual env-backed installs remain
        # fully usable when no deployment encryption key is configured.
        self._oauth_service: OAuthConnectorService | None = None
        self.integrations = IntegrationOperations(self)
        self.provider_imports = ProviderImportService(self)
        self.client_portal = ClientPortalOperations(self.store, self.company, new_id)
        self.feedback = FeedbackOperations(
            self.store.conn,
            new_id,
            self._require_person_access,
            self.embedding_provider,
        )
        self.understanding = UnderstandingService(self.store.conn, new_id, self._require_scope_access)
        self.performance = PerformanceOperations(self.store.conn, new_id, self._require_person_access)
        self.forecasts = ForecastOperations(self.store.conn, new_id, self._require_scope_access)
        self.revenue = RevenueOperations(self.store.conn, new_id, self._require_person_access, self.company)
        self.retention = RetentionOperations(
            self.store.conn,
            new_id,
            self._require_scope_access,
            self._evict_deleted_documents_from_live_projections,
        )
        self.intelligence = IntelligenceService(self)
        self.intelligence_contracts = IntelligenceContractService(self)
        self.intelligence_contracts.seed_defaults()
        self.intelligence_learning = IntelligenceLearningService(self, new_id)
        self.attribution = AttributionService(self.store.conn, new_id, self._require_person_access, self.intelligence_learning)
        self.intelligence_orchestrator = IntelligenceOrchestrator(self)
        self.agent_ops.intelligence_orchestrator = self.intelligence_orchestrator
        self.agent_ops.os = self
        self.intelligence_evaluation_safety = IntelligenceEvaluationSafety(self)
        self.proactive_intelligence = ProactiveIntelligenceService(self)
        self.onboarding = OnboardingService(self, self.store.conn, new_id, self._require_person_access)
        self.report_delivery = ReportDeliveryService(self, new_id)
        self.asset_recovery = AssetRecoveryService(self.store.conn, new_id)
        self.outbound = OutboundSendService(self.store.conn, new_id, self.jobs)
        self.operator_readiness = OperatorReadinessService(self)
        self.rebuild_projections(rebuild_graph=graph_projection is None)
        if graph_projection is not None:
            self._restore_durable_graph_generations()

    def oauth_service(self) -> OAuthConnectorService:
        if self._oauth_service is None:
            redirect_uris = {
                item.strip()
                for item in os.environ.get(AUREMGRID_OAUTH_REDIRECT_URIS, "").split(",")
                if item.strip()
            }
            allowlist = {
                provider: set(redirect_uris)
                for provider in ("google", "slack", "figma", "github")
            } if redirect_uris else {}
            self._oauth_service = OAuthConnectorService(self.store.conn, new_id, None, allowlist)
        return self._oauth_service

    def scheduler(self, organization_id: str, workspace_id: str | None, worker_id: str, poll_seconds: float = 1.0) -> DurableScheduler:
        return DurableScheduler(self, organization_id, workspace_id, worker_id, poll_seconds)

    def close(self) -> None:
        try:
            close_graph = getattr(self.graph, "close", None)
            if close_graph is not None:
                close_graph()
        finally:
            self.store.close()

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
