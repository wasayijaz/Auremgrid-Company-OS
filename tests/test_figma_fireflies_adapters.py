from __future__ import annotations
import json,unittest
from auremgrid.connectors.figma import FigmaConnector,FIGMA_REQUIRED_PERMISSIONS,FIGMA_OPTIONAL_PERMISSIONS,FIGMA_MAX_FRAME_TEXT,FIGMA_MAX_FRAME_PATH_ITEMS,FIGMA_MAX_COMMENTS
from auremgrid.connectors.http import ConnectorTransportError,HttpResponse,HttpTransport
from auremgrid.connectors.fireflies import FirefliesConnector,FIREFLIES_REQUIRED_SCOPES
from auremgrid.domain.errors import ValidationError

class QueueTransport:
    def __init__(self,payloads):self.payloads=list(payloads);self.calls=[]
    def request(self,method,url,headers,body=None):
        self.calls.append((method,url,headers,body));return HttpResponse(200,{},json.dumps(self.payloads.pop(0)).encode())

class AdapterContractTests(unittest.TestCase):
    def test_figma_verify_changed_unchanged_and_restart(self):
        t=QueueTransport([
            {"id":"u1","email":"x"},{"file":{"version":"v1","name":"Design"}},{"name":"Design","document":{}},
            {"file":{"version":"v1"}},{"name":"Design","document":{"name":"A"}},
            {"file":{"version":"v1"}},
        ])
        c=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"},expected_account_id="u1",granted_permissions=FIGMA_REQUIRED_PERMISSIONS)
        identity=c.verify_credentials();self.assertEqual(identity.granted_permissions,FIGMA_REQUIRED_PERMISSIONS)
        first=c.pull();self.assertEqual(first.events[0].source_key,"figma/files/f1")
        unchanged=c.pull(first.next_cursor);self.assertEqual(unchanged.events,())
        self.assertEqual(sum("/files/f1?version=v1" in call[1] for call in t.calls),1)
        restarted=FigmaConnector("secret",QueueTransport([{"file":{"version":"v1"}}]),file_workspace_mappings={"file:f1":"ws1"})
        self.assertEqual(restarted.pull(first.next_cursor).events,())

    def test_figma_metadata_requires_official_file_envelope(self):
        def malformed(_m,_u,_h,_b):return HttpResponse(200,{},json.dumps({"meta":{"version":"v1"}}).encode())
        connector=FigmaConnector("secret",HttpTransport(malformed),file_workspace_mappings={"file:f1":"ws1"})
        with self.assertRaises(ConnectorTransportError) as raised:
            connector.pull()
        self.assertIn("envelope",str(raised.exception))

    def test_figma_version_fence_is_encoded_in_content_request(self):
        t=QueueTransport([{"file":{"version":"v/1","name":"Design"}},{"name":"Design","document":{}}])
        connector=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"})
        connector.pull()
        self.assertIn("/files/f1?version=v%2F1",t.calls[-1][1])

    def test_figma_emits_bounded_frame_and_section_events_from_versioned_document(self):
        long_text="Visible copy "+"x"*(FIGMA_MAX_FRAME_TEXT+100)
        document={"id":"0:0","name":"Doc","type":"DOCUMENT","children":[
            {"id":"1:1","name":"Page","type":"CANVAS","children":[
                {"id":"2:1","name":"Hero","type":"FRAME","absoluteBoundingBox":{"x":1.23456,"y":2,"width":300,"height":200},"children":[
                    {"id":"3:1","type":"TEXT","characters":long_text},
                    {"id":"3:2","type":"TEXT","characters":"secret token=secret"},
                    {"id":"4:1","name":"Nested","type":"SECTION","children":[]},
                ]},
                {"id":"2:1","name":"Duplicate hero","type":"FRAME","children":[]},
                {"id":"2:2","name":"Footer","type":"SECTION","visible":False,"children":[]},
            ]}
        ]}
        t=QueueTransport([{"file":{"version":"v1","name":"Design"}},{"name":"Design","document":document}])
        result=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"}).pull()
        self.assertEqual([event.source_key for event in result.events],[
            "figma/files/f1",
            "figma/files/f1/nodes/2:1",
            "figma/files/f1/nodes/4:1",
            "figma/files/f1/nodes/2:2",
        ])
        self.assertEqual([event.event_type for event in result.events[1:]],["frame","section","section"])
        frame=json.loads(result.events[1].content)
        self.assertEqual(frame["workspace_ids"],["ws1"])
        self.assertEqual(frame["route_keys"],["file:f1"])
        self.assertEqual(frame["bounds"],{"x":1.235,"y":2.0,"width":300.0,"height":200.0})
        self.assertNotIn("secret",result.events[1].content)
        self.assertLessEqual(sum(len(item) for item in frame["texts"]),FIGMA_MAX_FRAME_TEXT)
        self.assertEqual(result.lifecycle_mutations[0].external_id,"figma/files/f1")
        self.assertEqual(len(result.lifecycle_mutations),1)

    def test_figma_frame_events_are_stable_and_skip_when_provider_version_unchanged(self):
        document={"id":"0:0","type":"DOCUMENT","children":[{"id":"2:1","name":"Hero","type":"FRAME","children":[]}]}
        t=QueueTransport([{"file":{"version":"v1"}},{"name":"Design","document":document},{"file":{"version":"v1"}}])
        connector=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"})
        first=connector.pull()
        self.assertEqual([event.source_key for event in first.events],["figma/files/f1","figma/files/f1/nodes/2:1"])
        replay=connector.pull(first.next_cursor)
        self.assertEqual(replay.events,())

    def test_figma_optional_versions_emit_bounded_evidence_only_on_file_change(self):
        permissions=FIGMA_REQUIRED_PERMISSIONS|FIGMA_OPTIONAL_PERMISSIONS
        t=QueueTransport([
            {"id":"u1","email":"owner@figma.test"},{"file":{"version":"v1"}},{"name":"Design","document":{}},{"versions":[{"id":"verify-only"}]},{"comments":[]},
            {"file":{"version":"v1"}},{"name":"Design","document":{}},{"comments":[]},{"versions":[{"id":"ver1","label":"Client review token=secret","created_at":"2026-08-19T00:00:00Z","user":{"id":"u1","handle":"Reviewer"}}]},
            {"file":{"version":"v1"}},
            {"file":{"version":"v2"}},{"name":"Design","document":{}},{"comments":[]},{"versions":[{"id":"ver1","label":"Client review token=secret","created_at":"2026-08-19T00:00:00Z"},{"id":"ver2","label":"Approved","created_at":"2026-08-19T00:10:00Z"}]},
        ])
        connector=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"},expected_account_id="u1",granted_permissions=permissions)
        identity=connector.verify_credentials();self.assertEqual(identity.granted_permissions,permissions)
        first=connector.pull()
        self.assertEqual([event.event_type for event in first.events],["file","version"])
        version=json.loads(first.events[-1].content)
        self.assertEqual(version["workspace_ids"],["ws1"])
        self.assertEqual(version["route_keys"],["file:f1"])
        self.assertEqual(version["version_id"],"ver1")
        self.assertNotIn("secret",first.events[-1].content)
        second=connector.pull(first.next_cursor)
        self.assertEqual(second.events,())
        third=connector.pull(second.next_cursor)
        self.assertEqual([event.source_key for event in third.events],["figma/files/f1","figma/files/f1/versions/ver1","figma/files/f1/versions/ver2"])
        self.assertEqual(sum("/versions" in call[1] for call in t.calls),3)
        self.assertEqual(sum("/comments" in call[1] for call in t.calls),3)
        self.assertEqual(sum("/files/f1?version=v1" in call[1] for call in t.calls),1)

    def test_figma_frame_path_metadata_is_bounded_for_deep_trees(self):
        node={"id":"deep-frame","name":"Target","type":"FRAME","children":[]}
        for index in range(80):
            node={"id":f"group-{index}","name":"Ancestor"*50,"type":"GROUP","children":[node]}
        document={"id":"0:0","type":"DOCUMENT","children":[node]}
        t=QueueTransport([{"file":{"version":"v1"}},{"name":"Design","document":document}])
        result=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"}).pull()
        frame=json.loads(result.events[1].content)
        self.assertLessEqual(len(frame["path"]),FIGMA_MAX_FRAME_PATH_ITEMS+1)
        self.assertEqual(frame["path"][0]["id"],"__truncated__")
        self.assertLess(len(result.events[1].content),12000)

    def test_figma_404_tombstone_and_retry_after(self):
        def missing(_m,_u,_h,_b):return HttpResponse(404,{},b"{}")
        connector=FigmaConnector("secret",HttpTransport(missing),file_workspace_mappings={"file:f1":"ws1"})
        cursor=json.dumps({"v":1,"file_key":"f1","provider_version":"v1"})
        result=connector.pull(cursor);self.assertEqual(result.events[0].event_type,"tombstone")
        self.assertEqual(connector.pull(result.next_cursor).events,())
        def limited(_m,_u,_h,_b):return HttpResponse(429,{"Retry-After":"7"},b"{}")
        with self.assertRaises(ConnectorTransportError) as raised:
            FigmaConnector("secret",HttpTransport(limited),file_workspace_mappings={"file:f1":"ws1"}).pull()
        self.assertTrue(raised.exception.retryable);self.assertEqual(raised.exception.retry_after,7)

    def test_figma_optional_comments_emit_on_file_change(self):
        from auremgrid.connectors.figma import FIGMA_MAX_COMMENTS
        permissions=FIGMA_REQUIRED_PERMISSIONS|frozenset({"comments:read"})
        t=QueueTransport([
            {"id":"u1","email":"owner@figma.test"},{"file":{"version":"v1"}},{"name":"Design","document":{}},{"comments":[]},
            {"file":{"version":"v1"}},{"name":"Design","document":{}},{"comments":[]},
            {"file":{"version":"v1"}},
            {"file":{"version":"v2"}},{"name":"Design","document":{}},{"comments":[{"id":"c1","body":"Looks good","user":{"id":"u2","name":"Alice"},"parent_id":"p1","resolved":False,"created_at":"2026-08-20T00:00:00Z"}]},
        ])
        connector=FigmaConnector("secret",t,file_workspace_mappings={"file:f1":"ws1"},expected_account_id="u1",granted_permissions=permissions)
        connector.verify_credentials()
        first=connector.pull()
        self.assertEqual([event.event_type for event in first.events],["file"])
        second=connector.pull(first.next_cursor)
        self.assertEqual(second.events,())
        third=connector.pull(second.next_cursor)
        self.assertEqual([event.event_type for event in third.events],["file","comment"])
        comment=json.loads(third.events[-1].content)
        self.assertEqual(comment["comment_id"],"c1")
        self.assertEqual(comment["message"],"Looks good")
        self.assertEqual(comment["resolved"],False)
        self.assertEqual(comment["user"]["name"],"Alice")
        self.assertTrue(sum("/comments" in call[1] for call in t.calls)>0)

