from __future__ import annotations

"""Small, offline evaluation harness for the Intelligence contract.

The scenarios are deliberately product-facing rather than model benchmarks:
they verify evidence provenance, ACL behavior, uncertainty, structured
reasoning, approval boundaries, and deterministic degradation.  They do not
contact a provider or mutate a durable database.
"""

from pathlib import Path
import json
from datetime import datetime, timezone
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


def _seed_decision_learning_fixture(os: CompanyOS) -> None:
    """Add a small, deterministic decision→outcome→learning chain."""
    decision = os.create_decision(
        "org_demo", "person_demo_owner", "Retargeting ad set delivery",
        "Keep the retargeting delivery owner-led and measure completion.",
        workspace_id="ws_alpha", evidence="evaluation fixture", tags=["evaluation_fixture"],
    )
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn = os.store.conn
    conn.execute(
        "UPDATE decisions SET effective_from=? WHERE id=?", ("2026-01-01T00:00:00+00:00", decision.id)
    )
    conn.execute(
        "INSERT INTO work_events(id,workspace_id,work_item_id,actor_id,action,from_status,to_status,detail,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("wev_evaluation_outcome", "ws_alpha", "work_demo_retargeting_ads", "act_alpha_admin",
         "transition", "assigned", "completed", "Retargeting ad set completed after decision", now),
    )
    conn.execute(
        "INSERT INTO feedback_events(id,organization_id,workspace_id,pattern_id,category,raw_feedback,source_type,source_id,recorded_by_person_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("feedback_evaluation_learning", "org_demo", "ws_alpha", None, "delivery",
         "Retargeting delivery completed cleanly after assigning an owner.", "evaluation", None,
         "person_demo_owner", now),
    )
    conn.commit()


def _canonical_refs_resolve(os: CompanyOS, value: Any, workspace_id: str) -> bool:
    tables = {
        "fact": "facts", "work_item": "work_items", "risk": "risks", "decision": "decisions",
        "signal": "signals", "work_event": "work_events", "client_health_snapshot": "client_health_snapshots",
        "feedback_event": "feedback_events", "performance_insight": "performance_insights",
    }
    refs: list[dict[str, Any]] = []
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("object_ref") or node.get("ref")
            if isinstance(ref, dict) and ref.get("type") and ref.get("id"):
                refs.append(ref)
            for child in node.values(): visit(child)
        elif isinstance(node, list):
            for child in node: visit(child)
    visit(value)
    if not refs:
        return False
    for ref in refs:
        table = tables.get(str(ref["type"]))
        if table is None:
            return False
        row = os.store.conn.execute(f"SELECT * FROM {table} WHERE id=?", (str(ref["id"]),)).fetchone()
        if row is None:
            return False
        if "workspace_id" in row.keys() and row["workspace_id"] != workspace_id:
            return False
    return True


def run_intelligence_evaluations() -> dict[str, Any]:
    """Run all built-in checks and return a stable JSON-compatible report."""
    checks: list[dict[str, Any]] = []
    owner_os = _seed()
    try:
        _seed_decision_learning_fixture(owner_os)
        owner = owner_os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        evidence = [item for finding in owner["findings"] for item in finding.get("evidence", [])]
        citations_ok = bool(evidence) and all(
            item.get("citation") and item.get("object_ref")
            and _canonical_refs_resolve(owner_os, item, "ws_alpha")
            and (item["citation"].get("source_id") is None or owner_os.store.conn.execute(
                "SELECT 1 FROM sources WHERE id=? AND workspace_id=?", (item["citation"]["source_id"], "ws_alpha")
            ).fetchone() is not None)
            for item in evidence
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

        # A stale graph/evidence watermark must be visible to callers as a
        # degraded, uncertain read rather than silently presented as current.
        owner_os.graph_health = {"status": "degraded", "detail": "stale_snapshot"}
        stale = owner_os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin",
        )
        stale_ok = (
            stale.get("status") == "degraded"
            and stale.get("uncertainty", {}).get("label") == "medium"
            and stale.get("degraded_reason")
            and stale.get("uncertainty", {}).get("reason") == stale.get("degraded_reason")
        )
        checks.append(_case("stale_evidence_degraded", stale_ok, "stale evidence watermark is explicit in status and uncertainty"))
        owner_os.graph_health = {}

        decision_links = owner.get("decision_action_outcome_learning", [])
        chain_ok = any(
            link.get("decision", {}).get("id")
            and link.get("outcomes")
            and link.get("learnings")
            and (link.get("evaluation") or {}).get("status") == "validated"
            for link in decision_links
        )
        checks.append(_case("decision_outcome_learning_chain", chain_ok, "fixture decision links to a terminal outcome and learning record"))
        assumptions_ok = bool(owner.get("recommended_plan", {}).get("constraints")) and all(
            finding.get("hypotheses") and any(h.get("assumptions") for h in finding.get("hypotheses", []))
            for finding in owner.get("findings", [])
        )
        checks.append(_case("assumptions_visible", assumptions_ok, "findings and plan expose explicit assumptions or constraints"))

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
            and {item.get("profile", {}).get("id") for item in specialists} == set(profile_ids)
            and all(item.get("evidence_for") and item.get("evidence_against") for item in specialists)
            and all(item.get("assumptions") and item.get("unknowns") for item in specialists)
            and all(_canonical_refs_resolve(owner_os, item, "ws_alpha") for item in specialists)
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

        # Golden contract checks: every confidence value is bounded and the
        # read-model sections required for scenario/historical reasoning are
        # always present, while action descriptors stay fenced behind the
        # approval boundary.
        bounded_confidence = (
            0.0 <= float(orchestrated.get("confidence", 0.0)) <= 1.0
            and all(0.0 <= float(item.get("confidence", 0.0)) <= 1.0 for item in specialists)
        )
        checks.append(_case(
            "bounded_confidence", bounded_confidence,
            "final and specialist confidence values remain within [0, 1]",
        ))
        read_models_explicit = (
            isinstance(orchestrated.get("scenario_analysis"), dict)
            and isinstance(orchestrated.get("historical_learning"), dict)
            and "status" in orchestrated["scenario_analysis"]
            and "status" in orchestrated["historical_learning"]
        )
        checks.append(_case(
            "scenario_historical_sections", read_models_explicit,
            "scenario assumptions and historical analogue status remain explicit",
        ))
        descriptors_fenced = all(
            item.get("disabled") is True and item.get("safe") is False
            and item.get("executable") is not True
            for item in orchestrated.get("action_descriptors", [])
        )
        checks.append(_case(
            "action_descriptors_fenced", descriptors_fenced,
            "orchestration action descriptors cannot bypass approval or execute writes",
        ))

        # V1 golden questions: each product question must have a bounded,
        # cited answer surface (or an explicit unknown/degraded state).  Keep
        # these checks deliberately structural so they remain deterministic
        # across fixture refreshes and provider versions.
        question_checks = {
            "attention": bool(owner.get("findings")),
            "risk": isinstance(owner.get("domains", {}).get("risks", {}).get("items"), list),
            "change": isinstance(owner.get("context", {}).get("change_count"), int) and owner["context"]["change_count"] > 0,
            "overdue": any("overdue" in json.dumps(f).lower() or "past its expected date" in json.dumps(f).lower() for f in owner.get("findings", [])),
            "scope": isinstance(owner.get("domains", {}).get("scope"), dict) and "usage_count" in owner["domains"]["scope"],
            "slip": any("slip" in json.dumps(f).lower() or "deadline" in json.dumps(f).lower() for f in owner.get("findings", [])),
            "overload": isinstance(owner.get("domains", {}).get("capacity"), dict) and "demand_hours" in owner["domains"]["capacity"],
            "campaign": isinstance(owner.get("domains", {}).get("campaign_metrics"), dict) and "items" in owner["domains"]["campaign_metrics"],
            "creative_fatigue": "creative" in json.dumps(owner).lower() or "campaign" in json.dumps(owner).lower(),
            "opportunity": isinstance(owner.get("recommendations"), list) and owner.get("recommendations") is not None,
            "analogue": isinstance(owner.get("historical_analogues"), list),
            "options": bool(owner.get("recommended_plan", {}).get("steps")),
            "scenario": any(
                f.get("scenarios") and any(s.get("assumptions") for s in f.get("scenarios", []))
                for f in owner.get("findings", [])
            ),
            "recommendation": isinstance(owner.get("recommendation_evaluation"), dict) and bool(owner["recommendation_evaluation"].get("status")),
            "opposing_evidence": any(f.get("hypotheses") and any(h.get("opposing_evidence") for h in f.get("hypotheses", [])) for f in owner.get("findings", [])),
            "confidence": bool(owner.get("findings")) and all(0.0 <= float((f.get("confidence") or {}).get("score", 0.0)) <= 1.0 for f in owner.get("findings", [])),
        }
        for question, passed in question_checks.items():
            checks.append(_case(
                f"golden_{question}", passed,
                f"{question.replace('_', ' ')} is represented by a bounded read-model field",
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
                and len(degraded.get("profiles", [])) == len(profile_ids_for_runbook)
                and {item.get("id") for item in degraded.get("profiles", [])} == set(profile_ids_for_runbook)
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
