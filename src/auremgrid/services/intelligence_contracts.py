from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.intelligence_contracts import ExpertProfile, ExpertResult, IntelligenceRunbook
from auremgrid.domain.models import AGENT_LEVEL_ORDER, normalize_agent_level


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, Any]) -> str:
    clean = {key: value for key, value in payload.items() if key not in {"content_hash"}}
    return hashlib.sha256(_json(clean).encode("utf-8")).hexdigest()


def _loads(value: Any) -> Any:
    return json.loads(value or "[]")


def _level_index(value: str) -> int:
    normalized = normalize_agent_level(value)
    return AGENT_LEVEL_ORDER.index(normalized)


class IntelligenceContractService:
    """ACL-safe facade for immutable Intelligence expert/runbook definitions."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def seed_defaults(self) -> dict[str, int]:
        profile_count = 0
        runbook_count = 0
        with self.os.store.atomic(immediate=True):
            for raw in DEFAULT_EXPERT_PROFILES:
                profile = ExpertProfile.from_mapping(raw)
                payload = profile.to_dict()
                content_hash = _hash(payload)
                existing = self.conn.execute(
                    "SELECT content_hash FROM expert_profiles WHERE id=? AND version=?",
                    (profile.id, profile.version),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ValidationError(f"expert profile {profile.id} v{profile.version} changed without a new version")
                    continue
                self.conn.execute(
                    """INSERT INTO expert_profiles(
                        id,version,name,specialty,mission,required_inputs_json,allowed_domains_json,
                        allowed_tools_json,required_evidence_json,reasoning_method,output_schema_json,
                        evaluation_criteria_json,escalation_policy,fallback_policy,max_context,
                        max_iterations,capability_level,title,summary,domains_json,allowed_tool_refs_json,
                        reasoning_methods_json,activation_triggers_json,outputs_json,handoff_targets_json,
                        quality_gates_json,constraints_json,status,content_hash,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        profile.id, profile.version, profile.name, profile.specialty, profile.mission,
                        _json(profile.required_inputs), _json(profile.allowed_domains),
                        _json(profile.allowed_tools), _json(profile.required_evidence),
                        profile.reasoning_method, _json(profile.output_schema),
                        _json(profile.evaluation_criteria), profile.escalation_policy,
                        profile.fallback_policy, profile.max_context, profile.max_iterations,
                        profile.capability_level, profile.title, profile.summary,
                        _json(profile.domains), _json(profile.allowed_tool_refs),
                        _json(profile.reasoning_methods), _json(profile.activation_triggers),
                        _json(profile.outputs), _json(profile.handoff_targets),
                        _json(profile.quality_gates), _json(profile.constraints),
                        profile.status, content_hash, _now(),
                    ),
                )
                profile_count += 1
            valid_profile_ids = {item["id"] for item in DEFAULT_EXPERT_PROFILES}
            for raw in DEFAULT_INTELLIGENCE_RUNBOOKS:
                runbook = IntelligenceRunbook.from_mapping(raw)
                missing = [profile_id for profile_id in runbook.profile_ids if profile_id not in valid_profile_ids]
                if missing:
                    raise ValidationError(f"runbook {runbook.id} references unknown profiles: {', '.join(missing)}")
                payload = runbook.to_dict()
                content_hash = _hash(payload)
                existing = self.conn.execute(
                    "SELECT content_hash FROM intelligence_runbooks WHERE id=? AND version=?",
                    (runbook.id, runbook.version),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ValidationError(f"intelligence runbook {runbook.id} v{runbook.version} changed without a new version")
                    continue
                self.conn.execute(
                    """INSERT INTO intelligence_runbooks(
                        id,version,name,trigger,required_domains_json,required_evidence_json,
                        specialists_json,topology,stages_json,quality_gates_json,contradiction_policy,
                        scenario_policy,escalation_policy,max_iterations,output_contract_json,
                        capability_level,summary,intent,domains_json,profile_ids_json,
                        activation_sequence_json,steps_json,handoff_gates_json,required_inputs_json,
                        outputs_json,stop_conditions_json,allowed_tool_refs_json,status,content_hash,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        runbook.id, runbook.version, runbook.name, runbook.trigger,
                        _json(runbook.required_domains), _json(runbook.required_evidence),
                        _json(runbook.specialists), runbook.topology,
                        _json([stage.to_dict() for stage in runbook.stages]),
                        _json(runbook.quality_gates), runbook.contradiction_policy,
                        runbook.scenario_policy, runbook.escalation_policy,
                        runbook.max_iterations, _json(runbook.output_contract),
                        runbook.capability_level, runbook.summary, runbook.intent,
                        _json(runbook.domains), _json(runbook.profile_ids), _json(runbook.activation_sequence),
                        _json([step.to_dict() for step in runbook.steps]), _json(runbook.handoff_gates),
                        _json(runbook.required_inputs), _json(runbook.outputs),
                        _json(runbook.stop_conditions), _json(runbook.allowed_tool_refs),
                        runbook.status, content_hash, _now(),
                    ),
                )
                runbook_count += 1
        return {"profiles_inserted": profile_count, "runbooks_inserted": runbook_count}

    def list_profiles(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        *,
        domain: str | None = None,
        capability_level: str | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        scope = self._scope(organization_id, workspace_id, person_id, capabilities)
        profiles = [self._profile_from_row(row) for row in self._active_rows("expert_profiles")]
        if domain:
            profiles = [profile for profile in profiles if domain in profile.domains]
        if capability_level:
            maximum = _level_index(capability_level)
            profiles = [profile for profile in profiles if _level_index(profile.capability_level) <= maximum]
        return tuple(self._filter_payload(profile.to_dict(), scope["capabilities"]) for profile in profiles)

    def get_profile(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        profile_id: str,
        *,
        version: int | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(organization_id, workspace_id, person_id, capabilities)
        row = self._definition_row("expert_profiles", profile_id, version)
        return self._filter_payload(self._profile_from_row(row).to_dict(), scope["capabilities"])

    def list_runbooks(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        *,
        domain: str | None = None,
        profile_id: str | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        scope = self._scope(organization_id, workspace_id, person_id, capabilities)
        runbooks = [self._runbook_from_row(row) for row in self._active_rows("intelligence_runbooks")]
        if domain:
            runbooks = [runbook for runbook in runbooks if domain in runbook.domains]
        if profile_id:
            runbooks = [runbook for runbook in runbooks if profile_id in runbook.profile_ids]
        return tuple(self._filter_payload(runbook.to_dict(), scope["capabilities"]) for runbook in runbooks)

    def get_runbook(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        runbook_id: str,
        *,
        version: int | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(organization_id, workspace_id, person_id, capabilities)
        row = self._definition_row("intelligence_runbooks", runbook_id, version)
        return self._filter_payload(self._runbook_from_row(row).to_dict(), scope["capabilities"])

    def expert_result(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        profile_id: str,
        *,
        capabilities: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(organization_id, workspace_id, person_id, capabilities)
        profile = self.get_profile(
            organization_id, workspace_id, person_id, profile_id, capabilities=scope["capabilities"]
        )
        runbooks = self.list_runbooks(
            organization_id, workspace_id, person_id, profile_id=profile_id, capabilities=scope["capabilities"]
        )
        return ExpertResult(
            status="available",
            scope={key: value for key, value in scope.items() if key != "capabilities"},
            profile=profile,
            runbooks=runbooks,
            allowed_actions=(),
        ).to_dict()

    def list_profiles_for_identity(self, identity: Any, workspace_id: str, **filters: Any) -> tuple[dict[str, Any], ...]:
        scoped = self._identity_scope(identity, workspace_id)
        return self.list_profiles(
            scoped.organization_id, workspace_id, scoped.person_id,
            capabilities=scoped.capabilities, **filters,
        )

    def list_runbooks_for_identity(self, identity: Any, workspace_id: str, **filters: Any) -> tuple[dict[str, Any], ...]:
        scoped = self._identity_scope(identity, workspace_id)
        return self.list_runbooks(
            scoped.organization_id, workspace_id, scoped.person_id,
            capabilities=scoped.capabilities, **filters,
        )

    def _identity_scope(self, identity: Any, workspace_id: str) -> Any:
        if identity.workspace_id not in {None, workspace_id}:
            raise AuthorizationError("identity workspace mismatch")
        return self.os.auth.scope_identity(identity, workspace_id) if identity.workspace_id is None else identity

    def _scope(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        capabilities: Iterable[str] | None,
    ) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        return {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "person_id": person_id,
            "capabilities": frozenset(capabilities or ()),
        }

    def _active_rows(self, table: str) -> list[Any]:
        return self.conn.execute(
            f"""SELECT * FROM {table}
                WHERE status='active'
                  AND version=(SELECT MAX(version) FROM {table} latest WHERE latest.id={table}.id)
                ORDER BY name,id""",
        ).fetchall()

    def _definition_row(self, table: str, item_id: str, version: int | None) -> Any:
        if version is None:
            row = self.conn.execute(
                f"SELECT * FROM {table} WHERE id=? AND status='active' ORDER BY version DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                f"SELECT * FROM {table} WHERE id=? AND version=? AND status='active'",
                (item_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"intelligence definition not found: {item_id}")
        return row

    @staticmethod
    def _profile_from_row(row: Any) -> ExpertProfile:
        return ExpertProfile.from_mapping({
            "id": row["id"],
            "version": row["version"],
            "name": row["name"],
            "specialty": row["specialty"],
            "mission": row["mission"],
            "required_inputs": _loads(row["required_inputs_json"]),
            "allowed_domains": _loads(row["allowed_domains_json"]),
            "allowed_tools": _loads(row["allowed_tools_json"]),
            "required_evidence": _loads(row["required_evidence_json"]),
            "reasoning_method": row["reasoning_method"],
            "output_schema": json.loads(row["output_schema_json"] or "{}"),
            "evaluation_criteria": _loads(row["evaluation_criteria_json"]),
            "escalation_policy": row["escalation_policy"],
            "fallback_policy": row["fallback_policy"],
            "max_context": row["max_context"],
            "max_iterations": row["max_iterations"],
            "capability_level": row["capability_level"],
            "title": row["title"],
            "summary": row["summary"],
            "domains": _loads(row["domains_json"]),
            "allowed_tool_refs": _loads(row["allowed_tool_refs_json"]),
            "reasoning_methods": _loads(row["reasoning_methods_json"]),
            "activation_triggers": _loads(row["activation_triggers_json"]),
            "outputs": _loads(row["outputs_json"]),
            "handoff_targets": _loads(row["handoff_targets_json"]),
            "quality_gates": _loads(row["quality_gates_json"]),
            "constraints": _loads(row["constraints_json"]),
            "status": row["status"],
            "content_hash": row["content_hash"],
        })

    @staticmethod
    def _runbook_from_row(row: Any) -> IntelligenceRunbook:
        return IntelligenceRunbook.from_mapping({
            "id": row["id"],
            "version": row["version"],
            "name": row["name"],
            "trigger": row["trigger"],
            "required_domains": _loads(row["required_domains_json"]),
            "required_evidence": _loads(row["required_evidence_json"]),
            "specialists": _loads(row["specialists_json"]),
            "topology": row["topology"],
            "stages": _loads(row["stages_json"]),
            "quality_gates": _loads(row["quality_gates_json"]),
            "contradiction_policy": row["contradiction_policy"],
            "scenario_policy": row["scenario_policy"],
            "escalation_policy": row["escalation_policy"],
            "max_iterations": row["max_iterations"],
            "output_contract": json.loads(row["output_contract_json"] or "{}"),
            "capability_level": row["capability_level"],
            "summary": row["summary"],
            "intent": row["intent"],
            "domains": _loads(row["domains_json"]),
            "profile_ids": _loads(row["profile_ids_json"]),
            "activation_sequence": _loads(row["activation_sequence_json"]),
            "steps": _loads(row["steps_json"]),
            "handoff_gates": _loads(row["handoff_gates_json"]),
            "required_inputs": _loads(row["required_inputs_json"]),
            "outputs": _loads(row["outputs_json"]),
            "stop_conditions": _loads(row["stop_conditions_json"]),
            "allowed_tool_refs": _loads(row["allowed_tool_refs_json"]),
            "status": row["status"],
            "content_hash": row["content_hash"],
        })

    @classmethod
    def _filter_payload(cls, payload: dict[str, Any], capabilities: frozenset[str]) -> dict[str, Any]:
        payload = json.loads(json.dumps(payload))
        payload["allowed_tool_refs"] = [
            tool for tool in payload.get("allowed_tool_refs", [])
            if cls._tool_visible(tool, capabilities)
        ]
        payload["allowed_tools"] = [
            tool for tool in payload.get("allowed_tools", [])
            if cls._tool_visible(tool, capabilities)
        ]
        return payload

    @staticmethod
    def _tool_visible(tool_ref: str, capabilities: frozenset[str]) -> bool:
        if not capabilities:
            return tool_ref in {"dashboard.intelligence.read", "brain.search", "work.list", "projects.list"}
        required = {
            "dashboard.intelligence.read": "workspace_read",
            "brain.search": "brain_read",
            "work.list": "workspace_read",
            "projects.list": "workspace_read",
            "risks.list": "workspace_read",
            "campaigns.list": "workspace_read",
            "finance.read": "finance_read",
            "capacity.read": "workspace_read",
            "reports.generate": "workspace_write",
            "approvals.request": "workspace_write",
            "agents.task.create": "agent_run",
        }.get(tool_ref)
        return required is None or required in capabilities


COMMON_CONSTRAINTS = (
    "Use only visible canonical evidence and approved read models",
    "Return descriptors only; never execute work or external sends",
    "Do not include generative instruction blocks or broad tenant context",
)


EXPERT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["finding", "confidence", "evidence", "unknowns", "handoff"],
}


def _profile(raw: dict[str, Any]) -> dict[str, Any]:
    tools = tuple(raw["allowed_tool_refs"])
    domains = tuple(raw["domains"])
    methods = tuple(raw["reasoning_methods"])
    evidence = tuple(raw["evidence_requirements"])
    return {
        **raw,
        "specialty": raw.get("specialty") or raw["title"],
        "mission": raw.get("mission") or raw["summary"],
        "required_inputs": raw.get("required_inputs") or raw["inputs_required"],
        "allowed_domains": raw.get("allowed_domains") or domains,
        "allowed_tools": raw.get("allowed_tools") or tools,
        "required_evidence": raw.get("required_evidence") or evidence,
        "reasoning_method": raw.get("reasoning_method") or methods[0],
        "output_schema": raw.get("output_schema") or EXPERT_OUTPUT_SCHEMA,
        "evaluation_criteria": raw.get("evaluation_criteria") or raw["quality_gates"],
        "escalation_policy": raw.get("escalation_policy") or "Escalate when evidence is missing, contradictory, or action is one-way",
        "fallback_policy": raw.get("fallback_policy") or "Return insufficient_evidence with unknowns and no action descriptors",
        "max_context": int(raw.get("max_context") or 24),
        "max_iterations": int(raw.get("max_iterations") or 3),
    }


DEFAULT_EXPERT_PROFILES: tuple[dict[str, Any], ...] = tuple(_profile(item) for item in (
    {
        "id": "cosmo_strategy_architect", "version": 1, "name": "Strategy Architect",
        "title": "Cross-domain strategy and decision framing",
        "summary": "Frames ambiguous agency situations into reversible choices, explicit assumptions, and decision gates.",
        "domains": ["strategy", "portfolio", "client_success"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "projects.list", "work.list"],
        "capability_level": "L3",
        "reasoning_methods": ["goal decomposition", "second-order effect scan", "two-way-door classification"],
        "activation_triggers": ["ambiguous executive decision", "cross-client priority conflict"],
        "inputs_required": ["scope contract", "visible evidence", "decision owner"],
        "outputs": ["decision frame", "tradeoff summary", "recommended gate"],
        "handoff_targets": ["cosmo_operations_controller", "cosmo_client_growth_strategist"],
        "quality_gates": ["assumptions are falsifiable", "one-way actions require approval"],
        "evidence_requirements": ["current decision records", "client/workspace facts", "recent operating signals"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_client_growth_strategist", "version": 1, "name": "Client Growth Strategist",
        "title": "Client account growth and retention strategy",
        "summary": "Connects client objectives, risks, opportunities, and delivery state into grounded growth moves.",
        "domains": ["client_success", "growth", "retention"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "risks.list", "projects.list"],
        "capability_level": "L3",
        "reasoning_methods": ["client objective map", "risk/opportunity separation", "stakeholder impact check"],
        "activation_triggers": ["churn risk", "upsell opportunity", "client planning review"],
        "inputs_required": ["client health", "risks", "opportunities", "current brain facts"],
        "outputs": ["client move", "retention risk note", "account handoff"],
        "handoff_targets": ["cosmo_performance_marketer", "cosmo_delivery_lead"],
        "quality_gates": ["recommendation cites client evidence", "commercial claim stays bounded"],
        "evidence_requirements": ["client health snapshot", "communication or meeting evidence"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_performance_marketer", "version": 1, "name": "Performance Marketer",
        "title": "Paid media and funnel performance diagnosis",
        "summary": "Diagnoses campaign and funnel variance without claiming causality from relevance alone.",
        "domains": ["paid_media", "growth", "analytics"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "campaigns.list", "brain.search"],
        "capability_level": "L2",
        "reasoning_methods": ["variance tree", "metric denominator check", "counterfactual candidate scan"],
        "activation_triggers": ["metric variance", "campaign launch review", "lead quality concern"],
        "inputs_required": ["campaign metrics", "funnel facts", "change log"],
        "outputs": ["variance diagnosis", "testable hypothesis", "measurement gate"],
        "handoff_targets": ["cosmo_data_analyst", "cosmo_conversion_copywriter"],
        "quality_gates": ["separate symptoms from causes", "include opposing evidence"],
        "evidence_requirements": ["latest metric snapshot", "recent campaign or work changes"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_conversion_copywriter", "version": 1, "name": "Conversion Copywriter",
        "title": "Offer, message, and landing-page copy review",
        "summary": "Turns cited offer and audience facts into conversion copy critique and revision direction.",
        "domains": ["content", "landing_pages", "conversion"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "work.list"],
        "capability_level": "L2",
        "reasoning_methods": ["message hierarchy check", "claim substantiation", "friction audit"],
        "activation_triggers": ["landing page review", "ad copy review", "offer mismatch"],
        "inputs_required": ["approved offer", "audience intent", "page or ad artifact"],
        "outputs": ["copy diagnosis", "claim evidence list", "revision brief"],
        "handoff_targets": ["cosmo_creative_director", "cosmo_delivery_lead"],
        "quality_gates": ["every claim has evidence", "no invented pricing or guarantees"],
        "evidence_requirements": ["approved offer fact", "brand or client brain rule"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_creative_director", "version": 1, "name": "Creative Director",
        "title": "Creative quality, brand fit, and asset handoff",
        "summary": "Reviews creative direction against brand rules, channel fit, and production constraints.",
        "domains": ["design", "creative", "brand"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "work.list"],
        "capability_level": "L2",
        "reasoning_methods": ["brand-rule diff", "channel fit review", "asset readiness gate"],
        "activation_triggers": ["creative review", "asset package handoff", "brand inconsistency"],
        "inputs_required": ["brand rules", "asset state", "channel requirements"],
        "outputs": ["creative critique", "handoff checklist", "revision gate"],
        "handoff_targets": ["cosmo_performance_marketer", "cosmo_delivery_lead"],
        "quality_gates": ["brand constraints explicit", "platform requirements named"],
        "evidence_requirements": ["current brand facts", "asset or deliverable records"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_product_engineer", "version": 1, "name": "Product Engineer",
        "title": "Product, implementation, and release reasoning",
        "summary": "Translates product work into acceptance, verification, release, and rollback gates.",
        "domains": ["product", "engineering", "release"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "projects.list", "work.list"],
        "capability_level": "L2",
        "reasoning_methods": ["acceptance criteria decomposition", "dependency check", "release risk review"],
        "activation_triggers": ["development release", "technical blocker", "acceptance ambiguity"],
        "inputs_required": ["work item", "acceptance criteria", "known dependencies"],
        "outputs": ["implementation risk note", "verification plan", "release handoff"],
        "handoff_targets": ["cosmo_qa_reviewer", "cosmo_operations_controller"],
        "quality_gates": ["tests or verification named", "rollback or monitoring considered"],
        "evidence_requirements": ["work state", "project decision", "release evidence"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_qa_reviewer", "version": 1, "name": "QA Reviewer",
        "title": "Evidence-backed quality and acceptance review",
        "summary": "Checks whether a deliverable satisfies the stated definition of done and review evidence.",
        "domains": ["quality", "reviews", "delivery"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "work.list", "projects.list"],
        "capability_level": "L2",
        "reasoning_methods": ["definition-of-done trace", "edge-case checklist", "rejection route check"],
        "activation_triggers": ["review gate", "launch gate", "quality dispute"],
        "inputs_required": ["review state", "required evidence", "definition of done"],
        "outputs": ["quality finding", "missing evidence list", "approval risk"],
        "handoff_targets": ["cosmo_delivery_lead", "cosmo_product_engineer"],
        "quality_gates": ["required evidence complete", "rework route explicit"],
        "evidence_requirements": ["review record", "work event", "approval state"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_data_analyst", "version": 1, "name": "Data Analyst",
        "title": "Metric, cohort, and evidence interpretation",
        "summary": "Normalizes metric reads, highlights missing denominators, and turns data into bounded findings.",
        "domains": ["analytics", "finance", "performance"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "finance.read", "campaigns.list"],
        "capability_level": "L2",
        "reasoning_methods": ["denominator audit", "period alignment", "confidence calibration"],
        "activation_triggers": ["metric anomaly", "finance readout", "report preparation"],
        "inputs_required": ["metric snapshot", "time period", "source status"],
        "outputs": ["metric readout", "data quality note", "confidence label"],
        "handoff_targets": ["cosmo_performance_marketer", "cosmo_finance_operator"],
        "quality_gates": ["periods reconciled", "missing values called out"],
        "evidence_requirements": ["canonical metric rows", "source freshness"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_finance_operator", "version": 1, "name": "Finance Operator",
        "title": "Revenue, margin, and billing context",
        "summary": "Reads connected finance and scope signals without inventing unavailable financial values.",
        "domains": ["finance", "scope", "revenue"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "finance.read", "reports.generate"],
        "capability_level": "L2",
        "reasoning_methods": ["cash/revenue separation", "scope-to-margin bridge", "unknown-value preservation"],
        "activation_triggers": ["margin concern", "scope usage review", "revenue report"],
        "inputs_required": ["finance connection status", "scope usage", "contract records"],
        "outputs": ["finance constraint", "scope pressure note", "report descriptor"],
        "handoff_targets": ["cosmo_strategy_architect", "cosmo_operations_controller"],
        "quality_gates": ["disconnected finance remains null", "source status included"],
        "evidence_requirements": ["finance connection or explicit not_connected state", "contract/scope rows"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_operations_controller", "version": 1, "name": "Operations Controller",
        "title": "Workflow, capacity, and handoff control",
        "summary": "Coordinates operating signals into next-step plans, capacity checks, and escalation gates.",
        "domains": ["operations", "capacity", "workflow"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "work.list", "capacity.read", "approvals.request"],
        "capability_level": "L2",
        "reasoning_methods": ["queue triage", "capacity fit check", "handoff contract validation"],
        "activation_triggers": ["blocked handoff", "capacity conflict", "overdue work"],
        "inputs_required": ["work queue", "capacity board", "workflow state"],
        "outputs": ["operating plan", "handoff gate", "escalation descriptor"],
        "handoff_targets": ["cosmo_delivery_lead", "cosmo_strategy_architect"],
        "quality_gates": ["owner named", "next gate checkable", "capacity constraint explicit"],
        "evidence_requirements": ["work item", "workflow stage", "capacity snapshot"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_delivery_lead", "version": 1, "name": "Delivery Lead",
        "title": "Client delivery, accountability, and recovery",
        "summary": "Turns delivery state into accountable commitments, blockers, and client-safe recovery paths.",
        "domains": ["delivery", "client_success", "workflow"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "work.list", "projects.list", "approvals.request"],
        "capability_level": "L2",
        "reasoning_methods": ["commitment trace", "blocker/root-cause split", "client impact classification"],
        "activation_triggers": ["delivery slip", "blocked work", "client escalation"],
        "inputs_required": ["open work", "client commitment", "recent changes"],
        "outputs": ["delivery risk", "recovery plan", "client handoff note"],
        "handoff_targets": ["cosmo_operations_controller", "cosmo_client_growth_strategist"],
        "quality_gates": ["commitment has owner/date", "client-visible risk separated from internal cause"],
        "evidence_requirements": ["work events", "project state", "client signal"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_research_synthesist", "version": 1, "name": "Research Synthesist",
        "title": "Evidence synthesis and uncertainty management",
        "summary": "Collects visible evidence into concise synthesis, dissent, and unknowns.",
        "domains": ["research", "brain", "strategy"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search"],
        "capability_level": "L3",
        "reasoning_methods": ["evidence clustering", "opposing-evidence pass", "unknowns ledger"],
        "activation_triggers": ["research question", "low-confidence finding", "conflicting evidence"],
        "inputs_required": ["query", "visible citations", "time fence"],
        "outputs": ["synthesis", "dissent", "unknowns"],
        "handoff_targets": ["cosmo_strategy_architect", "cosmo_data_analyst"],
        "quality_gates": ["unknowns preserved", "citations visible to caller"],
        "evidence_requirements": ["permitted source citations", "effective read timestamp"],
        "constraints": COMMON_CONSTRAINTS,
    },
    {
        "id": "cosmo_risk_compliance_reviewer", "version": 1, "name": "Risk Compliance Reviewer",
        "title": "Risk, approval, and one-way-door review",
        "summary": "Flags irreversible, externally visible, or policy-sensitive actions before they become work.",
        "domains": ["risk", "compliance", "approvals"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "risks.list", "approvals.request"],
        "capability_level": "L3",
        "reasoning_methods": ["one-way-door review", "approval threshold check", "blast-radius scan"],
        "activation_triggers": ["external send", "launch approval", "policy-sensitive recommendation"],
        "inputs_required": ["proposed action", "risk evidence", "approval policy"],
        "outputs": ["risk review", "approval gate", "blocked action reason"],
        "handoff_targets": ["cosmo_strategy_architect", "cosmo_operations_controller"],
        "quality_gates": ["human checkpoint named for one-way action", "denied action does not leak hidden records"],
        "evidence_requirements": ["risk row or policy reference", "action descriptor"],
        "constraints": COMMON_CONSTRAINTS,
    },
))


def _step(step_id: str, sequence: int, title: str, owner: str, method: str, gate: str, handoff_to: str | None = None) -> dict[str, Any]:
    return {
        "id": step_id,
        "sequence": sequence,
        "title": title,
        "owner_profile_id": owner,
        "method": method,
        "required_inputs": ["scope contract", "visible evidence"] if sequence == 1 else ["prior step output"],
        "outputs": [title.lower()],
        "gate": gate,
        "handoff_to": handoff_to,
    }


RUNBOOK_OUTPUT_CONTRACT: dict[str, Any] = {
    "type": "object",
    "required": ["status", "finding", "evidence_for", "evidence_against", "recommendation", "needs_review"],
}


def _runbook(raw: dict[str, Any]) -> dict[str, Any]:
    domains = tuple(raw["domains"])
    evidence = tuple(raw["evidence_requirements"])
    profiles = tuple(raw["profile_ids"])
    return {
        **raw,
        "trigger": raw.get("trigger") or raw["activation_sequence"][0],
        "required_domains": raw.get("required_domains") or domains,
        "required_evidence": raw.get("required_evidence") or evidence,
        "specialists": raw.get("specialists") or profiles,
        "topology": raw.get("topology") or "bounded fanout -> contradiction check -> synthesis -> reality check",
        "stages": raw.get("stages") or raw["steps"],
        "contradiction_policy": raw.get("contradiction_policy") or "Preserve opposing evidence and force review on material disagreement",
        "scenario_policy": raw.get("scenario_policy") or "Use explicit retained inputs only; unknown values stay unknown",
        "escalation_policy": raw.get("escalation_policy") or "Escalate on one-way action, missing required evidence, or ACL uncertainty",
        "max_iterations": int(raw.get("max_iterations") or 3),
        "output_contract": raw.get("output_contract") or RUNBOOK_OUTPUT_CONTRACT,
    }


DEFAULT_INTELLIGENCE_RUNBOOKS: tuple[dict[str, Any], ...] = tuple(_runbook(item) for item in (
    {
        "id": "client_growth_diagnosis", "version": 1, "name": "Client Growth Diagnosis",
        "summary": "Diagnose growth or retention moves from client evidence and delivery state.",
        "intent": "Produce a bounded client growth recommendation without executing account work.",
        "domains": ["client_success", "growth", "delivery"],
        "profile_ids": ["cosmo_client_growth_strategist", "cosmo_delivery_lead", "cosmo_strategy_architect"],
        "activation_sequence": ["confirm client scope", "collect health/risk evidence", "frame reversible move", "handoff gated next step"],
        "steps": [
            _step("client-context", 1, "Confirm client context", "cosmo_client_growth_strategist", "client objective map", "scope"),
            _step("delivery-risk", 2, "Check delivery constraints", "cosmo_delivery_lead", "commitment trace", "evidence", "cosmo_strategy_architect"),
            _step("growth-frame", 3, "Frame growth move", "cosmo_strategy_architect", "two-way-door classification", "approval"),
        ],
        "handoff_gates": ["client evidence visible", "delivery constraint named", "one-way account action requires approval"],
        "required_inputs": ["client health", "risks/opportunities", "open commitments"],
        "outputs": ["client growth finding", "retention risk", "next-step descriptor"],
        "evidence_requirements": ["client health or signal", "delivery/work citation"],
        "stop_conditions": ["no visible client evidence", "workspace membership denied"],
        "quality_gates": ["recommendation cites evidence", "unknown finance remains unknown"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "work.list", "risks.list"],
        "capability_level": "L3",
    },
    {
        "id": "campaign_variance_review", "version": 1, "name": "Campaign Variance Review",
        "summary": "Explain paid media variance with metric and change evidence.",
        "intent": "Separate campaign symptoms, candidate causes, and measurement gaps.",
        "domains": ["paid_media", "analytics"],
        "profile_ids": ["cosmo_performance_marketer", "cosmo_data_analyst"],
        "activation_sequence": ["align period", "build variance tree", "inspect changes", "state confidence"],
        "steps": [
            _step("metric-period", 1, "Align metric period", "cosmo_data_analyst", "period alignment", "scope"),
            _step("variance-tree", 2, "Build variance tree", "cosmo_performance_marketer", "variance tree", "evidence"),
            _step("confidence", 3, "Calibrate confidence", "cosmo_data_analyst", "confidence calibration", "quality"),
        ],
        "handoff_gates": ["date range reconciled", "denominator checked", "opposing evidence included"],
        "required_inputs": ["metric snapshot", "campaign changes"],
        "outputs": ["variance diagnosis", "testable hypothesis"],
        "evidence_requirements": ["campaign metric citation", "change/work citation"],
        "stop_conditions": ["metric source stale beyond SLA"],
        "quality_gates": ["no causality from relevance alone"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "campaigns.list", "brain.search"],
        "capability_level": "L2",
    },
    {
        "id": "landing_page_conversion_review", "version": 1, "name": "Landing Page Conversion Review",
        "summary": "Review landing-page message, creative, and acceptance evidence.",
        "intent": "Produce a cited revision brief for page conversion work.",
        "domains": ["landing_pages", "conversion", "design"],
        "profile_ids": ["cosmo_conversion_copywriter", "cosmo_creative_director", "cosmo_qa_reviewer"],
        "activation_sequence": ["confirm offer", "review message hierarchy", "check brand/quality gate"],
        "steps": [
            _step("offer", 1, "Confirm offer evidence", "cosmo_conversion_copywriter", "claim substantiation", "scope"),
            _step("creative-fit", 2, "Review creative fit", "cosmo_creative_director", "brand-rule diff", "evidence", "cosmo_qa_reviewer"),
            _step("qa", 3, "Check acceptance", "cosmo_qa_reviewer", "definition-of-done trace", "approval"),
        ],
        "handoff_gates": ["approved offer visible", "brand constraints named", "definition of done checked"],
        "required_inputs": ["approved offer", "brand rules", "page work item"],
        "outputs": ["revision brief", "missing evidence list"],
        "evidence_requirements": ["offer fact", "brand fact", "work/review row"],
        "stop_conditions": ["offer evidence absent"],
        "quality_gates": ["no invented claims", "review route explicit"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "work.list"],
        "capability_level": "L2",
    },
    {
        "id": "creative_asset_gate", "version": 1, "name": "Creative Asset Gate",
        "summary": "Gate creative packages for channel fit, brand fit, and delivery readiness.",
        "intent": "Make asset handoffs checkable before trafficking or launch.",
        "domains": ["creative", "brand", "paid_media"],
        "profile_ids": ["cosmo_creative_director", "cosmo_performance_marketer", "cosmo_delivery_lead"],
        "activation_sequence": ["read brand constraints", "check channel requirements", "handoff delivery evidence"],
        "steps": [
            _step("brand", 1, "Check brand fit", "cosmo_creative_director", "brand-rule diff", "scope"),
            _step("channel", 2, "Check channel fit", "cosmo_performance_marketer", "channel fit review", "evidence"),
            _step("handoff", 3, "Prepare delivery handoff", "cosmo_delivery_lead", "commitment trace", "handoff"),
        ],
        "handoff_gates": ["asset manifest present", "channel specs named", "delivery link available"],
        "required_inputs": ["brand rules", "asset state", "channel requirement"],
        "outputs": ["asset gate decision", "handoff checklist"],
        "evidence_requirements": ["creative asset/deliverable row", "brand facts"],
        "stop_conditions": ["asset missing", "brand evidence missing"],
        "quality_gates": ["platform requirements explicit"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "work.list"],
        "capability_level": "L2",
    },
    {
        "id": "development_release_review", "version": 1, "name": "Development Release Review",
        "summary": "Frame build, QA, and launch-readiness risk for product changes.",
        "intent": "Expose release risk and verification gates without shipping anything.",
        "domains": ["product", "engineering", "release"],
        "profile_ids": ["cosmo_product_engineer", "cosmo_qa_reviewer", "cosmo_operations_controller"],
        "activation_sequence": ["confirm acceptance", "check implementation evidence", "prepare release gate"],
        "steps": [
            _step("acceptance", 1, "Confirm acceptance", "cosmo_product_engineer", "acceptance criteria decomposition", "scope"),
            _step("verification", 2, "Review verification", "cosmo_qa_reviewer", "edge-case checklist", "quality"),
            _step("release", 3, "Prepare release gate", "cosmo_operations_controller", "handoff contract validation", "approval"),
        ],
        "handoff_gates": ["acceptance criteria present", "verification evidence present", "rollback/monitoring named"],
        "required_inputs": ["work item", "test evidence", "release note"],
        "outputs": ["release risk note", "verification plan"],
        "evidence_requirements": ["work/review row", "release evidence"],
        "stop_conditions": ["acceptance ambiguous after refinement"],
        "quality_gates": ["rollback considered", "health gate checkable"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "projects.list", "work.list"],
        "capability_level": "L2",
    },
    {
        "id": "capacity_conflict_triage", "version": 1, "name": "Capacity Conflict Triage",
        "summary": "Resolve work/capacity conflicts into prioritized, gated next steps.",
        "intent": "Create a read-only triage frame for overloaded or blocked teams.",
        "domains": ["capacity", "operations", "delivery"],
        "profile_ids": ["cosmo_operations_controller", "cosmo_delivery_lead", "cosmo_strategy_architect"],
        "activation_sequence": ["collect work demand", "compare capacity", "classify tradeoff", "handoff escalation gate"],
        "steps": [
            _step("demand", 1, "Collect work demand", "cosmo_operations_controller", "queue triage", "scope"),
            _step("commitment", 2, "Trace commitments", "cosmo_delivery_lead", "commitment trace", "evidence"),
            _step("tradeoff", 3, "Classify tradeoff", "cosmo_strategy_architect", "second-order effect scan", "approval"),
        ],
        "handoff_gates": ["capacity source fresh", "client commitment identified", "priority decision owner named"],
        "required_inputs": ["capacity board", "open work", "deadline signals"],
        "outputs": ["capacity triage", "escalation descriptor"],
        "evidence_requirements": ["capacity snapshot", "work queue rows"],
        "stop_conditions": ["capacity data unavailable and no work evidence"],
        "quality_gates": ["owner/date named"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "capacity.read", "work.list", "approvals.request"],
        "capability_level": "L3",
    },
    {
        "id": "finance_scope_pressure_review", "version": 1, "name": "Finance Scope Pressure Review",
        "summary": "Review margin, scope, and finance constraints without inventing missing values.",
        "intent": "Tie scope pressure to visible finance and contract evidence.",
        "domains": ["finance", "scope", "operations"],
        "profile_ids": ["cosmo_finance_operator", "cosmo_operations_controller", "cosmo_strategy_architect"],
        "activation_sequence": ["check finance connection", "read scope usage", "frame commercial constraint"],
        "steps": [
            _step("finance-state", 1, "Check finance state", "cosmo_finance_operator", "unknown-value preservation", "scope"),
            _step("scope", 2, "Review scope pressure", "cosmo_operations_controller", "capacity fit check", "evidence"),
            _step("constraint", 3, "Frame commercial constraint", "cosmo_strategy_architect", "two-way-door classification", "approval"),
        ],
        "handoff_gates": ["finance status explicit", "scope citation present", "commercial action approval required"],
        "required_inputs": ["finance status", "scope usage", "contract"],
        "outputs": ["finance constraint", "scope risk"],
        "evidence_requirements": ["finance connection or not_connected state", "scope/contract row"],
        "stop_conditions": ["workspace denied"],
        "quality_gates": ["null finance stays null"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "finance.read", "reports.generate"],
        "capability_level": "L3",
    },
    {
        "id": "risk_escalation_review", "version": 1, "name": "Risk Escalation Review",
        "summary": "Gate risk escalation, approval, and action descriptors.",
        "intent": "Keep irreversible or externally visible actions behind human checkpoints.",
        "domains": ["risk", "approvals", "operations"],
        "profile_ids": ["cosmo_risk_compliance_reviewer", "cosmo_operations_controller"],
        "activation_sequence": ["classify action", "scan blast radius", "choose approval gate"],
        "steps": [
            _step("classify", 1, "Classify action", "cosmo_risk_compliance_reviewer", "one-way-door review", "scope"),
            _step("radius", 2, "Scan blast radius", "cosmo_risk_compliance_reviewer", "blast-radius scan", "evidence"),
            _step("gate", 3, "Choose approval gate", "cosmo_operations_controller", "handoff contract validation", "approval"),
        ],
        "handoff_gates": ["one-way-door state explicit", "approval role named"],
        "required_inputs": ["risk signal", "proposed action"],
        "outputs": ["risk review", "approval gate"],
        "evidence_requirements": ["risk row or action descriptor"],
        "stop_conditions": ["no action to classify"],
        "quality_gates": ["external action not executed"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "risks.list", "approvals.request"],
        "capability_level": "L3",
    },
    {
        "id": "evidence_synthesis_review", "version": 1, "name": "Evidence Synthesis Review",
        "summary": "Synthesize visible evidence, dissent, confidence, and unknowns.",
        "intent": "Answer a research question from permitted evidence only.",
        "domains": ["research", "brain", "strategy"],
        "profile_ids": ["cosmo_research_synthesist", "cosmo_data_analyst"],
        "activation_sequence": ["collect citations", "cluster evidence", "run dissent pass", "calibrate confidence"],
        "steps": [
            _step("collect", 1, "Collect citations", "cosmo_research_synthesist", "evidence clustering", "scope"),
            _step("dissent", 2, "Run dissent pass", "cosmo_research_synthesist", "opposing-evidence pass", "quality"),
            _step("calibrate", 3, "Calibrate confidence", "cosmo_data_analyst", "confidence calibration", "quality"),
        ],
        "handoff_gates": ["citations visible", "unknowns preserved"],
        "required_inputs": ["query", "visible citations", "as_of"],
        "outputs": ["synthesis", "dissent", "unknowns"],
        "evidence_requirements": ["permitted source citations"],
        "stop_conditions": ["no visible evidence"],
        "quality_gates": ["no hidden-source inference"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search"],
        "capability_level": "L3",
    },
    {
        "id": "client_report_readiness", "version": 1, "name": "Client Report Readiness",
        "summary": "Check whether a client-facing report has sufficient evidence and approval gates.",
        "intent": "Prepare report descriptors without publishing or sending externally.",
        "domains": ["reporting", "client_success", "approvals"],
        "profile_ids": ["cosmo_data_analyst", "cosmo_client_growth_strategist", "cosmo_risk_compliance_reviewer"],
        "activation_sequence": ["validate data", "shape client narrative", "check approval boundary"],
        "steps": [
            _step("data", 1, "Validate data", "cosmo_data_analyst", "denominator audit", "scope"),
            _step("narrative", 2, "Shape client narrative", "cosmo_client_growth_strategist", "stakeholder impact check", "quality"),
            _step("approval", 3, "Check approval boundary", "cosmo_risk_compliance_reviewer", "approval threshold check", "approval"),
        ],
        "handoff_gates": ["data source status explicit", "client-sensitive claims cited", "approval gate chosen"],
        "required_inputs": ["report type", "workspace evidence", "recipient context"],
        "outputs": ["report readiness", "approval descriptor"],
        "evidence_requirements": ["metric/work/risk citations"],
        "stop_conditions": ["report source unavailable"],
        "quality_gates": ["no external send", "approval required for publication"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "reports.generate", "approvals.request"],
        "capability_level": "L3",
    },
    {
        "id": "workflow_handoff_audit", "version": 1, "name": "Workflow Handoff Audit",
        "summary": "Audit workflow handoffs, gates, and rework paths for current work.",
        "intent": "Surface missing handoff or gate evidence before a stage moves.",
        "domains": ["workflow", "operations", "quality"],
        "profile_ids": ["cosmo_operations_controller", "cosmo_qa_reviewer", "cosmo_delivery_lead"],
        "activation_sequence": ["read stage state", "check required evidence", "confirm rework route"],
        "steps": [
            _step("stage", 1, "Read stage state", "cosmo_operations_controller", "handoff contract validation", "scope"),
            _step("evidence", 2, "Check required evidence", "cosmo_qa_reviewer", "definition-of-done trace", "quality"),
            _step("route", 3, "Confirm rework route", "cosmo_delivery_lead", "blocker/root-cause split", "handoff"),
        ],
        "handoff_gates": ["stage visible", "required evidence present", "rework route explicit"],
        "required_inputs": ["workflow stage", "work item", "review state"],
        "outputs": ["handoff audit", "missing evidence list"],
        "evidence_requirements": ["workflow stage/run rows", "work/review rows"],
        "stop_conditions": ["no visible workflow state"],
        "quality_gates": ["target owner named"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "work.list"],
        "capability_level": "L2",
    },
    {
        "id": "executive_attention_brief", "version": 1, "name": "Executive Attention Brief",
        "summary": "Rank visible cross-workspace attention into a concise executive brief.",
        "intent": "Produce a top-attention read model from already ACL-filtered workspace projections.",
        "domains": ["portfolio", "strategy", "operations"],
        "profile_ids": ["cosmo_strategy_architect", "cosmo_operations_controller", "cosmo_research_synthesist"],
        "activation_sequence": ["aggregate visible workspaces", "rank attention", "run dissent pass", "state gates"],
        "steps": [
            _step("aggregate", 1, "Aggregate visible scope", "cosmo_research_synthesist", "evidence clustering", "scope"),
            _step("rank", 2, "Rank attention", "cosmo_operations_controller", "queue triage", "quality"),
            _step("frame", 3, "Frame executive choices", "cosmo_strategy_architect", "second-order effect scan", "approval"),
        ],
        "handoff_gates": ["only visible workspaces included", "top items cite evidence", "one-way decisions gated"],
        "required_inputs": ["portfolio projection", "workspace findings"],
        "outputs": ["executive attention brief", "decision gates"],
        "evidence_requirements": ["ACL-filtered workspace findings"],
        "stop_conditions": ["organization membership denied"],
        "quality_gates": ["no cross-client leakage"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search", "work.list"],
        "capability_level": "L3",
    },
))
