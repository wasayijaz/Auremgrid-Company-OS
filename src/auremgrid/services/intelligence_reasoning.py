from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import uuid
from typing import Any

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.models import AuditEvent
from auremgrid.adapters.reasoning import StrategicReasoningProvider, invoke_reasoning_provider
from auremgrid.services.intelligence_shared import _confidence, _iso, _now, _parse_time, _tokens


class IntelligenceReasoningMixin:
    @staticmethod
    def _estimated_tokens(value: Any) -> int:
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = str(value)
        return max(1, (len(encoded) + 3) // 4)

    @staticmethod
    def _reference_key(value: Any) -> tuple[str, str] | None:
        if not isinstance(value, dict) or value.get("type") in (None, "") or value.get("id") in (None, ""):
            return None
        return str(value["type"]), str(value["id"])

    @classmethod
    def _validate_evidence_references(cls, value: Any, evidence: list[dict[str, Any]]) -> None:
        allowed = {
            ref for item in evidence
            if (ref := cls._reference_key(item.get("object_ref"))) is not None
        }
        allowed_ids = {item_id for _kind, item_id in allowed}

        def visit(node: Any, key: str | None = None) -> None:
            if isinstance(node, dict):
                if "object_ref" in node:
                    ref = cls._reference_key(node.get("object_ref"))
                    if ref not in allowed:
                        raise ValidationError("reasoning evidence reference outside scoped visible evidence")
                if key == "evidence_refs":
                    ref = cls._reference_key(node)
                    if ref is None or ref not in allowed:
                        raise ValidationError("reasoning evidence reference outside scoped visible evidence")
                for child_key, child in node.items():
                    visit(child, str(child_key))
            elif isinstance(node, list):
                for child in node:
                    if key == "evidence_refs" and isinstance(child, str) and child not in allowed_ids:
                        raise ValidationError("reasoning evidence reference outside scoped visible evidence")
                    visit(child, key)

        visit(value)

    @staticmethod
    def _confidence_value(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            value = value.get("score")
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(score) or not 0 <= score <= 1:
            return None
        return _confidence(score)
    
    @classmethod
    def _validate_model_reasoning(cls, value: Any) -> dict[str, Any]:
        """Validate and normalize the intentionally small model output schema."""
        if not isinstance(value, dict):
            raise ValidationError("reasoning result must be an object")
        required = ("hypotheses", "options", "scenarios", "recommendation", "confidence", "dissent")
        if any(key not in value for key in required):
            raise ValidationError("reasoning result is missing a required field")
        for key in ("hypotheses", "options", "scenarios", "dissent"):
            if not isinstance(value[key], list) or len(value[key]) > 12:
                raise ValidationError(f"reasoning {key} must be a list of at most 12 items")
        confidence = cls._confidence_value(value["confidence"])
        if confidence is None:
            raise ValidationError("reasoning confidence must be a score between 0 and 1")
        recommendation = value["recommendation"]
        if not isinstance(recommendation, dict) or not isinstance(recommendation.get("summary"), str):
            raise ValidationError("reasoning recommendation must include a summary")
        normalized: dict[str, Any] = {
            "hypotheses": [], "options": [], "scenarios": [],
            "recommendation": cls._json_safe(recommendation),
            "confidence": confidence,
            "dissent": [],
        }
        for key in ("hypotheses", "options", "scenarios", "dissent"):
            for item in value[key]:
                if not isinstance(item, dict):
                    raise ValidationError(f"reasoning {key} items must be objects")
                # Preserve provider detail, while keeping all returned values
                # bounded and JSON-safe.  Confidence is normalized when present.
                clean = cls._json_safe(item)
                if "confidence" in item:
                    normalized_confidence = cls._confidence_value(item["confidence"])
                    if normalized_confidence is None:
                        raise ValidationError(f"reasoning {key} confidence is invalid")
                    clean["confidence"] = normalized_confidence
                normalized[key].append(clean)
        return normalized
    
    def _model_reasoning(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        actor_id: str | None,
        scope: dict[str, Any],
        context: dict[str, Any],
        evidence: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        analogues: list[dict[str, Any]],
        decision_links: list[dict[str, Any]],
        recommended_plan: dict[str, Any],
        scenario_inputs: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        provider = getattr(self.os, "strategic_reasoning_provider", None)
        evidence_refs = [item.get("object_ref") for item in evidence if item.get("object_ref")]
        provider_context = self._json_safe({
            "scope": scope,
            "context": context,
            # These are assembled only after workspace membership and Brain
            # source ACL checks.  No store/provider handle is exposed.
            "evidence": evidence,
            "findings": findings,
            "relationships": relationships,
            "historical_analogues": analogues,
            "decision_action_outcome_learning": decision_links,
            "recommended_plan": recommended_plan,
            "scenario_inputs": scenario_inputs,
        })
        hash_context = provider_context
        # Current reads use a fresh watermark; keep that volatile timestamp
        # out of the audit identity so unchanged dashboard refreshes dedupe.
        if not context.get("historical"):
            hash_context = dict(provider_context)
            hash_context["scope"] = dict(provider_context.get("scope") or {})
            hash_context["scope"]["as_of"] = "current"
        context_hash = hashlib.sha256(
            json.dumps(hash_context, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        base_meta: dict[str, Any] = {
            "status": "not_configured" if provider is None else "configured",
            "provider": None,
            "model": None,
            "version": None,
            "evidence_count": len(evidence),
            "evidence_refs": evidence_refs[:24],
            "context_hash": context_hash,
            "output_hash": None,
            "fallback_reason": None,
            "evaluation_safety": None,
        }
        if provider is None:
            # Offline deterministic mode is the normal path; do not create a
            # durable event for every dashboard read when no provider exists.
            return None, base_meta
        safety = getattr(self.os, "intelligence_evaluation_safety", None)
        safety_id: str | None = None
        safety_completed = False
        safety_policy: dict[str, Any] = {}
        input_tokens = self._estimated_tokens(provider_context)
        if safety is None:
            base_meta.update({"status": "blocked", "fallback_reason": "safety_unavailable"})
            return None, base_meta
        try:
            safety_decision = safety.can_start(organization_id, person_id, "reasoning")
            safety_policy = dict(safety_decision.get("policy") or {})
            base_meta["evaluation_safety"] = {
                "status": "shadow_only" if safety_decision.get("allowed") else "circuit_open",
                "reason": safety_decision.get("reason"),
                "estimated_input_tokens": input_tokens,
            }
            if not safety_decision.get("allowed"):
                base_meta.update({"status": "blocked", "fallback_reason": "circuit_open"})
                return None, base_meta
            if input_tokens > int(safety_policy.get("max_tokens", 50000)):
                base_meta.update({"status": "blocked", "fallback_reason": "token_cap"})
                base_meta["evaluation_safety"].update({"status": "blocked", "reason": "token_cap"})
                return None, base_meta
        except Exception:
            base_meta.update({"status": "blocked", "fallback_reason": "safety_unavailable"})
            return None, base_meta
        try:
            run = safety.start(
                organization_id, person_id, "reasoning", workspace_id=workspace_id,
                provider=str(getattr(provider, "name", "configured")),
                model=str(getattr(provider, "model", "configured")),
            )
            safety_id = run.get("id")
        except Exception:
            base_meta.update({"status": "blocked", "fallback_reason": "safety_telemetry_unavailable"})
            base_meta["evaluation_safety"] = {"status": "blocked", "reason": "safety_telemetry_unavailable"}
            return None, base_meta
        try:
            raw, identity = invoke_reasoning_provider(provider, provider_context)
            base_meta.update(identity)
            normalized = self._validate_model_reasoning(dict(raw))
            self._validate_evidence_references(normalized, evidence)
            output_tokens = self._estimated_tokens(normalized)
            completed = safety.complete(
                organization_id, person_id, safety_id,
                workspace_id=workspace_id, input_tokens=input_tokens,
                output_tokens=output_tokens, metadata={"provider_call": "native_reasoning"},
            )
            safety_completed = True
            base_meta["evaluation_safety"] = {
                "status": completed.get("status"),
                "cap_reason": completed.get("cap_reason"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            if completed.get("cap_reason"):
                raise ValidationError("reasoning token cap exceeded")
            output_hash = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            base_meta.update({"status": "used", "output_hash": output_hash})
            self._record_reasoning_audit(workspace_id, actor_id, "used", base_meta)
            return normalized, base_meta
        except Exception as exc:
            # Provider errors and malformed responses never replace the
            # deterministic projection.  Record only a stable error class.
            if safety_id and not safety_completed:
                try:
                    completed = safety.complete(
                        organization_id, person_id, safety_id,
                        workspace_id=workspace_id, input_tokens=input_tokens,
                        output_tokens=self._estimated_tokens(locals().get("raw")),
                        metadata={"provider_call": "native_reasoning", "failed": True},
                    )
                    base_meta["evaluation_safety"] = {
                        "status": completed.get("status"),
                        "cap_reason": completed.get("cap_reason"),
                        "input_tokens": input_tokens,
                    }
                except Exception:
                    base_meta["evaluation_safety"] = {"status": "telemetry_error"}
            base_meta["status"] = "fallback"
            if isinstance(exc, ValidationError) and "evidence reference" in str(exc):
                base_meta["fallback_reason"] = "invalid_evidence_reference"
            elif isinstance(exc, ValidationError) and "token cap" in str(exc):
                base_meta["fallback_reason"] = "token_cap"
            else:
                base_meta["fallback_reason"] = "invalid_output" if isinstance(exc, ValidationError) else str(exc).split(":", 1)[0][:100]
            self._record_reasoning_audit(workspace_id, actor_id, "fallback", base_meta)
            return None, base_meta
    
    def _record_reasoning_audit(
        self, workspace_id: str, actor_id: str | None, outcome: str, metadata: dict[str, Any]
    ) -> None:
        """Persist redacted run metadata; never persist prompt or model output."""
        if not actor_id:
            return
        try:
            detail = json.dumps(self._json_safe(metadata), sort_keys=True, separators=(",", ":"))
            existing = self.os.store.conn.execute(
                "SELECT 1 FROM audit_events WHERE workspace_id=? AND actor_id=? "
                "AND action=? AND outcome=? AND detail=? LIMIT 1",
                (workspace_id, actor_id, "intelligence.deliberate", outcome, detail),
            ).fetchone()
            if existing is not None:
                return
            self.os.store.create_audit(
                AuditEvent(
                    id=f"aud_{uuid.uuid4().hex[:16]}",
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    action="intelligence.deliberate",
                    target=workspace_id,
                    outcome=outcome,
                    detail=detail,
                    recorded_at=_now(),
                )
            )
        except Exception:
            # Audit availability must not make a read-only intelligence call
            # fail, especially for legacy fixtures without actor bindings.
            return
    
    def _deliberation(
        self,
        findings: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        analogues: list[dict[str, Any]],
        decision_links: list[dict[str, Any]],
        recommended_plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Expose Sol/Terra/Luna as deterministic review roles, not hidden autonomy."""
        evidence_count = sum(len(finding.get("evidence", [])) for finding in findings)
        scenario_count = sum(len(finding.get("scenarios", [])) for finding in findings)
        action_count = sum(len(finding.get("action_descriptors", [])) for finding in findings)
        validated_count = sum(
            1 for link in decision_links
            if (link.get("evaluation") or {}).get("status") == "validated"
        )
        pending_count = sum(
            1 for link in decision_links
            if (link.get("evaluation") or {}).get("status") == "pending_outcome"
        )
        plan_steps = len((recommended_plan or {}).get("steps", [])) if isinstance(recommended_plan, dict) else 0
        consensus_score = min(0.95, 0.42 + evidence_count * 0.03 + len(relationships) * 0.04 + validated_count * 0.08)
        if pending_count and not validated_count:
            consensus_score = max(0.2, consensus_score - 0.08)
        reviews = [
            {
                "agent": "Sol",
                "role": "strategic_reviewer",
                "level": "L3_REASON",
                "stance": "support" if evidence_count and analogues else "challenge",
                "summary": (
                    "Evidence and historical analogues support a bounded recommendation."
                    if evidence_count and analogues else
                    "Recommendation should stay provisional until stronger evidence or analogues are visible."
                ),
                "checks": {
                    "evidence_count": evidence_count,
                    "historical_analogue_count": len(analogues),
                    "relationship_count": len(relationships),
                },
            },
            {
                "agent": "Terra",
                "role": "builder",
                "level": "L2_BUILD",
                "stance": "support" if plan_steps or action_count else "hold",
                "summary": (
                    "The recommendation can be translated into scoped workflow or work actions."
                    if plan_steps or action_count else
                    "No executable workflow is offered until a permitted action descriptor exists."
                ),
                "checks": {
                    "plan_steps": plan_steps,
                    "action_descriptor_count": action_count,
                    "approval_required": bool(action_count),
                },
            },
            {
                "agent": "Luna",
                "role": "operator",
                "level": "L1_OPERATE",
                "stance": "support" if scenario_count else "hold",
                "summary": (
                    "Operational scenario assumptions and mitigations are visible for follow-through."
                    if scenario_count else
                    "Operational follow-through needs a scenario with assumptions, constraints, and mitigation."
                ),
                "checks": {
                    "scenario_count": scenario_count,
                    "validated_outcome_count": validated_count,
                    "pending_outcome_count": pending_count,
                },
            },
        ]
        challenges = [
            review["summary"] for review in reviews
            if review["stance"] in {"challenge", "hold"}
        ]
        return {
            "mode": "deterministic_evidence_review",
            "agents": reviews,
            "consensus": {
                "status": "ready" if consensus_score >= 0.55 and not challenges else "needs_more_evidence",
                "confidence": _confidence(consensus_score),
                "challenge_count": len(challenges),
                "challenges": challenges,
            },
            "execution_boundary": {
                "can_execute_without_approval": False,
                "reason": "Intelligence proposes canonical actions; execution still goes through approval/workflow routes.",
            },
        }
    
    def _workflow_chain(self, workspace_id: str, start: str, cutoff: str, terms: set[str]) -> list[dict[str, Any]]:
        rows = self._optional_rows(
            """SELECT r.id AS run_id,r.definition_key,r.definition_name,r.status AS run_status,
                      s.id AS stage_id,s.stage_key,s.name AS stage_name,s.status AS stage_status,
                      s.assignee_wing,s.assignee_role,s.assignee_person_id,s.due_at,
                      h.id AS history_id,h.action,h.to_status,h.reason,h.created_at
                 FROM workflow_runs r
                 LEFT JOIN workflow_stage_runs s ON s.run_id=r.id
                 LEFT JOIN workflow_transition_history h ON h.run_id=r.id AND (h.stage_run_id=s.id OR h.stage_run_id IS NULL)
                WHERE r.workspace_id=? AND r.created_at>=? AND r.created_at<=?
                ORDER BY r.created_at,s.sequence,h.created_at LIMIT 80""",
            (workspace_id, start, cutoff),
        )
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None, str | None]] = set()
        for row in rows:
            text = f"{row.get('definition_key')} {row.get('definition_name')} {row.get('stage_name')} {row.get('action')} {row.get('reason')}"
            if terms and not (terms & _tokens(text)):
                continue
            key = (str(row.get("run_id")), _iso(row.get("stage_id")), _iso(row.get("history_id")))
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "run": {"type": "workflow_run", "id": str(row.get("run_id")), "status": row.get("run_status"), "definition_key": row.get("definition_key")},
                "stage": {"type": "workflow_stage", "id": row.get("stage_id"), "key": row.get("stage_key"), "status": row.get("stage_status"), "owner_wing": row.get("assignee_wing"), "owner_role": row.get("assignee_role"), "person_id": row.get("assignee_person_id"), "due_at": row.get("due_at")},
                "event": {"type": "workflow_transition", "id": row.get("history_id"), "action": row.get("action"), "to_status": row.get("to_status"), "created_at": row.get("created_at")},
            })
        return result[:12]
    
    @staticmethod
    def _calibration(supporting: list[dict[str, Any]], opposing: list[dict[str, Any]], *, status: str) -> dict[str, Any]:
        support_scores = [float(item.get("confidence", {}).get("score", 0.0)) for item in supporting]
        oppose_scores = [float(item.get("confidence", {}).get("score", 0.0)) for item in opposing]
        support = sum(support_scores) / len(support_scores) if support_scores else 0.0
        oppose = sum(oppose_scores) / len(oppose_scores) if oppose_scores else 0.0
        return {
            "supporting_evidence_count": len(supporting),
            "opposing_evidence_count": len(opposing),
            "supporting_mean": round(support, 3),
            "opposing_mean": round(oppose, 3),
            "net": round(support - oppose, 3),
            "status": "uncalibrated" if status == "insufficient_evidence" else "calibrated_from_visible_records",
        }
