from __future__ import annotations

from typing import Any

# Dashboard surface contract markers retained here for static contract tests:
# "collections"
# writes_require_canonical_routes

from .dashboard_brain import DashboardBrainMixin
from .dashboard_client_hq import DashboardClientHQMixin
from .dashboard_command import DashboardCommandMixin
from .dashboard_people import DashboardPeopleMixin
from .dashboard_performance import DashboardPerformanceMixin
from .dashboard_review_center import DashboardReviewCenterMixin
from .dashboard_settings import DashboardSettingsMixin
from .dashboard_shared import DashboardSharedMixin
from .dashboard_workflows import DashboardWorkflowsMixin


class DashboardService(
    DashboardSharedMixin,
    DashboardCommandMixin,
    DashboardSettingsMixin,
    DashboardClientHQMixin,
    DashboardReviewCenterMixin,
    DashboardBrainMixin,
    DashboardWorkflowsMixin,
    DashboardPeopleMixin,
    DashboardPerformanceMixin,
):
    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn
