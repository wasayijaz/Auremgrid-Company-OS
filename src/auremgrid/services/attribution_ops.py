from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError


CAMPAIGN_METRICS = {"spend", "revenue", "leads", "impressions", "clicks", "cpl", "cac", "ctr", "cvr", "roas"}
SUBJECT_KINDS = {"campaign", "decision"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required")
    return text


def _parse_time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("metric_names_json", "baseline_summary_json", "outcome_summary_json", "values_json", "deltas_json"):
        if key in result:
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or "null")
    return result


class AttributionService:
    def __init__(
        self,
        conn: Any,
        new_id: Callable[[str], str],
        authorize: Callable[..., Any],
        learning: Any,
    ) -> None:
        self.conn = conn
        self.new_id = new_id
        self.authorize = authorize
        self.learning = learning

    def create_attribution_plan(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        subject_kind: str,
        subject_id: str,
        metric_names: list[str],
        evaluation_window_days: int,
    ) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        subject_kind = _text(subject_kind, "subject_kind")
        subject_id = _text(subject_id, "subject_id")
        if subject_kind not in SUBJECT_KINDS:
            raise ValidationError("unsupported attribution subject kind")
        metrics = self._metric_names(metric_names)
        if evaluation_window_days <= 0:
            raise ValidationError("evaluation_window_days must be positive")
        self._require_subject(organization_id, workspace_id, subject_kind, subject_id)

        now = _now()
        window_start = now
        window_end = now + timedelta(days=evaluation_window_days)
        values = self._capture_values(
            organization_id,
            workspace_id,
            subject_kind,
            subject_id,
            metrics,
            captured_before=now,
        )
        summary = self._summary(values)
        attribution_id = self.new_id("attrib")
        snapshot_id = self.new_id("attrib_snap")
        self.conn.execute(
            """INSERT INTO outcome_attribution_plans(
                id,organization_id,workspace_id,subject_kind,subject_id,metric_names_json,
                evaluation_window_days,evaluation_window_start,evaluation_window_end,status,
                baseline_summary_json,outcome_summary_json,created_by_person_id,evaluated_by_person_id,
                created_at,evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attribution_id,
                organization_id,
                workspace_id,
                subject_kind,
                subject_id,
                _json(metrics),
                evaluation_window_days,
                window_start.isoformat(),
                window_end.isoformat(),
                "planned",
                _json(summary),
                None,
                person_id,
                None,
                now.isoformat(),
                None,
            ),
        )
        self.conn.execute(
            """INSERT INTO outcome_attribution_snapshots(
                id,organization_id,workspace_id,attribution_id,snapshot_kind,
                captured_at,window_start,window_end,values_json,deltas_json,created_by_person_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                organization_id,
                workspace_id,
                attribution_id,
                "baseline",
                now.isoformat(),
                None,
                now.isoformat(),
                _json(values),
                None,
                person_id,
            ),
        )
        self.conn.commit()
        return self.get_attribution(organization_id, workspace_id, person_id, attribution_id)

    def evaluate(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        attribution_id: str,
    ) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        plan = self._plan(organization_id, workspace_id, attribution_id)
        metrics = json.loads(plan["metric_names_json"])
        baseline = self._latest_snapshot(organization_id, workspace_id, attribution_id, "baseline")
        if baseline is None:
            raise NotFoundError("baseline snapshot not found")
        window_start = _parse_time(plan["evaluation_window_start"], "evaluation_window_start")
        window_end = _parse_time(plan["evaluation_window_end"], "evaluation_window_end")
        values = self._capture_values(
            organization_id,
            workspace_id,
            plan["subject_kind"],
            plan["subject_id"],
            metrics,
            window_start=window_start,
            window_end=window_end,
        )
        baseline_values = json.loads(baseline["values_json"])
        deltas = self._deltas(baseline_values, values)
        outcome_summary = self._summary(values, deltas)
        now = _now().isoformat()
        snapshot_id = self.new_id("attrib_snap")
        self.conn.execute(
            """INSERT INTO outcome_attribution_snapshots(
                id,organization_id,workspace_id,attribution_id,snapshot_kind,
                captured_at,window_start,window_end,values_json,deltas_json,created_by_person_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                organization_id,
                workspace_id,
                attribution_id,
                "outcome",
                now,
                window_start.isoformat(),
                window_end.isoformat(),
                _json(values),
                _json(deltas),
                person_id,
            ),
        )
        self.conn.execute(
            """UPDATE outcome_attribution_plans
               SET status='evaluated', outcome_summary_json=?, evaluated_by_person_id=?, evaluated_at=?
               WHERE id=?""",
            (_json(outcome_summary), person_id, now, attribution_id),
        )
        self.conn.commit()
        learning_record = self._record_learning(
            organization_id,
            workspace_id,
            person_id,
            _row_dict({**dict(plan), "outcome_summary_json": _json(outcome_summary)}),
            values,
            deltas,
            window_start.isoformat(),
            window_end.isoformat(),
        )
        return {
            **self.get_attribution(organization_id, workspace_id, person_id, attribution_id),
            "learning_record": learning_record,
        }

    def get_attribution(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        attribution_id: str,
    ) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        plan = _row_dict(self._plan(organization_id, workspace_id, attribution_id))
        snapshots = [
            _row_dict(row)
            for row in self.conn.execute(
                """SELECT * FROM outcome_attribution_snapshots
                   WHERE organization_id=? AND workspace_id=? AND attribution_id=?
                   ORDER BY captured_at,id""",
                (organization_id, workspace_id, attribution_id),
            ).fetchall()
        ]
        return {**plan, "snapshots": snapshots}

    def list_attributions(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        *,
        subject_kind: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM outcome_attribution_plans WHERE organization_id=? AND workspace_id=?"
        params: list[Any] = [organization_id, workspace_id]
        if subject_kind is not None:
            sql += " AND subject_kind=?"
            params.append(subject_kind)
        if subject_id is not None:
            sql += " AND subject_id=?"
            params.append(subject_id)
        sql += " ORDER BY created_at DESC,id"
        return [_row_dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]

    def _metric_names(self, metric_names: list[str]) -> list[str]:
        if not isinstance(metric_names, list) or not metric_names:
            raise ValidationError("metric_names must be a non-empty list")
        normalized = []
        seen = set()
        for metric in metric_names:
            name = _text(metric, "metric_name")
            if name not in CAMPAIGN_METRICS:
                raise ValidationError("unsupported attribution metric")
            if name not in seen:
                seen.add(name)
                normalized.append(name)
        return normalized

    def _require_subject(self, organization_id: str, workspace_id: str, subject_kind: str, subject_id: str) -> None:
        tables = {
            "campaign": "campaigns",
            "decision": "decisions",
        }
        if not self.conn.execute(
            f"SELECT 1 FROM {tables[subject_kind]} WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, subject_id),
        ).fetchone():
            raise NotFoundError("attribution subject not found")

    def _capture_values(
        self,
        organization_id: str,
        workspace_id: str,
        subject_kind: str,
        subject_id: str,
        metric_names: list[str],
        *,
        captured_before: datetime | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        campaign_id = subject_id
        if subject_kind == "decision":
            decision = self.conn.execute(
                """SELECT campaign_id FROM decisions
                   WHERE organization_id=? AND workspace_id=? AND id=?""",
                (organization_id, workspace_id, subject_id),
            ).fetchone()
            campaign_id = decision["campaign_id"] if decision else None
            if not campaign_id:
                return [self._unknown(metric, "no canonical local metric rows for subject kind") for metric in metric_names]
        if captured_before is not None:
            row = self.conn.execute(
                """SELECT * FROM campaign_metric_snapshots
                   WHERE organization_id=? AND workspace_id=? AND campaign_id=? AND captured_at<=?
                   ORDER BY captured_at DESC,id DESC LIMIT 1""",
                (organization_id, workspace_id, campaign_id, captured_before.isoformat()),
            ).fetchone()
        else:
            row = self.conn.execute(
                """SELECT * FROM campaign_metric_snapshots
                   WHERE organization_id=? AND workspace_id=? AND campaign_id=?
                     AND captured_at>=? AND captured_at<=?
                   ORDER BY captured_at DESC,id DESC LIMIT 1""",
                (organization_id, workspace_id, campaign_id, window_start.isoformat(), window_end.isoformat()),
            ).fetchone()
        values = []
        for metric in metric_names:
            if row is None:
                values.append(self._unknown(metric, "no canonical local snapshot found"))
            elif row[metric] is None:
                values.append(self._unknown(metric, "canonical local snapshot has no sourced value", row))
            else:
                values.append(
                    {
                        "metric_name": metric,
                        "status": "known",
                        "value": row[metric],
                        "source": row["source"],
                        "source_table": "campaign_metric_snapshots",
                        "source_row_id": row["id"],
                        "captured_at": row["captured_at"],
                    }
                )
        return values

    @staticmethod
    def _unknown(metric_name: str, reason: str, row: Any | None = None) -> dict[str, Any]:
        value = {
            "metric_name": metric_name,
            "status": "unknown",
            "value": None,
            "reason": reason,
        }
        if row is not None:
            value.update(
                {
                    "source": row["source"],
                    "source_table": "campaign_metric_snapshots",
                    "source_row_id": row["id"],
                    "captured_at": row["captured_at"],
                }
            )
        return value

    @staticmethod
    def _summary(values: list[dict[str, Any]], deltas: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        known = sum(1 for item in values if item["status"] == "known")
        summary = {
            "known_count": known,
            "unknown_count": len(values) - known,
            "metrics": len(values),
        }
        if deltas is not None:
            computed = sum(1 for item in deltas if item["status"] == "computed")
            summary["computed_delta_count"] = computed
            summary["unknown_delta_count"] = len(deltas) - computed
        return summary

    @staticmethod
    def _deltas(baseline: list[dict[str, Any]], outcome: list[dict[str, Any]]) -> list[dict[str, Any]]:
        baseline_by_metric = {item["metric_name"]: item for item in baseline}
        result = []
        for item in outcome:
            metric = item["metric_name"]
            base = baseline_by_metric.get(metric)
            if base and base.get("status") == "known" and item.get("status") == "known":
                baseline_value = float(base["value"])
                outcome_value = float(item["value"])
                result.append(
                    {
                        "metric_name": metric,
                        "status": "computed",
                        "baseline_value": base["value"],
                        "outcome_value": item["value"],
                        "delta": round(outcome_value - baseline_value, 4),
                        "percent_delta": round((outcome_value - baseline_value) / abs(baseline_value), 4)
                        if baseline_value
                        else None,
                    }
                )
            else:
                result.append(
                    {
                        "metric_name": metric,
                        "status": "unknown",
                        "baseline_status": base.get("status") if base else "unknown",
                        "outcome_status": item.get("status"),
                        "reason": "baseline and outcome values must both be known",
                    }
                )
        return result

    def _record_learning(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        plan: dict[str, Any],
        values: list[dict[str, Any]],
        deltas: list[dict[str, Any]],
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        measured = [
            {
                "type": item["source_table"],
                "id": item["source_row_id"],
                "metric_name": item["metric_name"],
                "value": item["value"],
                "occurred_at": item["captured_at"],
            }
            for item in values
            if item.get("status") == "known"
        ]
        outcome = {
            "attribution_id": plan["id"],
            "subject": {"kind": plan["subject_kind"], "id": plan["subject_id"]},
            "measured_outcomes": measured,
            "deltas": deltas,
            "summary": plan["outcome_summary"],
        }
        return self.learning.record_hypothesis(
            organization_id,
            workspace_id,
            person_id,
            f"Automated attribution evaluated {plan['subject_kind']} {plan['subject_id']}.",
            subject=f"{plan['subject_kind']}:{plan['subject_id']}",
            evidence_for_refs=[],
            evidence_against_refs=[],
            status="resolved",
            confidence=self._score(deltas),
            assumptions=["Only existing canonical local metric rows were used; missing values remain unknown."],
            generated_by={"type": "system", "id": "attribution_ops"},
            resolution="Outcome attribution captured and linked to measured outcomes.",
            outcome=outcome,
            idempotency_key=f"attribution:{plan['id']}",
        )

    @staticmethod
    def _score(deltas: list[dict[str, Any]]) -> float:
        computed = sum(1 for item in deltas if item["status"] == "computed")
        return round(computed / len(deltas), 4) if deltas else 0.0

    def _plan(self, organization_id: str, workspace_id: str, attribution_id: str) -> Any:
        row = self.conn.execute(
            """SELECT * FROM outcome_attribution_plans
               WHERE organization_id=? AND workspace_id=? AND id=?""",
            (organization_id, workspace_id, attribution_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("attribution plan not found")
        return row

    def _latest_snapshot(self, organization_id: str, workspace_id: str, attribution_id: str, kind: str) -> Any:
        return self.conn.execute(
            """SELECT * FROM outcome_attribution_snapshots
               WHERE organization_id=? AND workspace_id=? AND attribution_id=? AND snapshot_kind=?
               ORDER BY captured_at DESC,id DESC LIMIT 1""",
            (organization_id, workspace_id, attribution_id, kind),
        ).fetchone()
