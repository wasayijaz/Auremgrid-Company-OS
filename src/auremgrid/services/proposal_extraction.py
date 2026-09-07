from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, ValidationError


REQUEST_KEYWORDS = {
    "campaign": ("campaign", "ads", "ad ", "paid social", "paid media", "meta", "google ads", "launch"),
    "design": ("design", "mockup", "figma", "logo", "brand", "creative", "visual"),
    "analytics": ("analytics", "dashboard", "metrics", "reporting", "ga4", "search console", "performance report"),
    "content": ("content", "copy", "blog", "post", "newsletter", "article", "caption", "social"),
    "website": ("website", "landing page", "homepage", "web page", "site", "webflow"),
}
REQUEST_TYPES = ("campaign", "design", "analytics", "content", "website", "other")
URGENT_RE = re.compile(r"\b(urgent|asap|emergency|critical)\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>()\"']+")
FILENAME_RE = re.compile(r"\b[\w()_-]+\.(?:pdf|docx?|xlsx?|pptx?|csv|png|jpe?g|gif|webp|svg|zip|txt)\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTH_DATE_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
ATTACHMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".zip", ".txt")


class ProposalExtractionService:
    def __init__(self, company_os: Any) -> None:
        self.os = company_os

    def draft_from_text(self, os_scope: Any, text: str) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id)
        body = str(text or "").strip()
        if not body:
            raise ValidationError("proposal text is required")

        request_type, type_confidence = self._request_type(body)
        goal, goal_confidence = self._goal(body)
        deadline, deadline_confidence = self._deadline(body)
        attachments, references = self._links_and_files(body)
        unknowns = []
        if not deadline:
            unknowns.append("deadline")
        if request_type == "other":
            unknowns.append("request_type")
        if not goal:
            unknowns.append("goal")

        return {
            "request_type": request_type,
            "goal": goal,
            "deadline": deadline,
            "priority": "urgent" if URGENT_RE.search(body) else "normal",
            "attachments": attachments,
            "references": references,
            "unknowns": unknowns,
            "confidence": {
                "request_type": type_confidence,
                "goal": goal_confidence,
                "deadline": deadline_confidence,
            },
        }

    def _authorize(self, organization_id: str, workspace_id: str, person_id: str) -> None:
        if hasattr(self.os, "_require_person_access"):
            self.os._require_person_access(organization_id, workspace_id, person_id)
        elif self.os.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("person is not an organization member")

    @staticmethod
    def _scope(os_scope: Any) -> tuple[str, str, str]:
        def value(key: str) -> Any:
            return os_scope.get(key) if isinstance(os_scope, Mapping) else getattr(os_scope, key, None)

        organization_id, workspace_id, person_id = (str(value(key) or "").strip() for key in ("organization_id", "workspace_id", "person_id"))
        if not organization_id or not workspace_id or not person_id:
            raise ValidationError("organization, workspace, and person are required")
        return organization_id, workspace_id, person_id

    @staticmethod
    def _request_type(text: str) -> tuple[str, str]:
        normalized = f" {text.lower()} "
        scores = {
            request_type: sum(1 for keyword in keywords if keyword in normalized)
            for request_type, keywords in REQUEST_KEYWORDS.items()
        }
        best = max(scores, key=lambda key: scores[key])
        if scores[best] <= 0:
            return "other", "low"
        return best, "high" if scores[best] >= 2 else "medium"

    @staticmethod
    def _goal(text: str) -> tuple[str, str]:
        for sentence in ProposalExtractionService._sentences(text):
            candidate = sentence.strip(" \t\r\n-–—")
            lowered = candidate.lower()
            if lowered.startswith(("please ", "kindly ")):
                candidate = candidate.split(" ", 1)[1].strip()
                lowered = candidate.lower()
            if lowered.startswith((
                "create ", "build ", "design ", "launch ", "make ", "prepare ", "write ", "analyze ",
                "update ", "send ", "set up ", "produce ", "develop ", "draft ", "review ",
            )):
                return candidate[:500], "high"
        sentences = ProposalExtractionService._sentences(text)
        fallback = sentences[0].strip()[:500] if sentences else ""
        return fallback, "medium" if fallback else "low"

    @staticmethod
    def _deadline(text: str) -> tuple[str | None, str]:
        now = datetime.now(timezone.utc).date()
        lowered = text.lower()
        if "end of week" in lowered:
            return (now + timedelta(days=(4 - now.weekday()) % 7)).isoformat(), "medium"
        if "end of month" in lowered:
            return date(now.year, now.month, calendar.monthrange(now.year, now.month)[1]).isoformat(), "medium"
        if re.search(r"\btomorrow\b", lowered):
            return (now + timedelta(days=1)).isoformat(), "medium"
        if "next week" in lowered:
            return (now + timedelta(days=7 - now.weekday())).isoformat(), "medium"

        match = ISO_DATE_RE.search(text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat(), "high"
            except ValueError:
                return None, "low"

        match = MONTH_DATE_RE.search(text)
        if match:
            month = ProposalExtractionService._month_number(match.group(1))
            day = int(match.group(2))
            try:
                parsed = date(now.year, month, day)
            except ValueError:
                return None, "low"
            if parsed < now:
                parsed = date(now.year + 1, month, day)
            return parsed.isoformat(), "high"
        return None, "low"

    @staticmethod
    def _links_and_files(text: str) -> tuple[list[str], list[str]]:
        urls = [item.rstrip(".,);]") for item in URL_RE.findall(text)]
        attachments = []
        references = []
        for url in urls:
            target = attachments if url.lower().split("?", 1)[0].endswith(ATTACHMENT_EXTENSIONS) else references
            if url not in target:
                target.append(url)
        for filename in FILENAME_RE.findall(text):
            clean = filename.strip(" .,\n\r\t")
            if clean and clean not in attachments and not any(clean in url for url in urls):
                attachments.append(clean)
        return attachments, references

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", text) if item.strip()]

    @staticmethod
    def _month_number(value: str) -> int:
        key = value.lower().rstrip(".")[:3]
        return ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec").index(key) + 1
