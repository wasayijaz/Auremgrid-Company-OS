from __future__ import annotations

"""Deterministic, citation-first workspace intelligence.

This module intentionally does not call an LLM or execute actions.  It turns
the canonical ledger and permitted brain facts into a small, inspectable
finding graph that the dashboard and agents can safely consume.
"""

from datetime import datetime, timezone
import math
import re
from typing import Any

from auremgrid.domain.errors import AuthorizationError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _confidence(score: float) -> dict[str, Any]:
    score = round(max(0.0, min(1.0, float(score))), 3)
    label = "high" if score >= 0.8 else "medium" if score >= 0.55 else "low"
    return {"label": label, "score": score}


def _parse_time(value: Any) -> datetime | None:
    """Parse a canonical timestamp without letting malformed rows break reads."""
    if value in (None, ""):
        return None
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{3,}", str(value or "").lower())
            if token not in {"the", "and", "for", "with", "from", "that", "this", "into", "work"}}


class IntelligenceService:
    """Build workspace-scoped intelligence without mutating the ledger."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn
        self._evidence_issue: str | None = None

    def workspace(self, organization_id: str, workspace_id: str, person_id: str,
                  actor_id: str | None = None, as_of: datetime | None = None,
                  query: str | None = None) -> dict[str, Any]:
        """Return a stable intelligence contract for one authorized workspace.

        ``as_of`` is a read watermark.  Current canonical operational rows are
        filtered by their recorded/updated timestamps where available; brain
        facts use the existing temporal/ACL filtering implementation.
        """
        membership = self.os._require_person_access(organization_id, workspace_id, person_id)
        can_write = membership.role in {"admin", "operator"}
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
            "SELECT id,title,request,status,needed_by,deadline,blocking_reason,priority,updated_at "
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
        domain_evidence = self._domain_evidence(domains)
        relationships = self._cross_domain_relationships(domains, domain_evidence)
        decision_links = self._decision_action_outcome_learning(
            organization_id, workspace_id, cutoff, decisions,
        )
        analogues = self._historical_analogues(
            organization_id, workspace_id, cutoff, risks, work, signals,
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
                domain_evidence, moment,
            )
        if as_of is not None or not can_write:
            # Historical intelligence is a read-only explanation; never offer
            # mutable action descriptors for a past state.
            for finding in findings:
                finding["actions"] = []
                finding["action_descriptors"] = []

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
        context = {
            "pipeline": ["evidence", "situation", "changes", "hypotheses", "scenarios", "impact", "recommendation"],
            "evidence_count": len(evidence),
            "canonical_record_count": canonical_count,
            "open_risk_count": len(risks),
            "open_work_count": len(work),
            "change_count": len(changes) + len(signals),
            "historical": as_of is not None,
            "query": query,
            "domains": sorted(domains),
            "cross_domain_relationship_count": len(relationships),
            "historical_analogue_count": len(analogues),
            "decision_link_count": len(decision_links),
        }
        return {
            "scope": scope,
            "context": context,
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
            "findings": findings,
            "generated_at": _now().isoformat(),
        }

    def portfolio(
        self,
        organization_id: str,
        person_id: str,
        actor_id: str | None = None,
        as_of: datetime | None = None,
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
                })
        attention.sort(key=lambda item: float((item.get("confidence") or {}).get("score", 0.0)), reverse=True)
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
            },
            "generated_at": _now().isoformat(),
        }

    def executive_brief(
        self,
        organization_id: str,
        person_id: str,
        actor_id: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Stable first-class executive output backed by the portfolio projection."""
        result = self.portfolio(organization_id, person_id, actor_id=actor_id, as_of=as_of)
        return {
            **result,
            "type": "executive_brief",
            "headline": "Portfolio operating brief",
            "sections": {
                "attention": result["portfolio"]["attention"],
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
        }

    def _rows(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def _optional_rows(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        """Read an optional canonical surface without turning a partial store into a 500."""
        try:
            return self._rows(sql, args)
        except Exception:
            return []

    def _optional_row(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._optional_rows(sql, args)
        return rows[0] if rows else None

    def _domain_snapshot(
        self,
        organization_id: str,
        workspace_id: str,
        cutoff: str,
        moment: datetime,
        work: list[dict[str, Any]],
        risks: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Collect only visible, timestamp-valid canonical domain inputs."""
        campaigns = self._optional_rows(
            """SELECT c.id,c.name,c.objective,c.platform,c.budget,c.currency,c.status,c.updated_at,
                      m.id AS metric_id,m.captured_at,m.spend,m.revenue,m.leads,m.impressions,m.clicks,
                      m.cpl,m.cac,m.ctr,m.cvr,m.roas,m.source AS metric_source
               FROM campaigns c LEFT JOIN campaign_metric_snapshots m
                 ON m.id=(SELECT m2.id FROM campaign_metric_snapshots m2
                           WHERE m2.campaign_id=c.id AND m2.organization_id=? AND m2.workspace_id=?
                             AND m2.captured_at<=? ORDER BY m2.captured_at DESC,m2.id DESC LIMIT 1)
              WHERE c.organization_id=? AND c.workspace_id=? AND c.updated_at<=?
              ORDER BY c.updated_at DESC,c.id""",
            (organization_id, workspace_id, cutoff, organization_id, workspace_id, cutoff),
        )

        finance_connection = self._optional_row(
            "SELECT status,provider,last_sync_at,last_error FROM finance_connections WHERE organization_id=?",
            (organization_id,),
        )
        finance: dict[str, Any] = {
            "status": (finance_connection or {}).get("status", "not_connected"),
            "provider": (finance_connection or {}).get("provider"),
            "recognized_revenue": None,
            "outstanding_revenue": None,
            "costs": None,
            "as_of": cutoff,
        }
        if finance["status"] == "connected":
            revenue = self._optional_row(
                "SELECT COALESCE(SUM(amount),0) AS amount FROM revenues WHERE organization_id=? AND workspace_id=? AND recognized_at<=?",
                (organization_id, workspace_id, cutoff),
            )
            outstanding = self._optional_row(
                "SELECT COALESCE(SUM(amount),0) AS amount FROM invoices WHERE organization_id=? AND workspace_id=? AND issued_at<=? AND status IN ('issued','overdue')",
                (organization_id, workspace_id, cutoff),
            )
            costs = self._optional_row(
                "SELECT COALESCE(SUM(amount),0) AS amount FROM costs WHERE organization_id=? AND workspace_id=? AND incurred_at<=?",
                (organization_id, workspace_id, cutoff),
            )
            finance.update({
                "recognized_revenue": float(revenue["amount"]) if revenue else 0.0,
                "outstanding_revenue": float(outstanding["amount"]) if outstanding else 0.0,
                "costs": float(costs["amount"]) if costs else 0.0,
            })

        health = self._optional_row(
            """SELECT * FROM client_health_snapshots
               WHERE organization_id=? AND workspace_id=? AND calculated_at<=?
               ORDER BY calculated_at DESC,id DESC LIMIT 1""",
            (organization_id, workspace_id, cutoff),
        )
        scope_usage = self._optional_rows(
            """SELECT u.id,u.contract_id,u.allowance_id,u.period_start,u.delivered_quantity,
                      u.in_review_quantity,u.requested_quantity,u.used_hours,u.calculated_at,
                      a.service_category,a.period,a.included_quantity,a.included_hours,a.revision_limit
               FROM scope_usage u JOIN scope_allowances a ON a.id=u.allowance_id
              WHERE u.organization_id=? AND u.workspace_id=? AND u.calculated_at<=?
              ORDER BY u.calculated_at DESC,u.id""",
            (organization_id, workspace_id, cutoff),
        )

        # Capacity snapshots are person-scoped.  Join through visible work to
        # avoid leaking another workspace's capacity while preserving the
        # canonical snapshot as the source of truth.
        assigned_people = sorted({item.get("assignee_person_id") or item.get("owner_person_id") for item in work if item.get("assignee_person_id") or item.get("owner_person_id")})
        capacity: list[dict[str, Any]] = []
        if assigned_people:
            marks = ",".join("?" for _ in assigned_people)
            capacity = self._optional_rows(
                f"""SELECT cs.* FROM capacity_snapshots cs
                    WHERE cs.organization_id=? AND cs.person_id IN ({marks}) AND cs.calculated_at<=?
                    ORDER BY cs.calculated_at DESC,cs.person_id,cs.id""",
                (organization_id, *assigned_people, cutoff),
            )
            latest: dict[str, dict[str, Any]] = {}
            for row in capacity:
                latest.setdefault(str(row["person_id"]), row)
            capacity = list(latest.values())
        demand_hours = sum(float(item.get("estimate_hours") or 0.0) for item in work)
        actual_hours = sum(float(item.get("actual_effort_hours") or 0.0) for item in work)

        stalled_reviews = self._optional_rows(
            """SELECT id,deliverable_id,status,opened_at,reviewer_person_id,decision
                 FROM reviews WHERE organization_id=? AND workspace_id=?
                   AND opened_at<=? AND status IN ('open','pending','in_review')
                 ORDER BY opened_at ASC,id""",
            (organization_id, workspace_id, cutoff),
        )
        return {
            "work": {"items": work, "open_count": len(work), "estimated_hours": round(demand_hours, 3), "actual_effort_hours": round(actual_hours, 3)},
            "risks": {"items": risks, "open_count": len(risks)},
            "campaign_metrics": {"items": campaigns, "measured_count": sum(1 for row in campaigns if row.get("metric_id"))},
            "finance": finance,
            "scope": {"usage": scope_usage, "usage_count": len(scope_usage)},
            "client_health": health,
            "capacity": {"snapshots": capacity, "demand_hours": round(demand_hours, 3)},
            "reviews": {"stalled": stalled_reviews, "stalled_count": len(stalled_reviews)},
            "decisions": {"items": decisions, "effective_count": len(decisions)},
            "signals": {"items": signals, "open_count": sum(1 for row in signals if row.get("status") not in {"resolved", "closed"})},
        }

    def _domain_evidence(self, domains: dict[str, Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for risk in domains["risks"]["items"]:
            evidence.append(self._canonical("risks", str(risk["id"]), str(risk.get("evidence") or risk.get("impact") or "Open risk"), float(risk.get("probability") or 0.5)))
        for item in domains["work"]["items"]:
            if item.get("blocking_reason") or self._delivery_risk(item, _now()):
                evidence.append(self._canonical("work_items", str(item["id"]), str(item.get("blocking_reason") or item.get("title")), 0.75))
        for row in domains["campaign_metrics"]["items"]:
            if row.get("metric_id"):
                text = f"{row.get('name')} metrics captured {row.get('captured_at')}"
                if row.get("roas") is not None:
                    text += f"; ROAS {row['roas']}"
                evidence.append(self._canonical("campaign_metric_snapshots", str(row["metric_id"]), text, 0.75))
        health = domains["client_health"]
        if health:
            evidence.append(self._canonical("client_health_snapshots", str(health["id"]), str(health.get("explanation") or f"Client health {health.get('overall')}"), 0.8))
        for row in domains["scope"]["usage"]:
            included = row.get("included_quantity") or row.get("included_hours")
            used = row.get("delivered_quantity") or row.get("used_hours")
            if included is not None and used is not None:
                evidence.append(self._canonical("scope_usage", str(row["id"]), f"{row.get('service_category')} used {used} of {included}", 0.74))
        for row in domains["capacity"]["snapshots"]:
            evidence.append(self._canonical("capacity_snapshots", str(row["id"]), f"Capacity remaining {row.get('remaining_hours')} hours", 0.72))
        for row in domains["reviews"]["stalled"]:
            evidence.append(self._canonical("reviews", str(row["id"]), f"Review has remained {row.get('status')} since {row.get('opened_at')}", 0.78))
        finance = domains["finance"]
        if finance.get("status") == "connected":
            evidence.append(self._canonical("finance", "workspace", f"Recognized revenue {finance.get('recognized_revenue')}; outstanding {finance.get('outstanding_revenue')}", 0.7))
        return evidence

    def _cross_domain_relationships(self, domains: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deterministically connect domain facts only where fields overlap."""
        links: list[dict[str, Any]] = []
        work = domains["work"]["items"]
        campaigns = domains["campaign_metrics"]["items"]
        risks = domains["risks"]["items"]
        capacity = domains["capacity"]["snapshots"]
        scope = domains["scope"]["usage"]
        health = domains["client_health"]
        for item in work:
            owner = item.get("assignee_person_id") or item.get("owner_person_id")
            owner_cap = next((row for row in capacity if str(row.get("person_id")) == str(owner)), None)
            if owner_cap and float(owner_cap.get("remaining_hours") or 0) < 0:
                links.append(self._causal_link(
                    "capacity", "work", "supports",
                    f"Owner capacity is negative while work item '{item.get('title')}' remains open.",
                    [self._ref("capacity_snapshots", owner_cap["id"]), self._ref("work_items", item["id"])],
                    0.78,
                ))
        for item in work:
            if item.get("campaign_id"):
                metric = next((row for row in campaigns if row.get("id") == item.get("campaign_id")), None)
                if metric and metric.get("metric_id"):
                    links.append(self._causal_link(
                        "campaign_metrics", "work", "unknown",
                        "Campaign-linked work is visible, but metrics do not establish whether delivery caused performance movement.",
                        [self._ref("campaign_metric_snapshots", metric["metric_id"]), self._ref("work_items", item["id"])],
                        0.42,
                    ))
        if health and risks:
            for risk in risks:
                links.append(self._causal_link(
                    "risks", "client_health", "supports" if str(health.get("trend")) == "down" or float(health.get("overall") or 1) < 0.6 else "unknown",
                    "An open risk co-occurs with the latest client-health snapshot; causation is not asserted without a linked outcome.",
                    [self._ref("risks", risk["id"]), self._ref("client_health_snapshots", health["id"])],
                    0.55,
                ))
        for usage in scope:
            included = usage.get("included_quantity") or usage.get("included_hours")
            used = usage.get("delivered_quantity") or usage.get("used_hours")
            if included and used is not None and float(used) > float(included):
                links.append(self._causal_link(
                    "scope", "risks", "supports",
                    "Recorded scope usage exceeds its allowance and supports a scope-pressure risk.",
                    [self._ref("scope_usage", usage["id"])], 0.86,
                ))
        return links

    @staticmethod
    def _ref(kind: str, item_id: Any) -> dict[str, str]:
        return {"type": kind.rstrip("s"), "id": str(item_id)}

    @classmethod
    def _causal_link(cls, source_domain: str, target_domain: str, relation: str, explanation: str, evidence: list[dict[str, Any]], score: float) -> dict[str, Any]:
        return {
            "source_domain": source_domain,
            "target_domain": target_domain,
            "relation": relation,
            "explanation": explanation,
            "evidence": evidence,
            "confidence": _confidence(score),
        }

    def _historical_analogues(
        self,
        organization_id: str,
        workspace_id: str,
        cutoff: str,
        risks: list[dict[str, Any]],
        work: list[dict[str, Any]],
        signals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Find prior same-workspace patterns; no cross-workspace similarity."""
        analogues: list[dict[str, Any]] = []
        current_terms = _tokens(" ".join([str(row.get("type") or "") + " " + str(row.get("evidence") or "") for row in risks]))
        current_terms.update(_tokens(" ".join(str(row.get("title") or "") + " " + str(row.get("blocking_reason") or "") for row in work)))
        if not current_terms:
            return analogues
        current_risk_ids = {str(row.get("id")) for row in risks}
        current_work_ids = {str(row.get("id")) for row in work}
        prior_risks = self._optional_rows(
            """SELECT id,type,evidence,detected_at,status,resolution,resolved_at FROM risks
               WHERE organization_id=? AND workspace_id=? AND detected_at<=?
               ORDER BY detected_at DESC,id LIMIT 100""",
            (organization_id, workspace_id, cutoff),
        )
        for row in prior_risks:
            if str(row.get("id")) in current_risk_ids:
                continue
            overlap = len(current_terms & _tokens(f"{row.get('type')} {row.get('evidence')}"))
            if overlap:
                analogues.append({
                    "kind": "risk_pattern",
                    "source": self._ref("risks", row["id"]),
                    "summary": f"Prior {row.get('type')} pattern with {overlap} overlapping signal term(s).",
                    "resolution": row.get("resolution"),
                    "resolved": bool(row.get("resolved_at") or row.get("status") == "resolved"),
                    "evidence": [self._ref("risks", row["id"])],
                    "confidence": _confidence(min(0.9, 0.48 + overlap * 0.12)),
                })
        prior_events = self._optional_rows(
            """SELECT id,work_item_id,action,detail,recorded_at FROM work_events
               WHERE workspace_id=? AND recorded_at<=? ORDER BY recorded_at DESC,id LIMIT 100""",
            (workspace_id, cutoff),
        )
        for row in prior_events:
            if str(row.get("work_item_id")) in current_work_ids:
                continue
            overlap = len(current_terms & _tokens(f"{row.get('action')} {row.get('detail')}"))
            if overlap:
                analogues.append({
                    "kind": "delivery_pattern",
                    "source": self._ref("work_events", row["id"]),
                    "summary": f"Prior delivery event shares {overlap} signal term(s).",
                    "resolution": row.get("detail"),
                    "resolved": False,
                    "evidence": [self._ref("work_events", row["id"])],
                    "confidence": _confidence(min(0.82, 0.42 + overlap * 0.1)),
                })
        return analogues[:8]

    def _decision_action_outcome_learning(
        self,
        organization_id: str,
        workspace_id: str,
        cutoff: str,
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Link explicit decision records to nearby work events and outcomes."""
        links: list[dict[str, Any]] = []
        for decision in decisions:
            statement_terms = _tokens(decision.get("statement"))
            actions = self._optional_rows(
                """SELECT id,work_item_id,action,from_status,to_status,detail,recorded_at
                   FROM work_events WHERE workspace_id=? AND recorded_at>=? AND recorded_at<=?
                   ORDER BY recorded_at,id""",
                (workspace_id, str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff),
            )
            matched = [row for row in actions if statement_terms & _tokens(f"{row.get('action')} {row.get('detail')}")]
            outcomes = [row for row in matched if row.get("to_status") in {"shipped", "completed", "approved", "cancelled"}]
            resolved_signals = self._optional_rows(
                """SELECT id,type,evidence,resolved_at FROM signals
                   WHERE organization_id=? AND workspace_id=? AND resolved_at>=? AND resolved_at<=?
                   ORDER BY resolved_at,id""",
                (organization_id, workspace_id, str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff),
            )
            feedback = self._optional_rows(
                """SELECT id,category,raw_feedback,source_type,source_id,created_at FROM feedback_events
                   WHERE organization_id=? AND workspace_id=? AND created_at>=? AND created_at<=?
                   ORDER BY created_at,id""",
                (organization_id, workspace_id, str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff),
            )
            insights = self._optional_rows(
                """SELECT id,insight_type,metric_name,direction,evidence_summary,created_at FROM performance_insights
                   WHERE organization_id=? AND workspace_id=? AND created_at>=? AND created_at<=?
                   ORDER BY created_at,id""",
                (organization_id, workspace_id, str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff),
            )
            outcomes.extend(resolved_signals)
            learning_refs = [self._ref("feedback_events", row["id"]) for row in feedback]
            learning_refs.extend(self._ref("performance_insights", row["id"]) for row in insights)
            learning = "No linked outcome or learning record yet; treat this decision as an unvalidated hypothesis."
            confidence = 0.38
            if outcomes and learning_refs:
                learning = "A linked outcome and learning record exist; compare the result with the decision's intended rationale before generalizing."
                confidence = 0.76
            elif outcomes:
                learning = "A linked terminal work or resolved-signal outcome exists; compare the result with the decision's intended rationale before generalizing."
                confidence = 0.7
            elif learning_refs:
                learning = "A feedback or performance learning record exists, but no linked terminal outcome is visible yet."
                confidence = 0.52
            links.append({
                "decision": self._ref("decisions", decision["id"]),
                "actions": [self._ref("work_events", row["id"]) for row in matched],
                "outcomes": [self._ref("work_events", row["id"]) if row.get("work_item_id") else self._ref("signals", row["id"]) for row in outcomes],
                "learnings": learning_refs,
                "learning": learning,
                "confidence": _confidence(confidence),
                "evidence": [self._ref("decisions", decision["id"]), *[self._ref("work_events", row["id"]) for row in matched[:4]], *learning_refs[:4]],
            })
        return links

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
            "scenarios": self._scenarios(domains, relationships, evidence),
            "impact": {"level": "medium", "summary": "Potential delivery, client, scope, capacity, and financial impact; magnitude is not estimated where source metrics are absent."},
            "recommendation": {"summary": recommendation, "rationale": "The engine exposes relationships and uncertainty rather than fabricating a single causal conclusion."},
            "actions": self._action(organization_id, person_id, actor_id, workspace_id, "Review cross-domain signal", recommendation, "Review cross-domain signal", recommendation),
            "action_descriptors": self._action(organization_id, person_id, actor_id, workspace_id, "Review cross-domain signal", recommendation, "Review cross-domain signal", recommendation),
        }

    def _scenarios(self, domains: dict[str, Any], relationships: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        overloaded = any(float(row.get("remaining_hours") or 0) < 0 for row in domains["capacity"]["snapshots"])
        scope_pressure = any(float(row.get("used_hours") or row.get("delivered_quantity") or 0) > float(row.get("included_hours") or row.get("included_quantity") or math.inf) for row in domains["scope"]["usage"])
        risk_count = domains["risks"]["open_count"]
        scenarios = [
            {
                "name": "stabilize",
                "assumptions": ["Visible owners act on open blockers", "No unobserved external shock"],
                "domain_impacts": {"work": "fewer blocked items", "capacity": "demand is rebalanced", "client_health": "may stabilize"},
                "constraints": ["No capacity or outcome data is fabricated"],
                "mitigations": ["Assign an owner", "recheck after the next canonical event"],
                "downside": "Stabilization may defer lower-priority work.",
                "confidence": _confidence(0.62 if relationships else 0.35),
                "evidence": evidence[:6],
            },
            {
                "name": "defer",
                "assumptions": ["Open risks or blockers remain unresolved"],
                "domain_impacts": {"work": "delivery pressure increases", "scope": "usage may exceed allowance" if scope_pressure else "unknown", "finance": "impact unknown"},
                "constraints": ["Finance is not connected" if domains["finance"]["status"] != "connected" else "Finance values are limited to recorded rows"],
                "mitigations": ["Set an explicit review date", "Record an outcome when action is taken"],
                "downside": "Deferral can compound delivery or client risk.",
                "confidence": _confidence(0.68 if risk_count or overloaded or scope_pressure else 0.4),
                "evidence": evidence[:6],
            },
        ]
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
        hypotheses.extend({
            "text": f"Competing explanation: {item['summary']}",
            "confidence": item["confidence"],
            "stance": "opposing",
            "supporting_evidence": opposing,
            "opposing_evidence": supporting,
            "assumptions": ["The opposing record is current at the read watermark"],
        } for item in opposing)
        finding["hypotheses"] = hypotheses
        scenarios = finding.get("scenarios") or self._scenarios(domains, relationships, domain_evidence)
        normalized_scenarios: list[dict[str, Any]] = []
        for scenario in scenarios:
            normalized = dict(scenario)
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
        finding["uncertainty"] = self._calibration(supporting, opposing, status="ready")

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
                "route": "/work/capture",
                "method": "POST",
                "payload": work_payload, "required_fields": [] if actor_id else ["actor_id"],
                "safe": True,
                "one_way": False,
                "requires_approval": False,
                "status": "proposed",
            },
            {
                "id": "record-decision",
                "action": "Record a decision", "label": "Record a decision",
                "kind": "decision.create",
                "route": "/decisions",
                "method": "POST",
                "payload": decision_payload, "required_fields": [],
                "safe": True,
                "one_way": False,
                "requires_approval": False,
                "status": "proposed",
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
