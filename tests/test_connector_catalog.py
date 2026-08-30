from __future__ import annotations
import unittest
from auremgrid.connectors import ConfiguredConnector,TARGET_CONNECTORS,connector_catalog
from auremgrid.connectors.bus import ConnectorEvent

class ConnectorCatalogTests(unittest.TestCase):
    def test_every_target_connector_has_explicit_permissions_and_not_connected_truth(self):
        catalog=connector_catalog();names={item["source"] for item in catalog}
        self.assertTrue({"slack","google_drive","gmail","clickup","figma","github","fireflies","meta_ads","google_ads","stripe_accounting","crm"}<=names)
        self.assertNotIn("notion", names)
        self.assertTrue(all(item["status"]=="not_connected" and item["permission_scopes"] for item in catalog))

    def test_catalog_exposes_truthful_provider_boundaries(self):
        boundaries = {item["source"]: item["boundary_status"] for item in connector_catalog()}
        self.assertEqual(
            {source for source, status in boundaries.items() if status == "LIVE READ"},
            {"slack", "clickup", "google_drive", "gmail", "figma", "fireflies"},
        )
        self.assertEqual(
            {source for source, status in boundaries.items() if status == "IMPORT ONLY"},
            {"stripe_accounting", "meta_ads", "google_ads", "crm"},
        )
        self.assertEqual(boundaries["github"], "DISABLED")
    def test_unconfigured_connector_returns_no_fabricated_events(self):
        connector=ConfiguredConnector(TARGET_CONNECTORS[0],{"ws_alpha":"C1"});self.assertEqual(connector.status,"not_connected");self.assertEqual(connector.pull(),[])
    def test_connector_rejects_unmapped_workspace_events(self):
        def transport(cursor):return [ConnectorEvent("ws_beta","x","x","x","test")],"next"
        connector=ConfiguredConnector(TARGET_CONNECTORS[0],{"ws_alpha":"C1"},transport)
        with self.assertRaises(ValueError):connector.pull()

if __name__=="__main__":unittest.main()
