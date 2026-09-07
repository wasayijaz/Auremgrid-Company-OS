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


class IntelligenceContextMixin:
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
    
    def _context_contract(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        cutoff: str,
        domains: dict[str, Any],
    ) -> dict[str, Any]:
        """Expose exactly which operating context this read is allowed to use."""
        workspace = self.os.store.get_workspace(workspace_id)
        projects = self._optional_rows(
            """SELECT id,name,owner_person_id,status FROM projects
               WHERE organization_id=? AND workspace_id=?
               ORDER BY name,id LIMIT 50""",
            (organization_id, workspace_id),
        )
        work_items = domains["work"]["items"]
        project_ids = sorted({str(item.get("project_id")) for item in work_items if item.get("project_id")})
        campaign_ids = sorted({str(row.get("id")) for row in domains["campaign_metrics"]["items"] if row.get("id")})
        campaign_ids.extend(str(item.get("campaign_id")) for item in work_items if item.get("campaign_id") and str(item.get("campaign_id")) not in campaign_ids)
        person_ids = sorted({
            str(value)
            for item in work_items
            for value in (item.get("owner_person_id"), item.get("assignee_person_id"), item.get("reviewer_person_id"))
            if value
        })
        people: list[dict[str, Any]] = []
        if person_ids:
            marks = ",".join("?" for _ in person_ids)
            people = self._optional_rows(
                f"""SELECT id,name,title,department,status FROM people
                    WHERE organization_id=? AND id IN ({marks})
                    ORDER BY name,id""",
                (organization_id, *person_ids),
            )
        client_roster = self._optional_rows(
            """SELECT rr.role_key,rr.wing,rr.person_id,r.version,r.effective_at
                 FROM client_account_rosters r
                 JOIN client_account_roster_roles rr ON rr.roster_id=r.id
                WHERE r.organization_id=? AND r.workspace_id=? AND r.effective_at<=?
                ORDER BY r.effective_at DESC,r.version DESC,rr.role_key,rr.wing LIMIT 24""",
            (organization_id, workspace_id, cutoff),
        )
        return {
            "organization_id": organization_id,
            "workspace": {
                "id": workspace_id,
                "name": workspace.name if workspace else None,
                "kind": self._workspace_kind(organization_id, workspace_id),
            },
            "reader": {"person_id": person_id},
            "client": {
                "workspace_id": workspace_id if self._workspace_kind(organization_id, workspace_id) == "client" else None,
                "roster": client_roster,
                "health_snapshot_id": (domains.get("client_health") or {}).get("id") if domains.get("client_health") else None,
            },
            "projects": [{"id": row.get("id"), "name": row.get("name"), "owner_person_id": row.get("owner_person_id"), "status": row.get("status")} for row in projects if not project_ids or str(row.get("id")) in project_ids],
            "campaigns": [
                {"id": row.get("id"), "name": row.get("name"), "platform": row.get("platform"), "status": row.get("status"), "metric_id": row.get("metric_id")}
                for row in domains["campaign_metrics"]["items"]
                if str(row.get("id")) in campaign_ids
            ],
            "people": people,
            "visibility": {
                "source": "membership_and_actor_acl",
                "bounded_to_workspace": True,
                "cross_workspace_rows": False,
                "external_provider_values": "explicit_null_unless_connected",
            },
        }
    
    def _selected_context(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        context_type: str | None,
        context_id: str | None,
    ) -> dict[str, Any]:
        kind = str(context_type or "workspace").strip().lower()
        identifier = str(context_id or workspace_id).strip()
        if kind not in {"workspace", "client", "project", "campaign", "person", "work"}:
            raise ValidationError("context_type must be workspace, client, project, campaign, person, or work")
        if kind in {"workspace", "client"}:
            if identifier != workspace_id:
                raise AuthorizationError("selected context is not visible")
            row = self._optional_row(
                """SELECT w.id,w.name,wo.kind FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
                   JOIN workspace_memberships wm ON wm.workspace_id=w.id
                   WHERE wo.organization_id=? AND w.id=? AND wm.person_id=?""",
                (organization_id, identifier, person_id),
            )
            if row is None or (kind == "client" and row.get("kind") != "client"):
                raise AuthorizationError("selected context is not visible")
            return {"type": kind, "id": row["id"], "label": row["name"], "workspace_id": row["id"]}
        definitions = {
            "project": ("projects", "id", "name", "organization_id=? AND workspace_id=? AND id=?"),
            "campaign": ("campaigns", "id", "name", "organization_id=? AND workspace_id=? AND id=?"),
            "work": ("work_items", "id", "title", "workspace_id=? AND id=?"),
        }
        if kind == "person":
            row = self._optional_row(
                """SELECT p.id,p.name FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                   WHERE p.organization_id=? AND wm.workspace_id=? AND p.id=?""",
                (organization_id, workspace_id, identifier),
            )
        else:
            table, id_column, label_column, clause = definitions[kind]
            args: tuple[Any, ...] = (
                (organization_id, workspace_id, identifier)
                if kind in {"project", "campaign"} else (workspace_id, identifier)
            )
            row = self._optional_row(
                f"SELECT {id_column} AS id,{label_column} AS name FROM {table} WHERE {clause}", args,
            )
        if row is None:
            raise AuthorizationError("selected context is not visible")
        return {"type": kind, "id": row["id"], "label": row["name"], "workspace_id": workspace_id}
    
    def _workspace_kind(self, organization_id: str, workspace_id: str) -> str | None:
        row = self._optional_row(
            "SELECT kind FROM workspace_organization WHERE organization_id=? AND workspace_id=?",
            (organization_id, workspace_id),
        )
        return str(row["kind"]) if row and row.get("kind") is not None else None
    
    @staticmethod
    def _scenario_inputs(domains: dict[str, Any], what_if: dict[str, Any] | None) -> dict[str, Any]:
        raw = what_if if isinstance(what_if, dict) else {}
        def number(key: str, default: float) -> float:
            try:
                result = float(raw.get(key, default))
                return round(result, 3) if math.isfinite(result) else round(default, 3)
            except (TypeError, ValueError):
                return round(default, 3)
        def first_number(keys: tuple[str, ...], default: float = 0.0) -> float:
            for key in keys:
                if key in raw:
                    return number(key, default)
            return round(default, 3)
        def action_value() -> str:
            value = str(raw.get("client_action", raw.get("client_decision", raw.get("keep_drop", ""))) or "").strip().lower()
            return value if value in {"keep", "drop"} else "unspecified"
        base_remaining = sum(float(row.get("remaining_hours") or 0.0) for row in domains["capacity"]["snapshots"])
        base_demand = float(domains["capacity"].get("demand_hours") or domains["work"].get("estimated_hours") or 0.0)
        included_scope = sum(float(row.get("included_hours") or row.get("included_quantity") or 0.0) for row in domains["scope"]["usage"])
        used_scope = sum(float(row.get("used_hours") or row.get("delivered_quantity") or 0.0) for row in domains["scope"]["usage"])
        health = domains.get("client_health") or {}
        finance = domains.get("finance") or {}
        retained = {
            "capacity_hours_delta": number("capacity_hours_delta", 0.0),
            "work_hours_delta": number("work_hours_delta", 0.0),
            "scope_usage_delta": number("scope_usage_delta", 0.0),
            "finance_amount_delta": number("finance_amount_delta", 0.0),
            "client_health_delta": number("client_health_delta", 0.0),
            "deadline_days_delta": number("deadline_days_delta", 0.0),
            # Growth and staffing inputs are intentionally explicit.  A zero
            # default means the engine never invents a utilization rate.
            "additional_clients": first_number(("additional_clients", "new_clients_delta", "new_clients")),
            "hours_per_new_client": first_number(("hours_per_new_client", "new_client_hours", "hours_per_client")),
            "leave_hours_delta": first_number(("leave_hours_delta", "leave_hours")),
            "hiring_hours_delta": first_number(("hiring_hours_delta", "hiring_capacity_hours")),
            "client_action": action_value(),
            "client_revenue_delta": first_number(("client_revenue_delta", "retainer_revenue_delta")),
            "client_cost_delta": first_number(("client_cost_delta", "delivery_cost_delta")),
            "client_hours_delta": first_number(("client_hours_delta", "retainer_hours_delta")),
        }
        added_client_hours = retained["additional_clients"] * retained["hours_per_new_client"]
        client_action_sign = 1.0 if retained["client_action"] == "keep" else -1.0 if retained["client_action"] == "drop" else 0.0
        client_margin_delta = client_action_sign * (retained["client_revenue_delta"] - retained["client_cost_delta"])
        client_capacity_delta = client_action_sign * retained["client_hours_delta"]
        projected_scope_used = used_scope + retained["scope_usage_delta"]
        projected_scope_ratio = None
        if included_scope > 0:
            projected_scope_ratio = round(projected_scope_used / included_scope, 3)
        projected_health = None
        if health.get("overall") is not None:
            projected_health = round(max(0.0, min(1.0, float(health["overall"]) + retained["client_health_delta"])), 3)
        projected_finance = None
        if finance.get("status") == "connected" and finance.get("recognized_revenue") is not None:
            projected_finance = round(float(finance.get("recognized_revenue") or 0.0) + retained["finance_amount_delta"], 3)
        campaign_projection: dict[str, Any] | None = None
        campaign_rows = (domains.get("campaign_metrics") or {}).get("items", []) if isinstance(domains.get("campaign_metrics"), dict) else []
        measured = next((row for row in campaign_rows if row.get("metric_id")), None)
        if measured:
            for metric in ("roas", "ctr", "cvr", "cpl", "cac"):
                value = measured.get(metric)
                if value is not None:
                    try:
                        numeric = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(numeric):
                        campaign_projection = {
                            "status": "observed",
                            "metric": metric,
                            "value": round(numeric, 3),
                            "source_ref": {"type": "campaign_metric_snapshots", "id": str(measured["metric_id"])},
                        }
                        break
        return {
            "retained_inputs": retained,
            "baseline": {
                "capacity_remaining_hours": round(base_remaining, 3),
                "work_demand_hours": round(base_demand, 3),
                "scope_used": round(used_scope, 3),
                "scope_included": round(included_scope, 3),
                "client_health": health.get("overall"),
                "recognized_revenue": finance.get("recognized_revenue"),
                "campaign": campaign_projection,
                "finance_status": finance.get("status"),
            },
            "projection": {
                "capacity_remaining_hours": round(base_remaining + retained["capacity_hours_delta"] + retained["hiring_hours_delta"] - retained["leave_hours_delta"] - retained["work_hours_delta"] - added_client_hours - client_capacity_delta, 3),
                "work_demand_hours": round(base_demand + retained["work_hours_delta"] + added_client_hours + client_capacity_delta, 3),
                "scope_used": round(projected_scope_used, 3),
                "scope_ratio": projected_scope_ratio,
                "client_health": projected_health,
                # Revenue and margin are separate measures.  Costs affect
                # margin only; never add them to recognized revenue.
                "recognized_revenue": round(projected_finance + client_action_sign * retained["client_revenue_delta"], 3) if projected_finance is not None else None,
                "campaign": campaign_projection,
                "client_margin_delta": round(client_margin_delta, 3),
                "added_client_hours": round(added_client_hours, 3),
                "deadline_days_delta": retained["deadline_days_delta"],
            },
            "constraints": [
                "Inputs are read-time scenario parameters and are not written to the ledger.",
                "Finance projection remains null unless finance is connected.",
                "Unprovided inputs default to zero change.",
                "New-client hours, leave, hiring, and keep/drop economics require explicit inputs; no agency averages are assumed.",
            ],
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
        # These are bounded hypotheses, not causal claims.  They make the
        # operating picture richer when two independently sourced domains
        # move together while preserving an explicit unknown relation.
        if campaigns and health:
            measured = [row for row in campaigns if row.get("metric_id")]
            if measured:
                latest = measured[0]
                metric_summary = ", ".join(
                    f"{name} {latest.get(name)}"
                    for name in ("ctr", "cvr", "roas")
                    if latest.get(name) is not None
                ) or "campaign metrics are present"
                links.append(self._causal_link(
                    "campaign_metrics", "client_health", "unknown",
                    f"Campaign performance ({metric_summary}) and client health are co-visible; performance may inform the relationship, but causation is unproven.",
                    [self._ref("campaign_metric_snapshots", latest["metric_id"]), self._ref("client_health_snapshots", health["id"])],
                    0.48,
                ))
        if domains["reviews"]["stalled"] and work:
            review = domains["reviews"]["stalled"][0]
            links.append(self._causal_link(
                "reviews", "work", "supports",
                "A stalled review can delay open work; this is a delivery-risk hypothesis until a linked transition confirms the effect.",
                [self._ref("reviews", review["id"]), self._ref("work_items", work[0]["id"])],
                0.63,
            ))
        if health and domains["finance"].get("status") == "connected":
            finance = domains["finance"]
            links.append(self._causal_link(
                "client_health", "finance", "unknown",
                "Client health and recorded revenue/cost are related operating signals; the ledger does not establish that one caused the other.",
                [self._ref("client_health_snapshots", health["id"]), self._ref("finance", "workspace")],
                0.44,
            ))
        if scope and work:
            allowance = next((row for row in scope if (row.get("included_hours") or row.get("included_quantity")) is not None), None)
            if allowance is not None:
                links.append(self._causal_link(
                    "scope", "work", "supports",
                    "Recorded scope consumption changes the delivery context for open work; additional effort should be checked against the allowance.",
                    [self._ref("scope_usage", allowance["id"]), self._ref("work_items", work[0]["id"])],
                    0.59,
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

