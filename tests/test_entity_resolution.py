from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.entity_resolution import EntityResolutionService


class EntityResolutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org-resolution")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client", "ws-resolution")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner", person_id="person-resolution")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.service = EntityResolutionService(self.os)
        self.scope = {"organization_id": self.org.id, "workspace_id": self.ws.id, "person_id": self.person.id}

    def tearDown(self) -> None:
        self.os.close()

    def test_alias_grouping_uses_canonical_and_domain_forms(self) -> None:
        entity = self.os.brain_ops.create_entity(
            self.org.id, self.ws.id, self.person.id, "Prime Clinics Canada", "company",
            ["Prime", "Prime Clinics", "primeclinics.ca"],
        )
        groups = self.service.group_aliases(self.scope, ["Prime", "Prime Clinics", "Prime Clinics Canada", "primeclinics.ca"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["entity_id"], entity["id"])
        self.assertEqual(set(groups[0]["matched_names"]), {"Prime", "Prime Clinics", "Prime Clinics Canada", "primeclinics.ca"})

    def test_merge_proposal_requires_evidence_and_records_provenance(self) -> None:
        source = self.os.brain_ops.create_entity(self.org.id, self.ws.id, self.person.id, "Prime", "company")
        target = self.os.brain_ops.create_entity(self.org.id, self.ws.id, self.person.id, "Prime Clinics Canada", "company")
        proposal = self.service.propose_merge(
            self.scope, source["id"], target["id"], evidence="same domain and billing account", confidence=0.96,
            evidence_refs={"sources": ["source-1"]},
        )
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["kind"], "merge")
        row = self.os.store.conn.execute("SELECT evidence,evidence_refs FROM entity_resolution_proposals WHERE id=?", (proposal["id"],)).fetchone()
        self.assertEqual(row["evidence"], "same domain and billing account")
        self.assertIn("source-1", row["evidence_refs"])

    def test_uncertain_matches_are_not_proposed(self) -> None:
        source = self.os.brain_ops.create_entity(self.org.id, self.ws.id, self.person.id, "Prime", "company")
        target = self.os.brain_ops.create_entity(self.org.id, self.ws.id, self.person.id, "Prime Clinics Canada", "company")
        with self.assertRaises(ValidationError):
            self.service.propose_merge(self.scope, source["id"], target["id"], evidence="maybe", confidence=0.72, evidence_refs={"sources": ["s"]})
        with self.assertRaises(ValidationError):
            self.service.propose_merge(self.scope, source["id"], target["id"], evidence="same domain", confidence=0.95)

    def test_fencing_excludes_other_organization_and_rejects_cross_scope_merge(self) -> None:
        other = self.os.create_organization("Other", "org-other-resolution")
        outsider = self.os.create_person(other.id, "Other Owner", role="owner", person_id="person-other-resolution")
        other_ws = self.os.create_organization_workspace(other.id, "Other", "client", "ws-other-resolution")
        self.os.add_person_to_workspace(other.id, other_ws.id, outsider.id, "admin")
        foreign = self.os.brain_ops.create_entity(other.id, other_ws.id, outsider.id, "Prime", "company")
        foreign_target = self.os.brain_ops.create_entity(other.id, other_ws.id, outsider.id, "Prime Clinics", "company")
        self.assertEqual(self.service.group_aliases(self.scope, ["Prime"]), [])
        with self.assertRaises(AuthorizationError):
            self.service.propose_merge(self.scope, foreign["id"], foreign_target["id"], evidence="same", confidence=0.99, evidence_refs={"sources": ["s"]})

    def test_merge_history_is_read_only_and_scoped(self) -> None:
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM entity_merge_history").fetchone()[0]
        self.assertEqual(self.service.merge_history(self.scope), [])
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM entity_merge_history").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
