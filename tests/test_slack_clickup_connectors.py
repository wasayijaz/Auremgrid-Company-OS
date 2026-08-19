from __future__ import annotations

import json
import time
import unittest

from auremgrid.connectors.clickup import ClickUpConnector
from auremgrid.connectors.http import ConnectorTransportError, HttpResponse, HttpTransport, sanitize_content
from auremgrid.connectors.slack import SlackConnector
from auremgrid.domain.errors import AuthorizationError


class InjectedTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)


class ConnectorAdapterTests(unittest.TestCase):
    def test_slack_pull_maps_workspace_and_advances_cursor(self) -> None:
        injected = InjectedTransport([
            HttpResponse(200, {}, json.dumps({"ok": True, "messages": [{"ts": "1700000000.0", "user": "U1", "text": "hello"}], "response_metadata": {"next_cursor": "next"}}).encode())
        ])
        connector = SlackConnector("slack-secret", {"C1": "workspace-1"}, transport=HttpTransport(injected))
        events = connector.pull()
        checkpoint = json.loads(connector.next_cursor)
        self.assertEqual(checkpoint["page_cursor"], "next")
        self.assertIsNone(checkpoint["oldest"])
        self.assertEqual(checkpoint["candidate_oldest"], "1700000000.000000")
        self.assertTrue(connector.has_more)
        self.assertIn("limit=15", injected.calls[0][1])
        self.assertEqual(events[0].workspace_id, "workspace-1")
        self.assertIn("hello", events[0].content)
        self.assertNotIn("slack-secret", events[0].content)
        self.assertEqual(injected.calls[0][2]["Authorization"], "Bearer slack-secret")

    def test_slack_two_page_cycle_promotes_watermark_only_after_final_page(self) -> None:
        first = HttpResponse(200, {}, b'{"ok":true,"messages":[{"ts":"100.0","text":"one"}],"response_metadata":{"next_cursor":"page2"}}')
        second = HttpResponse(200, {}, b'{"ok":true,"messages":[{"ts":"200.0","text":"two"}],"response_metadata":{}}')
        third = HttpResponse(200, {}, b'{"ok":true,"messages":[],"response_metadata":{}}')
        injected = InjectedTransport([first, second, third])
        connector = SlackConnector("secret", {"C1": "workspace-1"}, transport=HttpTransport(injected))
        connector.pull()
        page_checkpoint = json.loads(connector.next_cursor)
        self.assertEqual(page_checkpoint["page_cursor"], "page2")
        self.assertIsNone(page_checkpoint["oldest"])
        self.assertEqual(page_checkpoint["candidate_oldest"], "100.000000")
        connector.pull()
        final_checkpoint = json.loads(connector.next_cursor)
        self.assertIsNone(final_checkpoint["page_cursor"])
        self.assertEqual(final_checkpoint["oldest"], "200.000000")
        self.assertIsNone(final_checkpoint["candidate_oldest"])
        self.assertFalse(connector.has_more)
        connector.pull()
        self.assertIn("oldest=200.000000", injected.calls[2][1])

    def test_slack_rate_limit_is_structured_and_cursor_does_not_advance(self) -> None:
        injected = InjectedTransport([HttpResponse(200, {}, b'{"ok":false,"error":"ratelimited","retry_after":3}')])
        connector = SlackConnector("secret", {"C1": "workspace-1"}, cursor="old", transport=HttpTransport(injected))
        with self.assertRaises(ConnectorTransportError) as raised:
            connector.pull()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 3.0)
        self.assertEqual(json.loads(connector.next_cursor)["page_cursor"], "old")

    def test_slack_verify_credentials_returns_immutable_identity_and_rejects_mismatch(self) -> None:
        response = HttpResponse(200, {"X-OAuth-Scopes": "channels:read, channels:history"}, b'{"ok":true,"team_id":"T1","team":"Neutral Team","user_id":"U1","user":"operator"}')
        identity = SlackConnector("secret", {"C1": "workspace-1"}, transport=HttpTransport(InjectedTransport([response]))).verify_credentials()
        self.assertEqual(identity.team_id, "T1")
        self.assertEqual(identity.granted_scopes, frozenset({"channels:read", "channels:history"}))
        with self.assertRaises(AttributeError):
            identity.team_id = "other"
        failed = HttpResponse(200, {}, b'{"ok":false,"error":"invalid_auth"}')
        with self.assertRaises(ConnectorTransportError) as raised:
            SlackConnector("secret", {"C1": "workspace-1"}, transport=HttpTransport(InjectedTransport([failed]))).verify_credentials()
        self.assertEqual(raised.exception.status, 401)
        mismatch = HttpResponse(200, {}, b'{"ok":true,"team_id":"T1","team":"Neutral Team","user_id":"U1","user":"operator"}')
        with self.assertRaises(AuthorizationError):
            SlackConnector("secret", {"C1": "workspace-1"}, expected_team_id="T2", transport=HttpTransport(InjectedTransport([mismatch]))).verify_credentials()

    def test_slack_replayed_edit_keeps_source_key_but_changes_content(self) -> None:
        first = HttpResponse(200, {}, b'{"ok":true,"messages":[{"ts":"1700000000.0","user":"U1","text":"draft"}],"response_metadata":{}}')
        second = HttpResponse(200, {}, b'{"ok":true,"messages":[{"ts":"1700000000.0","user":"U1","text":"edited"}],"response_metadata":{}}')
        connector = SlackConnector("secret", {"C1": "workspace-1"}, transport=HttpTransport(InjectedTransport([first, second])))
        original = connector.pull()[0]
        replay = connector.pull()[0]
        self.assertEqual(original.source_key, replay.source_key)
        self.assertNotEqual(original.content, replay.content)

    def test_unmapped_slack_channel_is_rejected(self) -> None:
        connector = SlackConnector("secret", {"C1": "workspace-1"}, channel_ids=["C2"], transport=HttpTransport(lambda *_: None))
        with self.assertRaises(ValueError):
            connector.pull()

    def test_clickup_pull_maps_tasks_and_page_cursor(self) -> None:
        injected = InjectedTransport([
            HttpResponse(200, {}, json.dumps({"last_page": False, "tasks": [{"id": "t1", "name": "Ship", "description": "Do it", "status": {"status": "open"}, "date_updated": "1700000000000", "url": "https://app.clickup.com/t/t1"}]}).encode())
        ])
        connector = ClickUpConnector("clickup-secret", {"L1": "workspace-1"}, transport=HttpTransport(injected))
        events = connector.pull()
        self.assertEqual(connector.next_cursor, "1")
        self.assertTrue(connector.has_more)
        self.assertEqual(events[0].source_key, "clickup:L1:t1")
        self.assertEqual(events[0].workspace_id, "workspace-1")
        self.assertNotIn("clickup-secret", events[0].content)

    def test_clickup_verify_credentials_and_rate_reset(self) -> None:
        response = HttpResponse(200, {}, b'{"teams":[{"id":"T1","name":"Neutral Team"}]}')
        hierarchy = HttpResponse(200, {}, b'{"id":"L1","space":{"id":"S1"}}')
        spaces = HttpResponse(200, {}, b'{"spaces":[{"id":"S1"}]}')
        teams = ClickUpConnector("secret", {"L1": "workspace-1"}, expected_team_id="T1", transport=HttpTransport(InjectedTransport([response, hierarchy, spaces]))).verify_credentials()
        self.assertEqual(teams[0].team_id, "T1")
        self.assertIsInstance(teams, tuple)
        self.assertFalse(ClickUpConnector("secret", {"L1": "workspace-1"}).has_more)
        done_transport = InjectedTransport([HttpResponse(200, {}, b'{"last_page":true,"tasks":[]}'), HttpResponse(200, {}, b'{"last_page":true,"tasks":[]}')])
        done = ClickUpConnector("secret", {"L1": "workspace-1"}, cursor="3", transport=HttpTransport(done_transport))
        done.pull()
        self.assertEqual(done.next_cursor, "0")
        self.assertFalse(done.has_more)
        done.pull()
        self.assertIn("page=0", done_transport.calls[1][1])
        injected = InjectedTransport([HttpResponse(429, {"X-RateLimit-Reset": str(int(time.time()) + 5)}, b"rate")])
        with self.assertRaises(ConnectorTransportError) as raised:
            HttpTransport(injected).request("GET", "https://api.clickup.com/api/v2/team")
        self.assertTrue(raised.exception.retryable)
        self.assertGreaterEqual(raised.exception.retry_after or 0, 0)

    def test_clickup_verification_rejects_foreign_team_list(self) -> None:
        injected = InjectedTransport([
            HttpResponse(200, {}, b'{"teams":[{"id":"T1","name":"Expected"},{"id":"T2","name":"Foreign"}]}'),
            HttpResponse(200, {}, b'{"id":"L1","space":{"id":"S_FOREIGN"}}'),
            HttpResponse(200, {}, b'{"spaces":[{"id":"S_EXPECTED"}]}'),
        ])
        with self.assertRaises(AuthorizationError):
            ClickUpConnector("secret", {"L1": "workspace-1"}, expected_team_id="T1", transport=HttpTransport(injected)).verify_credentials()

    def test_http_retryable_status_exposes_retry_after_without_retrying(self) -> None:
        injected = InjectedTransport([HttpResponse(429, {"Retry-After": "4"}, b"rate")])
        with self.assertRaises(ConnectorTransportError) as raised:
            HttpTransport(injected).request("GET", "https://example.test")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.retry_after, 4.0)
        self.assertEqual(len(injected.calls), 1)

    def test_sanitize_content_redacts_secret_values_and_credential_shapes(self) -> None:
        raw = {"Authorization": "Bearer abc", "api_key": "key-123", "message": "token=abc secret-key"}
        safe = sanitize_content(raw, ("secret-key", "abc"))
        self.assertEqual(safe["Authorization"], "[REDACTED]")
        self.assertEqual(safe["api_key"], "[REDACTED]")
        self.assertNotIn("abc", safe["message"])
        self.assertNotIn("secret-key", safe["message"])


if __name__ == "__main__":
    unittest.main()
