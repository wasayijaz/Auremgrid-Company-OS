from __future__ import annotations

import unittest
from pathlib import Path

from auremgrid.connectors.bus import ConnectorBus
from auremgrid.connectors.simulated import SimulatedWorkspaceConnector
from auremgrid.services.brain import CompanyOS


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class IngestionBusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)

    def tearDown(self) -> None:
        self.os.close()

    def test_simulated_connectors_ingest_into_same_evidence_layer(self) -> None:
        bus = ConnectorBus(self.os, "act_alpha_admin")
        bus.register(SimulatedWorkspaceConnector.slack("ws_alpha"))
        bus.register(SimulatedWorkspaceConnector.drive("ws_alpha"))
        bus.register(SimulatedWorkspaceConnector.clickup("ws_alpha"))
        bus.register(SimulatedWorkspaceConnector.figma("ws_alpha"))
        results = bus.sync()
        self.assertTrue(any(result.created for result in results))
        brief = self.os.account_brief("ws_alpha", "act_alpha_operator", query="Current consult")
        self.assertFalse(brief.evidence["unknown"])
        neighbors = self.os.neighbors("ws_alpha", "act_alpha_operator", "Consultation landing page")
        self.assertTrue(neighbors["relations"])

    def test_hybrid_search_exposes_retrieval_channels(self) -> None:
        bundle = self.os.search("ws_alpha", "act_alpha_operator", "consultation price")
        self.assertFalse(bundle.unknown)
        channels = {channel for item in bundle.items for channel in item.payload.get("channels", [])}
        self.assertTrue({"keyword", "graph"} & channels)

    def test_connector_events_cannot_cross_workspaces(self) -> None:
        bus = ConnectorBus(self.os, "act_beta_admin")
        bus.register(SimulatedWorkspaceConnector.drive("ws_beta"))
        bus.sync()
        leaked = self.os.search("ws_beta", "act_beta_admin", "navy and cream")
        self.assertTrue(leaked.unknown)


if __name__ == "__main__":
    unittest.main()
