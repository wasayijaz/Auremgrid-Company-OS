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


class IntelligenceHistoryMixin:
    def _historical_analogues(
        self,
        organization_id: str,
        workspace_id: str,
        cutoff: str,
        risks: list[dict[str, Any]],
        work: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        domains: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Find prior same-workspace patterns; no cross-workspace similarity."""
        analogues: list[dict[str, Any]] = []
        current_terms = _tokens(" ".join([str(row.get("type") or "") + " " + str(row.get("evidence") or "") for row in risks]))
        current_terms.update(_tokens(" ".join(str(row.get("title") or "") + " " + str(row.get("blocking_reason") or "") for row in work)))
        current_terms.update(_tokens(" ".join(str(row.get("type") or "") + " " + str(row.get("evidence") or "") for row in signals)))
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
            candidate_terms = _tokens(f"{row.get('type')} {row.get('evidence')}")
            overlap = len(current_terms & candidate_terms)
            if overlap:
                similarity = round(overlap / max(1, len(current_terms | candidate_terms)), 3)
                metadata = self._analogue_metadata(
                    current_terms, candidate_terms,
                    matching_dimensions=["signal_terms"],
                    different_dimensions=["resolution_state"] if row.get("status") != "open" else [],
                    intervention=row.get("resolution") or "No recorded intervention.",
                    subsequent_outcome=row.get("resolution") or row.get("status"),
                    similarity=similarity,
                )
                analogues.append({
                    "kind": "risk_pattern",
                    "source": self._ref("risks", row["id"]),
                    "summary": f"Prior {row.get('type')} pattern with {overlap} overlapping signal term(s).",
                    "resolution": row.get("resolution"),
                    "resolved": bool(row.get("resolved_at") or row.get("status") == "resolved"),
                    "outcome_stats": {
                        "matched_events": 1,
                        "resolved_count": 1 if row.get("resolved_at") or row.get("status") == "resolved" else 0,
                        "resolution_rate": 1.0 if row.get("resolved_at") or row.get("status") == "resolved" else 0.0,
                        "median_days_to_resolution": self._days_between(row.get("detected_at"), row.get("resolved_at")),
                    },
                    "evidence": [self._ref("risks", row["id"])],
                    "confidence": _confidence(min(0.9, 0.48 + overlap * 0.12)),
                    **metadata,
                })
        prior_signals = self._optional_rows(
            """SELECT id,type,evidence,status,created_at,resolved_at FROM signals
               WHERE organization_id=? AND workspace_id=? AND created_at<=?
               ORDER BY created_at DESC,id LIMIT 100""",
            (organization_id, workspace_id, cutoff),
        )
        current_signal_ids = {str(row.get("id")) for row in signals}
        for row in prior_signals:
            if str(row.get("id")) in current_signal_ids:
                continue
            candidate_terms = _tokens(f"{row.get('type')} {row.get('evidence')}")
            overlap = len(current_terms & candidate_terms)
            if not overlap:
                continue
            resolved = bool(row.get("resolved_at") or row.get("status") in {"resolved", "closed"})
            analogues.append({
                "kind": "signal_pattern", "source": self._ref("signals", row["id"]),
                "summary": f"Prior signal shares {overlap} signal term(s).", "resolution": row.get("status"),
                "resolved": resolved,
                "outcome_stats": {"matched_events": 1, "resolved_count": int(resolved), "resolution_rate": 1.0 if resolved else 0.0, "median_days_to_resolution": self._days_between(row.get("created_at"), row.get("resolved_at"))},
                "evidence": [self._ref("signals", row["id"])],
                "confidence": _confidence(min(0.82, 0.42 + overlap * 0.1)),
                **self._analogue_metadata(current_terms, candidate_terms, matching_dimensions=["signal_terms"], different_dimensions=[], intervention="No recorded intervention.", subsequent_outcome=row.get("status"), similarity=round(overlap / max(1, len(current_terms | candidate_terms)), 3)),
            })
        prior_events = self._optional_rows(
            """SELECT id,work_item_id,action,from_status,to_status,detail,recorded_at FROM work_events
               WHERE workspace_id=? AND recorded_at<=? ORDER BY recorded_at DESC,id LIMIT 100""",
            (workspace_id, cutoff),
        )
        for row in prior_events:
            if str(row.get("work_item_id")) in current_work_ids:
                continue
            candidate_terms = _tokens(f"{row.get('action')} {row.get('detail')}")
            overlap = len(current_terms & candidate_terms)
            if overlap:
                similarity = round(overlap / max(1, len(current_terms | candidate_terms)), 3)
                metadata = self._analogue_metadata(
                    current_terms, candidate_terms,
                    matching_dimensions=["signal_terms", "delivery_action"],
                    different_dimensions=["current_work_item"] if row.get("work_item_id") not in current_work_ids else [],
                    intervention=row.get("detail") or row.get("action"),
                    subsequent_outcome=row.get("to_status") or row.get("action"),
                    similarity=similarity,
                )
                analogues.append({
                    "kind": "delivery_pattern",
                    "source": self._ref("work_events", row["id"]),
                    "summary": f"Prior delivery event shares {overlap} signal term(s).",
                    "resolution": row.get("detail"),
                    "resolved": False,
                    "outcome_stats": {
                        "matched_events": 1,
                        "resolved_count": 1 if row.get("action") in {"complete", "ship", "approve"} else 0,
                        "resolution_rate": 1.0 if row.get("action") in {"complete", "ship", "approve"} else 0.0,
                        "median_days_to_resolution": None,
                    },
                    "evidence": [self._ref("work_events", row["id"])],
                    "confidence": _confidence(min(0.82, 0.42 + overlap * 0.1)),
                    **metadata,
                })
        # Add bounded, same-workspace snapshots for dimensions that are not
        # represented by a risk/event row.  Every comparison is sourced from
        # canonical historical rows; absent fields remain unknown.
        domains = domains or {}
        current = self._analogue_dimensions(domains, work, risks, signals)
        current_health = (domains.get("client_health") or {}).get("id") if isinstance(domains.get("client_health"), dict) else None
        snapshots = self._optional_rows(
            """SELECT id,overall,relationship,delivery,performance,finance,scope,sentiment,calculated_at
               FROM client_health_snapshots WHERE organization_id=? AND workspace_id=? AND calculated_at<=?
               ORDER BY calculated_at DESC,id LIMIT 24""",
            (organization_id, workspace_id, cutoff),
        )
        for row in snapshots:
            if str(row.get("id")) == str(current_health):
                continue
            candidate = {"relationship_health": row.get("relationship"), "work_pressure": row.get("delivery"), "scope": row.get("scope")}
            matching, different = self._dimension_comparison(current, candidate)
            if not matching:
                continue
            analogues.append({
                "kind": "health_snapshot",
                "source": self._ref("client_health_snapshots", row["id"]),
                "summary": "Prior client-health snapshot matches visible operating dimensions.",
                "resolved": False,
                "outcome_stats": {"matched_events": 1, "resolved_count": 0, "resolution_rate": None, "median_days_to_resolution": None},
                "evidence": [self._ref("client_health_snapshots", row["id"])],
                "confidence": _confidence(0.55 + 0.08 * len(matching)),
                **self._analogue_metadata(set(), set(), matching_dimensions=matching, different_dimensions=different, intervention="No recorded intervention.", subsequent_outcome=row.get("calculated_at"), similarity=min(0.95, 0.45 + 0.1 * len(matching))),
            })
        current_stage = {str(row.get("status")) for row in (domains.get("campaign_metrics", {}).get("items", []) if isinstance(domains.get("campaign_metrics"), dict) else []) if row.get("status")}
        if current_stage:
            campaign_rows = self._optional_rows(
                """SELECT m.id,c.status,m.captured_at FROM campaign_metric_snapshots m
                   JOIN campaigns c ON c.id=m.campaign_id
                  WHERE m.organization_id=? AND m.workspace_id=? AND m.captured_at<=?
                  ORDER BY m.captured_at DESC,m.id LIMIT 24""",
                (organization_id, workspace_id, cutoff),
            )
            for row in campaign_rows:
                if str(row.get("status")) not in current_stage:
                    continue
                analogue = self._analogue_metadata(set(), set(), matching_dimensions=["campaign_stage"], different_dimensions=[], intervention="No recorded intervention.", subsequent_outcome=row.get("captured_at"), similarity=0.6)
                analogues.append({"kind": "campaign_stage", "source": self._ref("campaign_metric_snapshots", row["id"]), "summary": "Prior campaign snapshot shares the current campaign stage.", "resolved": False, "outcome_stats": {"matched_events": 1, "resolved_count": 0, "resolution_rate": None, "median_days_to_resolution": None}, "evidence": [self._ref("campaign_metric_snapshots", row["id"])], "confidence": _confidence(0.6), **analogue})
        return analogues[:8]
    
    @staticmethod
    def _analogue_dimensions(domains: dict[str, Any], work: list[dict[str, Any]], risks: list[dict[str, Any]], signals: list[dict[str, Any]]) -> dict[str, Any]:
        work_items = domains.get("work", {}).get("items", work) if isinstance(domains.get("work"), dict) else work
        scope_rows = domains.get("scope", {}).get("usage", []) if isinstance(domains.get("scope"), dict) else []
        finance = domains.get("finance") if isinstance(domains.get("finance"), dict) else {}
        capacity = domains.get("capacity", {}).get("snapshots", []) if isinstance(domains.get("capacity"), dict) else []
        included = sum(float(row.get("included_quantity") or row.get("included_hours") or 0) for row in scope_rows)
        used = sum(float(row.get("delivered_quantity") or row.get("used_hours") or 0) for row in scope_rows)
        revenue, costs = finance.get("recognized_revenue"), finance.get("costs")
        return {
            "work_pressure": len(work_items),
            "scope": round(used / included, 3) if included else None,
            "relationship_health": (domains.get("client_health") or {}).get("relationship") if isinstance(domains.get("client_health"), dict) else None,
            "revenue_margin": round((float(revenue) - float(costs)) / float(revenue), 3) if revenue not in (None, 0) and costs is not None else None,
            "team_load": round(sum(float(row.get("remaining_hours") or 0) for row in capacity), 3) if capacity else None,
            "signal_patterns": _tokens(" ".join(str(row.get("type") or "") + " " + str(row.get("evidence") or "") for row in risks + signals)),
        }
    
    @staticmethod
    def _dimension_comparison(current: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[str], list[str]]:
        matching: list[str] = []
        different: list[str] = []
        for key, label in (("relationship_health", "relationship_health"), ("work_pressure", "work_pressure"), ("scope", "scope")):
            left, right = current.get(key), candidate.get(key)
            if left is None or right is None:
                continue
            if abs(float(left) - float(right)) <= 0.2 * max(1.0, abs(float(left)), abs(float(right))):
                matching.append(label)
            else:
                different.append(label)
        return matching, different
    
    @staticmethod
    def _analogue_metadata(
        current_terms: set[str],
        candidate_terms: set[str],
        *,
        matching_dimensions: list[str],
        different_dimensions: list[str],
        intervention: Any,
        subsequent_outcome: Any,
        similarity: float,
    ) -> dict[str, Any]:
        """Describe only dimensions supported by the compared canonical rows."""
        return {
            "matching_dimensions": matching_dimensions,
            "different_dimensions": different_dimensions,
            "intervention": intervention,
            "subsequent_outcome": subsequent_outcome,
            "similarity": _confidence(similarity),
            "evidence_status": "available" if candidate_terms else "unavailable",
        }
    
    def _portfolio_analogues(
        self,
        organization_id: str,
        visible_workspaces: list[dict[str, Any]],
        workspace_results: list[dict[str, Any]],
        cutoff: str,
    ) -> list[dict[str, Any]]:
        """Find resolved patterns across only the reader's visible workspaces.
    
        This is intentionally a bounded lexical baseline.  It supplies outcome
        distributions to portfolio intelligence without allowing a candidate
        from an unpermitted client workspace to influence the result.
        """
        visible_ids = [str(row.get("id")) for row in visible_workspaces if row.get("id")]
        if len(visible_ids) < 2:
            return []
        names = {str(row.get("id")): str(row.get("name") or row.get("id")) for row in visible_workspaces}
        current_terms: dict[str, set[str]] = {}
        for result in workspace_results:
            workspace_id = str(result.get("scope", {}).get("workspace_id") or "")
            terms = _tokens(" ".join(
                [str(item.get("title") or "") + " " + str(item.get("summary") or "") for item in result.get("findings", [])]
                + [str(item.get("type") or "") + " " + str(item.get("evidence") or "") for item in (result.get("domains", {}).get("risks", {}).get("items", []) or [])]
                + [str(item.get("title") or "") + " " + str(item.get("blocking_reason") or "") for item in (result.get("domains", {}).get("work", {}).get("items", []) or [])]
            ))
            if terms:
                current_terms[workspace_id] = terms
        if not current_terms:
            return []
        marks = ",".join("?" for _ in visible_ids)
        risks = self._optional_rows(
            f"""SELECT id,workspace_id,type,evidence,status,resolution,detected_at,resolved_at
                  FROM risks WHERE organization_id=? AND workspace_id IN ({marks}) AND detected_at<=?
                  ORDER BY detected_at DESC,id LIMIT 300""",
            (organization_id, *visible_ids, cutoff),
        )
        events = self._optional_rows(
            f"""SELECT id,workspace_id,work_item_id,action,detail,recorded_at
                  FROM work_events WHERE workspace_id IN ({marks}) AND recorded_at<=?
                  ORDER BY recorded_at DESC,id LIMIT 300""",
            (*visible_ids, cutoff),
        )
        candidates: list[dict[str, Any]] = []
        for kind, rows in (("risk_pattern", risks), ("delivery_pattern", events)):
            for row in rows:
                candidate_ws = str(row.get("workspace_id") or "")
                text = f"{row.get('type')} {row.get('evidence')} {row.get('action')} {row.get('detail')}"
                candidate_terms = _tokens(text)
                matched_readers = [ws for ws, terms in current_terms.items() if ws != candidate_ws and terms.intersection(candidate_terms)]
                if not matched_readers:
                    continue
                candidates.append({"kind": kind, "row": row, "matched_readers": matched_readers, "overlap": max(len(current_terms[ws].intersection(candidate_terms)) for ws in matched_readers)})
        if not candidates:
            return []
        # The distribution is calculated over all matched, visible outcomes so
        # each analogue reports how often this pattern resolved historically.
        resolved_rows = [item for item in candidates if (
            (item["kind"] == "risk_pattern" and (item["row"].get("resolved_at") or item["row"].get("status") == "resolved"))
            or (item["kind"] == "delivery_pattern" and item["row"].get("action") in {"complete", "ship", "approve"})
        )]
        resolution_rate = round(len(resolved_rows) / len(candidates), 3) if candidates else 0.0
        analogues: list[dict[str, Any]] = []
        for item in sorted(candidates, key=lambda entry: (-int(entry["overlap"]), str(entry["row"].get("id"))))[:12]:
            row = item["row"]
            resolved = item in resolved_rows
            candidate_terms = _tokens(f"{row.get('type')} {row.get('evidence')} {row.get('action')} {row.get('detail')}")
            reader_terms = current_terms.get(str(item["matched_readers"][0]), set())
            analogues.append({
                "kind": item["kind"],
                "source": {"type": "risk" if item["kind"] == "risk_pattern" else "work_event", "id": str(row.get("id")), "workspace_id": str(row.get("workspace_id")), "workspace_name": names.get(str(row.get("workspace_id")), str(row.get("workspace_id")))},
                "summary": f"Visible prior {item['kind'].replace('_', ' ')} overlaps the current portfolio signal across workspace boundaries.",
                "resolved": resolved,
                "outcome_stats": {
                    "matched_events": len(candidates),
                    "resolved_count": len(resolved_rows),
                    "resolution_rate": resolution_rate,
                    "visible_workspace_count": len({str(entry["row"].get("workspace_id")) for entry in candidates}),
                },
                "evidence": [self._ref("risks" if item["kind"] == "risk_pattern" else "work_events", str(row.get("id")))],
                "confidence": _confidence(min(0.9, 0.48 + int(item["overlap"]) * 0.08)),
                "matching_dimensions": ["signal_terms"],
                "different_dimensions": ["workspace_scope"],
                "intervention": row.get("resolution") or row.get("detail") or row.get("action") or "No recorded intervention.",
                "subsequent_outcome": row.get("resolution") or row.get("status") or row.get("action"),
                "similarity": _confidence(round(int(item["overlap"]) / max(1, len(reader_terms | candidate_terms)), 3)),
            })
        return analogues
    
    @staticmethod
    def _days_between(start: Any, end: Any) -> float | None:
        start_at = _parse_time(start)
        end_at = _parse_time(end)
        if start_at is None or end_at is None:
            return None
        return round(max(0.0, (end_at - start_at).total_seconds() / 86400), 3)
    
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
                """SELECT id,type,evidence,status,created_at,resolved_at FROM signals
                   WHERE organization_id=? AND workspace_id=?
                     AND ((resolved_at>=? AND resolved_at<=?)
                          OR (status IN ('resolved','closed') AND created_at>=? AND created_at<=?))
                   ORDER BY COALESCE(resolved_at,created_at),id""",
                (organization_id, workspace_id,
                 str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff,
                 str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff),
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
            workflow_chain = self._workflow_chain(workspace_id, str(decision.get("effective_from") or "0001-01-01T00:00:00+00:00"), cutoff, statement_terms)
            links.append({
                "decision": self._ref("decisions", decision["id"]),
                "workflow": workflow_chain,
                "actions": [self._ref("work_events", row["id"]) for row in matched],
                "outcomes": [self._ref("work_events", row["id"]) if row.get("work_item_id") else self._ref("signals", row["id"]) for row in outcomes],
                "learnings": learning_refs,
                "learning": learning,
                "evaluation": {
                    "status": "validated" if outcomes else "pending_outcome",
                    "outcome_count": len(outcomes),
                    "learning_count": len(learning_refs),
                    "matched_action_count": len(matched),
                    "calibration_delta": round((0.08 if outcomes else -0.05) + (0.04 if learning_refs else 0), 3),
                },
                "confidence": _confidence(confidence),
                "evidence": [self._ref("decisions", decision["id"]), *[self._ref("work_events", row["id"]) for row in matched[:4]], *learning_refs[:4]],
            })
        return links

