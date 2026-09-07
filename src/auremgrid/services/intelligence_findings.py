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


class IntelligenceFindingsMixin:
    def _synthesis_finding(
        self,
        organization_id: str,
        person_id: str,
        actor_id: str | None,
        workspace_id: str,
        relationships: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        domains: dict[str, Any],
    ) -> dict[str, Any]:
        score = sum(float(link["confidence"]["score"]) for link in relationships) / len(relationships)
        recommendation = "Review the linked canonical records and choose a reversible next step; do not infer causation from co-occurrence alone."
        return {
            "id": "intelligence-cross-domain-synthesis",
            "type": "cross_domain_synthesis",
            "title": "Cross-domain operating signal",
            "summary": "Visible work, capacity, client, campaign, finance, scope, risk, and decision records form a bounded operating picture.",
            "confidence": _confidence(score),
            "evidence": evidence,
            "situation": {"state": "cross_domain_signal", "domains": sorted(domains)},
            "changes": [],
            "hypotheses": [{"text": link["explanation"], "confidence": link["confidence"]} for link in relationships],
            "scenarios": self._scenarios(domains, relationships, evidence, self._scenario_inputs(domains, None)),
            "impact": {"level": "medium", "summary": "Potential delivery, client, scope, capacity, and financial impact; magnitude is not estimated where source metrics are absent."},
            "recommendation": {"summary": recommendation, "rationale": "The engine exposes relationships and uncertainty rather than fabricating a single causal conclusion."},
            "actions": self._action(organization_id, person_id, actor_id, workspace_id, "Review cross-domain signal", recommendation, "Review cross-domain signal", recommendation),
            "action_descriptors": self._action(organization_id, person_id, actor_id, workspace_id, "Review cross-domain signal", recommendation, "Review cross-domain signal", recommendation),
        }
    
    def _scenarios(self, domains: dict[str, Any], relationships: list[dict[str, Any]], evidence: list[dict[str, Any]], scenario_inputs: dict[str, Any]) -> list[dict[str, Any]]:
        overloaded = any(float(row.get("remaining_hours") or 0) < 0 for row in domains["capacity"]["snapshots"])
        scope_pressure = any(float(row.get("used_hours") or row.get("delivered_quantity") or 0) > float(row.get("included_hours") or row.get("included_quantity") or math.inf) for row in domains["scope"]["usage"])
        risk_count = domains["risks"]["open_count"]
        projection = scenario_inputs.get("projection", {})
        projected_overload = float(projection.get("capacity_remaining_hours") or 0) < 0
        projected_scope_pressure = (projection.get("scope_ratio") is not None and float(projection["scope_ratio"]) > 1)
        projected_health = projection.get("client_health")
        projected_finance = projection.get("recognized_revenue")
        retained_numeric = [
            float(value or 0.0)
            for key, value in scenario_inputs["retained_inputs"].items()
            if key != "client_action"
        ]
        selected_action = str(scenario_inputs["retained_inputs"].get("client_action") or "unspecified")
        selected_sign = 1.0 if selected_action == "keep" else -1.0 if selected_action == "drop" else 0.0
        client_capacity_delta = selected_sign * float(scenario_inputs["retained_inputs"].get("client_hours_delta") or 0.0)
        scenarios = [
            {
                "name": "stabilize",
                "retained_inputs": scenario_inputs["retained_inputs"],
                "assumptions": ["Visible owners act on open blockers", "No unobserved external shock"],
                "domain_impacts": {
                    "work": f"projected demand {projection.get('work_demand_hours')}h",
                    "capacity": f"projected remaining capacity {projection.get('capacity_remaining_hours')}h",
                    "scope": "scope pressure remains" if projected_scope_pressure else "scope pressure may ease or remain unknown",
                    "finance": f"recognized revenue projects to {projected_finance}" if projected_finance is not None else "finance impact unknown",
                    "client_health": f"projected health {projected_health}" if projected_health is not None else "client health impact unknown",
                    "campaign": projection.get("campaign") or "campaign impact unknown",
                },
                "constraints": ["No capacity or outcome data is fabricated", *scenario_inputs["constraints"]],
                "mitigations": ["Assign an owner", "recheck after the next canonical event"],
                "downside": "Stabilization may defer lower-priority work.",
                "confidence": _confidence((0.68 if relationships else 0.4) - (0.08 if projected_overload else 0)),
                "evidence": evidence[:6],
            },
            {
                "name": "defer",
                "retained_inputs": scenario_inputs["retained_inputs"],
                "assumptions": ["Open risks or blockers remain unresolved"],
                "domain_impacts": {
                    "work": "delivery pressure increases",
                    "capacity": "overload likely persists" if overloaded or projected_overload else "capacity impact unknown",
                    "scope": "usage may exceed allowance" if scope_pressure or projected_scope_pressure else "unknown",
                    "finance": "impact unknown" if projected_finance is None else f"visible revenue remains bounded at {projected_finance}",
                    "client_health": "risk of decline" if projected_health is None or float(projected_health) < 0.65 else "current score may cushion impact",
                    "campaign": projection.get("campaign") or "campaign impact unknown",
                },
                "constraints": ["Finance is not connected" if domains["finance"]["status"] != "connected" else "Finance values are limited to recorded rows", *scenario_inputs["constraints"]],
                "mitigations": ["Set an explicit review date", "Record an outcome when action is taken"],
                "downside": "Deferral can compound delivery or client risk.",
                "confidence": _confidence(0.72 if risk_count or overloaded or scope_pressure or projected_overload or projected_scope_pressure else 0.4),
                "evidence": evidence[:6],
            },
            {
                "name": "parameterized_what_if",
                "retained_inputs": scenario_inputs["retained_inputs"],
                "assumptions": ["The provided deltas are hypothetical read-time inputs", "Current canonical records remain otherwise unchanged"],
                "domain_impacts": {
                    "work": f"demand changes to {projection.get('work_demand_hours')}h",
                    "capacity": f"remaining capacity changes to {projection.get('capacity_remaining_hours')}h",
                    "scope": f"scope ratio changes to {projection.get('scope_ratio')}",
                    "finance": f"recognized revenue changes to {projected_finance}" if projected_finance is not None else "finance projection unavailable",
                    "client_health": f"health changes to {projected_health}" if projected_health is not None else "health projection unavailable",
                    "campaign": projection.get("campaign") or "campaign impact unknown",
                },
                "constraints": scenario_inputs["constraints"],
                "mitigations": ["Convert the chosen scenario into canonical work before acting", "Compare the next outcome with this retained input set"],
                "downside": "The projection is directional because no hidden provider or market data is inferred.",
                "confidence": _confidence(0.58 if any(retained_numeric) else 0.35),
                "evidence": evidence[:6],
            },
        ]
        growth_inputs = scenario_inputs["retained_inputs"]
        growth_is_configured = bool(
            growth_inputs.get("additional_clients")
            or growth_inputs.get("hours_per_new_client")
            or growth_inputs.get("leave_hours_delta")
            or growth_inputs.get("hiring_hours_delta")
        )
        scenarios.append({
            "name": "growth_plus_clients",
            "retained_inputs": growth_inputs,
            "assumptions": [
                "Each additional client consumes the supplied hours_per_new_client value",
                "Hiring adds the supplied capacity hours and leave removes the supplied capacity hours",
                "No pipeline conversion probability or hidden client demand is inferred",
            ],
            "domain_impacts": {
                "capacity": f"remaining capacity changes to {projection.get('capacity_remaining_hours')}h",
                "work": f"demand changes to {projection.get('work_demand_hours')}h",
                "finance": "margin impact is visible only when finance and client economics are supplied",
                "client_health": "new-client relationship impact is unknown until delivery evidence exists",
            },
            "constraints": [
                "Growth is a directional capacity check, not a hiring recommendation",
                *scenario_inputs["constraints"],
            ],
            "mitigations": [
                "Validate pipeline probability and retainer assumptions before committing",
                "Assign an owner for onboarding and recheck capacity after staffing or leave changes",
            ],
            "downside": "Accepting new clients without explicit hours can hide a delivery bottleneck.",
            "confidence": _confidence(0.58 if growth_is_configured else 0.3),
            "evidence": evidence[:8],
        })
        action = str(growth_inputs.get("client_action") or "unspecified")
        # Remove the selected keep/drop effect so each alternative below is
        # calculated independently from the same canonical baseline.
        base_client_capacity = float(projection.get("capacity_remaining_hours") or 0.0) + client_capacity_delta
        base_client_work = float(projection.get("work_demand_hours") or 0.0) - client_capacity_delta
        for name, sign, label in (("keep_client", 1.0, "Keeping"), ("drop_client", -1.0, "Dropping")):
            revenue = growth_inputs.get("client_revenue_delta")
            cost = growth_inputs.get("client_cost_delta")
            hours = growth_inputs.get("client_hours_delta")
            configured = bool(revenue or cost or hours or action == ("keep" if sign > 0 else "drop"))
            alternative_capacity = round(base_client_capacity - sign * float(hours or 0.0), 3)
            alternative_work = round(base_client_work + sign * float(hours or 0.0), 3)
            alternative_revenue = round(float(projected_finance) + sign * float(revenue or 0.0), 3) if projected_finance is not None else None
            alternative_margin = round(sign * (float(revenue or 0.0) - float(cost or 0.0)), 3)
            scenarios.append({
                "name": name,
                "retained_inputs": growth_inputs,
                "assumptions": [
                    f"{label} is evaluated using the explicitly supplied client revenue, cost, and hours deltas",
                    "A missing economic input remains unknown rather than being estimated",
                ],
                "domain_impacts": {
                    "finance": f"recognized revenue projection {alternative_revenue}; margin delta {alternative_margin}" if alternative_revenue is not None else f"recognized revenue unknown; margin delta {alternative_margin}",
                    "capacity": f"remaining capacity changes to {alternative_capacity}h" if hours else "capacity impact unknown",
                    "work": f"work demand changes to {alternative_work}h" if hours else "work impact unknown",
                    "client_health": "relationship health may improve through focus" if name == "drop_client" else "relationship continuity is preserved; delivery load remains",
                    "scope": "scope obligations require explicit closeout or renewal evidence",
                },
                "constraints": [
                    "Keep/drop is a decision aid and does not terminate a contract or create a write",
                    *scenario_inputs["constraints"],
                ],
                "mitigations": [
                    "Review contract, payment history, scope overage, and relationship evidence before deciding",
                    "Record the approved decision and outcome so later recommendations can be evaluated",
                ],
                "downside": "The scenario is incomplete when client economics or capacity hours are not connected.",
                "confidence": _confidence(0.56 if configured else 0.28),
                "evidence": evidence[:8],
            })
        # Scenario v2 branches are additive: retain legacy names above while
        # exposing four explicit decision branches with honest missing-data
        # and sensitivity metadata.
        retained = scenario_inputs.get("retained_inputs", {})
        has_inputs = any(value not in (None, "", 0, 0.0) for key, value in retained.items() if key != "client_action")
        baseline_projection = {
            "capacity": projection.get("capacity_remaining_hours"),
            "delivery": projection.get("work_demand_hours"),
            "finance": projection.get("recognized_revenue"),
            "client_health": projected_health,
            "campaign": projection.get("campaign"),
        }
        missing = [key for key, value in baseline_projection.items() if value is None]
        v2 = [
            ("baseline", {}, "Observe current canonical state without intervention."),
            ("option_a", {"capacity_hours_delta": retained.get("capacity_hours_delta", 0.0)}, "Add or protect capacity using only supplied hours."),
            ("option_b", {"work_hours_delta": retained.get("work_hours_delta", 0.0)}, "Change delivery demand using only supplied work hours."),
            ("option_c", {"client_action": retained.get("client_action")}, "Change client scope only when an explicit client action is supplied."),
        ]
        for name, changed, summary in v2:
            branch_inputs = {key: value for key, value in changed.items() if value not in (None, "", 0, 0.0)}
            branch_missing = list(missing)
            if name == "option_c" and not branch_inputs:
                branch_missing.append("client_action")
            sensitivity = None
            if has_inputs and branch_inputs:
                sensitivity = {
                    "status": "bounded",
                    "basis": sorted(branch_inputs),
                    "direction": "improves_capacity_or_delivery" if name in {"option_a", "option_b"} else "depends_on_client_economics",
                }
            scenarios.append({
                "name": name,
                "summary": summary,
                "assumptions": ["Only retained read-time inputs are changed", "Unobserved market and causal factors remain unknown"],
                "changed_inputs": branch_inputs,
                "retained_inputs": retained,
                "domain_impacts": {
                    "capacity": baseline_projection["capacity"] if name == "baseline" else ("unknown" if "capacity" in branch_missing else baseline_projection["capacity"]),
                    "delivery": baseline_projection["delivery"],
                    "finance": baseline_projection["finance"],
                    "client_health": baseline_projection["client_health"],
                    "campaign": baseline_projection["campaign"],
                },
                "risks": ["Branch is directional and requires a measured canonical outcome."],
                "missing_data": branch_missing,
                "sensitivity": sensitivity,
                "confidence": _confidence(0.62 if branch_inputs and not branch_missing else 0.32),
                "constraints": ["No metrics are fabricated", *scenario_inputs["constraints"]],
                "mitigations": ["Convert an approved branch into canonical work before acting", "Measure the next outcome"],
                "evidence": evidence[:6],
            })
        return scenarios
    
    def _enrich_finding(
        self,
        finding: dict[str, Any],
        domains: dict[str, Any],
        relationships: list[dict[str, Any]],
        analogues: list[dict[str, Any]],
        decision_links: list[dict[str, Any]],
        domain_evidence: list[dict[str, Any]],
        moment: datetime,
        scenario_inputs: dict[str, Any],
    ) -> None:
        supporting = list(finding.get("evidence", []))
        opposing: list[dict[str, Any]] = []
        # Explicitly preserve evidence that weakens a hypothesis: healthy client
        # score, positive campaign ROAS, or a shipped linked work event.
        health = domains["client_health"]
        if health and float(health.get("overall") or 0) >= 0.75:
            opposing.append(self._canonical("client_health_snapshots", str(health["id"]), f"Healthy client score {health.get('overall')} opposes an unqualified client-health decline.", 0.8))
        for row in domains["campaign_metrics"]["items"]:
            if row.get("roas") is not None and float(row["roas"]) >= 1:
                opposing.append(self._canonical("campaign_metric_snapshots", str(row["metric_id"]), f"ROAS {row['roas']} is positive and opposes a blanket campaign-underperformance claim.", 0.76))
        finding["supporting_evidence"] = supporting
        finding["opposing_evidence"] = opposing
        finding["causal_links"] = relationships
        hypotheses: list[dict[str, Any]] = []
        for hypothesis in finding.get("hypotheses", []):
            item = dict(hypothesis)
            item.setdefault("stance", "leading")
            item.setdefault("supporting_evidence", supporting)
            item.setdefault("opposing_evidence", opposing)
            item.setdefault("assumptions", ["The cited records are representative of the visible state"])
            hypotheses.append(item)
        # Attach relevant cross-domain links as competing hypotheses.  A link
        # is deliberately phrased as a hypothesis and carries its own evidence
        # and confidence rather than being promoted to an asserted cause.
        finding_refs = {
            (str(item.get("object_ref", {}).get("type")), str(item.get("object_ref", {}).get("id")))
            for item in supporting
            if isinstance(item, dict)
        }
        for link in relationships:
            link_refs = {
                (str(item.get("type")), str(item.get("id")))
                for item in link.get("evidence", [])
                if isinstance(item, dict)
            }
            if not finding_refs or not finding_refs.intersection(link_refs):
                continue
            hypotheses.append({
                "text": f"Cross-domain hypothesis: {link.get('explanation')}",
                "confidence": link.get("confidence") or _confidence(0.4),
                "stance": "cross_domain",
                "relation": link.get("relation", "unknown"),
                "supporting_evidence": list(link.get("evidence", [])),
                "opposing_evidence": opposing,
                "assumptions": ["The linked records are comparable at the read watermark", "Co-occurrence is not proof of causation"],
            })
        hypotheses.extend({
            "text": f"Competing explanation: {item['summary']}",
            "confidence": item["confidence"],
            "stance": "opposing",
            "supporting_evidence": opposing,
            "opposing_evidence": supporting,
            "assumptions": ["The opposing record is current at the read watermark"],
        } for item in opposing)
        finding["hypotheses"] = hypotheses
        scenarios = list(finding.get("scenarios") or self._scenarios(domains, relationships, domain_evidence, scenario_inputs))
        expanded = self._scenarios(domains, relationships, domain_evidence, scenario_inputs)
        existing_names = {str(scenario.get("name")) for scenario in scenarios}
        for candidate in expanded:
            if candidate.get("name") not in existing_names:
                scenarios.append(candidate)
                existing_names.add(str(candidate.get("name")))
        normalized_scenarios: list[dict[str, Any]] = []
        for scenario in scenarios:
            normalized = dict(scenario)
            normalized.setdefault("retained_inputs", scenario_inputs["retained_inputs"])
            normalized.setdefault("assumptions", ["Only visible canonical records are used", "No unobserved external shock"])
            normalized.setdefault("domain_impacts", {"work": "impact not quantified", "client_health": "impact not quantified", "finance": "impact not quantified"})
            normalized.setdefault("constraints", ["Provider and source gaps remain explicit", "No disconnected value is inferred"])
            normalized.setdefault("mitigations", ["Set an owner and review date", "Record an outcome after acting"])
            normalized.setdefault("downside", "The scenario may not address an unobserved cause.")
            normalized.setdefault("evidence", domain_evidence[:6])
            normalized.setdefault("confidence", _confidence(0.45))
            normalized_scenarios.append(normalized)
        finding["scenarios"] = normalized_scenarios
        finding["historical_analogues"] = analogues
        finding["decision_action_outcome_learning"] = decision_links
        finding["recommendation_evaluation"] = self._recommendation_evaluation([finding], decision_links, analogues)
        finding["uncertainty"] = self._calibration(supporting, opposing, status="ready")
    
    def _cross_wing_plan(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        domains: dict[str, Any],
        relationships: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        scenario_inputs: dict[str, Any],
        moment: datetime,
    ) -> dict[str, Any]:
        """Create a reversible plan descriptor; it does not create workflow runs."""
        active_wings = sorted({
            str(row.get("assignee_wing"))
            for row in self._optional_rows(
                """SELECT DISTINCT s.assignee_wing
                     FROM workflow_runs r JOIN workflow_stage_runs s ON s.run_id=r.id
                    WHERE r.organization_id=? AND r.workspace_id=? AND r.status NOT IN ('completed','cancelled')
                      AND s.assignee_wing IS NOT NULL
                    ORDER BY s.assignee_wing""",
                (organization_id, workspace_id),
            )
            if row.get("assignee_wing")
        })
        if not active_wings:
            active_wings = ["Client Success", "Operations", "Strategy"]
        dependency_refs = [link.get("evidence", []) for link in relationships[:4]]
        flattened_dependencies = [ref for group in dependency_refs for ref in group]
        lead_finding = findings[0] if findings else None
        work_hours = float(scenario_inputs["projection"].get("work_demand_hours") or 0)
        capacity_remaining = float(scenario_inputs["projection"].get("capacity_remaining_hours") or 0)
        deadline_shift = float(scenario_inputs["retained_inputs"].get("deadline_days_delta") or 0)
        deadline = (moment.replace(microsecond=0) + self._days(max(1, min(30, 7 + deadline_shift)))).isoformat()
        steps = [
            {
                "id": "plan-triage",
                "wing": active_wings[0],
                "title": "Triage cited operating signal",
                "depends_on": [],
                "resources": {"person_id": person_id, "estimated_hours": 1.0},
                "deadline": deadline,
                "risks": ["Wrong prioritization if cited evidence is stale"],
                "evidence": (lead_finding or {}).get("evidence", [])[:3],
            },
            {
                "id": "plan-unblock",
                "wing": active_wings[min(1, len(active_wings) - 1)],
                "title": "Resolve blocker or record explicit deferral",
                "depends_on": ["plan-triage"],
                "resources": {"capacity_remaining_hours": capacity_remaining, "projected_work_hours": work_hours},
                "deadline": deadline,
                "risks": ["Capacity remains negative after the intervention"] if capacity_remaining < 0 else ["Outcome not measured after action"],
                "evidence": flattened_dependencies[:4],
            },
            {
                "id": "plan-learn",
                "wing": active_wings[-1],
                "title": "Record outcome and update confidence",
                "depends_on": ["plan-unblock"],
                "resources": {"requires_canonical_outcome": True},
                "deadline": deadline,
                "risks": ["No learning loop if outcome is not linked to the decision or workflow"],
                "evidence": [],
            },
        ]
        return {
            "goal": "Turn the highest-confidence visible intelligence signal into a reversible cross-wing operating plan.",
            "status": "proposed_read_only",
            "steps": steps,
            "dependencies": flattened_dependencies[:12],
            "constraints": ["Plan descriptors are not executed by the Intelligence service", *scenario_inputs["constraints"]],
            "confidence": _confidence(0.66 if findings else 0.34),
            "action_boundary": "requires_canonical_route_before_write",
        }
    
    @staticmethod
    def _days(value: float) -> Any:
        from datetime import timedelta
        return timedelta(days=value)
    
    def _recommendation_evaluation(
        self,
        findings: list[dict[str, Any]],
        decision_links: list[dict[str, Any]],
        analogues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        outcome_count = sum(int((link.get("evaluation") or {}).get("outcome_count") or 0) for link in decision_links)
        learning_count = sum(int((link.get("evaluation") or {}).get("learning_count") or 0) for link in decision_links)
        validated_links = sum(1 for link in decision_links if (link.get("evaluation") or {}).get("status") == "validated")
        analogue_rates = [
            float((item.get("outcome_stats") or {}).get("resolution_rate"))
            for item in analogues
            if (item.get("outcome_stats") or {}).get("resolution_rate") is not None
        ]
        analogue_rate = round(sum(analogue_rates) / len(analogue_rates), 3) if analogue_rates else None
        confidence_scores = [float((finding.get("confidence") or {}).get("score") or 0.0) for finding in findings]
        base_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0
        calibration_delta = (0.06 * validated_links) + (0.03 * learning_count) - (0.04 if findings and outcome_count == 0 else 0.0)
        calibrated = max(0.0, min(1.0, base_confidence + calibration_delta))
        return {
            "status": "outcome_backed" if outcome_count else "pending_outcome",
            "outcome_count": outcome_count,
            "learning_count": learning_count,
            "validated_decision_link_count": validated_links,
            "historical_resolution_rate": analogue_rate,
            "base_confidence": _confidence(base_confidence),
            "calibrated_confidence": _confidence(calibrated),
            "calibration_delta": round(calibration_delta, 3),
            "next_measurement": "Record a linked work, workflow, signal, feedback, or performance outcome after acting.",
        }
    
    def _brain_evidence(self, workspace_id: str, actor_id: str | None, as_of: datetime,
                        query: str | None = None) -> list[dict[str, Any]]:
        if not actor_id:
            self._evidence_issue = "actor_binding_unavailable"
            return []
        try:
            actor = self.os._require_actor(workspace_id, actor_id)
            if query:
                bundle = self.os.search(workspace_id, actor_id, query, as_of=as_of, limit=8)
                terms = [token for token in query.lower().split() if token not in {"no", "such", "the", "a", "an", "for", "with", "and", "or", "of", "to", "evidence", "information", "data", "status"}]
                matched = []
                for item in bundle.items:
                    haystack = " ".join([item.citation.evidence_span, str(item.payload)]).lower()
                    if terms and all(term in haystack for term in terms):
                        matched.append(item)
                return [
                    {
                        "object_ref": {"type": item.kind, "id": item.payload.get("id") or item.payload.get("fact_id") or item.payload.get("document_id")},
                        "citation": item.citation.to_dict(),
                        "summary": item.citation.evidence_span,
                        "confidence": _confidence(item.citation.confidence if item.citation.confidence is not None else item.score),
                    }
                    for item in matched
                ]
            sources = self.os.store.allowed_sources(workspace_id, actor, as_of=as_of)
            source_ids = [source.id for source in sources]
            facts = self.os.store.list_facts(workspace_id, source_ids, as_of=as_of)
            result = []
            for fact in facts[-12:]:
                result.append({
                    "object_ref": {"type": "fact", "id": fact.id},
                    "citation": fact.citation.to_dict(),
                    "summary": f"{fact.subject} {fact.predicate} {fact.object}",
                    "confidence": _confidence(fact.confidence),
                })
            return result
        except AuthorizationError:
            # A missing/invalid actor binding is a scoped evidence issue, not
            # an opaque provider failure.
            self._evidence_issue = "actor_binding_unavailable"
            return []
        except Exception:
            # Intelligence remains useful from canonical operational rows when
            # an optional brain provider/actor binding is unavailable.
            self._evidence_issue = "evidence_retrieval_failed"
            return []
    
    @staticmethod
    def _canonical(table: str, item_id: str, text: str, score: float = 0.65) -> dict[str, Any]:
        return {
            "object_ref": {"type": table.rstrip("s"), "id": item_id},
            "citation": {
                "source_id": None,
                "source_key": f"canonical://{table}/{item_id}",
                "locator": f"{table}:{item_id}",
                "content_hash": None,
                "evidence_span": text,
                "confidence": round(score, 3),
            },
            "summary": text,
            "confidence": _confidence(score),
        }
    
    @classmethod
    def _evidence(cls, table: str, item_id: str, text: str, brain: list[dict[str, Any]], score: float = 0.65) -> list[dict[str, Any]]:
        return [cls._canonical(table, item_id, text, score), *brain[:2]]
    
    @staticmethod
    def _query_finding(query: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        scores = [float(item.get("confidence", {}).get("score", 0.0)) for item in evidence]
        score = sum(scores) / len(scores) if scores else 0.0
        return {
            "id": "intelligence-query-evidence",
            "type": "evidence_synthesis",
            "title": f"Evidence for: {query}",
            "summary": f"Auremgrid found {len(evidence)} permitted evidence item{'s' if len(evidence) != 1 else ''}. No causal conclusion is asserted from retrieval alone.",
            "confidence": _confidence(score),
            "evidence": evidence,
            "situation": {"state": "evidence_retrieved", "query": query},
            "changes": [],
            "hypotheses": [{"text": "A causal explanation requires corroborating operational changes or an explicit decision record.", "confidence": _confidence(0.35)}],
            "scenarios": [],
            "impact": {"level": "unknown", "summary": "Impact cannot be estimated from retrieved evidence alone."},
            "recommendation": {"summary": "Review the cited evidence before creating a decision or workflow.", "rationale": "Retrieval relevance is not proof of causation."},
            "actions": [],
            "action_descriptors": [],
        }
    
    @staticmethod
    def _action(organization_id: str, person_id: str, actor_id: str | None, workspace_id: str, title: str, request: str, statement: str, rationale: str) -> list[dict[str, Any]]:
        actor = actor_id or ""
        work_payload = {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id, "actor_id": actor, "requested_by": person_id, "title": title, "request": request}
        decision_payload = {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id, "statement": statement, "rationale": rationale}
        approval_payload = {"organization_id": organization_id, "workspace_id": workspace_id, "requested_by_type": "person", "requested_by_id": person_id, "requested_for": title, "action_type": "intelligence.review", "payload": {"statement": statement, "request": request}, "reason": rationale}
        return [
            {
                "id": "capture-follow-up-work",
                "action": "Create follow-up work", "label": "Create follow-up work",
                "kind": "work.capture",
                "route": None,
                "method": None,
                "payload": work_payload, "required_fields": [] if actor_id else ["actor_id"],
                "safe": True,
                "one_way": False,
                "requires_approval": True,
                "status": "review_only",
                "executable": False,
                "execution_note": "No supervised catalog action exists for work.capture; this is a read-only recommendation.",
            },
            {
                "id": "record-decision",
                "action": "Record a decision", "label": "Record a decision",
                "kind": "decision.create",
                "route": None,
                "method": None,
                "payload": decision_payload, "required_fields": [],
                "safe": True,
                "one_way": False,
                "requires_approval": True,
                "status": "review_only",
                "executable": False,
                "execution_note": "No supervised catalog action exists for decision.create; this is a read-only recommendation.",
            },
            {
                "id": "generate-client-weekly-report",
                "action": "Generate report", "label": "Generate report",
                "kind": "report.generate",
                "route": None,
                "method": None,
                "payload": {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id, "type": "client_weekly_report"},
                "required_fields": [],
                "safe": True,
                "one_way": False,
                "requires_approval": True,
                "status": "supervised_catalog_only",
                "executable": False,
                "supervised_action": "generate_report",
                "execution_note": "Execute only through an approved agent action descriptor from the supervised catalog.",
            },
            {
                "id": "request-approval",
                "action": "Request approval", "label": "Request approval",
                "kind": "approval.request",
                "route": "/approvals",
                "method": "POST",
                "payload": approval_payload, "required_fields": [],
                "safe": True,
                "one_way": False,
                "requires_approval": True,
                "status": "proposed",
            },
        ]
    
    @classmethod
    def _risk_finding(cls, organization_id: str, person_id: str, actor_id: str | None, workspace_id: str, risk: dict[str, Any], brain: list[dict[str, Any]], changes: list[dict[str, Any]]) -> dict[str, Any]:
        score = max(0.2, min(0.97, float(risk.get("probability") or 0.5)))
        severity = str(risk.get("severity") or "medium")
        impact = str(risk.get("impact") or "Operational impact is not yet quantified.")
        recommendation = str(risk.get("recommended_action") or "Review with the workspace owner.")
        text = str(risk.get("evidence") or impact)
        hypothesis = f"The {risk.get('type', 'operational')} signal is likely to persist without an owner-led intervention."
        return {
            "id": f"intelligence-risk-{risk['id']}", "type": "risk", "title": f"{severity.title()} risk: {risk.get('type', 'operational')}",
            "summary": text, "confidence": _confidence(score),
            "evidence": cls._evidence("risks", risk["id"], text, brain, score),
            "situation": {"state": "open", "severity": severity, "owner_person_id": risk.get("owner_person_id")},
            "changes": [{"id": row["id"], "type": "work_event", "summary": row.get("detail") or row.get("action"), "recorded_at": row.get("recorded_at")} for row in changes[:3]],
            "hypotheses": [{"text": hypothesis, "confidence": _confidence(max(0.35, score - 0.08))}],
            "scenarios": [{"name": "intervene", "likelihood": "higher", "impact": impact}, {"name": "defer", "likelihood": "possible", "impact": "Risk may compound or become harder to reverse."}],
            "impact": {"level": severity, "summary": impact},
            "recommendation": {"summary": recommendation, "rationale": "The recommendation is grounded in the open canonical risk and its cited evidence."},
            "actions": cls._action(organization_id, person_id, actor_id, workspace_id, f"Review {risk.get('type', 'risk')}", recommendation, f"Address {risk.get('type', 'risk')} risk", recommendation),
            "action_descriptors": cls._action(organization_id, person_id, actor_id, workspace_id, f"Review {risk.get('type', 'risk')}", recommendation, f"Address {risk.get('type', 'risk')} risk", recommendation),
        }
    
    @classmethod
    def _delivery_risk(cls, item: dict[str, Any], moment: datetime) -> bool:
        if item.get("blocking_reason"):
            return True
        deadline = item.get("deadline") or item.get("needed_by")
        if not deadline:
            return False
        try:
            stamp = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp < moment
        except ValueError:
            return False
    
    @classmethod
    def _work_finding(cls, organization_id: str, person_id: str, actor_id: str | None, workspace_id: str, item: dict[str, Any], brain: list[dict[str, Any]], changes: list[dict[str, Any]], moment: datetime) -> dict[str, Any]:
        text = str(item.get("blocking_reason") or f"Work item remains {item.get('status', 'open')} beyond its expected date.")
        score = 0.88 if item.get("blocking_reason") else 0.73
        recommendation = "Assign an owner and confirm the next unblock step before the deadline moves again."
        actions = cls._action(organization_id, person_id, actor_id, workspace_id, f"Unblock: {item['title']}", text, f"Resolve blocker for {item['title']}", recommendation)
        return {
            "id": f"intelligence-work-{item['id']}", "type": "delivery_risk", "title": f"Delivery attention: {item['title']}",
            "summary": text, "confidence": _confidence(score),
            "evidence": cls._evidence("work_items", item["id"], text, brain, score),
            "situation": {"state": item.get("status"), "priority": item.get("priority"), "deadline": item.get("deadline") or item.get("needed_by")},
            "changes": [{"id": row["id"], "type": "work_event", "summary": row.get("detail") or row.get("action"), "recorded_at": row.get("recorded_at")} for row in changes if row.get("work_item_id") == item["id"]][:5],
            "hypotheses": [{"text": "The delivery path is blocked or under-owned.", "confidence": _confidence(0.72)}],
            "scenarios": [{"name": "unblock", "likelihood": "higher", "impact": "Delivery returns to plan with explicit ownership."}, {"name": "defer", "likelihood": "possible", "impact": "Deadline and downstream commitments slip."}],
            "impact": {"level": "high" if item.get("blocking_reason") else "medium", "summary": "Potential schedule and client-confidence impact."},
            "recommendation": {"summary": recommendation, "rationale": "The work item is open and shows a blocker or past expected date."},
            "actions": actions, "action_descriptors": actions,
        }
    
    @classmethod
    def _decision_finding(cls, organization_id: str, person_id: str, actor_id: str | None, workspace_id: str, decision: dict[str, Any], brain: list[dict[str, Any]], changes: list[dict[str, Any]]) -> dict[str, Any]:
        text = str(decision.get("evidence") or decision.get("rationale") or decision.get("statement"))
        actions = cls._action(organization_id, person_id, actor_id, workspace_id, "Validate current decision", text, str(decision.get("statement")), str(decision.get("rationale")))
        return {
            "id": f"intelligence-decision-{decision['id']}", "type": "decision_signal", "title": "Recent decision to validate",
            "summary": str(decision.get("statement")), "confidence": _confidence(0.62),
            "evidence": cls._evidence("decisions", decision["id"], text, brain, 0.62),
            "situation": {"state": "effective", "effective_from": decision.get("effective_from"), "effective_until": decision.get("effective_until")},
            "changes": [{"id": row["id"], "type": "work_event", "summary": row.get("detail") or row.get("action"), "recorded_at": row.get("recorded_at")} for row in changes[:3]],
            "hypotheses": [{"text": "The decision may need a current owner check as operating conditions change.", "confidence": _confidence(0.52)}],
            "scenarios": [{"name": "validate", "likelihood": "higher", "impact": "Current work remains aligned."}, {"name": "leave_stale", "likelihood": "possible", "impact": "Teams may optimize against outdated direction."}],
            "impact": {"level": "medium", "summary": "Alignment and execution risk if the decision is stale."},
            "recommendation": {"summary": "Validate the decision with its owner before changing course.", "rationale": "A durable decision is present, but the engine cannot infer continued applicability without fresh evidence."},
            "actions": actions, "action_descriptors": actions,
        }

