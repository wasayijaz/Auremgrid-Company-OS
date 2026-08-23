from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


TEMPLATE_VERSION = "2026-08-23"
IMPORT_TYPES = {"client_workspaces", "campaigns", "campaign_metrics"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _optional_float(value: str, field: str, errors: list[dict[str, str]]) -> float | None:
    text = _clean(value)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        errors.append({"field": field, "message": f"{field} must be a number"})
        return None
    if number < 0:
        errors.append({"field": field, "message": f"{field} cannot be negative"})
        return None
    return number


def _require(value: str, field: str, errors: list[dict[str, str]]) -> str:
    text = _clean(value)
    if not text:
        errors.append({"field": field, "message": f"{field} is required"})
    return text


class OnboardingService:
    def __init__(
        self,
        os: Any,
        conn: Any,
        new_id: Callable[[str], str],
        authorize: Callable[..., Any],
    ) -> None:
        self.os = os
        self.conn = conn
        self.new_id = new_id
        self.authorize = authorize

    def templates(self) -> dict[str, Any]:
        return {
            "version": TEMPLATE_VERSION,
            "templates": {
                "client_workspaces": {
                    "required": ["name"],
                    "optional": ["workspace_id", "kind"],
                    "csv": "name,workspace_id,kind\nExample Client,,client\n",
                },
                "campaigns": {
                    "required": ["name", "objective", "platform"],
                    "optional": ["budget", "currency", "start_date", "end_date"],
                    "csv": "name,objective,platform,budget,currency,start_date,end_date\nLead Gen,Booked calls,meta,1000,USD,,\n",
                },
                "campaign_metrics": {
                    "required": ["campaign_id", "source"],
                    "optional": ["spend", "revenue", "leads", "impressions", "clicks"],
                    "csv": "campaign_id,source,spend,revenue,leads,impressions,clicks\ncampaign_123,manual import,100,400,10,10000,200\n",
                },
            },
        }

    def onboard_agency(
        self,
        agency_name: str,
        workspace_id: str,
        admin_name: str,
        operator_name: str | None = None,
    ) -> dict[str, Any]:
        workspace = self.os.create_workspace(agency_name, workspace_id=workspace_id)
        admin = self.os.create_actor(workspace.id, admin_name, "admin", f"act_{workspace.id}_admin")
        operator = self.os.create_actor(
            workspace.id,
            operator_name or f"{agency_name} Operator",
            "operator",
            f"act_{workspace.id}_operator",
        )
        agent = self.os.create_actor(workspace.id, f"{agency_name} Agent", "agent", f"act_{workspace.id}_agent")
        self.os.stack.bind_agent(workspace.id, agent.id)
        self.os.upsert_client_brain(
            workspace.id,
            admin.id,
            snapshot=f"{agency_name} workspace. Fill this brain before starting client work.",
            brand_rules="Add approved visual and voice rules here.",
            dos=["Cite current approved facts", "Capture work before producing"],
            donts=["Do not invent prices or brand rules"],
            open_loops=["Complete the first client brain"],
        )
        return {
            "workspace": workspace.to_dict(),
            "admin": admin.to_dict(),
            "operator": operator.to_dict(),
            "agent": agent.to_dict(),
            "ingested_sources": 0,
            "import_templates": self.templates(),
            "engines": [item["name"] for item in self.os.stack.contributions(workspace.id, agency_name, agent.id)],
        }

    def preview_csv_import(
        self,
        organization_id: str,
        workspace_id: str | None,
        person_id: str,
        import_type: str,
        csv_text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        import_type = self._import_type(import_type)
        idempotency_key = self._required_idempotency(idempotency_key)
        workspace_id = self._workspace_for_import(import_type, workspace_id)
        self._authorize_import(organization_id, workspace_id, person_id, write=True)
        payload_hash = self._payload_hash(import_type, csv_text)
        existing = self._receipt_by_key(organization_id, "preview", idempotency_key)
        if existing:
            self._assert_same_payload(existing, payload_hash)
            return self._batch_response(existing["batch_id"], replayed=True)
        rows = self._parse_csv(import_type, csv_text, organization_id, workspace_id)
        now = _now()
        batch_id = self.new_id("import_batch")
        valid = sum(1 for row in rows if not row["errors"])
        invalid = len(rows) - valid
        with self.conn:
            self.conn.execute(
                """INSERT INTO onboarding_import_batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id, organization_id, workspace_id, person_id, import_type,
                    idempotency_key, payload_hash, TEMPLATE_VERSION, len(rows), valid, invalid, now,
                ),
            )
            for row in rows:
                row_id = self.new_id("import_row")
                status = "preview_valid" if not row["errors"] else "quarantined"
                self.conn.execute(
                    """INSERT INTO onboarding_import_rows VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row_id, batch_id, organization_id, workspace_id, row["row_number"],
                        row["row_hash"], status, _compact_json(row["raw"]),
                        _compact_json(row["normalized"] if not row["errors"] else {}), now,
                    ),
                )
                for error in row["errors"]:
                    self._insert_error(batch_id, row_id, organization_id, workspace_id, row["row_number"], error, now)
            self._insert_receipt(
                batch_id, organization_id, workspace_id, person_id, "preview", "previewed",
                idempotency_key, payload_hash, {"total_rows": len(rows), "valid_rows": valid, "invalid_rows": invalid}, now,
            )
        return self._batch_response(batch_id)

    def commit_csv_import(
        self,
        organization_id: str,
        batch_id: str,
        person_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        idempotency_key = self._required_idempotency(idempotency_key)
        batch = self._batch(batch_id)
        if batch["organization_id"] != organization_id:
            raise NotFoundError("import batch not found")
        self._authorize_import(organization_id, batch["workspace_id"], person_id, write=True)
        payload_hash = str(batch["payload_hash"])
        existing = self._receipt_by_key(organization_id, "commit", idempotency_key)
        if existing:
            if existing["batch_id"] != batch_id:
                raise ValidationError("idempotency key already used for another commit")
            self._assert_same_payload(existing, payload_hash)
            return self._batch_response(batch_id, replayed=True)
        committed = self.conn.execute(
            "SELECT 1 FROM onboarding_import_receipts WHERE batch_id=? AND phase='commit' AND status IN ('committed','committed_with_errors')",
            (batch_id,),
        ).fetchone()
        if committed:
            raise ValidationError("import batch has already been committed")
        now = _now()
        created: list[dict[str, str]] = []
        failed = 0
        rows = self.conn.execute(
            "SELECT * FROM onboarding_import_rows WHERE batch_id=? AND status='preview_valid' ORDER BY row_number",
            (batch_id,),
        ).fetchall()
        with self.conn:
            for row in rows:
                normalized = json.loads(row["normalized_json"])
                try:
                    item = self._commit_row(dict(batch), normalized, person_id)
                    created.append({"row_number": int(row["row_number"]), "entity_type": item["entity_type"], "entity_id": item["entity_id"]})
                except (AuthorizationError, NotFoundError, ValidationError) as exc:
                    failed += 1
                    self._insert_error(
                        batch_id, row["id"], organization_id, batch["workspace_id"], int(row["row_number"]),
                        {"field": "row", "message": str(exc)}, now,
                    )
            status = "committed_with_errors" if failed else "committed"
            self._insert_receipt(
                batch_id, organization_id, batch["workspace_id"], person_id, "commit", status,
                idempotency_key, payload_hash, {"created": created, "failed_rows": failed}, now,
            )
        return self._batch_response(batch_id)

    def list_import_batches(
        self, organization_id: str, workspace_id: str | None, person_id: str, limit: int = 10
    ) -> dict[str, Any]:
        if workspace_id:
            self._authorize_import(organization_id, workspace_id, person_id)
            rows = self.conn.execute(
                """SELECT * FROM onboarding_import_batches
                   WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at DESC,id DESC LIMIT ?""",
                (organization_id, workspace_id, limit),
            ).fetchall()
        else:
            if self.os.company.org_membership(organization_id, person_id) is None:
                raise AuthorizationError("organization membership required")
            rows = self.conn.execute(
                """SELECT * FROM onboarding_import_batches
                   WHERE organization_id=?
                   ORDER BY created_at DESC,id DESC LIMIT ?""",
                (organization_id, limit),
            ).fetchall()
        return {"imports": [self._batch_summary(dict(row)) for row in rows]}

    def latest_status(self, organization_id: str, workspace_ids: list[str]) -> dict[str, Any]:
        if not workspace_ids:
            return {"recent": [], "quarantined_rows": 0, "commit_required": 0}
        marks = ",".join("?" for _ in workspace_ids)
        batches = [
            self._batch_summary(dict(row))
            for row in self.conn.execute(
                f"""SELECT * FROM onboarding_import_batches
                    WHERE organization_id=? AND workspace_id IN ({marks})
                    ORDER BY created_at DESC,id DESC LIMIT 5""",
                (organization_id, *workspace_ids),
            ).fetchall()
        ]
        quarantined = int(self.conn.execute(
            f"""SELECT COUNT(*) FROM onboarding_import_rows
                WHERE organization_id=? AND workspace_id IN ({marks}) AND status='quarantined'""",
            (organization_id, *workspace_ids),
        ).fetchone()[0])
        commit_required = int(self.conn.execute(
            f"""SELECT COUNT(*) FROM onboarding_import_batches b
                WHERE b.organization_id=? AND b.workspace_id IN ({marks})
                  AND b.valid_rows > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM onboarding_import_receipts r
                    WHERE r.batch_id=b.id AND r.phase='commit'
                      AND r.status IN ('committed','committed_with_errors')
                  )""",
            (organization_id, *workspace_ids),
        ).fetchone()[0])
        return {"recent": batches, "quarantined_rows": quarantined, "commit_required": commit_required}

    def _parse_csv(
        self, import_type: str, csv_text: str, organization_id: str, workspace_id: str | None
    ) -> list[dict[str, Any]]:
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise ValidationError("csv text is required")
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            if not reader.fieldnames:
                raise ValidationError("csv header row is required")
            rows = []
            for row_number, raw in enumerate(reader, start=2):
                clean_raw = {str(key or "").strip(): _clean(value) for key, value in raw.items() if key is not None}
                errors: list[dict[str, str]] = []
                normalized = self._normalize_row(import_type, clean_raw, organization_id, workspace_id, errors)
                rows.append({
                    "row_number": row_number,
                    "raw": clean_raw,
                    "normalized": normalized,
                    "errors": errors,
                    "row_hash": _hash_text(_compact_json(clean_raw)),
                })
        except csv.Error as exc:
            raise ValidationError(f"csv could not be parsed: {exc}") from exc
        if not rows:
            raise ValidationError("csv must include at least one data row")
        if import_type == "client_workspaces":
            seen: set[str] = set()
            for row in rows:
                row_workspace_id = row["normalized"].get("workspace_id")
                if not row_workspace_id:
                    continue
                if row_workspace_id in seen:
                    row["errors"].append({"field": "workspace_id", "message": "workspace_id is duplicated in this CSV"})
                seen.add(row_workspace_id)
        return rows

    def _normalize_row(
        self, import_type: str, row: dict[str, str], organization_id: str, workspace_id: str | None,
        errors: list[dict[str, str]],
    ) -> dict[str, Any]:
        if import_type == "client_workspaces":
            kind = _clean(row.get("kind")) or "client"
            if kind != "client":
                errors.append({"field": "kind", "message": "client workspace imports only support kind=client"})
            workspace_id_value = _clean(row.get("workspace_id")) or None
            if workspace_id_value and self.os.company.workspace_scope(workspace_id_value) is not None:
                errors.append({"field": "workspace_id", "message": "workspace_id already exists"})
            return {
                "name": _require(row.get("name", ""), "name", errors),
                "workspace_id": workspace_id_value,
                "kind": kind,
            }
        if import_type == "campaigns":
            return {
                "workspace_id": workspace_id,
                "name": _require(row.get("name", ""), "name", errors),
                "objective": _require(row.get("objective", ""), "objective", errors),
                "platform": _require(row.get("platform", ""), "platform", errors),
                "budget": _optional_float(row.get("budget", ""), "budget", errors),
                "currency": _clean(row.get("currency")) or "USD",
                "start_date": _clean(row.get("start_date")) or None,
                "end_date": _clean(row.get("end_date")) or None,
            }
        if import_type == "campaign_metrics":
            campaign_id = _require(row.get("campaign_id", ""), "campaign_id", errors)
            if campaign_id and not self.conn.execute(
                "SELECT id FROM campaigns WHERE organization_id=? AND workspace_id=? AND id=?",
                (organization_id, workspace_id, campaign_id),
            ).fetchone():
                errors.append({"field": "campaign_id", "message": "campaign_id was not found in this workspace"})
            normalized = {
                "workspace_id": workspace_id,
                "campaign_id": campaign_id,
                "source": _require(row.get("source", ""), "source", errors),
                "spend": _optional_float(row.get("spend", ""), "spend", errors),
                "revenue": _optional_float(row.get("revenue", ""), "revenue", errors),
                "leads": _optional_float(row.get("leads", ""), "leads", errors),
                "impressions": _optional_float(row.get("impressions", ""), "impressions", errors),
                "clicks": _optional_float(row.get("clicks", ""), "clicks", errors),
            }
            if all(normalized[field] is None for field in ("spend", "revenue", "leads", "impressions", "clicks")):
                errors.append({"field": "metrics", "message": "at least one metric value is required"})
            return normalized
        raise ValidationError("unsupported import type")

    def _commit_row(self, batch: dict[str, Any], row: dict[str, Any], person_id: str) -> dict[str, str]:
        import_type = batch["import_type"]
        organization_id = batch["organization_id"]
        workspace_id = batch["workspace_id"]
        if import_type == "client_workspaces":
            workspace = self.os.create_organization_workspace(
                organization_id, row["name"], row["kind"], row.get("workspace_id")
            )
            if self.os.company.workspace_membership(workspace.id, person_id) is None:
                self.os.add_person_to_workspace(organization_id, workspace.id, person_id, "admin")
            return {"entity_type": "workspace", "entity_id": workspace.id}
        if import_type == "campaigns":
            item = self.os.agency_ops.create_campaign(
                organization_id, workspace_id, person_id, row["name"], row["objective"], row["platform"],
                budget=row.get("budget"), currency=row.get("currency") or "USD",
                start_date=row.get("start_date"), end_date=row.get("end_date"),
            )
            return {"entity_type": "campaign", "entity_id": item["id"]}
        if import_type == "campaign_metrics":
            item = self.os.agency_ops.record_campaign_metrics(
                organization_id, workspace_id, person_id, row["campaign_id"], row["source"],
                spend=row.get("spend"), revenue=row.get("revenue"), leads=row.get("leads"),
                impressions=row.get("impressions"), clicks=row.get("clicks"),
            )
            return {"entity_type": "campaign_metric", "entity_id": item["id"]}
        raise ValidationError("unsupported import type")

    def _insert_error(
        self, batch_id: str, row_id: str | None, organization_id: str, workspace_id: str | None,
        row_number: int, error: dict[str, str], created_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO onboarding_import_errors VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                self.new_id("import_error"), batch_id, row_id, organization_id, workspace_id,
                row_number, error.get("field") or "row", error.get("message") or "invalid row", created_at,
            ),
        )

    def _insert_receipt(
        self, batch_id: str, organization_id: str, workspace_id: str | None, person_id: str,
        phase: str, status: str, idempotency_key: str, payload_hash: str, summary: dict[str, Any],
        created_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO onboarding_import_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.new_id("import_receipt"), batch_id, organization_id, workspace_id,
                person_id, phase, status, idempotency_key, payload_hash, _compact_json(summary), created_at,
            ),
        )

    def _batch_response(self, batch_id: str, replayed: bool = False) -> dict[str, Any]:
        batch = self._batch(batch_id)
        rows = [dict(row) for row in self.conn.execute(
            "SELECT * FROM onboarding_import_rows WHERE batch_id=? ORDER BY row_number", (batch_id,)
        ).fetchall()]
        errors = [dict(row) for row in self.conn.execute(
            "SELECT * FROM onboarding_import_errors WHERE batch_id=? ORDER BY row_number,created_at,id", (batch_id,)
        ).fetchall()]
        receipts = [self._receipt_dict(dict(row)) for row in self.conn.execute(
            "SELECT * FROM onboarding_import_receipts WHERE batch_id=? ORDER BY created_at,id", (batch_id,)
        ).fetchall()]
        return {
            "batch": self._batch_summary(dict(batch)),
            "rows": [self._row_dict(row) for row in rows],
            "errors": errors,
            "receipts": receipts,
            "replayed": replayed,
        }

    def _batch_summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        commit = self.conn.execute(
            "SELECT * FROM onboarding_import_receipts WHERE batch_id=? AND phase='commit' ORDER BY created_at DESC,id DESC LIMIT 1",
            (batch["id"],),
        ).fetchone()
        status = commit["status"] if commit else "commit_required" if int(batch["valid_rows"]) else "quarantined"
        return {
            "id": batch["id"],
            "organization_id": batch["organization_id"],
            "workspace_id": batch["workspace_id"],
            "import_type": batch["import_type"],
            "status": status,
            "total_rows": int(batch["total_rows"]),
            "valid_rows": int(batch["valid_rows"]),
            "invalid_rows": int(batch["invalid_rows"]),
            "created_at": batch["created_at"],
        }

    def _row_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "row_number": int(row["row_number"]),
            "status": row["status"],
            "raw": json.loads(row["raw_json"]),
            "normalized": json.loads(row["normalized_json"]),
        }

    def _receipt_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        row["summary"] = json.loads(row.pop("summary_json"))
        return row

    def _batch(self, batch_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM onboarding_import_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("import batch not found")
        return dict(row)

    def _receipt_by_key(self, organization_id: str, phase: str, idempotency_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM onboarding_import_receipts WHERE organization_id=? AND phase=? AND idempotency_key=?",
            (organization_id, phase, idempotency_key),
        ).fetchone()
        return dict(row) if row else None

    def _assert_same_payload(self, receipt: dict[str, Any], payload_hash: str) -> None:
        if receipt["payload_hash"] != payload_hash:
            raise ValidationError("idempotency key was already used with different import content")

    def _import_type(self, import_type: str) -> str:
        value = _clean(import_type)
        if value not in IMPORT_TYPES:
            raise ValidationError("unsupported import type")
        return value

    def _required_idempotency(self, value: str) -> str:
        text = _clean(value)
        if not text:
            raise ValidationError("idempotency_key is required")
        return text

    def _payload_hash(self, import_type: str, csv_text: str) -> str:
        return _hash_text(_compact_json({"type": import_type, "csv": csv_text}))

    def _workspace_for_import(self, import_type: str, workspace_id: str | None) -> str | None:
        if import_type == "client_workspaces":
            return None
        if not workspace_id:
            raise ValidationError("workspace_id is required")
        return workspace_id

    def _authorize_import(
        self, organization_id: str, workspace_id: str | None, person_id: str, write: bool = False
    ) -> None:
        if workspace_id:
            self.authorize(organization_id, workspace_id, person_id, write=write)
            return
        if self.os.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")
