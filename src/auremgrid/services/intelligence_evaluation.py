from __future__ import annotations

"""Small, offline evaluation harness for the Intelligence contract.

The scenarios are deliberately product-facing rather than model benchmarks:
they verify evidence provenance, ACL behavior, uncertainty, structured
reasoning, approval boundaries, and deterministic degradation.  They do not
contact a provider or mutate a durable database.
"""

from pathlib import Path
from typing import Any

from auremgrid.services.brain import CompanyOS
from auremgrid.services.intelligence_orchestrator import IntelligenceOrchestrator


FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


class _StructuredEvaluationProvider:
    name = "evaluation_fixture"
    model = "deterministic"
    version = "1"

    def deliberate(self, _context: dict[str, Any]) -> dict[str, Any]:
        return {
            "hypotheses": [{
                "text": "The visible delivery signal needs an owner check.",
                "confidence": 0.72,
                "supporting_evidence": [],
                "opposing_evidence": [],
            }],
            "options": [{
                "title": "Owner review",
                "summary": "Review the cited signal before changing scope.",
                "tradeoffs": ["Uses review capacity"],
            }],
            "scenarios": [{
                "name": "bounded_review",
                "assumptions": ["Visible evidence remains current"],
                "mitigations": ["Re-check after the review"],
            }],
            "recommendation": {
                "summary": "Review the cited signal before taking a one-way action.",
                "rationale": "The recommendation is reversible and evidence-backed.",
            },
            "confidence": 0.66,
            "dissent": [{"text": "The signal may be stale."}],
        }


class _MalformedEvaluationProvider:
    name = "evaluation_malformed"
    model = "deterministic"
    version = "1"

    def deliberate(self, _context: dict[str, Any]) -> dict[str, Any]:
        return {"hypotheses": []}


