from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.understanding.pipeline import extract


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if not str(value).strip():
        raise ValidationError("observed_at is required")
    return str(value)


class UnderstandingService:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any]) -> None:
        self.conn, self.new_id, self.authorize = conn, new_id, authorize

    def record_source(
        self,
        organization_id: str,
        person_id: str,
        source_type: str,
        text: str,
        observed_at: datetime | str,
    ) -> dict[str, Any]:
        self.authorize(organization_id, person_id, write=True)
        if not source_type.strip():
            raise ValidationError("source_type is required")
        if not text.strip():
            raise ValidationError("text is required")
        now = _now().isoformat()
        source_id = self.new_id("usrc")
        self.conn.execute(
            """INSERT INTO understanding_sources
               (id,organization_id,workspace_id,source_type,text,observed_at,created_by_person_id,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (source_id, organization_id, None, source_type.strip(), text, _iso(observed_at), person_id, now),
        )
        proposals = extract(text, source_ref=source_id)
        for proposal in proposals:
            data = asdict(proposal)
            self.conn.execute(
                """INSERT INTO understanding_proposals
                   (id,organization_id,workspace_id,source_id,kind,subject,payload_json,evidence_json,
                    confidence,extractor_version,status,created_by_person_id,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["proposal_id"],
                    organization_id,
                    None,
                    source_id,
                    data["kind"],
                    data["subject"],
                    json.dumps(data["payload"], sort_keys=True),
                    json.dumps(data["evidence"], sort_keys=True),
                    data["confidence"],
                    data["extractor_version"],
                    data["status"],
                    person_id,
                    now,
                    now,
                ),
            )
        self.conn.commit()
        return {"source": self._source(source_id), "proposals": [self._proposal(row) for row in self._proposal_rows(source_id)]}

    def list_proposals(
        self,
        organization_id: str,
        person_id: str,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        self.authorize(organization_id, person_id)
        sql = "SELECT * FROM understanding_proposals WHERE organization_id=?"
        params: list[Any] = [organization_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        rows = self.conn.execute(sql + " ORDER BY created_at DESC,id", params).fetchall()
        return [self._proposal(row) for row in rows]

    def promote(self, organization_id: str, person_id: str, proposal_id: str, decision: str) -> dict[str, Any]:
        self.authorize(organization_id, person_id, write=True)
        if decision not in {"confirmed", "rejected"}:
            raise ValidationError("decision must be confirmed or rejected")
        row = self.conn.execute(
            "SELECT * FROM understanding_proposals WHERE id=? AND organization_id=?",
            (proposal_id, organization_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("proposal not found")
        prior_status = row["status"]
        if prior_status != "proposed":
            raise ValidationError("can only promote proposed understanding proposals")
        now = _now().isoformat()
        event_id = self.new_id("upe")
        self.conn.execute(
            """INSERT INTO understanding_proposal_events
               (id,organization_id,workspace_id,proposal_id,reviewer_person_id,from_status,to_status,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (event_id, organization_id, row["workspace_id"], proposal_id, person_id, prior_status, decision, now),
        )
        self.conn.execute(
            "UPDATE understanding_proposals SET status=?,updated_at=? WHERE id=? AND organization_id=?",
            (decision, now, proposal_id, organization_id),
        )
        self.conn.commit()
        return self._proposal(
            self.conn.execute(
                "SELECT * FROM understanding_proposals WHERE id=? AND organization_id=?",
                (proposal_id, organization_id),
            ).fetchone()
        )

    def _source(self, source_id: str) -> dict[str, Any]:
        return dict(self.conn.execute("SELECT * FROM understanding_sources WHERE id=?", (source_id,)).fetchone())

    def _proposal_rows(self, source_id: str) -> list[Any]:
        return self.conn.execute(
            "SELECT * FROM understanding_proposals WHERE source_id=? ORDER BY created_at,id",
            (source_id,),
        ).fetchall()

    def _proposal(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item
