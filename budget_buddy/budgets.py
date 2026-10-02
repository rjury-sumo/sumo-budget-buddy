"""budgets.py — client for the /v2/ingestBudgets API (ingestBudgetManagementV2).

Field/behavior notes are live-verified against a real org — see
docs/dev/budget-buddy-plan.md, "Research: the budget APIs (live-verified)":
`budgetType` (dailyVolume|minuteVolume) works on create despite being absent
from the published OpenAPI schema; omitting it defaults to dailyVolume with
auditThreshold defaulting to 85; DELETE returns 204 with an empty body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from budget_buddy import http_client as _http

logger = logging.getLogger("budget_buddy.budgets")


class BudgetAPIError(Exception):
    def __init__(self, status_code: int, message: str, operation: str = ""):
        super().__init__(f"{operation}: HTTP {status_code} - {message}" if operation else message)
        self.status_code = status_code
        self.operation = operation


@dataclass(frozen=True)
class IngestBudget:
    id: str
    name: str
    scope: str
    capacity_bytes: int
    action: str
    budget_type: str
    description: str
    timezone: str
    reset_time: str
    audit_threshold: int
    usage_bytes: int
    usage_status: str  # Normal | Approaching | Exceeded | Unknown
    created_at: str
    modified_at: str

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "IngestBudget":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            scope=d.get("scope", ""),
            capacity_bytes=int(d.get("capacityBytes", 0)),
            action=d.get("action", ""),
            budget_type=d.get("budgetType", "dailyVolume"),
            description=d.get("description", ""),
            timezone=d.get("timezone", "Etc/UTC"),
            reset_time=d.get("resetTime", "00:00"),
            audit_threshold=int(d.get("auditThreshold", 85)),
            usage_bytes=int(d.get("usageBytes", 0)),
            usage_status=d.get("usageStatus", "Unknown"),
            created_at=d.get("createdAt", ""),
            modified_at=d.get("modifiedAt", ""),
        )


class IngestBudgetsV2Client:
    def __init__(self, access_id: str, access_key: str, endpoint: str):
        self.base = endpoint.rstrip("/") + "/api/v2"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(access_id, access_key)
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.throttle = _http.Throttle()

    def _check(self, resp: requests.Response, operation: str) -> dict:
        logger.debug("%s -> HTTP %s", operation, resp.status_code)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise BudgetAPIError(resp.status_code, str(detail), operation) from exc
        return resp.json() if resp.text.strip() else {}

    def _request(self, method: str, path: str, *, params=None, json_body=None, operation: str = "") -> dict:
        url = f"{self.base}{path}"
        resp = _http.send_with_retry(
            self.session, method, url, params=params, json=json_body, throttle=self.throttle,
        )
        return self._check(resp, operation or f"{method.upper()} {path}")

    def list_budgets(self, *, limit: int = 100) -> list[IngestBudget]:
        items: list[dict] = []
        token: str | None = None
        while True:
            params = {"limit": limit}
            if token:
                params["token"] = token
            page = self._request("get", "/ingestBudgets", params=params, operation="list ingest budgets")
            items.extend(page.get("data", []))
            token = page.get("next")
            if not token:
                break
        return [IngestBudget.from_api(d) for d in items]

    def get_budget(self, budget_id: str) -> IngestBudget:
        d = self._request("get", f"/ingestBudgets/{budget_id}", operation=f"get budget ({budget_id})")
        return IngestBudget.from_api(d)

    def create_budget(self, *, name: str, scope: str, capacity_bytes: int, action: str,
                       budget_type: str, description: str = "", timezone: str = "Etc/UTC",
                       reset_time: str = "00:00", audit_threshold: int = 85) -> IngestBudget:
        body = {
            "name": name,
            "scope": scope,
            "capacityBytes": int(capacity_bytes),
            "action": action,
            "budgetType": budget_type,
            "description": description,
            "timezone": timezone,
            "resetTime": reset_time,
            "auditThreshold": audit_threshold,
        }
        d = self._request("post", "/ingestBudgets", json_body=body, operation=f"create budget ({name!r})")
        return IngestBudget.from_api(d)

    def delete_budget(self, budget_id: str) -> bool:
        """Returns True on success. A 404 (already gone) is treated as a
        non-fatal success by the caller — see reconcile.py sweep logic."""
        url = f"{self.base}/ingestBudgets/{budget_id}"
        self.throttle.wait()
        resp = self.session.delete(url)
        if resp.status_code == 404:
            return False
        if resp.status_code not in (200, 204):
            self._check(resp, f"delete budget ({budget_id})")
        return True

    def reset_usage(self, budget_id: str) -> None:
        self._request("post", f"/ingestBudgets/{budget_id}/usage/reset",
                       operation=f"reset usage ({budget_id})")
