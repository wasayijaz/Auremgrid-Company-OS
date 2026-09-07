"""Agent operations, execution, automations, and reporting coordinator."""
from __future__ import annotations

from typing import Any, Callable

from auremgrid.services.agent_ops_shared import (
    MAX_AGENT_DELEGATION_DEPTH,
    _action_operator_next_step,
    _json,
    _loads,
    _now,
    _optional_text,
    _stable_hash,
)
from auremgrid.services.agent_ops_config import AgentOpsConfigMixin
from auremgrid.services.agent_ops_execution import AgentOpsExecutionMixin
from auremgrid.services.agent_ops_automations import AgentOpsAutomationsMixin
from auremgrid.services.agent_ops_reporting import AgentOpsReportingMixin


class AgentOperations(
    AgentOpsConfigMixin,
    AgentOpsExecutionMixin,
    AgentOpsAutomationsMixin,
    AgentOpsReportingMixin,
):
    def __init__(self, conn: Any, new_id: Callable[[str], str], company: Any, approvals: Any, client_ops: Any, capacity: Any | None = None) -> None:
        self.conn, self.new_id, self.company, self.approvals, self.client_ops, self.capacity = conn, new_id, company, approvals, client_ops, capacity
        self.intelligence_orchestrator: Any | None = None
        self.os: Any | None = None
