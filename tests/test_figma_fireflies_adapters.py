from __future__ import annotations
import json,unittest
from auremgrid.connectors.figma import FigmaConnector,FIGMA_REQUIRED_PERMISSIONS
from auremgrid.connectors.http import ConnectorTransportError,HttpResponse,HttpTransport

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

if __name__=="__main__":unittest.main()
