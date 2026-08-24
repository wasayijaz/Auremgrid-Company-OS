"""Immutable expert and runbook contracts for Auremgrid Intelligence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from auremgrid.domain.errors import ValidationError
from auremgrid.domain.models import AgentLevel, normalize_agent_level


STATUSES: tuple[str, ...] = ("active", "retired")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _tuple_text(values: Iterable[Any] | None, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{field_name} must be a list of strings")
    result = tuple(_text(value, field_name) for value in values)
    if len(set(result)) != len(result):
        raise ValidationError(f"{field_name} must not contain duplicates")
    return result


def _level(value: Any) -> str:
    try:
        return normalize_agent_level(value).value
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@dataclass(frozen=True)
class ExpertProfile:
    """A bounded expert perspective; not a prompt or autonomous role."""

    id: str
    version: int
    name: str
    specialty: str
    mission: str
    required_inputs: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    required_evidence: tuple[str, ...]
    reasoning_method: str
    output_schema: dict[str, Any]
    evaluation_criteria: tuple[str, ...]
    escalation_policy: str
    fallback_policy: str
    max_context: int
    max_iterations: int
    capability_level: str
    title: str
    summary: str
    domains: tuple[str, ...]
    allowed_tool_refs: tuple[str, ...]
    reasoning_methods: tuple[str, ...]
    activation_triggers: tuple[str, ...]
    outputs: tuple[str, ...]
    handoff_targets: tuple[str, ...]
    quality_gates: tuple[str, ...]
    constraints: tuple[str, ...]
    status: str = "active"
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValidationError("profile version must be positive")
        _text(self.id, "profile id")
        _text(self.name, "profile name")
        _text(self.specialty, "profile specialty")
        _text(self.mission, "profile mission")
        _text(self.reasoning_method, "profile reasoning_method")
        _text(self.escalation_policy, "profile escalation_policy")
        _text(self.fallback_policy, "profile fallback_policy")
        if not self.required_inputs:
            raise ValidationError(f"profile {self.id} must declare required_inputs")
        if not self.allowed_domains:
            raise ValidationError(f"profile {self.id} must declare allowed_domains")
        if not self.allowed_tools:
            raise ValidationError(f"profile {self.id} must declare allowed_tools")
        if not self.required_evidence:
            raise ValidationError(f"profile {self.id} must declare required_evidence")
        if not self.output_schema:
            raise ValidationError(f"profile {self.id} must declare output_schema")
        if not self.evaluation_criteria:
            raise ValidationError(f"profile {self.id} must declare evaluation_criteria")
        if not isinstance(self.max_context, int) or self.max_context < 1:
            raise ValidationError(f"profile {self.id} max_context must be positive")
        if not isinstance(self.max_iterations, int) or self.max_iterations < 1:
            raise ValidationError(f"profile {self.id} max_iterations must be positive")
        if not self.domains:
            raise ValidationError(f"profile {self.id} must declare domains")
        if not self.reasoning_methods:
            raise ValidationError(f"profile {self.id} must declare reasoning methods")
        if not self.outputs:
            raise ValidationError(f"profile {self.id} must declare outputs")
        object.__setattr__(self, "capability_level", _level(self.capability_level))
        if self.status not in STATUSES:
            raise ValidationError(f"profile {self.id} has invalid status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "specialty": self.specialty,
            "mission": self.mission,
            "required_inputs": list(self.required_inputs),
            "allowed_domains": list(self.allowed_domains),
            "allowed_tools": list(self.allowed_tools),
            "required_evidence": list(self.required_evidence),
            "reasoning_method": self.reasoning_method,
            "output_schema": self.output_schema,
            "evaluation_criteria": list(self.evaluation_criteria),
            "escalation_policy": self.escalation_policy,
            "fallback_policy": self.fallback_policy,
            "max_context": self.max_context,
            "max_iterations": self.max_iterations,
            "capability_level": self.capability_level,
            "title": self.title,
            "summary": self.summary,
            "domains": list(self.domains),
            "allowed_tool_refs": list(self.allowed_tool_refs),
            "reasoning_methods": list(self.reasoning_methods),
            "activation_triggers": list(self.activation_triggers),
            "outputs": list(self.outputs),
            "handoff_targets": list(self.handoff_targets),
            "quality_gates": list(self.quality_gates),
            "constraints": list(self.constraints),
            "status": self.status,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExpertProfile":
        return cls(
            id=_text(raw.get("id"), "profile id"),
            version=int(raw.get("version", 1)),
            name=_text(raw.get("name"), "profile name"),
            specialty=_text(raw.get("specialty"), "profile specialty"),
            mission=_text(raw.get("mission"), "profile mission"),
            required_inputs=_tuple_text(raw.get("required_inputs"), "required_inputs"),
            allowed_domains=_tuple_text(raw.get("allowed_domains"), "allowed_domains"),
            allowed_tools=_tuple_text(raw.get("allowed_tools"), "allowed_tools"),
            required_evidence=_tuple_text(raw.get("required_evidence"), "required_evidence"),
            reasoning_method=_text(raw.get("reasoning_method"), "reasoning_method"),
            output_schema=dict(raw.get("output_schema") or {}),
            evaluation_criteria=_tuple_text(raw.get("evaluation_criteria"), "evaluation_criteria"),
            escalation_policy=_text(raw.get("escalation_policy"), "escalation_policy"),
            fallback_policy=_text(raw.get("fallback_policy"), "fallback_policy"),
            max_context=int(raw.get("max_context")),
            max_iterations=int(raw.get("max_iterations")),
            capability_level=_level(raw.get("capability_level", AgentLevel.L1_OPERATE.value)),
            title=_text(raw.get("title"), "profile title"),
            summary=_text(raw.get("summary"), "profile summary"),
            domains=_tuple_text(raw.get("domains"), "domains"),
            allowed_tool_refs=_tuple_text(raw.get("allowed_tool_refs"), "allowed_tool_refs"),
            reasoning_methods=_tuple_text(raw.get("reasoning_methods"), "reasoning_methods"),
            activation_triggers=_tuple_text(raw.get("activation_triggers"), "activation_triggers"),
            outputs=_tuple_text(raw.get("outputs"), "outputs"),
            handoff_targets=_tuple_text(raw.get("handoff_targets"), "handoff_targets"),
            quality_gates=_tuple_text(raw.get("quality_gates"), "quality_gates"),
            constraints=_tuple_text(raw.get("constraints"), "constraints"),
            status=str(raw.get("status", "active")),
            content_hash=str(raw.get("content_hash") or ""),
        )


@dataclass(frozen=True)
class RunbookStep:
    id: str
    sequence: int
    title: str
    owner_profile_id: str
    method: str
    required_inputs: tuple[str, ...] = field(default_factory=tuple)
    outputs: tuple[str, ...] = field(default_factory=tuple)
    gate: str = "none"
    handoff_to: str | None = None

    def __post_init__(self) -> None:
        _text(self.id, "step id")
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValidationError(f"step {self.id} sequence must be positive")
        _text(self.title, "step title")
        _text(self.owner_profile_id, "step owner profile")
        _text(self.method, "step method")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "title": self.title,
            "owner_profile_id": self.owner_profile_id,
            "method": self.method,
            "required_inputs": list(self.required_inputs),
            "outputs": list(self.outputs),
            "gate": self.gate,
            "handoff_to": self.handoff_to,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RunbookStep":
        return cls(
            id=_text(raw.get("id"), "step id"),
            sequence=int(raw.get("sequence")),
            title=_text(raw.get("title"), "step title"),
            owner_profile_id=_text(raw.get("owner_profile_id"), "step owner profile"),
            method=_text(raw.get("method"), "step method"),
            required_inputs=_tuple_text(raw.get("required_inputs"), "step required_inputs"),
            outputs=_tuple_text(raw.get("outputs"), "step outputs"),
            gate=str(raw.get("gate", "none")),
            handoff_to=raw.get("handoff_to") or None,
        )


@dataclass(frozen=True)
class IntelligenceRunbook:
    """A reusable intelligence procedure with explicit gates and handoffs."""

    id: str
    version: int
    name: str
    trigger: str
    required_domains: tuple[str, ...]
    required_evidence: tuple[str, ...]
    specialists: tuple[str, ...]
    topology: str
    stages: tuple[RunbookStep, ...]
    quality_gates: tuple[str, ...]
    contradiction_policy: str
    scenario_policy: str
    escalation_policy: str
    max_iterations: int
    output_contract: dict[str, Any]
    capability_level: str
    summary: str
    intent: str
    domains: tuple[str, ...]
    profile_ids: tuple[str, ...]
    activation_sequence: tuple[str, ...]
    steps: tuple[RunbookStep, ...]
    handoff_gates: tuple[str, ...]
    required_inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    allowed_tool_refs: tuple[str, ...]
    status: str = "active"
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValidationError("runbook version must be positive")
        _text(self.id, "runbook id")
        _text(self.trigger, "runbook trigger")
        _text(self.topology, "runbook topology")
        _text(self.contradiction_policy, "runbook contradiction_policy")
        _text(self.scenario_policy, "runbook scenario_policy")
        _text(self.escalation_policy, "runbook escalation_policy")
        if not self.required_domains:
            raise ValidationError(f"runbook {self.id} must declare required_domains")
        if not self.required_evidence:
            raise ValidationError(f"runbook {self.id} must declare required_evidence")
        if not self.specialists:
            raise ValidationError(f"runbook {self.id} must declare specialists")
        if not self.stages:
            raise ValidationError(f"runbook {self.id} must declare stages")
        if not isinstance(self.max_iterations, int) or self.max_iterations < 1:
            raise ValidationError(f"runbook {self.id} max_iterations must be positive")
        if not self.output_contract:
            raise ValidationError(f"runbook {self.id} must declare output_contract")
        if not self.domains:
            raise ValidationError(f"runbook {self.id} must declare domains")
        if not self.profile_ids:
            raise ValidationError(f"runbook {self.id} must declare profile ids")
        if not self.steps:
            raise ValidationError(f"runbook {self.id} must declare steps")
        if [step.sequence for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValidationError(f"runbook {self.id} steps must be ordered 1..N")
        if [stage.sequence for stage in self.stages] != list(range(1, len(self.stages) + 1)):
            raise ValidationError(f"runbook {self.id} stages must be ordered 1..N")
        if not self.outputs:
            raise ValidationError(f"runbook {self.id} must declare outputs")
        object.__setattr__(self, "capability_level", _level(self.capability_level))
        if self.status not in STATUSES:
            raise ValidationError(f"runbook {self.id} has invalid status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "trigger": self.trigger,
            "required_domains": list(self.required_domains),
            "required_evidence": list(self.required_evidence),
            "specialists": list(self.specialists),
            "topology": self.topology,
            "stages": [stage.to_dict() for stage in self.stages],
            "quality_gates": list(self.quality_gates),
            "contradiction_policy": self.contradiction_policy,
            "scenario_policy": self.scenario_policy,
            "escalation_policy": self.escalation_policy,
            "max_iterations": self.max_iterations,
            "output_contract": self.output_contract,
            "capability_level": self.capability_level,
            "summary": self.summary,
            "intent": self.intent,
            "domains": list(self.domains),
            "profile_ids": list(self.profile_ids),
            "activation_sequence": list(self.activation_sequence),
            "steps": [step.to_dict() for step in self.steps],
            "handoff_gates": list(self.handoff_gates),
            "required_inputs": list(self.required_inputs),
            "outputs": list(self.outputs),
            "stop_conditions": list(self.stop_conditions),
            "allowed_tool_refs": list(self.allowed_tool_refs),
            "status": self.status,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IntelligenceRunbook":
        raw_steps = raw.get("steps", ())
        if not isinstance(raw_steps, (list, tuple)):
            raise ValidationError("runbook steps must be a list")
        if any(not isinstance(item, Mapping) for item in raw_steps):
            raise ValidationError("runbook steps must contain objects")
        raw_stages = raw.get("stages", ())
        if not isinstance(raw_stages, (list, tuple)):
            raise ValidationError("runbook stages must be a list")
        if any(not isinstance(item, Mapping) for item in raw_stages):
            raise ValidationError("runbook stages must contain objects")
        return cls(
            id=_text(raw.get("id"), "runbook id"),
            version=int(raw.get("version", 1)),
            name=_text(raw.get("name"), "runbook name"),
            trigger=_text(raw.get("trigger"), "runbook trigger"),
            required_domains=_tuple_text(raw.get("required_domains"), "required_domains"),
            required_evidence=_tuple_text(raw.get("required_evidence"), "required_evidence"),
            specialists=_tuple_text(raw.get("specialists"), "specialists"),
            topology=_text(raw.get("topology"), "topology"),
            stages=tuple(RunbookStep.from_mapping(item) for item in raw_stages),
            quality_gates=_tuple_text(raw.get("quality_gates"), "quality_gates"),
            contradiction_policy=_text(raw.get("contradiction_policy"), "contradiction_policy"),
            scenario_policy=_text(raw.get("scenario_policy"), "scenario_policy"),
            escalation_policy=_text(raw.get("escalation_policy"), "escalation_policy"),
            max_iterations=int(raw.get("max_iterations")),
            output_contract=dict(raw.get("output_contract") or {}),
            capability_level=_level(raw.get("capability_level", AgentLevel.L1_OPERATE.value)),
            summary=_text(raw.get("summary"), "runbook summary"),
            intent=_text(raw.get("intent"), "runbook intent"),
            domains=_tuple_text(raw.get("domains"), "domains"),
            profile_ids=_tuple_text(raw.get("profile_ids"), "profile_ids"),
            activation_sequence=_tuple_text(raw.get("activation_sequence"), "activation_sequence"),
            steps=tuple(RunbookStep.from_mapping(item) for item in raw_steps),
            handoff_gates=_tuple_text(raw.get("handoff_gates"), "handoff_gates"),
            required_inputs=_tuple_text(raw.get("required_inputs"), "required_inputs"),
            outputs=_tuple_text(raw.get("outputs"), "outputs"),
            stop_conditions=_tuple_text(raw.get("stop_conditions"), "stop_conditions"),
            allowed_tool_refs=_tuple_text(raw.get("allowed_tool_refs"), "allowed_tool_refs"),
            status=str(raw.get("status", "active")),
            content_hash=str(raw.get("content_hash") or ""),
        )


@dataclass(frozen=True)
class ExpertResult:
    status: str
    scope: dict[str, Any]
    finding: str = ""
    evidence_for: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    evidence_against: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    unknowns: tuple[str, ...] = field(default_factory=tuple)
    hypothesis: str = ""
    confidence: float = 0.0
    historical_analogues: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    risks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    options: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    recommendation: dict[str, Any] = field(default_factory=dict)
    expected_impact: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = True
    profile: dict[str, Any] | None = None
    runbooks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    allowed_actions: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scope": self.scope,
            "finding": self.finding,
            "evidence_for": list(self.evidence_for),
            "evidence_against": list(self.evidence_against),
            "assumptions": list(self.assumptions),
            "unknowns": list(self.unknowns),
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
            "historical_analogues": list(self.historical_analogues),
            "risks": list(self.risks),
            "options": list(self.options),
            "recommendation": self.recommendation,
            "expected_impact": self.expected_impact,
            "needs_review": self.needs_review,
            "profile": self.profile,
            "runbooks": list(self.runbooks),
            "allowed_actions": list(self.allowed_actions),
        }
