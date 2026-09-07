from __future__ import annotations
from typing import Any, Callable
from auremgrid.services.workflow_ops_shared import WorkflowOpsSharedMixin
from auremgrid.services.workflow_ops_runs import WorkflowOpsRunsMixin
from auremgrid.services.workflow_ops_template import WorkflowOpsTemplateMixin
from auremgrid.storage.workflows import WorkflowRepository

class WorkflowOperations(WorkflowOpsSharedMixin, WorkflowOpsRunsMixin, WorkflowOpsTemplateMixin):
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any] | None = None) -> None:
        self.conn = conn
        self.new_id = new_id
        self.authorize = authorize
        self.repo = WorkflowRepository(conn, new_id)
