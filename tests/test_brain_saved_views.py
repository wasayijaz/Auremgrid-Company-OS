from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import LATEST_SCHEMA_VERSION, issue_identity


class BrainSavedViewsPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.os.create_organization("Org", "org_brain")
        self.os.create_organization_workspace("org_brain", "Alpha", "client", "ws_alpha")
        self.os.create_organization_workspace("org_brain", "Beta", "client", "ws_beta")
        self.owner = self.os.create_person("org_brain", "Owner", "owner@brain.test", role="owner", person_id="person_owner")
        self.viewer = self.os.create_person("org_brain", "Viewer", "viewer@brain.test", role="member", person_id="person_viewer")
        self.os.add_person_to_workspace("org_brain", "ws_alpha", self.owner.id, "admin")
        self.os.add_person_to_workspace("org_brain", "ws_beta", self.owner.id, "admin")
        self.os.add_person_to_workspace("org_brain", "ws_alpha", self.viewer.id, "viewer")
        self.alpha_actor = self.os.create_actor("ws_alpha", "Alpha Admin", "admin", "actor_alpha")
        self.viewer_actor = self.os.create_actor("ws_alpha", "Viewer Actor", "agent", "actor_viewer")
        self.beta_actor = self.os.create_actor("ws_beta", "Beta Admin", "admin", "actor_beta")
        _, self.identity = issue_identity(self.os, "org_brain", self.owner.id, "ws_alpha", self.alpha_actor.id)
        _, self.beta_identity = issue_identity(self.os, "org_brain", self.owner.id, "ws_beta", self.beta_actor.id)
        viewer_token, self.viewer_identity = issue_identity(self.os, "org_brain", self.viewer.id, "ws_alpha")
        self.os.store.conn.execute(
            "INSERT INTO principal_actor_bindings(principal_id,workspace_id,actor_id,created_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (self.viewer_identity.principal_id, "ws_alpha", self.viewer_actor.id),
        )
        self.os.store.conn.commit()
        self.viewer_identity = self.os.auth.authenticate_session(viewer_token, workspace_id="ws_alpha")
        self.public_ingest = self.os.ingest_text(
            "ws_alpha", self.alpha_actor.id, "public.md",
            "FACT: Prime | offer | public consultation", "memory://public",
        )
        self.hidden_actor = self.os.create_actor("ws_alpha", "Hidden Actor", "agent", "actor_hidden")
        self.hidden_ingest = self.os.ingest_text(
            "ws_alpha", self.alpha_actor.id, "hidden.md",
            "FACT: Prime | margin | secret", "memory://hidden",
            allowed_actor_ids=[self.hidden_actor.id],
        )
        self.beta_ingest = self.os.ingest_text(
            "ws_beta", self.beta_actor.id, "beta.md",
            "FACT: Beta | offer | separate", "memory://beta",
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_folder_hierarchy_is_workspace_scoped_and_validated(self) -> None:
        root = self.os.brain_ops.create_folder(self.identity, "ws_alpha", "Research")
        child = self.os.brain_ops.create_folder(self.identity, "ws_alpha", "Offers", parent_id=root["id"])
        self.assertEqual(child["parent_id"], root["id"])
        with self.assertRaises(NotFoundError):
            self.os.brain_ops.create_folder(self.beta_identity, "ws_beta", "Bad Parent", parent_id=root["id"])

    def test_tags_respect_source_and_document_acl(self) -> None:
        tag = self.os.brain_ops.create_tag(self.identity, "ws_alpha", "Offer", color="#2266aa")
        self.os.brain_ops.tag_source(self.identity, "ws_alpha", self.public_ingest.source.id, tag["id"])
        self.os.brain_ops.tag_source(self.identity, "ws_alpha", self.hidden_ingest.source.id, tag["id"])
        self.os.brain_ops.tag_document(self.identity, "ws_alpha", str(self.public_ingest.document_id), tag["id"])
        self.os.brain_ops.tag_document(self.identity, "ws_alpha", str(self.hidden_ingest.document_id), tag["id"])

        owner_sources = self.os.brain_ops.list_tagged_sources(self.identity, "ws_alpha", tag["id"])
        viewer_sources = self.os.brain_ops.list_tagged_sources(self.viewer_identity, "ws_alpha", tag["id"])
        self.assertEqual({row["id"] for row in owner_sources}, {self.public_ingest.source.id, self.hidden_ingest.source.id})
        self.assertEqual([row["id"] for row in viewer_sources], [self.public_ingest.source.id])

        viewer_documents = self.os.brain_ops.list_tagged_documents(self.viewer_identity, "ws_alpha", tag["id"])
        self.assertEqual([row["id"] for row in viewer_documents], [self.public_ingest.document_id])

    def test_collections_filter_items_by_underlying_acl_and_workspace(self) -> None:
        collection = self.os.brain_ops.create_collection(self.identity, "ws_alpha", "Evidence", visibility="shared")
        self.os.brain_ops.add_collection_item(self.identity, "ws_alpha", collection["id"], "source", self.public_ingest.source.id)
        self.os.brain_ops.add_collection_item(self.identity, "ws_alpha", collection["id"], "source", self.hidden_ingest.source.id)
        items = self.os.brain_ops.list_collection_items(self.viewer_identity, "ws_alpha", collection["id"])
        self.assertEqual([item["item_id"] for item in items], [self.public_ingest.source.id])
        with self.assertRaises(NotFoundError):
            self.os.brain_ops.add_collection_item(self.identity, "ws_alpha", collection["id"], "source", self.beta_ingest.source.id)

    def test_saved_views_persist_json_definition_versions_and_audit_without_results(self) -> None:
        folder = self.os.brain_ops.create_folder(self.identity, "ws_alpha", "Views")
        view = self.os.brain_ops.save_view(
            self.identity, "ws_alpha", "Current Offers",
            folder_id=folder["id"], visibility="owner",
            query={"text": "offer"}, filters={"tag_ids": ["tag_offer"]},
            sort=[{"field": "recorded_at", "direction": "desc"}],
            idempotency_key="view-create",
        )
        same = self.os.brain_ops.save_view(
            self.identity, "ws_alpha", "Current Offers",
            folder_id=folder["id"], visibility="owner",
            query={"text": "offer"}, filters={"tag_ids": ["tag_offer"]},
            sort=[{"field": "recorded_at", "direction": "desc"}],
            idempotency_key="view-create",
        )
        self.assertEqual(same["id"], view["id"])
        self.assertEqual(view["version"], 1)
        with self.assertRaises(ValidationError):
            self.os.brain_ops.save_view(
                self.identity, "ws_alpha", "Different",
                query={"text": "offer"}, idempotency_key="view-create",
            )
        with self.assertRaises(NotFoundError):
            self.os.brain_ops.get_view(self.viewer_identity, "ws_alpha", view["id"])

        updated = self.os.brain_ops.update_view(
            self.identity, "ws_alpha", view["id"], visibility="shared",
            filters={"tag_ids": ["tag_offer"], "source_kind": "document"},
            idempotency_key="view-update",
        )
        repeated = self.os.brain_ops.update_view(
            self.identity, "ws_alpha", view["id"], visibility="shared",
            filters={"tag_ids": ["ignored"]}, idempotency_key="view-update",
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(repeated["version"], 2)
        self.assertEqual(
            self.os.brain_ops.get_view(self.viewer_identity, "ws_alpha", view["id"])["filters"]["source_kind"],
            "document",
        )
        versions = self.os.brain_ops.view_versions(self.identity, "ws_alpha", view["id"])
        self.assertEqual([version["version"] for version in versions], [1, 2])
        self.assertEqual(versions[0]["filters"], {"tag_ids": ["tag_offer"]})
        audit_rows = self.os.store.conn.execute(
            "SELECT action,payload_json FROM brain_mutation_audit WHERE entity_type='saved_view' AND entity_id=? ORDER BY created_at",
            (view["id"],),
        ).fetchall()
        self.assertEqual([row["action"] for row in audit_rows], ["create", "update"])
        self.assertNotIn("public consultation", "".join(row["payload_json"] for row in audit_rows))

    def test_owner_and_workspace_isolation_for_saved_views(self) -> None:
        private = self.os.brain_ops.save_view(self.identity, "ws_alpha", "Private", query={"text": "alpha"})
        shared = self.os.brain_ops.save_view(self.identity, "ws_alpha", "Shared", query={"text": "alpha"}, visibility="shared")
        beta = self.os.brain_ops.save_view(self.beta_identity, "ws_beta", "Beta", query={"text": "beta"}, visibility="shared")
        self.assertEqual({item["id"] for item in self.os.brain_ops.list_views(self.viewer_identity, "ws_alpha")}, {shared["id"]})
        self.assertEqual({item["id"] for item in self.os.brain_ops.list_views(self.identity, "ws_alpha")}, {private["id"], shared["id"]})
        with self.assertRaises(NotFoundError):
            self.os.brain_ops.get_view(self.identity, "ws_alpha", beta["id"])
        with self.assertRaises(AuthorizationError):
            self.os.brain_ops.update_view(self.viewer_identity, "ws_alpha", shared["id"], name="Nope")

    def test_schema_27_replays_on_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brain.sqlite"
            first = CompanyOS(path)
            self.assertEqual(first.store.schema_version, LATEST_SCHEMA_VERSION)
            first.close()
            conn = sqlite3.connect(path)
            conn.execute("DELETE FROM schema_migrations WHERE version=27")
            conn.commit()
            conn.close()
            second = CompanyOS(path)
            try:
                self.assertEqual(second.store.schema_version, LATEST_SCHEMA_VERSION)
                row = second.store.conn.execute(
                    "SELECT name FROM schema_migrations WHERE version=27"
                ).fetchone()
                self.assertEqual(row["name"], "brain_workspace_persistence")
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
