"""Context bounding, retrieval plans, and citation weighting helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from auremgrid.services.intelligence_orchestrator_shared import MAX_ITEMS, _bounded_list, _json, _ref_id, _score, _text


class IntelligenceOrchestratorRetrievalMixin:
    _DOMAIN_EVIDENCE_KINDS = {
        "performance": frozenset({"campaign_metric_snapshot", "campaign_metrics", "performance_insight"}),
        "analytics": frozenset({"campaign_metric_snapshot", "campaign_metrics", "performance_insight"}),
        "campaigns": frozenset({"campaign_metric_snapshot", "campaign_metrics"}),
        "delivery": frozenset({"work_item", "work_event", "review"}),
        "workflow": frozenset({"work_item", "work_event", "review"}),
        "capacity": frozenset({"capacity_snapshot"}),
        "finance": frozenset({"finance", "revenue", "invoice", "cost"}),
        "scope": frozenset({"scope_usage", "contract", "scope_allowance"}),
        "client_success": frozenset({"client_health_snapshot", "decision", "signal"}),
        "relationships": frozenset({"client_health_snapshot", "touchpoint", "feedback_event"}),
        "risk": frozenset({"risk", "signal"}),
        "research": frozenset({"fact", "document", "source"}),
        "brain": frozenset({"fact", "document", "source"}),
    }
    @staticmethod
    def _visible_refs(situation: Mapping[str, Any]) -> set[str]:
        refs: set[str] = set()
        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                ref = _ref_id(value)
                if ref:
                    refs.add(ref)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(situation)
        return refs

    def _bounded_context(self, situation: Mapping[str, Any]) -> dict[str, Any]:
        context = {
            "scope": situation.get("scope"), "status": situation.get("status"),
            "domains": situation.get("domains", {}),
            "findings": situation.get("findings", [])[: self.limits.max_items],
            "historical_analogues": situation.get("historical_analogues", [])[: self.limits.max_items],
            "decision_action_outcome_learning": situation.get("decision_action_outcome_learning", [])[: self.limits.max_items],
            "scenario_inputs": situation.get("context", {}).get("scenario_inputs", {}),
        }
        encoded = json.dumps(_json(context), separators=(",", ":"), sort_keys=True)
        if len(encoded) > 256 * 1024:
            context["findings"] = context["findings"][:8]
            context["historical_analogues"] = context["historical_analogues"][:8]
        return context

    def _build_retrieval_plan(
        self,
        profile: Any,
        context: Mapping[str, Any],
        domain: str | None = None,
        runbook: Any | None = None,
    ) -> dict[str, Any]:
        """Build a specialist-specific evidence subset from domains and required types."""
        domains = [
            str(item).strip().lower()
            for item in (self._field(profile, "domains") or self._field(profile, "allowed_domains") or ())
            if str(item).strip()
        ]
        requested = str(domain or "").strip().lower()
        if requested:
            domains = [requested] if (requested in domains or not domains) else domains
        if runbook is None:
            runbook = context.get("runbook")
        declared_types: list[str] = []
        for source in (profile, runbook):
            if source is None:
                continue
            for key in ("required_evidence", "required_evidence_types", "evidence_requirements"):
                for item in (self._field(source, key) or ()):
                    token = str(item or "").strip().lower()
                    if token and token not in declared_types:
                        declared_types.append(token)
        matchable_types = self._matchable_evidence_types(declared_types)
        inferred_types: list[str] = []
        for item in domains:
            for kind in sorted(self._DOMAIN_EVIDENCE_KINDS.get(item, {item})):
                if kind not in inferred_types:
                    inferred_types.append(kind)
        evidence_types = matchable_types or inferred_types or declared_types
        selected_refs = self._collect_plan_refs(context, domains, matchable_types)
        if not selected_refs:
            selected_refs = self._collect_plan_refs(context, domains, ())
        if not selected_refs:
            selected_refs = self._collect_plan_refs(context, (), ())
        return {
            "profile_id": self._profile_key(profile),
            "domains": domains,
            "evidence_types": evidence_types,
            "evidence_refs": selected_refs[:MAX_ITEMS],
        }

    def _collect_plan_refs(
        self,
        context: Mapping[str, Any],
        domains: Sequence[str],
        matchable_types: Sequence[str],
    ) -> list[str]:
        selected_refs: list[str] = []
        seen: set[str] = set()
        for finding in context.get("findings", []) or []:
            if not isinstance(finding, Mapping):
                continue
            finding_domain = str(finding.get("domain") or finding.get("type") or "").strip().lower()
            for evidence in finding.get("evidence", []) or []:
                if not self._evidence_matches_plan(evidence, domains, matchable_types, finding_domain):
                    continue
                ref = _ref_id(evidence)
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                selected_refs.append(ref)
        return selected_refs[:MAX_ITEMS]

    @classmethod
    def _matchable_evidence_types(cls, types: Sequence[str]) -> list[str]:
        known: set[str] = set()
        for kinds in cls._DOMAIN_EVIDENCE_KINDS.values():
            known.update(kinds)
        result: list[str] = []
        for token in types:
            item = str(token or "").strip().lower()
            if not item:
                continue
            if " " in item and item not in known:
                continue
            if item not in result:
                result.append(item)
        return result

    @classmethod
    def _evidence_kind(cls, evidence: Any) -> str:
        if not isinstance(evidence, Mapping):
            return str(evidence or "").strip().lower()
        for key in ("evidence_type", "type", "kind"):
            value = evidence.get(key)
            if value not in (None, "") and not isinstance(value, Mapping):
                return str(value).strip().lower()
        ref = evidence.get("object_ref") or evidence.get("ref") or {}
        if isinstance(ref, Mapping):
            return str(ref.get("type") or "").strip().lower()
        return str(ref or "").strip().lower()

    @classmethod
    def _evidence_matches_plan(
        cls,
        evidence: Any,
        domains: Sequence[str],
        matchable_types: Sequence[str],
        finding_domain: str = "",
    ) -> bool:
        domain_hit = (not domains) or finding_domain in {str(item).lower() for item in domains} or any(
            cls._evidence_matches_domain(evidence, domain) for domain in domains
        )
        kind = cls._evidence_kind(evidence)
        type_hit = (not matchable_types) or kind in set(matchable_types) or any(
            token in kind for token in matchable_types if token
        )
        return domain_hit and type_hit

    def _specialist_evidence_weight(
        self,
        specialist: Mapping[str, Any],
        situation: Mapping[str, Any],
        allowed_refs: set[str],
    ) -> float:
        """Weight a specialist by resolved citation quality, domain match, and recency."""
        by_ref = self._situation_evidence_by_ref(situation)
        resolved: list[Mapping[str, Any]] = []
        for item in specialist.get("evidence_for", []) or []:
            ref = _ref_id(item)
            if not ref or ref not in allowed_refs:
                continue
            resolved.append(item if isinstance(item, Mapping) else {"ref": ref})
        quality_sum = 0.0
        recency_sum = 0.0
        domain_hits = 0.0
        profile_domains = self._specialist_domains(specialist)
        for item in resolved:
            ref = _ref_id(item)
            source = by_ref.get(ref or "", item)
            quality_sum += self._citation_quality(source)
            recency_sum += self._citation_recency(source)
            if profile_domains and any(self._evidence_matches_domain(source, domain) for domain in profile_domains):
                domain_hits += 1.0
        return 1.0 + quality_sum + recency_sum + domain_hits

    @staticmethod
    def _specialist_domains(specialist: Mapping[str, Any]) -> list[str]:
        profile = specialist.get("profile") if isinstance(specialist.get("profile"), Mapping) else {}
        coverage = specialist.get("domain_coverage") if isinstance(specialist.get("domain_coverage"), Mapping) else {}
        values = list(profile.get("domains") or profile.get("allowed_domains") or coverage.keys() or ())
        return [str(item).strip().lower() for item in values if str(item).strip()]

    @staticmethod
    def _situation_evidence_by_ref(situation: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        found: dict[str, Mapping[str, Any]] = {}
        for finding in situation.get("findings", []) or []:
            if not isinstance(finding, Mapping):
                continue
            for evidence in finding.get("evidence", []) or []:
                if not isinstance(evidence, Mapping):
                    continue
                ref = _ref_id(evidence)
                if ref and ref not in found:
                    found[ref] = evidence
        return found

    @staticmethod
    def _citation_quality(item: Mapping[str, Any]) -> float:
        for key in ("quality", "score", "weight"):
            if item.get(key) not in (None, ""):
                return _score(item.get(key), 1.0)
        confidence = item.get("confidence")
        if isinstance(confidence, Mapping):
            return _score(confidence.get("score"), 1.0)
        if confidence not in (None, ""):
            return _score(confidence, 1.0)
        return 1.0

    @staticmethod
    def _citation_recency(item: Mapping[str, Any]) -> float:
        parsed = None
        for key in ("timestamp", "as_of", "observed_at", "generated_at", "created_at"):
            parsed = IntelligenceOrchestratorRetrievalMixin._parse_timestamp(item.get(key))
            if parsed is not None:
                break
        if parsed is None:
            return 0.0
        age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
        return max(0.0, min(1.0, 1.0 - (age_days / 365.0)))

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _restrict_profile_context(self, profile: Any, context: Mapping[str, Any]) -> dict[str, Any]:
        """Give each specialist only evidence in its declared domains."""
        plan = context.get("retrieval_plan") if isinstance(context.get("retrieval_plan"), Mapping) else None
        if plan is None:
            plan = self._build_retrieval_plan(profile, context)
        domains = {
            str(item).strip().lower()
            for item in (plan.get("domains") or self._field(profile, "domains") or self._field(profile, "allowed_domains") or ())
            if str(item).strip()
        }
        result = dict(context)
        result["retrieval_plan"] = plan
        tools = self._field(profile, "allowed_tools") or self._field(profile, "allowed_tool_refs") or ()
        result["allowed_tools"] = [str(item) for item in tools][:MAX_ITEMS]
        result["tools"] = list(result["allowed_tools"])
        if not domains:
            return result
        planned_refs = {str(ref) for ref in (plan.get("evidence_refs") or []) if ref}
        findings = []
        for item in context.get("findings", []) or []:
            if not isinstance(item, Mapping):
                continue
            marker = " ".join(str(item.get(key) or "").lower() for key in ("id", "type", "title", "domain"))
            raw_evidence = item.get("evidence", []) or []
            if planned_refs:
                domain_evidence = [entry for entry in raw_evidence if _ref_id(entry) in planned_refs]
            else:
                domain_evidence = self._domain_matched_evidence(raw_evidence, domains)
            if any(domain in marker for domain in domains) or domain_evidence:
                scoped = dict(item)
                if domain_evidence:
                    matched = next((domain for domain in domains if self._evidence_matches_domain(domain_evidence[0], domain)), None)
                    scoped["evidence"] = domain_evidence[:4]
                    if matched:
                        scoped.setdefault("domain", matched)
                elif planned_refs:
                    continue
                findings.append(scoped)
        # A workspace can legitimately have no finding whose title carries a
        # specialist's domain label. Preserve one ACL-visible anchor in that
        # case so offline specialists still return cited, useful uncertainty
        # instead of an uncited generic fallback.
        if not findings:
            # A finding title is not guaranteed to carry its source domain.
            # Recover a specialist-specific anchor from the retrieval plan so
            # deterministic specialists do not all inherit findings[0].
            domain_evidence: list[tuple[str | None, Mapping[str, Any], Mapping[str, Any]]] = []
            for item in context.get("findings", []) or []:
                if not isinstance(item, Mapping):
                    continue
                for evidence in item.get("evidence", []) or []:
                    ref = _ref_id(evidence)
                    if planned_refs and ref not in planned_refs:
                        continue
                    matched = next((domain for domain in domains if self._evidence_matches_domain(evidence, domain)), None)
                    if matched or (planned_refs and ref in planned_refs):
                        domain_evidence.append((matched, evidence, item))
            if domain_evidence:
                chosen_domain = domain_evidence[0][0] or next(iter(domains), "")
                parent = domain_evidence[0][2]
                findings = [{
                    "summary": f"Visible {chosen_domain} evidence requiring a domain-specific review.",
                    "domain": chosen_domain,
                    "evidence": [evidence for _domain, evidence, _parent in domain_evidence][:4],
                    "confidence": parent.get("confidence"),
                    "opposing_evidence": parent.get("opposing_evidence", []),
                    "recommendation": parent.get("recommendation", {}),
                    "impact": parent.get("impact", {}),
                }]
            else:
                first = next((item for item in context.get("findings", []) or [] if isinstance(item, Mapping)), None)
                if first is not None and first.get("evidence"):
                    findings = [{
                        "summary": "Visible evidence requiring a domain-specific review.",
                        "evidence": list(first.get("evidence", []))[:4],
                        "confidence": first.get("confidence"),
                        "opposing_evidence": first.get("opposing_evidence", []),
                        "recommendation": first.get("recommendation", {}),
                        "impact": first.get("impact", {}),
                    }]
        result["findings"] = findings
        if isinstance(context.get("domains"), Mapping):
            result["domains"] = {
                key: value for key, value in context["domains"].items()
                if str(key).lower() in domains
            }
        coverage = {domain: {"finding_count": 0, "evidence_count": 0} for domain in sorted(domains)}
        for item in findings:
            marker = " ".join(str(item.get(key) or "").lower() for key in ("id", "type", "title", "domain", "summary"))
            matched = [domain for domain in domains if domain in marker] or list(domains)
            evidence_count = len(item.get("evidence", []) or [])
            for domain in matched:
                coverage[domain]["finding_count"] += 1
                coverage[domain]["evidence_count"] += evidence_count
        result["domain_coverage"] = coverage
        # These aggregates lack reliable domain labels; do not leak broad
        # company history into a domain-scoped specialist.
        result["historical_analogues"] = [
            item for item in context.get("historical_analogues", []) or []
            if isinstance(item, Mapping) and any(domain in json.dumps(item, sort_keys=True).lower() for domain in domains)
        ]
        result["decision_action_outcome_learning"] = [
            item for item in context.get("decision_action_outcome_learning", []) or []
            if isinstance(item, Mapping) and any(domain in json.dumps(item, sort_keys=True).lower() for domain in domains)
        ]
        scenario = context.get("scenario_inputs") or {}
        allowed_scenario = {
            "work": {"work_hours_delta", "deadline_days_delta"},
            "capacity": {"capacity_hours_delta", "leave_hours_delta", "hiring_hours_delta"},
            "finance": {"finance_amount_delta", "client_revenue_delta", "client_cost_delta"},
            "scope": {"scope_usage_delta"},
            "client_health": {"client_health_delta"},
        }
        keys = set().union(*(allowed_scenario.get(domain, set()) for domain in domains))
        result["scenario_inputs"] = {key: value for key, value in scenario.items() if key in keys}
        return result

    @staticmethod
    def _first_cited_finding_anchor(findings: Any) -> dict[str, Any] | None:
        for item in findings or []:
            if not isinstance(item, Mapping):
                continue
            evidence = next((entry for entry in item.get("evidence", []) or [] if _ref_id(entry)), None)
            if evidence is not None:
                return {
                    "summary": _text(item.get("summary") or item.get("title"), "Visible evidence"),
                    "evidence": [_json(evidence)],
                }
        return None

    @staticmethod
    def _filter_raw_evidence_refs(value: Any, allowed_refs: set[str]) -> Any:
        if not isinstance(value, Mapping):
            return value
        filtered = dict(value)
        dropped_citations = 0
        for field in ("evidence_for", "evidence_against", "analogues", "dissent"):
            if field not in filtered:
                continue
            checked: list[Any] = []
            for item in filtered.get(field, []) or []:
                ref = _ref_id(item)
                if ref is None or ref not in allowed_refs:
                    dropped_citations += 1
                    continue
                checked.append(item)
            filtered[field] = checked
        if dropped_citations:
            filtered["status"] = "degraded"
            filtered["needs_review"] = True
            unknowns = [_text(item) for item in _bounded_list(filtered.get("unknowns"))]
            filtered["unknowns"] = unknowns[: MAX_ITEMS - 1] + [
                f"{dropped_citations} evidence item(s) lacked an allowed citation and were dropped."
            ]
        return filtered

    @staticmethod
    def _evidence_matches_domain(evidence: Any, domain: str) -> bool:
        """Match canonical evidence kinds to a declared specialist domain."""
        if not isinstance(evidence, Mapping):
            return False
        kind = IntelligenceOrchestratorRetrievalMixin._evidence_kind(evidence)
        aliases = IntelligenceOrchestratorRetrievalMixin._DOMAIN_EVIDENCE_KINDS.get(domain, {domain})
        return kind in aliases or domain in kind

    def _select_deterministic_finding(self, findings: Any, profile_domains: Sequence[str], key: str) -> Mapping[str, Any]:
        candidates = [item for item in (findings or []) if isinstance(item, Mapping)]
        if not candidates:
            return {}
        domains = [str(item).strip().lower() for item in profile_domains if str(item).strip()]
        if domains:
            scored: list[tuple[int, int, Mapping[str, Any]]] = []
            for index, item in enumerate(candidates):
                marker = " ".join(str(item.get(field) or "").lower() for field in ("id", "type", "title", "domain", "summary"))
                evidence = self._domain_matched_evidence(item.get("evidence", []), domains)
                score = len(evidence) * 2 + sum(1 for domain in domains if domain in marker)
                if score:
                    scored.append((score, -index, item))
            if scored:
                return max(scored, key=lambda item: (item[0], item[1]))[2]
        return candidates[sum(ord(char) for char in key) % len(candidates)]

    @staticmethod
    def _domain_matched_evidence(evidence: Any, domains: Sequence[str]) -> list[Any]:
        domain_set = [str(item).strip().lower() for item in domains if str(item).strip()]
        if not domain_set:
            return []
        return [
            item for item in (evidence or [])
            if any(IntelligenceOrchestratorRetrievalMixin._evidence_matches_domain(item, domain) for domain in domain_set)
        ]
