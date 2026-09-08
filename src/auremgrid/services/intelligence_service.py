from __future__ import annotations

"""Deterministic, citation-first workspace intelligence service."""

from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.intelligence_shared import _now
from auremgrid.services.intelligence_context import IntelligenceContextMixin
from auremgrid.services.intelligence_history import IntelligenceHistoryMixin
from auremgrid.services.intelligence_reasoning import IntelligenceReasoningMixin
from auremgrid.services.intelligence_findings import IntelligenceFindingsMixin


class IntelligenceService(IntelligenceContextMixin, IntelligenceHistoryMixin, IntelligenceReasoningMixin, IntelligenceFindingsMixin):
    @staticmethod
    def _json_safe(value: Any) -> Any:
        """Keep provider context JSON-compatible and strip obvious secrets."""
        secret_terms = ("secret", "token", "password", "api_key", "authorization", "credential")
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if any(term in str(key).lower() for term in secret_terms)
                else IntelligenceService._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [IntelligenceService._json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn
        self._evidence_issue: str | None = None
    
    def workspace(self, organization_id: str, workspace_id: str, person_id: str,
                  actor_id: str | None = None, as_of: datetime | None = None,
                  query: str | None = None,
                  what_if: dict[str, Any] | None = None,
                  context_type: str | None = None,
                  context_id: str | None = None,
                  capabilities: Any = None,
                  use_reasoning_provider: bool = True) -> dict[str, Any]:
        """Return a stable intelligence contract for one authorized workspace.
    
        ``as_of`` is a read watermark.  Current canonical operational rows are
        filtered by their recorded/updated timestamps where available; brain
        facts use the existing temporal/ACL filtering implementation.
        """
        membership = self.os._require_person_access(organization_id, workspace_id, person_id)
        can_write = membership.role in {"admin", "operator"} and (
            capabilities is None or "workspace_write" in set(capabilities)
        )
        self._evidence_issue = None
        workspace = self.os.store.get_workspace(workspace_id)
        if workspace is None:
            raise AuthorizationError("workspace not found")
        moment = as_of.astimezone(timezone.utc) if as_of is not None else _now()
        cutoff = moment.isoformat()
        scope = {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace.name,
            "person_id": person_id,
            "as_of": moment.isoformat(),
        }
    
        risks = self._rows(
            "SELECT id,type,severity,probability,impact,evidence,recommended_action,owner_person_id,status,detected_at "
            "FROM risks WHERE organization_id=? AND workspace_id=? AND status='open' AND detected_at<=? ORDER BY detected_at DESC,id",
            (organization_id, workspace_id, cutoff),
        )
        work = self._rows(
            "SELECT id,title,request,status,needed_by,deadline,blocking_reason,priority,project_id,campaign_id,owner_person_id,assignee_person_id,reviewer_person_id,estimate_hours,actual_effort_hours,updated_at "
            "FROM work_items WHERE workspace_id=? AND status!='shipped' AND updated_at<=? ORDER BY updated_at DESC,id",
            (workspace_id, cutoff),
        )
        decisions = self._rows(
            "SELECT id,statement,rationale,evidence,effective_from,effective_until,source_id,source_locator "
            "FROM decisions WHERE organization_id=? AND workspace_id=? AND effective_from<=? "
            "AND (effective_until IS NULL OR effective_until>?) ORDER BY effective_from DESC,id",
            (organization_id, workspace_id, cutoff, cutoff),
        )
        changes = self._rows(
            "SELECT id,work_item_id,action,from_status,to_status,detail,recorded_at FROM work_events "
            "WHERE workspace_id=? AND recorded_at<=? ORDER BY recorded_at DESC,id LIMIT 12",
            (workspace_id, cutoff),
        )
        signals = self._rows(
            "SELECT id,type,source_type,source_id,evidence,confidence,status,created_at FROM signals "
            "WHERE organization_id=? AND workspace_id=? AND created_at<=? ORDER BY created_at DESC,id LIMIT 12",
            (organization_id, workspace_id, cutoff),
        )
    
        # The engine is a read-only projection over canonical records.  Each
        # source below is fenced by both organization and workspace (or by a
        # workspace join where the legacy table has no organization column).
        domains = self._domain_snapshot(
            organization_id, workspace_id, cutoff, moment, work, risks, decisions, signals,
        )
        scenario_inputs = self._scenario_inputs(domains, what_if)
        scope_contract = self._context_contract(organization_id, workspace_id, person_id, cutoff, domains)
        scope_contract["current"] = self._selected_context(
            organization_id, workspace_id, person_id, context_type, context_id,
        )
        domain_evidence = self._domain_evidence(domains)
        relationships = self._cross_domain_relationships(domains, domain_evidence)
        decision_links = self._decision_action_outcome_learning(
            organization_id, workspace_id, cutoff, decisions,
        )
        analogues = self._historical_analogues(
            organization_id, workspace_id, cutoff, risks, work, signals, domains,
        )
    
        query = query.strip() if isinstance(query, str) else None
        evidence = self._brain_evidence(workspace_id, actor_id, moment, query)
        canonical_count = len(risks) + len(work) + len(decisions) + len(changes) + len(signals)
        findings: list[dict[str, Any]] = []
        if query:
            if evidence:
                findings.append(self._query_finding(query, evidence))
        else:
            for risk in risks:
                findings.append(self._risk_finding(organization_id, person_id, actor_id, workspace_id, risk, evidence, changes))
            for item in work:
                if self._delivery_risk(item, moment):
                    findings.append(self._work_finding(organization_id, person_id, actor_id, workspace_id, item, evidence, changes, moment))
            if not findings and decisions and evidence:
                findings.append(self._decision_finding(organization_id, person_id, actor_id, workspace_id, decisions[0], evidence, changes))
            if not findings and relationships:
                findings.append(self._synthesis_finding(
                    organization_id, person_id, actor_id, workspace_id,
                    relationships, domain_evidence, domains,
                ))
    
        # Attach the governing architecture to every finding, while retaining
        # the original first-slice fields for existing consumers.
        for finding in findings:
            self._enrich_finding(
                finding, domains, relationships, analogues, decision_links,
                domain_evidence, moment, scenario_inputs,
            )
        if as_of is not None or not can_write:
            # Historical intelligence is a read-only explanation; never offer
            # mutable action descriptors for a past state.
            for finding in findings:
                finding["actions"] = []
                finding["action_descriptors"] = []
        elif capabilities is not None:
            allowed_capabilities = set(capabilities)
            for finding in findings:
                descriptors = [
                    descriptor for descriptor in finding.get("action_descriptors", [])
                    if self._descriptor_allowed_by_capability(descriptor, allowed_capabilities)
                ]
                finding["actions"] = descriptors
                finding["action_descriptors"] = descriptors
    
        provider_reasons: list[str] = []
        if self._evidence_issue:
            provider_reasons.append(self._evidence_issue)
        if (getattr(self.os, "graph_health", {}) or {}).get("status") not in {None, "healthy"}:
            provider_reasons.append("graph_provider_degraded")
        if (getattr(self.os, "embedding_health", {}) or {}).get("status") not in {None, "healthy"}:
            provider_reasons.append("semantic_provider_degraded")
        if query and not evidence:
            status = "insufficient_evidence"
            degraded_reason = "query_no_visible_evidence"
        elif not evidence and canonical_count == 0:
            status = "insufficient_evidence"
            degraded_reason = "no_visible_evidence"
        elif provider_reasons:
            status = "degraded"
            degraded_reason = ";".join(provider_reasons)
        else:
            status = "ready"
            degraded_reason = None
        pipeline = [
            "evidence", "situation", "changes", "hypotheses", "historical_analogues",
            "scenarios", "impact", "recommendation", "deliberation", "decision",
            "workflow", "outcome", "learning",
        ]
        context = {
            "pipeline": pipeline,
            "evidence_count": len(evidence),
            "canonical_record_count": canonical_count,
            "open_risk_count": len(risks),
            "open_work_count": len(work),
            "change_count": len(changes) + len(signals),
            "historical": as_of is not None,
            "query": query,
            "domains": sorted(domains),
            "scope_contract": scope_contract,
            "scenario_inputs": scenario_inputs,
            "cross_domain_relationship_count": len(relationships),
            "historical_analogue_count": len(analogues),
            "decision_link_count": len(decision_links),
        }
        recommended_plan = self._cross_wing_plan(
            organization_id, workspace_id, person_id, domains, relationships, findings, scenario_inputs, moment,
        )
        recommendation_evaluation = self._recommendation_evaluation(findings, decision_links, analogues)
        deliberation = self._deliberation(findings, relationships, analogues, decision_links, recommended_plan)
        if use_reasoning_provider:
            model_reasoning, reasoning_meta = self._model_reasoning(
                organization_id=organization_id,
                workspace_id=workspace_id,
                person_id=person_id,
                actor_id=actor_id,
                scope=scope,
                context=context,
                evidence=evidence,
                findings=findings,
                relationships=relationships,
                analogues=analogues,
                decision_links=decision_links,
                recommended_plan=recommended_plan,
                scenario_inputs=scenario_inputs,
            )
        else:
            model_reasoning = None
            reasoning_meta = {
                "status": "disabled",
                "provider": None,
                "model": None,
                "version": None,
                "evidence_count": len(evidence),
                "evidence_refs": [item.get("object_ref") for item in evidence if item.get("object_ref")][:24],
                "context_hash": None,
                "output_hash": None,
                "fallback_reason": None,
                "evaluation_safety": None,
            }
        if reasoning_meta.get("fallback_reason"):
            provider_reasons.append(f"reasoning_{reasoning_meta['fallback_reason']}")
            if status in {"ready", "degraded"}:
                status = "degraded"
                degraded_reason = ";".join(provider_reasons)
        if model_reasoning is not None:
            deliberation.update(model_reasoning)
            deliberation["mode"] = "model_backed"
        deliberation["provider_metadata"] = reasoning_meta
        for finding in findings:
            finding["deliberation"] = self._deliberation(
                [finding], relationships, finding.get("historical_analogues", analogues),
                finding.get("decision_action_outcome_learning", decision_links), recommended_plan,
            )
            if model_reasoning is not None:
                finding["deliberation"].update(model_reasoning)
                finding["deliberation"]["mode"] = "model_backed"
                finding["deliberation"]["provider_metadata"] = reasoning_meta
        generated_at = _now().isoformat()
        hypothesis_records = self._hypothesis_records(findings, generated_at)
        recommendation_records = self._recommendation_records(
            findings, recommendation_evaluation, deliberation, decision_links, scope, generated_at,
        )
        return {
            "scope": scope,
            "context": context,
            "scope_contract": scope_contract,
            "status": status,
            "degraded_reason": degraded_reason,
            "uncertainty": {
                "label": "high" if status == "insufficient_evidence" else "medium" if status == "degraded" else "low",
                "reason": degraded_reason,
                "calibration": self._calibration(
                    [item for finding in findings for item in finding.get("evidence", [])] or domain_evidence,
                    [],
                    status=status,
                ),
            },
            "domains": domains,
            "cross_domain_relationships": relationships,
            "historical_analogues": analogues,
            "decision_action_outcome_learning": decision_links,
            "recommended_plan": recommended_plan,
            "recommendation_evaluation": recommendation_evaluation,
            "deliberation": deliberation,
            "findings": findings,
            "hypotheses": hypothesis_records,
            "recommendations": recommendation_records,
            "generated_at": generated_at,
        }
    
    @staticmethod
    def _descriptor_allowed_by_capability(descriptor: dict[str, Any], capabilities: set[str]) -> bool:
        if descriptor.get("executable") is False:
            return True
        route = str(descriptor.get("route") or "")
        if route.startswith("/agents"):
            return "agent_run" in capabilities
        if route == "/reports/generate":
            return "workspace_write" in capabilities
        if route in {"/work/capture", "/decisions", "/approvals"}:
            return "workspace_write" in capabilities
        return True
    
    @staticmethod
    def _hypothesis_records(findings: list[dict[str, Any]], generated_at: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for finding in findings:
            for item in finding.get("hypotheses", [])[:12]:
                confidence = item.get("confidence") or _confidence(0.0)
                records.append({
                    "statement": item.get("text") or item.get("hypothesis") or "Unknown hypothesis",
                    "subject": finding.get("title") or finding.get("id"),
                    "status": "review" if finding.get("needs_review") else "proposed",
                    "confidence": confidence,
                    "evidence_for": item.get("supporting_evidence", finding.get("supporting_evidence", []))[:8],
                    "evidence_against": item.get("opposing_evidence", finding.get("opposing_evidence", []))[:8],
                    "assumptions": item.get("assumptions", [])[:8],
                    "generated_by": item.get("generated_by") or {"type": "intelligence_service", "id": "workspace"},
                    "created_at": generated_at,
                    "updated_at": generated_at,
                    "resolved_at": None,
                    "outcome": None,
                })
        return records[:64]
    
    @staticmethod
    def _recommendation_records(
        findings: list[dict[str, Any]],
        evaluation: dict[str, Any],
        deliberation: dict[str, Any],
        decision_links: list[dict[str, Any]],
        scope: dict[str, Any],
        generated_at: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for finding in findings:
            recommendation = finding.get("recommendation") or {}
            if not isinstance(recommendation, dict):
                recommendation = {"summary": str(recommendation)}
            records.append({
                "statement": recommendation.get("summary") or "Review the visible evidence.",
                "recommended_at": generated_at,
                "runbook": None,
                "experts": [agent.get("agent") for agent in (deliberation.get("agents") or []) if agent.get("agent")],
                "confidence": finding.get("confidence") or _confidence(0.0),
                "accepted": None,
                "rejected": None,
                "chosen_option": None,
                "measured_outcomes": [outcome for link in decision_links for outcome in link.get("outcomes", [])][:16],
                "evaluation_window": {"start": scope.get("as_of"), "end": generated_at},
                "score": (evaluation.get("calibrated_confidence") or {}).get("score"),
                "lessons": evaluation.get("next_measurement"),
                "created_at": generated_at,
                "updated_at": generated_at,
            })
        return records[:32]
    
    def portfolio(
        self,
        organization_id: str,
        person_id: str,
        actor_id: str | None = None,
        as_of: datetime | None = None,
        use_reasoning_provider: bool = True,
    ) -> dict[str, Any]:
        """Return an organization portfolio without crossing workspace ACLs."""
        membership = self.os.company.org_membership(organization_id, person_id)
        if membership is None:
            raise AuthorizationError("organization membership required")
        rows = self._rows(
            """SELECT w.id,w.name,wo.kind,wm.role FROM workspaces w
               JOIN workspace_organization wo ON wo.workspace_id=w.id AND wo.organization_id=?
               JOIN workspace_memberships wm ON wm.workspace_id=w.id AND wm.person_id=?
               ORDER BY w.name,w.id""",
            (organization_id, person_id),
        )
        workspaces: list[dict[str, Any]] = []
        for row in rows:
            workspaces.append(self.workspace(
                organization_id, row["id"], person_id, actor_id=actor_id, as_of=as_of,
                use_reasoning_provider=use_reasoning_provider,
            ))
        # Cross-client portfolio aggregates use only the already ACL-filtered
        # workspace projections.  Missing provider values remain null.
        domain_counts: dict[str, int] = {}
        for item in workspaces:
            for domain, value in item.get("domains", {}).items():
                domain_counts[domain] = domain_counts.get(domain, 0) + int(value.get("open_count", value.get("stalled_count", value.get("effective_count", 0))) or 0) if isinstance(value, dict) else domain_counts.get(domain, 0)
        attention: list[dict[str, Any]] = []
        for item in workspaces:
            for finding in item.get("findings", []):
                attention.append({
                    "workspace_id": item["scope"]["workspace_id"],
                    "workspace_name": item["scope"]["workspace_name"],
                    "finding_id": finding["id"],
                    "title": finding["title"],
                    "type": finding["type"],
                    "confidence": finding.get("confidence"),
                    "impact": finding.get("impact"),
                    "summary": finding.get("summary"),
                    "recommendation": finding.get("recommendation"),
                    "evidence": finding.get("evidence", [])[:6],
                })
        attention.sort(key=lambda item: float((item.get("confidence") or {}).get("score", 0.0)), reverse=True)
        portfolio_analogues = self._portfolio_analogues(organization_id, rows, workspaces, (as_of.astimezone(timezone.utc) if as_of else _now()).isoformat())
        status = "ready"
        if any(item.get("status") == "insufficient_evidence" for item in workspaces):
            status = "degraded"
        finance_rows = [item.get("domains", {}).get("finance", {}) for item in workspaces]
        connected_finance = [row for row in finance_rows if row.get("status") == "connected"]
        health_rows = [
            {"workspace_id": item["scope"]["workspace_id"], "health": item.get("domains", {}).get("client_health")}
            for item in workspaces if item.get("domains", {}).get("client_health") is not None
        ]
        capacity_rows = [row for item in workspaces for row in item.get("domains", {}).get("capacity", {}).get("snapshots", [])]
        return {
            "scope": {"organization_id": organization_id, "person_id": person_id, "as_of": (as_of.astimezone(timezone.utc) if as_of else _now()).isoformat()},
            "status": status,
            "workspaces": workspaces,
            "portfolio": {
                "workspace_count": len(workspaces),
                "client_count": sum(1 for row in rows if row["kind"] == "client"),
                "domain_counts": domain_counts,
                "open_work": sum(item.get("domains", {}).get("work", {}).get("open_count", 0) for item in workspaces),
                "open_risks": sum(item.get("domains", {}).get("risks", {}).get("open_count", 0) for item in workspaces),
                "stalled_reviews": sum(item.get("domains", {}).get("reviews", {}).get("stalled_count", 0) for item in workspaces),
                "capacity_overloaded": sum(1 for row in capacity_rows if float(row.get("remaining_hours") or 0) < 0),
                "finance": {
                    "status": "connected" if connected_finance else "not_connected",
                    "recognized_revenue": round(sum(float(row.get("recognized_revenue") or 0) for row in connected_finance), 3) if connected_finance else None,
                    "outstanding_revenue": round(sum(float(row.get("outstanding_revenue") or 0) for row in connected_finance), 3) if connected_finance else None,
                },
                "client_health": health_rows,
                "attention": attention[:20],
                "historical_analogues": portfolio_analogues,
            },
            "historical_analogues": portfolio_analogues,
            "generated_at": _now().isoformat(),
        }
    
    def executive_brief(
        self,
        organization_id: str,
        person_id: str,
        actor_id: str | None = None,
        as_of: datetime | None = None,
        use_reasoning_provider: bool = True,
    ) -> dict[str, Any]:
        """Stable first-class executive output backed by the portfolio projection."""
        result = self.portfolio(
            organization_id, person_id, actor_id=actor_id, as_of=as_of,
            use_reasoning_provider=use_reasoning_provider,
        )
        attention = result["portfolio"].get("attention", [])[:3]
        narrative_items = []
        for rank, item in enumerate(attention, start=1):
            confidence = item.get("confidence") or {}
            impact = item.get("impact") or {}
            recommendation = item.get("recommendation") or {}
            evidence = item.get("evidence", [])[:6]
            causes = item.get("hypotheses") or [{"text": item.get("summary") or item.get("title"), "supporting_evidence": evidence}]
            supporting = item.get("supporting_evidence") or evidence
            opposing = item.get("opposing_evidence") or []
            options = item.get("options") or ([recommendation] if recommendation else [])
            narrative_items.append({
                "rank": rank,
                "workspace_id": item.get("workspace_id"),
                "workspace_name": item.get("workspace_name"),
                "title": item.get("title"),
                "what_changed": item.get("summary") or item.get("title"),
                "why_it_matters": impact.get("summary") or "Impact is not quantified in the visible records.",
                "next_step": recommendation.get("summary") or "Review the cited records and choose a reversible next step.",
                "confidence": confidence,
                "evidence": evidence,
                "causes": causes[:6],
                "supporting_evidence": supporting[:6],
                "opposing_evidence": opposing[:6],
                "options": options[:6],
                "effects": impact,
                "recommendation": recommendation or {"summary": "Review the cited records."},
                "human_decision_needed": bool(item.get("needs_review", True) or not recommendation),
            })
        inaction = self._what_happens_if_do_nothing(narrative_items)
        return {
            **result,
            "type": "executive_brief",
            "headline": "Portfolio operating brief",
            "what_happens_if_do_nothing": inaction,
            "sections": {
                "attention": result["portfolio"]["attention"],
                "top_three": narrative_items,
                "conclusions": narrative_items,
                "what_happens_if_do_nothing": inaction,
                "narrative": {
                    "headline": "Three things need attention" if narrative_items else "No evidence-backed attention items",
                    "items": narrative_items,
                },
                "client_health": [
                    {
                        "workspace_id": item["scope"]["workspace_id"],
                        "workspace_name": item["scope"]["workspace_name"],
                        "health": item.get("domains", {}).get("client_health"),
                    }
                    for item in result["workspaces"]
                    if item.get("domains", {}).get("client_health") is not None
                ],
                "constraints": [
                    {"workspace_id": item["scope"]["workspace_id"], "scenario": scenario}
                    for item in result["workspaces"]
                    for finding in item.get("findings", [])
                    for scenario in finding.get("scenarios", [])
                    if scenario.get("name") == "defer"
                ][:20],
            },
            "conclusions": narrative_items,
        }
    
    @staticmethod
    def _what_happens_if_do_nothing(narrative_items: list[dict[str, Any]]) -> dict[str, Any]:
        evidence: list[Any] = []
        summaries: list[str] = []
        unknowns: list[str] = []
        for item in narrative_items[:3]:
            item_evidence = [entry for entry in item.get("evidence", []) if entry][:4]
            if item_evidence:
                evidence.extend(item_evidence)
                title = item.get("title") or "Attention item"
                impact = item.get("effects") or {}
                impact_summary = impact.get("summary") if isinstance(impact, dict) else None
                summaries.append(f"{title}: {impact_summary or item.get('why_it_matters') or 'impact is not quantified in the visible records'}")
            else:
                unknowns.append(f"{item.get('title') or 'Attention item'} has no linkable evidence for an inaction forecast.")
        if not evidence:
            return {
                "status": "unknown",
                "summary": "No permitted evidence is sufficient to model what happens if no action is taken.",
                "evidence": [],
                "unknowns": unknowns or ["No evidence-backed attention items are visible for this person."],
            }
        return {
            "status": "evidence_backed",
            "summary": "If no action is taken, the visible attention items are expected to remain open: " + " | ".join(summaries)[:1200],
            "evidence": evidence[:12],
            "unknowns": unknowns[:8] or ["Unobserved external changes and uncaptured follow-through remain unknown."],
        }
