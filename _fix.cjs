const fs = require("fs");
const fp = process.argv[2];
let c = fs.readFileSync(fp, "utf8");
c = c.replace("FIGMA_MAX_FRAME_PATH_ITEMS", "FIGMA_MAX_FRAME_PATH_ITEMS,FIGMA_MAX_COMMENTS");
const newTest = [
    "    def test_figma_optional_comments_emit_on_file_change(self):",
    "        from auremgrid.connectors.figma import FIGMA_MAX_COMMENTS",
    "        permissions=FIGMA_REQUIRED_PERMISSIONS|frozenset({", String.fromCharCode(39), "comments:read", String.fromCharCode(39), "})",
    "        t=QueueTransport([",
    "            {\"id\":\"u1\",\"email\":\"owner@figma.test\"},{\"file\":{\"version\":\"v1\"}},{\"name\":\"Design\",\"document\":{}},{\"comments\":[]},",
    "            {\"file\":{\"version\":\"v1\"}},{\"name\":\"Design\",\"document\":{}},{\"comments\":[]},",
    "            {\"file\":{\"version\":\"v2\"}},{\"name\":\"Design\",\"document\":{}},{\"comments\":[{\"id\":\"c1\",\"body\":\"Looks good\",\"user\":{\"id\":\"u2\",\"name\":\"Alice\"},\"parent_id\":\"p1\",\"resolved\":False,\"created_at\":\"2026-08-20T00:00:00Z\"}]},",
    "        ])",
    "        connector=FigmaConnector(\"secret\",t,file_workspace_mappings={\"file:f1\":\"ws1\"},expected_account_id=\"u1\",granted_permissions=permissions)",
    "        connector.verify_credentials()",
    "        first=connector.pull()",
    "        self.assertEqual([event.event_type for event in first.events],[\"file\"])",
    "        second=connector.pull(first.next_cursor)",
    "        self.assertEqual(second.events,())",
    "        third=connector.pull(second.next_cursor)",
    "        self.assertEqual([event.event_type for event in third.events],[\"file\",\"comment\"])",
    "        comment=json.loads(third.events[-1].content)",
    "        self.assertEqual(comment[\"comment_id\"],\"c1\")",
    "        self.assertEqual(comment[\"message\"],\"Looks good\")",
    "        self.assertEqual(comment[\"resolved\"],False)",
    "        self.assertEqual(comment[\"user\"][\"name\"],\"Alice\")",
    "        self.assertTrue(sum(\"/comments\" in call[1] for call in t.calls)>0)",
].join(String.fromCharCode(10));
c = c.replace("if __name__==", newTest + String.fromCharCode(10) + String.fromCharCode(10) + "if __name__==");
fs.writeFileSync(fp, c, "utf8");
console.log("done");