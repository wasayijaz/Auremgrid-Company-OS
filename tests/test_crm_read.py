from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.crm_read import CrmReadService


class CrmReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Path(self.tempdir.name) / "crm.json"
        self.storage.write_text(json.dumps({
            "clients": [
                {"organization_id": "org-1", "id": "client-b", "name": "Beta Studio"},
                {"organization_id": "org-1", "id": "client-a", "name": "Acme Co"},
                {"organization_id": "org-2", "id": "client-x", "name": "Other Org"},
            ],
            "contacts": [
                {"organization_id": "org-1", "client_id": "client-a", "id": "ct-2", "name": "Zara", "role": "CMO", "email": "zara@example.test"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "ct-1", "name": "Ari", "role": "Founder", "email": "ari@example.test"},
                {"organization_id": "org-2", "client_id": "client-x", "id": "ct-x", "name": "Other", "email": "other@example.test"},
            ],
            "interactions": [
                {"organization_id": "org-1", "client_id": "client-a", "id": "i-old", "occurred_at": "2026-08-30T12:00:00Z", "kind": "email", "summary": "Kickoff", "contact_id": "ct-1"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "i-new", "occurred_at": "2026-09-05T12:00:00Z", "kind": "call", "summary": "Weekly sync", "contact_id": "ct-2"},
                {"organization_id": "org-2", "client_id": "client-x", "id": "i-x", "occurred_at": "2026-09-06T12:00:00Z", "kind": "meeting", "summary": "Hidden"},
            ],
            "open_items": [
                {"organization_id": "org-1", "client_id": "client-a", "id": "todo-1", "status": "open"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "todo-2", "status": "closed"},
                {"organization_id": "org-1", "client_id": "client-b", "id": "todo-3"},
                {"organization_id": "org-2", "client_id": "client-x", "id": "todo-x", "status": "open"},
            ],
        }), encoding="utf-8")
        self.service = CrmReadService(
            self.storage,
            clock=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_list_clients_returns_org_scoped_health_summary(self) -> None:
        clients = self.service.list_clients("org-1")
        self.assertEqual([client["id"] for client in clients], ["client-a", "client-b"])
        self.assertEqual(clients[0]["health"], {
            "last_interaction_at": "2026-09-05T12:00:00Z",
            "last_interaction_age_days": 2,
            "open_items_count": 1,
        })
        self.assertEqual(clients[1]["health"], {
            "last_interaction_at": None,
            "last_interaction_age_days": None,
            "open_items_count": 1,
        })

    def test_contact_directory_is_sorted_and_client_org_fenced(self) -> None:
        contacts = self.service.contact_directory("org-1", "client-a")
        self.assertEqual([contact["name"] for contact in contacts], ["Ari", "Zara"])
        self.assertEqual({contact["organization_id"] for contact in contacts}, {"org-1"})
        with self.assertRaises(AuthorizationError):
            self.service.contact_directory("org-1", "client-x")

    def test_interaction_timeline_is_newest_first_and_org_scoped(self) -> None:
        timeline = self.service.interaction_timeline("org-1", "client-a")
        self.assertEqual([item["id"] for item in timeline], ["i-new", "i-old"])
        self.assertEqual(timeline[0]["summary"], "Weekly sync")
        self.assertEqual({item["organization_id"] for item in timeline}, {"org-1"})

    def test_read_validation_fails_closed_for_bad_storage_and_missing_client(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.list_clients("")
        with self.assertRaises(NotFoundError):
            self.service.interaction_timeline("org-1", "missing")
        self.storage.write_text(json.dumps({"clients": {}, "contacts": [], "interactions": [], "open_items": []}), encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.service.list_clients("org-1")

    def test_service_exposes_no_mutation_methods(self) -> None:
        public_methods = {name for name in dir(self.service) if not name.startswith("_") and callable(getattr(self.service, name))}
        self.assertFalse(public_methods & {"create_client", "update_client", "delete_client", "sync", "write"})


if __name__ == "__main__":
    unittest.main()
