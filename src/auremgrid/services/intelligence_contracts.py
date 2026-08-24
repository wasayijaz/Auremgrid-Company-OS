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
    # This is the specialist runtime contract (not a generic narrative
    # shape). ``analogues`` remains an accepted wire alias for older callers.
    "required": [
        "finding", "evidence_for", "evidence_against", "assumptions",
        "unknowns", "hypothesis", "confidence", "analogues", "risks",
        "options", "recommendation", "expected_impact", "needs_review",
    ],
    "properties": {
        "historical_analogues": {"type": "array"},
        "analogues": {"type": "array"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
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


_PROFILE_SPECS = (
    ("account_strategist", "Account Strategist", "Account strategy, retention, and expansion framing", ("client_success", "strategy")),
    ("relationship_analyst", "Relationship Analyst", "Stakeholder and relationship health analysis", ("client_success", "relationships")),
    ("delivery_analyst", "Delivery Analyst", "Delivery status, commitments, and recovery analysis", ("delivery", "workflow")),
    ("performance_analyst", "Performance Analyst", "Campaign and operating performance variance analysis", ("performance", "analytics")),
    ("finance_scope_analyst", "Finance & Scope Analyst", "Margin, revenue, and scope pressure analysis", ("finance", "scope")),
    ("capacity_planner", "Capacity Planner", "Capacity, demand, and staffing scenario planning", ("capacity", "operations")),
    ("brand_creative_analyst", "Brand / Creative Analyst", "Brand, creative quality, and asset-fit analysis", ("brand", "creative")),
    ("research_analyst", "Research Analyst", "Evidence synthesis, uncertainty, and research analysis", ("research", "brain")),
    ("risk_analyst", "Risk Analyst", "Risk, compliance, and approval-boundary analysis", ("risk", "approvals")),
    ("scenario_analyst", "Scenario Analyst", "Explicit what-if branches and sensitivity analysis", ("scenario", "strategy")),
    ("historical_analogue_analyst", "Historical Analogue Analyst", "Situation matching against prior outcomes", ("historical", "learning")),
    ("reality_checker", "Reality Checker", "Contradiction, evidence, and assumption challenge", ("quality", "reality_check")),
    ("executive_synthesizer", "Executive Synthesizer", "Executive decision brief synthesis and prioritization", ("executive", "strategy")),
)


def _native_profile(profile_id: str, name: str, specialty: str, domains: tuple[str, ...]) -> dict[str, Any]:
    tool_refs = ["dashboard.intelligence.read", "brain.search"]
    if profile_id == "finance_scope_analyst":
        tool_refs.extend(("finance.read", "reports.generate"))
    return _profile({
        "id": profile_id,
        "version": 1,
        "name": name,
        "title": specialty,
        "specialty": specialty,
        "summary": f"{name} produces bounded, evidence-backed findings for {', '.join(domains)}.",
        "domains": list(domains),
        "allowed_tool_refs": tool_refs,
        "capability_level": "L3" if name in {"Account Strategist", "Research Analyst", "Risk Analyst", "Executive Synthesizer", "Reality Checker"} else "L2",
        "reasoning_methods": ["evidence review", "opposing-evidence pass", "bounded recommendation"],
        "activation_triggers": [f"{name.lower()} review", "material change", "decision support"],
        "inputs_required": ["scope contract", "visible evidence", "decision context"],
        "outputs": ["finding", "hypothesis", "recommendation", "unknowns"],
        "handoff_targets": ["reality_checker", "executive_synthesizer"],
        "quality_gates": ["all claims cite permitted evidence", "unknowns remain explicit", "one-way actions require approval"],
        "evidence_requirements": ["ACL-visible canonical records", "current operating signals"],
        "constraints": COMMON_CONSTRAINTS,
    })


DEFAULT_EXPERT_PROFILES: tuple[dict[str, Any], ...] = tuple(_native_profile(*spec) for spec in _PROFILE_SPECS)


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



# The native completion pack supersedes the original generic Cosmo catalogue.
# Keep one active definition set so callers never see duplicate contracts.
_RUNBOOK_SPECS = (
    ("client_health_drop", "Client Health Drop", ("client_success", "relationships"), ("relationship_analyst", "account_strategist", "reality_checker")),
    ("client_churn_risk", "Client Churn Risk", ("client_success", "retention"), ("relationship_analyst", "risk_analyst", "account_strategist")),
    ("renewal_review", "Renewal Review", ("client_success", "finance", "scope"), ("account_strategist", "finance_scope_analyst", "executive_synthesizer")),
    ("scope_overrun", "Scope Overrun", ("scope", "delivery", "finance"), ("delivery_analyst", "finance_scope_analyst", "risk_analyst")),
    ("margin_pressure", "Margin Pressure", ("finance", "scope", "performance"), ("finance_scope_analyst", "performance_analyst", "scenario_analyst")),
    ("project_delay", "Project Delay", ("delivery", "workflow", "capacity"), ("delivery_analyst", "capacity_planner", "risk_analyst")),
    ("campaign_performance_drop", "Campaign Performance Drop", ("performance", "analytics", "campaigns"), ("performance_analyst", "research_analyst", "scenario_analyst")),
    ("creative_fatigue", "Creative Fatigue", ("creative", "brand", "performance"), ("brand_creative_analyst", "performance_analyst", "historical_analogue_analyst")),
    ("client_relationship_problem", "Client Relationship Problem", ("relationships", "client_success", "risk"), ("relationship_analyst", "risk_analyst", "reality_checker")),
    ("team_overload", "Team Overload", ("capacity", "operations", "delivery"), ("capacity_planner", "delivery_analyst", "scenario_analyst")),
    ("account_expansion_opportunity", "Account Expansion Opportunity", ("client_success", "growth", "finance"), ("account_strategist", "performance_analyst", "finance_scope_analyst")),
    ("quarterly_account_review", "Quarterly Account Review", ("client_success", "portfolio", "strategy"), ("account_strategist", "relationship_analyst", "executive_synthesizer")),
)


def _native_runbook(runbook_id: str, name: str, domains: tuple[str, ...], profile_ids: tuple[str, ...]) -> dict[str, Any]:
    steps = [
        _step(f"{runbook_id}-situation", 1, "Build situation", profile_ids[0], "bounded evidence review", "scope", profile_ids[1]),
        _step(f"{runbook_id}-challenge", 2, "Challenge explanation", profile_ids[1], "opposing-evidence pass", "quality", profile_ids[2]),
        _step(f"{runbook_id}-recommend", 3, "Frame recommendation", profile_ids[2], "reversible option framing", "approval"),
    ]
    return _runbook({
        "id": runbook_id,
        "version": 1,
        "name": name,
        "summary": f"Run a bounded {name.lower()} review from permitted evidence.",
        "intent": f"Explain and frame {name.lower()} without executing external work.",
        "domains": list(domains),
        "profile_ids": list(profile_ids),
        "activation_sequence": ["confirm scope", "collect required evidence", "challenge explanation", "frame gated recommendation"],
        "steps": steps,
        "handoff_gates": ["scope is authorized", "required evidence is cited", "one-way actions require approval"],
        "required_inputs": ["scope contract", "current canonical signals", "decision context"],
        "outputs": ["finding", "hypothesis", "scenarios", "recommendation"],
        "evidence_requirements": ["ACL-visible canonical records", "current operating signals"],
        "stop_conditions": ["workspace membership denied", "required evidence unavailable"],
        "quality_gates": ["unsupported claims remain unknown", "opposing evidence is preserved"],
        "allowed_tool_refs": ["dashboard.intelligence.read", "brain.search"],
        "capability_level": "L3",
    })


DEFAULT_INTELLIGENCE_RUNBOOKS: tuple[dict[str, Any], ...] = tuple(
    _native_runbook(*spec) for spec in _RUNBOOK_SPECS
)
