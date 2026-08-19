"""Immutable, validation-heavy workflow template primitives.

The catalog is deliberately credential- and tenant-neutral.  A template is a
reusable operating contract; a run engine may later project it into work items
without changing the source definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from auremgrid.domain.errors import ValidationError


WINGS: tuple[str, ...] = (
    "Client Strategy/Marketing",
    "Product & Engineering",
    "Paid Media",
    "Design",
    "Video Production",
    "Operations",
)

APPROVAL_GATES: tuple[str, ...] = ("none", "internal", "client", "compliance", "launch")


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
    return result


@dataclass(frozen=True)
class WorkflowStage:
    """One ordered stage in a workflow template."""

    id: str
    order: int
    name: str
    owner_wing: str
    owner_role: str
    handoff_target: str | None = None
    on_reject_stage_id: str | None = None
    approval_gate: str = "none"
    approver_role: str | None = None
    required_evidence: tuple[str, ...] = field(default_factory=tuple)
    sla_hours: float | None = None
    expected_duration_hours: float | None = None
    escalation_trigger: str = ""
    cadence: str = "one-off"
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    quality_checklist: tuple[str, ...] = field(default_factory=tuple)
    post_launch_review: str | None = None
    completion_outcome: str | None = None

    @property
    def stage_id(self) -> str:
        return self.id

    @property
    def sequence(self) -> int:
        return self.order

    @property
    def approval_gate_type(self) -> str:
        return self.approval_gate

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order": self.order,
            "name": self.name,
            "owner_wing": self.owner_wing,
            "owner_role": self.owner_role,
            "handoff_target": self.handoff_target,
            "on_reject_stage_id": self.on_reject_stage_id,
            "approval_gate": self.approval_gate,
            "approver_role": self.approver_role,
            "required_evidence": list(self.required_evidence),
            "sla_hours": self.sla_hours,
            "expected_duration_hours": self.expected_duration_hours,
            "escalation_trigger": self.escalation_trigger,
            "cadence": self.cadence,
            "dependencies": list(self.dependencies),
            "quality_checklist": list(self.quality_checklist),
            "post_launch_review": self.post_launch_review,
            "completion_outcome": self.completion_outcome,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkflowStage":
        # Keep parsing permissive about omitted optional fields, while the
        # constructor/validator remains strict about required operating data.
        return cls(
            id=_text(raw.get("id"), "stage id"),
            order=raw.get("order"),
            name=_text(raw.get("name"), "stage name"),
            owner_wing=_text(raw.get("owner_wing"), "owner_wing"),
            owner_role=_text(raw.get("owner_role"), "owner_role"),
            handoff_target=(raw.get("handoff_target") or None),
            on_reject_stage_id=(raw.get("on_reject_stage_id") or None),
            approval_gate=raw.get("approval_gate", "none"),
            approver_role=(raw.get("approver_role") or None),
            required_evidence=_tuple_text(raw.get("required_evidence"), "required_evidence"),
            sla_hours=raw.get("sla_hours"),
            expected_duration_hours=raw.get("expected_duration_hours"),
            escalation_trigger=raw.get("escalation_trigger", ""),
            cadence=raw.get("cadence", "one-off"),
            dependencies=_tuple_text(raw.get("dependencies"), "dependencies"),
            quality_checklist=_tuple_text(raw.get("quality_checklist"), "quality_checklist"),
            post_launch_review=(raw.get("post_launch_review") or None),
            completion_outcome=(raw.get("completion_outcome") or None),
        )


@dataclass(frozen=True)
class WorkflowTemplate:
    """Reusable cross-wing workflow contract."""

    id: str
    name: str
    wings: tuple[str, ...]
    description: str
    stages: tuple[WorkflowStage, ...]
    cadence: str = "one-off"
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    quality_checklist: tuple[str, ...] = field(default_factory=tuple)
    post_launch_review: str = ""
    completion_outcomes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def template_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "wings": list(self.wings),
            "description": self.description,
            "stages": [stage.to_dict() for stage in self.stages],
            "cadence": self.cadence,
            "dependencies": list(self.dependencies),
            "quality_checklist": list(self.quality_checklist),
            "post_launch_review": self.post_launch_review,
            "completion_outcomes": list(self.completion_outcomes),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkflowTemplate":
        raw_stages = raw.get("stages", ())
        if not isinstance(raw_stages, (list, tuple)):
            raise ValidationError("stages must be a list")
        if any(not isinstance(item, Mapping) for item in raw_stages):
            raise ValidationError("stages must contain objects")
        stages = tuple(WorkflowStage.from_mapping(item) for item in raw_stages)
        return cls(
            id=_text(raw.get("id"), "template id"),
            name=_text(raw.get("name"), "template name"),
            wings=_tuple_text(raw.get("wings"), "wings"),
            description=_text(raw.get("description"), "description"),
            stages=stages,
            cadence=raw.get("cadence", "one-off"),
            dependencies=_tuple_text(raw.get("dependencies"), "dependencies"),
            quality_checklist=_tuple_text(raw.get("quality_checklist"), "quality_checklist"),
            post_launch_review=raw.get("post_launch_review", ""),
            completion_outcomes=_tuple_text(raw.get("completion_outcomes"), "completion_outcomes"),
        )


def validate_stage(stage: WorkflowStage, prior_stage_ids: set[str] | None = None) -> None:
    """Validate one stage and its relationship to already-seen stages."""

    if not stage.id.strip():
        raise ValidationError("stage id must be a non-empty string")
    if not isinstance(stage.order, int) or isinstance(stage.order, bool) or stage.order < 1:
        raise ValidationError(f"stage {stage.id} order must be a positive integer")
    if stage.owner_wing not in WINGS:
        raise ValidationError(f"stage {stage.id} has an unknown owner wing")
    if stage.approval_gate not in APPROVAL_GATES:
        raise ValidationError(f"stage {stage.id} has an invalid approval gate")
    if stage.approval_gate != "none" and (not stage.approver_role or not stage.required_evidence):
        raise ValidationError(f"gate stage {stage.id} requires approver_role and required_evidence")
    for duration_name in ("sla_hours", "expected_duration_hours"):
        duration = getattr(stage, duration_name)
        if duration is not None and (not isinstance(duration, (int, float)) or duration <= 0):
            raise ValidationError(f"stage {stage.id} {duration_name} must be positive")
    if prior_stage_ids is not None and any(dependency not in prior_stage_ids for dependency in stage.dependencies):
        raise ValidationError(f"stage {stage.id} dependencies must reference earlier stages")
    if stage.on_reject_stage_id is not None and (
        prior_stage_ids is None or stage.on_reject_stage_id not in prior_stage_ids
    ):
        raise ValidationError(f"stage {stage.id} on_reject_stage_id must reference an earlier stage")


def validate_template(
    template: WorkflowTemplate,
    known_template_ids: set[str] | None = None,
    known_stage_ids: set[str] | None = None,
) -> None:
    """Validate all catalog invariants for a template."""

    if not template.id.strip():
        raise ValidationError("template id must be a non-empty string")
    if known_template_ids is not None and template.id in known_template_ids:
        raise ValidationError(f"duplicate template id: {template.id}")
    if not template.wings or any(wing not in WINGS for wing in template.wings):
        raise ValidationError(f"template {template.id} must use known wings")
    if len(set(template.wings)) != len(template.wings):
        raise ValidationError(f"template {template.id} has duplicate wings")
    if not template.stages:
        raise ValidationError(f"template {template.id} must contain stages")
    expected_orders = list(range(1, len(template.stages) + 1))
    actual_orders = [stage.order for stage in template.stages]
    if actual_orders != expected_orders:
        raise ValidationError(f"template {template.id} stages must be ordered 1..N")
    stage_ids: set[str] = set()
    prior_stage_ids: set[str] = set()
    for index, stage in enumerate(template.stages):
        if stage.id in stage_ids or (known_stage_ids is not None and stage.id in known_stage_ids):
            raise ValidationError(f"duplicate stage id: {stage.id}")
        validate_stage(stage, prior_stage_ids)
        if index < len(template.stages) - 1:
            next_stage = template.stages[index + 1]
            if stage.owner_wing != next_stage.owner_wing and not stage.handoff_target:
                raise ValidationError(f"cross-wing stage {stage.id} requires handoff_target")
        stage_ids.add(stage.id)
        prior_stage_ids.add(stage.id)
    if not template.completion_outcomes and not any(stage.completion_outcome for stage in template.stages):
        raise ValidationError(f"template {template.id} must define a completion/launch outcome")


def validate_catalog(templates: Iterable[WorkflowTemplate]) -> tuple[WorkflowTemplate, ...]:
    """Return an immutable tuple after validating unique template identifiers."""

    result = tuple(templates)
    seen: set[str] = set()
    seen_stage_ids: set[str] = set()
    for template in result:
        validate_template(template, seen, seen_stage_ids)
        seen.add(template.id)
        seen_stage_ids.update(stage.id for stage in template.stages)
    return result
