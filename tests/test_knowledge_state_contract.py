from datetime import datetime, timedelta, timezone
import unittest

from auremgrid.domain import KNOWLEDGE_STATES, KnowledgeState
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class KnowledgeStateContractTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Knowledge")
        self.workspace = self.os.create_organization_workspace(self.org.id, "Main", "client")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.workspace.id, self.person.id, "admin")
        _, self.identity = issue_identity(self.os, self.org.id, self.person.id, self.workspace.id)

    def tearDown(self):
        self.os.close()

    def test_all_states_are_representable_and_transitions_are_legal(self):
        subject = "knowledge-subject"
        for state in ("proposed", "inferred", "high_confidence", "verified", "conflicted", "stale"):
            self.os.brain_ops.record_knowledge_state(
                self.org.id, self.workspace.id, "fact", subject, KnowledgeState(state), state, self.identity,
            )
        rows = self.os.store.conn.execute(
            "SELECT state FROM knowledge_state_events WHERE subject_id=? ORDER BY event_sequence", (subject,)
        ).fetchall()
        self.assertEqual([row["state"] for row in rows], list(("proposed", "inferred", "high_confidence", "verified", "conflicted", "stale")))
        self.assertEqual(set(row["state"] for row in rows), KNOWLEDGE_STATES)

    def test_invalid_state_and_illegal_transition_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.os.brain_ops.record_knowledge_state(self.org.id, self.workspace.id, "fact", "bad", "bogus", "bad", self.identity)
        self.os.brain_ops.record_knowledge_state(self.org.id, self.workspace.id, "fact", "decided", "verified", "review", self.identity)
        with self.assertRaises(ValidationError):
            self.os.brain_ops.record_knowledge_state(self.org.id, self.workspace.id, "fact", "decided", "proposed", "reopen", self.identity)

    def test_event_sequence_is_monotonic_and_supersedes_previous(self):
        for state in ("inferred", "high_confidence", "verified"):
            self.os.brain_ops.record_knowledge_state(self.org.id, self.workspace.id, "fact", "ordered", state, state, self.identity)
        rows = self.os.store.conn.execute(
            "SELECT event_sequence,supersedes_event_id FROM knowledge_state_events WHERE subject_id=? ORDER BY event_sequence", ("ordered",)
        ).fetchall()
        self.assertEqual([row["event_sequence"] for row in rows], [1, 2, 3])
        self.assertIsNone(rows[0]["supersedes_event_id"])
        self.assertEqual(rows[1]["supersedes_event_id"], self.os.store.conn.execute("SELECT id FROM knowledge_state_events WHERE subject_id=? AND event_sequence=1", ("ordered",)).fetchone()[0])

    def test_org_and_workspace_fencing_applies_to_state_write_and_read(self):
        other_org = self.os.create_organization("Other")
        with self.assertRaises(AuthorizationError):
            self.os.brain_ops.record_knowledge_state(other_org.id, self.workspace.id, "fact", "fenced", "verified", "x", self.identity)
        self.os.brain_ops.record_knowledge_state(self.org.id, self.workspace.id, "fact", "fenced", "verified", "x", self.identity)
        self.assertEqual(self.os.brain_ops.knowledge_state(self.org.id, self.workspace.id, self.person.id, "fact", "fenced")["state"], "verified")
        with self.assertRaises(AuthorizationError):
            self.os.brain_ops.knowledge_state(other_org.id, self.workspace.id, self.person.id, "fact", "fenced")

    def test_stale_derivation_is_deterministic(self):
        past = datetime.now(timezone.utc) - timedelta(days=2)
        self.os.brain_ops.record_knowledge_state(self.org.id, self.workspace.id, "fact", "aging", "inferred", "old", self.identity, effective_from=past)
        as_of = datetime.now(timezone.utc)
        first = self.os.brain_ops.derive_stale_states(self.org.id, self.workspace.id, self.identity, 86400, as_of)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["state"], "stale")
        self.assertEqual(self.os.brain_ops.derive_stale_states(self.org.id, self.workspace.id, self.identity, 86400, as_of), [])


if __name__ == "__main__":
    unittest.main()
