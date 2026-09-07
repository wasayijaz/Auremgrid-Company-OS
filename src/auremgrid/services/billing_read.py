"""Offline, disk-backed billing read model.

The caller owns the storage file.  This service only reads billing records from
that file and exposes organization-scoped projections for agency finance work.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


Clock = Callable[[], datetime]
AGING_BUCKETS = ("0-30", "31-60", "61-90", "90+")


class BillingReadService:
    """Small, deterministic, organization-scoped offline billing reader."""

    def __init__(self, storage_path: str | Path, *, clock: Clock | None = None) -> None:
        self.storage_path = Path(storage_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def receivables_summary(self, organization_id: str) -> list[dict[str, Any]]:
        """Return invoice and receivables aging summaries per client."""
        if not organization_id:
            raise ValidationError("organization is required")
        data = self._read_store()
        clients = [client for client in self._records(data, "clients") if self._record_org(client) == organization_id]
        invoices = [invoice for invoice in self._records(data, "invoices") if self._record_org(invoice) == organization_id]

        result = []
        for client in sorted(clients, key=lambda item: str(item.get("id") or "")):
            client_id = self._client_id(client)
            client_invoices = [invoice for invoice in invoices if str(invoice.get("client_id") or "") == client_id]
            result.append({
                "client_id": client_id,
                "client_name": str(client.get("name") or ""),
                "organization_id": organization_id,
                "open_balance": self._number(sum((self._open_balance(invoice) for invoice in client_invoices), Decimal("0"))),
                "overdue_count": sum(1 for invoice in client_invoices if self._is_overdue(invoice)),
                "aging_buckets": self._aging(client_invoices),
            })
        return result

    def payment_history(self, organization_id: str, client_id: str) -> list[dict[str, Any]]:
        """Return newest-first payment history for one organization-scoped client."""
        self._require_client(organization_id, client_id)
        data = self._read_store()
        payments = [
            payment for payment in self._records(data, "payments")
            if self._record_org(payment) == organization_id and str(payment.get("client_id") or "") == client_id
        ]
        ordered = sorted(payments, key=lambda payment: self._parse_time(payment.get("paid_at")), reverse=True)
        return [
            {
                "id": self._record_id(payment, "payment id is required"),
                "client_id": client_id,
                "organization_id": organization_id,
                "invoice_id": str(payment.get("invoice_id") or ""),
                "paid_at": self._parse_time(payment.get("paid_at")).isoformat().replace("+00:00", "Z"),
                "amount": self._number(self._money(payment.get("amount"), "payment amount is required")),
                "method": str(payment.get("method") or ""),
            }
            for payment in ordered
        ]

    def revenue_by_month(self, organization_id: str) -> list[dict[str, Any]]:
        """Return paid revenue rollups by month for one organization."""
        if not organization_id:
            raise ValidationError("organization is required")
        data = self._read_store()
        totals: dict[str, Decimal] = {}
        for payment in self._records(data, "payments"):
            if self._record_org(payment) != organization_id:
                continue
            month = self._parse_time(payment.get("paid_at")).strftime("%Y-%m")
            totals[month] = totals.get(month, Decimal("0")) + self._money(payment.get("amount"), "payment amount is required")
        return [
            {"organization_id": organization_id, "month": month, "revenue": self._number(totals[month])}
            for month in sorted(totals)
        ]

    def _require_client(self, organization_id: str, client_id: str) -> Mapping[str, Any]:
        if not organization_id or not client_id:
            raise ValidationError("organization and client are required")
        data = self._read_store()
        scoped = [
            client for client in self._records(data, "clients")
            if str(client.get("id") or "") == client_id
        ]
        if not scoped:
            raise NotFoundError("billing client was not found")
        for client in scoped:
            if self._record_org(client) == organization_id:
                return client
        raise AuthorizationError("billing client is outside organization scope")

    def _read_store(self) -> Mapping[str, Any]:
        if not self.storage_path.exists():
            raise ValidationError("billing storage file is required")
        try:
            with self.storage_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValidationError("billing storage file is invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise ValidationError("billing storage root must be an object")
        return data

    def _aging(self, invoices: list[Mapping[str, Any]]) -> dict[str, float]:
        buckets = {bucket: Decimal("0") for bucket in AGING_BUCKETS}
        today = self._today()
        for invoice in invoices:
            balance = self._open_balance(invoice)
            if balance <= 0:
                continue
            age = max((today - self._parse_date(invoice.get("due_date"))).days, 0)
            if age <= 30:
                bucket = "0-30"
            elif age <= 60:
                bucket = "31-60"
            elif age <= 90:
                bucket = "61-90"
            else:
                bucket = "90+"
            buckets[bucket] += balance
        return {bucket: self._number(buckets[bucket]) for bucket in AGING_BUCKETS}

    def _is_overdue(self, invoice: Mapping[str, Any]) -> bool:
        return self._open_balance(invoice) > 0 and self._parse_date(invoice.get("due_date")) < self._today()

    def _open_balance(self, invoice: Mapping[str, Any]) -> Decimal:
        status = str(invoice.get("status") or "open").lower()
        if status in {"paid", "void", "canceled", "cancelled"}:
            return Decimal("0")
        if "balance_due" in invoice:
            balance = self._money(invoice.get("balance_due"), "invoice balance is required")
        else:
            balance = self._money(invoice.get("amount"), "invoice amount is required") - self._money(invoice.get("paid_amount", 0), "invoice paid amount is invalid")
        return max(balance, Decimal("0"))

    def _today(self) -> date:
        return self._coerce_clock(self.clock()).date()

    @staticmethod
    def _records(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        values = data.get(key, [])
        if not isinstance(values, list):
            raise ValidationError(f"billing {key} must be a list")
        if not all(isinstance(item, Mapping) for item in values):
            raise ValidationError(f"billing {key} records must be objects")
        return values

    @staticmethod
    def _client_id(client: Mapping[str, Any]) -> str:
        return BillingReadService._record_id(client, "client id is required")

    @staticmethod
    def _record_id(record: Mapping[str, Any], message: str) -> str:
        value = str(record.get("id") or "").strip()
        if not value:
            raise ValidationError(message)
        return value

    @staticmethod
    def _record_org(record: Mapping[str, Any]) -> str:
        value = str(record.get("organization_id") or "").strip()
        if not value:
            raise ValidationError("organization id is required")
        return value

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("billing timestamp is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("billing timestamp is invalid") from exc
        return BillingReadService._coerce_clock(parsed)

    @staticmethod
    def _parse_date(value: Any) -> date:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("invoice due date is required")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("invoice due date is invalid") from exc

    @staticmethod
    def _money(value: Any, message: str) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(message) from exc
        if amount < 0:
            raise ValidationError(message)
        return amount

    @staticmethod
    def _number(value: Decimal) -> float:
        return float(value.quantize(Decimal("0.01")))

    @staticmethod
    def _coerce_clock(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


BillingReadServiceAlias = BillingReadService