class FirefliesAdapterContractTests(unittest.TestCase):
    def test_fireflies_requires_single_account_mapping(self):
        with self.assertRaises(ValidationError):
            FirefliesConnector("secret",workspace_mappings={})
        with self.assertRaises(ValidationError):
            FirefliesConnector("secret",workspace_mappings={"account:a1":"ws1","account:a2":"ws2"})
        with self.assertRaises(ValidationError):
            FirefliesConnector("secret",workspace_mappings={"bad:a1":"ws1"})

    def test_fireflies_verify_and_pull_emits_bounded_transcript_events(self):
        t=QueueTransport([
            {"id":"u1","email":"owner@fireflies.test"},
            {"transcripts":[{
                "id":"m1","title":"Client sync","date":"2026-08-19T00:00:00Z","duration":1800,
                "participants":[{"name":"Alice"},{"name":"Bob"}],"sentiment":"positive",
                "summary":{"short":"Discussed scope","long":"Detailed notes token=secret"},
                "speakers":[{"id":"s1","name":"Alice","sentences":[{"text":"Hello there","start_time":0}]}],
                "recording_url":"https://fireflies.test/rec/m1",
            }]},
        ])
        connector=FirefliesConnector("secret",t,workspace_mappings={"account:a1":"ws1"},expected_account_id="u1")
        identity=connector.verify_credentials()
        self.assertEqual(identity.user_id,"u1");self.assertEqual(identity.granted_scopes,FIREFLIES_REQUIRED_SCOPES)
        result=connector.pull()
        self.assertEqual(len(result.events),1)
        event=result.events[0]
        self.assertEqual(event.source_key,"fireflies/meetings/m1")
        self.assertEqual(event.event_type,"transcript")
        payload=json.loads(event.content)
        self.assertEqual(payload["meeting_id"],"m1")
        self.assertEqual(payload["title"],"Client sync")
        self.assertNotIn("secret",event.content)
        self.assertEqual(len(result.lifecycle_mutations),1)
        mutation=result.lifecycle_mutations[0]
        self.assertEqual(mutation.route_key,"account:a1");self.assertEqual(mutation.workspace_id,"ws1")
        self.assertEqual(mutation.operation,"upsert")

    def test_fireflies_account_mismatch_is_rejected(self):
        t=QueueTransport([{"id":"other","email":"x"}])
        connector=FirefliesConnector("secret",t,workspace_mappings={"account:a1":"ws1"},expected_account_id="u1")
        with self.assertRaises(ConnectorTransportError):
            connector.verify_credentials()

    def test_fireflies_cursor_advances_and_dedupes_across_pulls(self):
        t=QueueTransport([
            {"transcripts":[{"id":"m1","date":"2026-08-19T00:00:00Z","title":"First"}]},
            {"transcripts":[]},
        ])
        connector=FirefliesConnector("secret",t,workspace_mappings={"account:a1":"ws1"})
        first=connector.pull()
        self.assertEqual(len(first.events),1);self.assertIsNotNone(first.next_cursor)
        second=connector.pull(first.next_cursor)
        self.assertEqual(second.events,())
        self.assertIn("from_date=2026-08-19T00:00:00Z",t.calls[-1][1])

if __name__=="__main__":unittest.main()
