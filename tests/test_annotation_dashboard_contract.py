from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "src" / "auremgrid" / "api" / "dashboard" / "annotation-review.js"
INDEX = ROOT / "src" / "auremgrid" / "api" / "dashboard" / "index.html"


class AnnotationDashboardContractTests(unittest.TestCase):
    def test_annotation_payload_uses_review_workspace_and_rich_fields(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for dashboard annotation contract evaluation")

        runner = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            const source = fs.readFileSync({json.dumps(str(SCRIPT))}, 'utf8');
            global.window = {{}};
            global.localStorage = {{getItem() {{ return ''; }}}};
            global.fetch = async () => ({{ok: true, json: async () => ({{}})}});
            global.document = {{
              readyState: 'loading',
              addEventListener() {{}},
              getElementById() {{ return null; }},
              createElement() {{
                return {{
                  textContent: '',
                  get innerHTML() {{
                    return String(this.textContent)
                      .replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;')
                      .replace(/'/g, '&#39;');
                  }}
                }};
              }}
            }};
            vm.runInThisContext(source, {{filename: 'annotation-review.js'}});
            const test = window.AuremgridAnnotationReview._test;
            const data = {{
              waiting_for_me: [{{id: 'review-alpha', workspace_id: 'ws_alpha'}}],
              waiting_for_team: [{{id: 'review-beta', workspace_id: 'ws_review'}}],
              waiting_for_client: [],
              revision_requested: [],
              stalled: [],
              approved_today: []
            }};
            const review = test.findReview(data, 'review-beta');
            const values = new Map([
              ['annotation_type', 'video_range'],
              ['body', 'Trim this segment'],
              ['coordinates', JSON.stringify({{x: 0.25, y: 0.75}})],
              ['page_number', '4'],
              ['start_seconds', '12.5'],
              ['end_seconds', '18.25']
            ]);
            const payload = test.buildAnnotationPayload(
              {{organization_id: 'org_1', person_id: 'person_1'}},
              {{...review, source_locator: 'https://assets.test/review.mp4'}},
              values,
              'idem-1'
            );
            if(payload.workspace_id !== 'ws_review') throw new Error('workspace_id did not come from the review row');
            if(payload.review_id !== 'review-beta') throw new Error('review_id was not preserved');
            if(payload.coordinates.x !== 0.25 || payload.coordinates.y !== 0.75) throw new Error('coordinates were not preserved');
            if(payload.page_number !== 4) throw new Error('page_number was not included');
            if(payload.start_seconds !== 12.5 || payload.end_seconds !== 18.25) throw new Error('video time fields were not included');
            if(payload.source_locator !== 'https://assets.test/review.mp4') throw new Error('source_locator was not included');
            const region = test.buildAnnotationPayload(
              {{organization_id: 'org_1', person_id: 'person_1'}},
              {{...review, source_locator: 'https://assets.test/layout.png'}},
              new Map([
                ['annotation_type', 'image_region'],
                ['body', 'Move this card'],
                ['coordinates', JSON.stringify({{x: 0.1, y: 0.2, width: 0.3, height: 0.4}})]
              ]),
              'idem-2'
            );
            if(region.coordinates.width !== 0.3 || region.coordinates.height !== 0.4) throw new Error('region coordinates were not preserved');
            if(test.requiredFields('document_region').join(',') !== 'page_number,coordinates') throw new Error('document region required fields are not explicit');
            if(test.sourceKind({{deliverable_type: 'video', source_locator: 'fixture://clip.mp4'}}) !== 'video') throw new Error('video source kind was not detected');
            """
        )
        subprocess.run([node, "-e", runner], check=True, cwd=ROOT)

    def test_annotation_owner_is_single_and_no_raw_inputs_are_exposed(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        shell = INDEX.read_text(encoding="utf-8")
        self.assertIn("box.dataset.owner = 'annotation-review'", script)
        self.assertIn('name="review_id"', script)
        self.assertIn('type="hidden" name="review_id"', script)
        self.assertIn('type="hidden" name="coordinates"', script)
        self.assertNotIn("annotation-preview.js", shell)
        self.assertLess(shell.find("annotation-review.js"), shell.find("dashboard.js"))

    def test_annotation_panel_has_media_previews_and_honest_capability_controls(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        css = ROOT.joinpath("src", "auremgrid", "api", "dashboard", "dashboard.css").read_text(encoding="utf-8")
        for marker in (
            "annotation-capabilities",
            "annotation-document-preview",
            "annotation-video-preview",
            "annotation-page-pad",
            "annotation-video-tools",
            "installRegionCapture",
            "Use current time as start",
            "Drag across the preview image",
        ):
            self.assertIn(marker, script)
        self.assertNotIn("&& !REGION_TYPES.has(type)", script)
        self.assertIn("capabilityFor(review, type).status === 'ready'", script)
        self.assertIn("requiredFields(type).includes('coordinates')", script)
        for marker in ("annotation-region-grid", "annotation-capability.ready", "annotation-video-tools"):
            self.assertIn(marker, css)


if __name__ == "__main__":
    unittest.main()
