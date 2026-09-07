"""Credential-backed, workspace-scoped connector synchronization."""

from __future__ import annotations

from typing import Any, Callable

from auremgrid.connectors.google_auth import ConnectorInboxRepository
from auremgrid.services.integration_ops_connection import IntegrationOpsConnectionMixin
from auremgrid.services.integration_ops_provider import IntegrationOpsProviderMixin
from auremgrid.services.integration_ops_shared import (
    CONFIGURABLE_SOURCES,
    GMAIL_READ_SCOPE,
    GOOGLE_DRIVE_READ_SCOPE,
    LIVE_SOURCES,
    IntegrationOpsSharedMixin,
)
from auremgrid.services.integration_ops_sync import IntegrationOpsSyncMixin


class IntegrationOperations(
    IntegrationOpsConnectionMixin,
    IntegrationOpsSyncMixin,
    IntegrationOpsProviderMixin,
    IntegrationOpsSharedMixin,
):
    """Own connection truth and process connector pages through the durable inbox."""

    def __init__(self, os: Any, connector_factory: Callable[..., Any] | None = None) -> None:
        self.os = os
        self.conn = os.store.conn
        self.inbox = ConnectorInboxRepository(self.conn, os.jobs.new_id)
        self.connector_factory = connector_factory
