from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.domain.workflows import WorkflowStage, WorkflowTemplate, validate_catalog
from auremgrid.services.workflow_catalog import WorkflowCatalog, load_workflow_catalog


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "workflows" / "catalog.json"


class WorkflowCatalogTests(unittest.TestCase):
    def test_fixture_loads_all_representative_templates_and_wings(self) -> None:
        catalog = load_workflow_catalog(FIXTURE)
        self.assertEqual(len(catalog.templates), 8)
        self.assertEqual(
            {wing for template in catalog for wing in template.wings},
            {
                "Client Strategy/Marketing",
                "Product & Engineering",
                "Paid Media",
                "Design",
                "Video Production",
                "Operations",
            },
        )
        self.assertTrue({"campaign_launch", "landing_page", "creative_production", "video_production", "development_release", "performance_monitoring", "client_request", "account_project_review"} <= {template.id for template in catalog})

    def test_catalog_is_immutable_and_lookup_is_read_only(self) -> None:
        catalog = load_workflow_catalog(FIXTURE)
        self.assertIsInstance(catalog.all(), tuple)
        self.assertEqual(catalog.get("campaign_launch").stages[0].order, 1)
        with self.assertRaises(AttributeError):
            catalog._templates = ()
        with self.assertRaises(NotFoundError):
            catalog.get("missing")
        payload = catalog.to_dict()
        payload["templates"][0]["name"] = "changed"
        self.assertNotEqual(catalog.get("campaign_launch").name, "changed")

    def test_gate_stage_requires_approver_and_evidence(self) -> None:
        stage = WorkflowStage(
            id="gate", order=1, name="Gate", owner_wing="Operations", owner_role="Coordinator", approval_gate="client"
        )
        template = WorkflowTemplate(
            id="invalid_gate", name="Invalid", wings=("Operations",), description="x", stages=(stage,), completion_outcomes=("done",)
        )
        with self.assertRaises(ValidationError):
            validate_catalog((template,))

    def test_cross_wing_handoff_and_order_are_enforced(self) -> None:
        first = WorkflowStage(id="a", order=1, name="A", owner_wing="Operations", owner_role="Ops")
        second = WorkflowStage(id="b", order=2, name="B", owner_wing="Design", owner_role="Designer")
        template = WorkflowTemplate(
            id="invalid_handoff", name="Invalid", wings=("Operations", "Design"), description="x", stages=(first, second), completion_outcomes=("done",)
        )
        with self.assertRaises(ValidationError):
            validate_catalog((template,))

    def test_duplicate_template_and_stage_ids_are_rejected(self) -> None:
        stage = WorkflowStage(id="same", order=1, name="A", owner_wing="Operations", owner_role="Ops")
        template = WorkflowTemplate(id="duplicate", name="A", wings=("Operations",), description="x", stages=(stage,), completion_outcomes=("done",))
        with self.assertRaises(ValidationError):
            validate_catalog((template, template))
        duplicate_stage = WorkflowStage(id="same", order=2, name="B", owner_wing="Operations", owner_role="Ops")
        invalid = WorkflowTemplate(id="stages", name="B", wings=("Operations",), description="x", stages=(stage, duplicate_stage), completion_outcomes=("done",))
        with self.assertRaises(ValidationError):
            validate_catalog((invalid,))
        other = WorkflowTemplate(id="other", name="Other", wings=("Operations",), description="x", stages=(stage,), completion_outcomes=("done",))
        with self.assertRaises(ValidationError):
            validate_catalog((template, other))

    def test_loader_rejects_malformed_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"templates": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_workflow_catalog(path)


if __name__ == "__main__":
    unittest.main()