def _case(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _seed(provider: Any | None = None) -> CompanyOS:
    os = CompanyOS(":memory:", strategic_reasoning_provider=provider)
    # Evaluation must remain deterministic even if the shell has an optional
    # remote provider configured for normal application runs.
    if provider is None:
        os.strategic_reasoning_provider = None
    os.seed_demo(FIXTURES)
    return os


def run_intelligence_evaluations() -> dict[str, Any]:
    """Run all built-in checks and return a stable JSON-compatible report."""
    checks: list[dict[str, Any]] = []
    owner_os = _seed()
    try:
        owner = owner_os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        evidence = [item for finding in owner["findings"] for item in finding.get("evidence", [])]
        citations_ok = bool(evidence) and all(
            item.get("citation") and item.get("object_ref") for item in evidence
        )
        checks.append(_case(
            "evidence_citations", citations_ok,
            f"{len(evidence)} evidence items carry object references and citations",
        ))

        action_items = [item for finding in owner["findings"] for item in finding.get("actions", [])]
        action_safe = all(
            item.get("safe") is True and item.get("one_way") is False
            and item.get("requires_approval") is True
            and item.get("status") in {"proposed", "review_only", "supervised_catalog_only"}
            and (item.get("route") == "/approvals" or item.get("executable") is False)
            for item in action_items
        )
        approval_descriptor = any(item.get("requires_approval") is True for item in action_items)
        checks.append(_case(
            "no_unauthorized_actions", action_safe,
            f"{len(action_items)} proposed actions remain reversible and unexecuted",
        ))
        checks.append(_case(
            "approval_descriptors", approval_descriptor,
            "at least one proposed action explicitly carries an approval boundary",
        ))

        unknown = owner_os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin",
            query="evaluation query with no visible evidence",
        )
        uncertainty_ok = (
            unknown["status"] == "insufficient_evidence"
            and unknown["uncertainty"]["label"] == "high"
            and not unknown["findings"]
        )
        checks.append(_case(
            "uncertainty", uncertainty_ok,
            "unknown evidence remains insufficient with high uncertainty",
        ))

        viewer = owner_os.create_person(
            "org_demo", "Evaluation Viewer", "evaluation-viewer@demo.invalid",
            role="member", person_id="person_evaluation_viewer",
        )
        owner_os.add_person_to_workspace("org_demo", "ws_alpha", viewer.id, "viewer")
        viewer_result = owner_os.intelligence.workspace(
            "org_demo", "ws_alpha", viewer.id,
        )
        viewer_actions = [
            action for finding in viewer_result["findings"] for action in finding.get("actions", [])
        ]
        checks.append(_case(
            "acl_read_only", not viewer_actions,
            "viewer membership receives no mutation descriptors",
        ))
        checks.append(_case(
            "acl_evidence_scope", viewer_result["context"]["evidence_count"] == 0,
            "viewer without an actor binding receives no Brain-source evidence",
        ))

        profile_ids = [item["id"] for item in owner_os.intelligence_contracts.list_profiles(
            "org_demo", "ws_alpha", "person_demo_owner"
        )]
        orchestrated = owner_os.intelligence_orchestrator.run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
            profile_ids=profile_ids,
        )
        specialists = orchestrated.get("specialists", [])
        specialists_ok = (
            len(specialists) == 13
            and len({item.get("specialist_id") for item in specialists}) == 13
            and all(item.get("profile", {}).get("id") for item in specialists)
        )
        checks.append(_case(
            "orchestrator_profile_fanout", specialists_ok,
            f"offline orchestration produced {len(specialists)} profile-scoped specialist outputs",
        ))
        persisted = owner_os.intelligence_orchestrator.get_run(
            orchestrated["trace_id"], "org_demo", "ws_alpha", "person_demo_owner"
        )
        checks.append(_case(
            "orchestrator_trace_persistence", persisted is not None and persisted.get("trace_id") == orchestrated["trace_id"],
            "orchestrator trace is retrievable from the durable scoped store",
        ))

        def specialist_result(_ctx: dict[str, Any]) -> dict[str, Any]:
            return {
                "finding": "bounded finding", "evidence_for": [], "evidence_against": [],
                "assumptions": [], "unknowns": [], "hypothesis": "profile-specific dissent",
                "confidence": 0.8, "analogues": [], "risks": [], "options": [],
                "recommendation": {"summary": "review"}, "expected_impact": {"level": "medium"},
                "needs_review": False, "dissent": [{"text": "opposing bounded view"}],
            }
        dissent_orchestrator = IntelligenceOrchestrator(
            owner_os,
            owner_os.intelligence_contracts,
            specialist_handlers={
                "account_strategist": specialist_result,
                "delivery_analyst": specialist_result,
            },
        )
        dissent_run = dissent_orchestrator.run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
                profile_ids=["account_strategist", "delivery_analyst"],
        )
        dissent_ok = "dissent" in dissent_run and isinstance(dissent_run["dissent"], list)
        checks.append(_case("orchestrator_dissent_retention", dissent_ok, "bounded dissent remains in the offline result"))

        # Exercise every native runbook contract through the same offline
        # orchestrator boundary. One profile receives an intentionally
        # unauthorized citation so each case proves fail-closed citation
        # handling, bounded specialist fan-out, and a usable degraded brief.
        runbooks = owner_os.intelligence_contracts.list_runbooks(
            "org_demo", "ws_alpha", "person_demo_owner"
        )
        canonical_tables = ("facts", "decisions", "work_items")
        before_counts = {
            table: owner_os.store.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE workspace_id=?",
                ("ws_alpha",),
            ).fetchone()[0]
            for table in canonical_tables
        }
        def visible_refs(value: Any) -> set[tuple[str, str]]:
            refs: set[tuple[str, str]] = set()
            def visit(node: Any) -> None:
                if isinstance(node, dict):
                    ref = node.get("object_ref") or node.get("ref")
                    if isinstance(ref, dict) and ref.get("type") and ref.get("id"):
                        refs.add((str(ref["type"]), str(ref["id"])))
                    for child in node.values():
                        visit(child)
                elif isinstance(node, list):
                    for child in node:
                        visit(child)
            visit(owner)
            return refs
        allowed_refs = visible_refs(owner)
        for runbook in runbooks:
            runbook_id = str(runbook["id"])
            profile_ids_for_runbook = [str(item) for item in runbook.get("profile_ids", [])]
            malformed_profile = profile_ids_for_runbook[0] if profile_ids_for_runbook else None
            def malformed(_context: dict[str, Any]) -> dict[str, Any]:
                return {
                    "finding": "malformed citation fixture", "evidence_for": [{"ref": "unauthorized"}],
                    "evidence_against": [], "assumptions": [], "unknowns": [],
                    "hypothesis": "fixture hypothesis", "confidence": 0.8,
                    "analogues": [], "risks": [], "options": [],
                    "recommendation": {"summary": "Review the bounded fallback."},
                    "expected_impact": {"level": "unknown"}, "needs_review": False,
                }
            degraded = IntelligenceOrchestrator(
                owner_os,
                owner_os.intelligence_contracts,
                specialist_handlers={malformed_profile: malformed} if malformed_profile else {},
            ).run(
                "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
                runbook_id=runbook_id, profile_ids=profile_ids_for_runbook,
            )
            safe_citations = all(
                (str(item.get("ref", {}).get("type")), str(item.get("ref", {}).get("id"))) in allowed_refs
                for specialist in degraded.get("specialists", [])
                for field in ("evidence_for", "evidence_against", "analogues", "dissent")
                for item in specialist.get(field, [])
                if isinstance(item, dict) and isinstance(item.get("ref"), dict)
            )
            usable = isinstance(degraded.get("recommendation"), dict) and bool(
                degraded.get("recommendation", {}).get("summary")
            )
            checks.append(_case(
                f"runbook_{runbook_id}",
                degraded.get("runbook_route", {}).get("status") == "matched"
                and degraded.get("runbook", {}).get("id") == runbook_id
                and degraded.get("status") == "degraded"
                and len(degraded.get("profiles", [])) <= 13
                and safe_citations and usable and degraded.get("needs_review") is True,
                f"{runbook_id}: bounded degraded fallback with citation safety",
            ))
        after_counts = {
            table: owner_os.store.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE workspace_id=?",
                ("ws_alpha",),
            ).fetchone()[0]
            for table in canonical_tables
        }
        checks.append(_case(
            "runbook_evaluation_no_canonical_writes",
            before_counts == after_counts,
            "all twelve runbook evaluations leave facts, decisions, and work items unchanged",
        ))
    finally:
        owner_os.close()

    model_os = _seed(_StructuredEvaluationProvider())
    try:
        model = model_os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        deliberation = model["deliberation"]
        fields = ("hypotheses", "options", "scenarios", "recommendation", "confidence", "dissent")
        structured_ok = (
            deliberation.get("mode") == "model_backed"
            and all(field in deliberation for field in fields)
            and deliberation["provider_metadata"]["status"] == "used"
        )
        checks.append(_case(
            "structured_reasoning", structured_ok,
            "provider output is schema-validated and exposed as deliberation",
        ))
    finally:
        model_os.close()

    fallback_os = _seed(_MalformedEvaluationProvider())
    try:
        fallback = fallback_os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        fallback_ok = (
            fallback["deliberation"].get("mode") == "deterministic_evidence_review"
            and fallback["deliberation"]["provider_metadata"]["status"] == "fallback"
            and bool(fallback["findings"])
        )
        checks.append(_case(
            "deterministic_fallback", fallback_ok,
            "malformed provider output leaves deterministic intelligence intact",
        ))
    finally:
        fallback_os.close()

    passed = sum(1 for check in checks if check["passed"])
    return {
        "suite": "auremgrid_intelligence_contract",
        "version": 1,
        "passed": passed == len(checks),
        "summary": {"passed": passed, "total": len(checks)},
        "checks": checks,
    }
