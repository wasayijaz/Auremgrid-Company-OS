from __future__ import annotations

from datetime import datetime, timezone

from auremgrid.connectors.bus import ConnectorEvent


class SimulatedWorkspaceConnector:
    """Offline stand-in for Slack / Drive / ClickUp / Figma until live credentials exist."""

    def __init__(self, workspace_id: str, system: str, events: list[ConnectorEvent] | None = None) -> None:
        self.workspace_id = workspace_id
        self.name = f"simulated-{system}"
        self._events = events or []

    def pull(self) -> list[ConnectorEvent]:
        return list(self._events)

    @classmethod
    def slack(cls, workspace_id: str) -> "SimulatedWorkspaceConnector":
        return cls(
            workspace_id,
            "slack",
            [
                ConnectorEvent(
                    workspace_id=workspace_id,
                    source_key="slack-status.md",
                    locator="simulated://slack/#status-update",
                    connector="simulated-slack",
                    observed_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
                    content=(
                        "# Slack status\n\n"
                        "META: valid_from=2026-04-12T00:00:00+00:00\n"
                        "FACT: Consultation landing page | status | shipped\n"
                        "REL: Channel Lead | requested | Consultation landing page\n"
                    ),
                )
            ],
        )

    @classmethod
    def drive(cls, workspace_id: str) -> "SimulatedWorkspaceConnector":
        return cls(
            workspace_id,
            "drive",
            [
                ConnectorEvent(
                    workspace_id=workspace_id,
                    source_key="drive-offer.md",
                    locator="simulated://drive/offers/consultation.md",
                    connector="simulated-drive",
                    observed_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                    content=(
                        "# Drive offer\n\n"
                        "META: valid_from=2026-04-01T00:00:00+00:00\n"
                        "FACT: Consultation | offer_name | Current consult\n"
                        "REL: Client Alpha | stores_assets_in | Shared Drive\n"
                    ),
                )
            ],
        )

    @classmethod
    def clickup(cls, workspace_id: str) -> "SimulatedWorkspaceConnector":
        return cls(
            workspace_id,
            "clickup",
            [
                ConnectorEvent(
                    workspace_id=workspace_id,
                    source_key="clickup-board.md",
                    locator="simulated://clickup/account-services",
                    connector="simulated-clickup",
                    observed_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
                    content=(
                        "# ClickUp board\n\n"
                        "META: valid_from=2026-04-13T00:00:00+00:00\n"
                        "FACT: Retargeting ad set | status | assigned\n"
                        "REL: Retargeting ad set | assigned_to | Alpha Operator\n"
                    ),
                )
            ],
        )

    @classmethod
    def figma(cls, workspace_id: str) -> "SimulatedWorkspaceConnector":
        return cls(
            workspace_id,
            "figma",
            [
                ConnectorEvent(
                    workspace_id=workspace_id,
                    source_key="figma-handoff.md",
                    locator="simulated://figma/consultation-page",
                    connector="simulated-figma",
                    observed_at=datetime(2026, 4, 11, tzinfo=timezone.utc),
                    content=(
                        "# Figma handoff\n\n"
                        "META: valid_from=2026-04-11T00:00:00+00:00\n"
                        "FACT: Consultation landing page | safe_zone | approved\n"
                        "REL: Consultation landing page | designed_in | Figma\n"
                    ),
                )
            ],
        )
