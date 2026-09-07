from __future__ import annotations

"""Bounded, read-only orchestration over the native Intelligence projection."""

import threading
from typing import Any, Callable, Mapping

from auremgrid.services.intelligence_orchestrator_contracts import IntelligenceOrchestratorContractsMixin
from auremgrid.services.intelligence_orchestrator_retrieval import IntelligenceOrchestratorRetrievalMixin
from auremgrid.services.intelligence_orchestrator_runs import IntelligenceOrchestratorRunsMixin
from auremgrid.services.intelligence_orchestrator_shared import OrchestrationLimits, validate_expert_result
from auremgrid.services.intelligence_orchestrator_specialists import IntelligenceOrchestratorSpecialistsMixin
from auremgrid.services.intelligence_orchestrator_synthesis import IntelligenceOrchestratorSynthesisMixin


class IntelligenceOrchestrator(
    IntelligenceOrchestratorRunsMixin,
    IntelligenceOrchestratorSpecialistsMixin,
    IntelligenceOrchestratorSynthesisMixin,
    IntelligenceOrchestratorContractsMixin,
    IntelligenceOrchestratorRetrievalMixin,
):
    """Compose native Intelligence and immutable expert/runbook contracts."""

    _DOMAIN_EVIDENCE_KINDS = IntelligenceOrchestratorRetrievalMixin._DOMAIN_EVIDENCE_KINDS

    def __init__(
        self,
        os: Any,
        contracts: Any | None = None,
        *,
        limits: OrchestrationLimits | None = None,
        specialist_handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        specialist_provider: Any | Mapping[str, Any] | None = None,
    ) -> None:
        self.os = os
        self.contracts = contracts
        self.limits = limits or OrchestrationLimits()
        self.specialist_handlers = dict(specialist_handlers or {})
        self.specialist_provider = specialist_provider
        self._budget_lock = threading.Lock()
        self._run_budget: dict[str, Any] | None = None
