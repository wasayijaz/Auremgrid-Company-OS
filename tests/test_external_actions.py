"""Tests for Release C controlled external actions with outbox, approval, and fencing."""

from __future__ import annotations

import unittest
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.external_actions import (
    ACTION_TYPE_TO_PROVIDER,
    EXTERNAL_ACTION_PROVIDERS,
    ExternalActionIntent,
    ExternalActionReceipt,
    ExternalActionService,
    SimulatedProviderDispatcher,
    validate_action_payload,
)


class ExternalActionsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Test Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client Workspace", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.worker = self.os.create_person(self.org.id, "Worker")
        self.conn = self.os.store.conn
        self.service = ExternalActionService(self.conn)
        self.org_id = self.org.id
        self.workspace_id = self.ws.id
        self.person_id = self.worker.id
        self.approver_id = self.owner.id

    def test_queue_intent_creates_pending_outbox_and_approval_request(self) -> None:
        intent = ExternalActionIntent(
            organization_id=self.org_id,
            workspace_id=self.workspace_id,
            action_type="gmail.draft",
            provider="gmail",
            payload={
                "recipient": "client@acme.com",
                "subject": "Q3 Performance Review Draft",
                "body_text": "Here is the performance review summary for Q3.",
            },
            requested_by_id=self.person_id,
            reason="Prepare client review email",
            approver_person_id=self.approver_id,
        )

        result = self.service.queue_action(intent)
        self.assertFalse(result["reused"])

        outbox = result["outbox_event"]
        self.assertEqual(outbox["organization_id"], self.org_id)
        self.assertEqual(outbox["workspace_id"], self.workspace_id)
        self.assertEqual(outbox["event_type"], "gmail.draft")
        self.assertEqual(outbox["status"], "pending")
        self.assertIsNotNone(outbox["payload_hash"])

        approval = result["approval_request"]
        self.assertIsNotNone(approval)
        self.assertEqual(approval["organization_id"], self.org_id)
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(approval["policy"], "human")
        self.assertEqual(approval["requested_for"], outbox["id"])

    def test_gmail_action_enforces_draft_only_and_forbids_send(self) -> None:
        # 1. Direct send flag in payload must fail
        with self.assertRaises(ValidationError) as cm:
            ExternalActionIntent(
                organization_id=self.org_id,
                action_type="gmail.draft",
                provider="gmail",
                payload={
                    "recipient": "client@acme.com",
                    "subject": "Direct Send Attempt",
                    "body_text": "Sending without draft",
                    "send": True,
                },
                requested_by_id=self.person_id,
            )
        self.assertIn("draft-only", str(cm.exception))

        # 2. Action or mode set to 'send' must fail
        with self.assertRaises(ValidationError) as cm2:
            ExternalActionIntent(
                organization_id=self.org_id,
                action_type="gmail.draft",
                provider="gmail",
                payload={
                    "recipient": "client@acme.com",
                    "subject": "Direct Send Mode",
                    "body_text": "Sending mode",
                    "mode": "send",
                },
                requested_by_id=self.person_id,
            )
        self.assertIn("draft-only", str(cm2.exception))

        # 3. Valid draft-only payload succeeds
        valid_intent = ExternalActionIntent(
            organization_id=self.org_id,
            action_type="gmail.draft",
            provider="gmail",
            payload={
                "recipient": "client@acme.com",
                "subject": "Safe Draft",
                "body_text": "Draft content only",
            },
            requested_by_id=self.person_id,
        )
        self.assertEqual(valid_intent.action_type, "gmail.draft")

    def test_dispatch_blocked_without_explicit_human_approval(self) -> None:
        intent = ExternalActionIntent(
            organization_id=self.org_id,
            action_type="slack.reply",
            provider="slack",
            payload={"channel_id": "C123456", "message_text": "Campaign budget approved."},
            requested_by_id=self.person_id,
        )
        queued = self.service.queue_action(intent)
        outbox_id = queued["outbox_event"]["id"]

        # Attempt dispatch while approval is still pending
        with self.assertRaises(AuthorizationError) as cm:
            self.service.dispatch_action(self.org_id, outbox_id)
        self.assertIn("pending human approval", str(cm.exception))

        # Verify outbox state remained pending
        outbox_row = self.conn.execute("SELECT status FROM outbox_events WHERE id=?", (outbox_id,)).fetchone()
        self.assertEqual(outbox_row["status"], "pending")

    def test_dispatch_blocked_when_approval_rejected(self) -> None:
        intent = ExternalActionIntent(
            organization_id=self.org_id,
            action_type="clickup.task",
            provider="clickup",
            payload={"list_id": "L999", "name": "Urgent campaign rework"},
            requested_by_id=self.person_id,
        )
        queued = self.service.queue_action(intent)
        outbox_id = queued["outbox_event"]["id"]
        approval_id = queued["approval_request"]["id"]

        # Reject the approval
        self.service.decide_approval(
            organization_id=self.org_id,
            approver_person_id=self.approver_id,
            approval_id=approval_id,
            approved=False,
            comments="Budget not authorized for extra contractor task",
        )

        # Attempt dispatch
        with self.assertRaises(AuthorizationError) as cm:
            self.service.dispatch_action(self.org_id, outbox_id)
        self.assertIn("approval was rejected", str(cm.exception))

        # Verify outbox state remained pending
        outbox_row = self.conn.execute("SELECT status FROM outbox_events WHERE id=?", (outbox_id,)).fetchone()
        self.assertEqual(outbox_row["status"], "pending")

    def test_dispatch_enforces_provider_allowlist_fencing(self) -> None:
        dispatcher = SimulatedProviderDispatcher()
        # Verify unknown provider is rejected at dispatch
        with self.assertRaises(AuthorizationError) as cm:
            dispatcher.dispatch(
                provider="untrusted_external_service",
                organization_id=self.org_id,
                workspace_id=None,
                action_type="custom.action",
                payload={"data": "test"},
            )
        self.assertIn("allowlist", str(cm.exception))

    def test_dispatch_enforces_organization_scoping(self) -> None:
        org_alpha = self.os.create_organization("Org Alpha")
        org_beta = self.os.create_organization("Org Beta")
        intent = ExternalActionIntent(
            organization_id=org_alpha.id,
            action_type="drive.report",
            provider="google_drive",
            payload={"folder_id": "FDR_100", "title": "Monthly Performance Report"},
            requested_by_id=self.person_id,
        )
        queued = self.service.queue_action(intent)
        outbox_id = queued["outbox_event"]["id"]
        approval_id = queued["approval_request"]["id"]

        self.service.decide_approval(org_alpha.id, self.approver_id, approval_id, approved=True)

        # Attempt to dispatch from a different organization scope
        with self.assertRaises(AuthorizationError) as cm:
            self.service.dispatch_action(org_beta.id, outbox_id)
        self.assertIn("belongs to another organization", str(cm.exception))

    def test_idempotency_prevents_duplicate_action_queueing(self) -> None:
        idempotency_key = "idemp_action_unique_001"
        intent1 = ExternalActionIntent(
            organization_id=self.org_id,
            action_type="portal.publish",
            provider="client_portal",
            payload={"title": "Q3 Executive Summary", "deliverable_id": "deliv_123"},
            requested_by_id=self.person_id,
            idempotency_key=idempotency_key,
        )

        res1 = self.service.queue_action(intent1)
        self.assertFalse(res1["reused"])
        outbox_id1 = res1["outbox_event"]["id"]

        # Queue again with identical idempotency key
        intent2 = ExternalActionIntent(
            organization_id=self.org_id,
            action_type="portal.publish",
            provider="client_portal",
            payload={"title": "Q3 Executive Summary", "deliverable_id": "deliv_123"},
            requested_by_id=self.person_id,
            idempotency_key=idempotency_key,
        )
        res2 = self.service.queue_action(intent2)
        self.assertTrue(res2["reused"])
        self.assertEqual(res2["outbox_event"]["id"], outbox_id1)

        # Check only 1 row exists in DB
        count = self.conn.execute(
            "SELECT count(*) FROM outbox_events WHERE organization_id=? AND idempotency_key=?",
            (self.org_id, idempotency_key),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_successful_fenced_dispatch_records_receipt_and_publishes_outbox(self) -> None:
        intent = ExternalActionIntent(
            organization_id=self.org_id,
            workspace_id=self.workspace_id,
            action_type="gmail.draft",
            provider="gmail",
            payload={
                "recipient": "marketing@acme.com",
                "subject": "Approved Campaign Plan",
                "body_html": "<p>Here is the approved campaign plan.</p>",
            },
            requested_by_id=self.person_id,
        )
        queued = self.service.queue_action(intent)
        outbox_id = queued["outbox_event"]["id"]
        approval_id = queued["approval_request"]["id"]

        # Approve
        self.service.decide_approval(self.org_id, self.approver_id, approval_id, approved=True)

        # Dispatch
        receipt = self.service.dispatch_action(self.org_id, outbox_id)
        self.assertIsInstance(receipt, ExternalActionReceipt)
        self.assertEqual(receipt.status, "published")
        self.assertEqual(receipt.provider, "gmail")
        self.assertEqual(receipt.outbox_event_id, outbox_id)
        self.assertTrue(receipt.details["is_draft"])
        self.assertFalse(receipt.details["sent"])
        self.assertIsNotNone(receipt.external_reference_id)

        # Verify DB row is published
        outbox_row = self.conn.execute("SELECT * FROM outbox_events WHERE id=?", (outbox_id,)).fetchone()
        self.assertEqual(outbox_row["status"], "published")
        self.assertIsNotNone(outbox_row["published_at"])

        # Verify get_action_receipt retrieves it
        fetched_receipt = self.service.get_action_receipt(self.org_id, outbox_id)
        self.assertIsNotNone(fetched_receipt)
        self.assertEqual(fetched_receipt.receipt_id, receipt.receipt_id)
        self.assertEqual(fetched_receipt.external_reference_id, receipt.external_reference_id)

    def test_all_five_action_types_execute_with_simulated_receipts(self) -> None:
        actions = [
            (
                "gmail.draft",
                "gmail",
                {"recipient": "client@domain.com", "subject": "Draft Subject", "body_text": "Body"},
                lambda d: d["is_draft"] is True and d["sent"] is False,
            ),
            (
                "slack.reply",
                "slack",
                {"channel_id": "C_MARKETING", "message_text": "Acknowledged issue."},
                lambda d: d["channel_id"] == "C_MARKETING",
            ),
            (
                "clickup.task",
                "clickup",
                {"list_id": "L_SPRINT_4", "name": "Review creative fatigue"},
                lambda d: d["list_id"] == "L_SPRINT_4" and d["name"] == "Review creative fatigue",
            ),
            (
                "drive.report",
                "google_drive",
                {"folder_id": "F_REPORTS", "title": "August Performance Audit"},
                lambda d: d["folder_id"] == "F_REPORTS" and d["title"] == "August Performance Audit",
            ),
            (
                "portal.publish",
                "client_portal",
                {"title": "Published Client Dashboard", "deliverable_id": "deliv_999"},
                lambda d: d["title"] == "Published Client Dashboard",
            ),
        ]

        for action_type, provider, payload, validator in actions:
            intent = ExternalActionIntent(
                organization_id=self.org_id,
                workspace_id=self.workspace_id,
                action_type=action_type,
                provider=provider,
                payload=payload,
                requested_by_id=self.person_id,
            )
            queued = self.service.queue_action(intent)
            outbox_id = queued["outbox_event"]["id"]
            approval_id = queued["approval_request"]["id"]

            self.service.decide_approval(self.org_id, self.approver_id, approval_id, approved=True)
            receipt = self.service.dispatch_action(self.org_id, outbox_id)

            self.assertEqual(receipt.status, "published", f"Failed for {action_type}")
            self.assertEqual(receipt.provider, provider)
            self.assertTrue(validator(receipt.details), f"Validator failed for {action_type}: {receipt.details}")


if __name__ == "__main__":
    unittest.main()
