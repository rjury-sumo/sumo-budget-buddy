"""search.py — minimal Search Job API client for budget-buddy's one need:
run an aggregate query (create -> poll -> fetch records -> delete) and get
the resulting rows back. Modeled on
docs/search-job-api-reference/sumo_search_client.py's lifecycle logic, built
on budget_buddy's own vendored `http_client` (Throttle + send_with_retry) —
see docs/dev/budget-buddy-plan.md, "Standalone portability". No raw-message
pagination, no PII redaction, no result caching, no discovery endpoints —
budget-buddy only ever runs small aggregate queries against
`sumologic_volume`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from budget_buddy import http_client as _http

logger = logging.getLogger("budget_buddy.search")

DONE_STATE = "DONE GATHERING RESULTS"
TERMINAL_FAIL_STATES = {"CANCELLED", "FORCE PAUSED"}
DEFAULT_POLL_TIMEOUT_S = 300.0
DEFAULT_PAGE_SIZE = 1000


class SearchError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, job_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.job_id = job_id


class SearchJobFailed(SearchError):
    """Job reported pendingErrors, or ended CANCELLED / FORCE PAUSED."""


class SearchTimeout(SearchError):
    """Polling exceeded the deadline without reaching DONE."""


@dataclass
class SearchResult:
    job_id: str
    total: int
    records: list[dict[str, Any]] = field(default_factory=list)
    pending_warnings: list[Any] = field(default_factory=list)


class SearchClient:
    """Throttled client for the aggregate-query subset of the Search Job API."""

    def __init__(self, access_id: str, access_key: str, endpoint: str):
        self.base = endpoint.rstrip("/") + "/api/v1"
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
            raise SearchError(f"{operation} failed: HTTP {resp.status_code} - {detail}",
                               status_code=resp.status_code) from exc
        return resp.json() if resp.text.strip() else {}

    def _request(self, method: str, path: str, *, params=None, json_body=None, operation: str = "") -> dict:
        url = f"{self.base}{path}"
        resp = _http.send_with_retry(
            self.session, method, url, params=params, json=json_body, throttle=self.throttle,
        )
        return self._check(resp, operation or f"{method.upper()} {path}")

    def create_job(self, query: str, from_ms: int, to_ms: int, *, time_zone: str = "UTC") -> str:
        body = {
            "query": query,
            "from": str(int(from_ms)),
            "to": str(int(to_ms)),
            "timeZone": time_zone,
            "requiresRawMessages": False,
        }
        job = self._request("post", "/search/jobs", json_body=body, operation="create search job")
        return job["id"]

    def get_status(self, job_id: str) -> dict:
        return self._request("get", f"/search/jobs/{job_id}", operation=f"get status ({job_id})")

    def get_records(self, job_id: str, offset: int, limit: int) -> dict:
        return self._request("get", f"/search/jobs/{job_id}/records",
                              params={"offset": offset, "limit": limit},
                              operation=f"fetch records ({job_id})")

    def delete_job(self, job_id: str) -> None:
        try:
            self.throttle.wait()
            resp = self.session.delete(f"{self.base}/search/jobs/{job_id}")
            if resp.status_code not in (200, 204):
                logger.warning("delete job %s returned HTTP %s", job_id, resp.status_code)
        except requests.RequestException:
            logger.warning("failed to delete job %s (non-fatal)", job_id, exc_info=True)

    def poll_until_done(self, job_id: str, *, timeout_s: float = DEFAULT_POLL_TIMEOUT_S) -> dict:
        deadline = time.monotonic() + timeout_s
        interval = 1.0
        last_state = None
        while time.monotonic() < deadline:
            status = self.get_status(job_id)
            errors = status.get("pendingErrors") or []
            if errors:
                raise SearchJobFailed(f"job {job_id} reported pendingErrors: {errors}", job_id=job_id)
            state = status.get("state", "")
            if state != last_state:
                logger.debug("job %s state=%s recordCount=%s", job_id, state, status.get("recordCount"))
                last_state = state
            if state == DONE_STATE:
                return status
            if state in TERMINAL_FAIL_STATES:
                raise SearchJobFailed(f"job {job_id} ended in state {state}", job_id=job_id)
            sleep_time = min(interval, max(0.0, deadline - time.monotonic()))
            if sleep_time > 0:
                time.sleep(sleep_time)
            interval = min(interval * 2, 30.0)
        raise SearchTimeout(f"timed out after {timeout_s}s waiting for job {job_id}", job_id=job_id)

    def run_aggregate(self, query: str, from_ms: int, to_ms: int, *,
                       time_zone: str = "UTC", page_size: int = DEFAULT_PAGE_SIZE,
                       poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S) -> SearchResult:
        """Create -> poll -> fetch all records -> delete, in one call. The job
        is always deleted, even if polling/fetching raises. Aggregate queries
        only — budget-buddy never needs raw messages."""
        job_id = self.create_job(query, from_ms, to_ms, time_zone=time_zone)
        try:
            status = self.poll_until_done(job_id, timeout_s=poll_timeout_s)
            total = status.get("recordCount", 0)
            records: list[dict[str, Any]] = []
            offset = 0
            while offset < total:
                batch = min(page_size, total - offset)
                page = self.get_records(job_id, offset, batch)
                rows = page.get("records", [])
                if not rows:
                    break
                records.extend(rows)
                offset += len(rows)
            return SearchResult(job_id=job_id, total=total, records=records,
                                 pending_warnings=status.get("pendingWarnings", []))
        finally:
            self.delete_job(job_id)
