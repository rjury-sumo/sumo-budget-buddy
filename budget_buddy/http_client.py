"""
http_client.py — shared HTTP retry/throttle policy for budget_buddy's REST
clients (search.py, budgets.py).

Vendored from cli/http_client.py so budget_buddy has no import dependency on
the parent sumo-ai repo's cli package (see docs/dev/budget-buddy-plan.md,
"Standalone portability" for why) — keep the two in sync by hand if the
shared retry policy changes in either place.

  - Per-instance rate throttle (default 4 requests/second — the Sumo Logic
    per-key limit).
  - Retry ONLY on HTTP 429, with exponential backoff + jitter, honoring the
    `Retry-After` header when present — 400/401/403/404 never succeed on
    retry, and 5xx retry is intentionally out of scope.
  - Non-429 responses (success OR error) are returned to the caller unchanged
    so each client keeps its own status-check / error-type / JSON-parsing
    logic.

The helper operates on a passed-in `requests`-style session, so it has no
`requests` dependency of its own and is trivially unit-testable with a stub
session, an injected `sleep`, and a seeded `rng`.
"""

from __future__ import annotations

import random
import sys
import time

DEFAULT_MIN_INTERVAL = 0.25   # 4 requests/second
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 5.0
DEFAULT_MAX_BACKOFF = 60.0


class Throttle:
    """Minimum-interval rate limiter for one client instance.

    Not thread-safe by design — budget_buddy issues requests serially. Call
    `wait()` immediately before each request; it sleeps just long enough to
    keep calls at or below `1 / min_interval` per second.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


def retry_after_seconds(resp) -> float | None:
    """Parse a numeric `Retry-After` header (seconds) from a response, or None."""
    try:
        val = resp.headers.get("Retry-After")
    except AttributeError:
        return None
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return None


def backoff_seconds(attempt: int, *, base: float = DEFAULT_BASE_BACKOFF,
                    cap: float = DEFAULT_MAX_BACKOFF,
                    retry_after: float | None = None,
                    jitter: bool = True, rng=random) -> float:
    """Backoff for a 0-based attempt: `min(base * 2**attempt, cap)`, raised to
    `retry_after` when the server asked for longer, plus optional +[0, base) jitter."""
    wait = min(base * (2 ** attempt), cap)
    if retry_after is not None:
        wait = max(wait, retry_after)
    if jitter:
        wait += rng.uniform(0, base)
    return wait


def _default_rate_limit_notice(attempt: int, max_retries: int, wait: float) -> None:
    print(f"  [rate-limited] waiting {wait:.0f}s before retry "
          f"(attempt {attempt}/{max_retries})...", file=sys.stderr)


def send_with_retry(session, method: str, url, *,
                    params=None, json=None, timeout=None,
                    throttle=None, max_retries: int = DEFAULT_MAX_RETRIES,
                    base_backoff: float = DEFAULT_BASE_BACKOFF,
                    max_backoff: float = DEFAULT_MAX_BACKOFF,
                    jitter: bool = True, on_rate_limit=None,
                    sleep=time.sleep, rng=random):
    """Send an HTTP request with throttle + 429-only retry; return the Response.

    Args:
      session:      a requests-style session (has .get/.post etc.).
      method:       "get" | "post" | ... (case-insensitive).
      throttle:     optional rate limiter invoked before EACH attempt — either a
                    Throttle instance (its .wait() is called) or a zero-arg
                    callable.
      max_retries:  number of retries after the first attempt (total = N+1).
      on_rate_limit: optional callback(attempt_number, max_retries, wait_seconds)
                    for custom progress messaging; defaults to a stderr notice.
      sleep, rng:   injectable for tests.

    Only HTTP 429 is retried. Every other response (2xx/4xx/5xx) is returned as
    is for the caller to handle.
    """
    fn = getattr(session, method.lower())
    kwargs: dict = {}
    if params is not None:
        kwargs["params"] = params
    if json is not None:
        kwargs["json"] = json
    if timeout is not None:
        kwargs["timeout"] = timeout

    # Accept either a Throttle instance (.wait) or a zero-arg callable.
    throttle_fn = None
    if throttle is not None:
        throttle_fn = throttle.wait if hasattr(throttle, "wait") else throttle

    notice = on_rate_limit or _default_rate_limit_notice
    resp = None
    for attempt in range(max_retries + 1):
        if throttle_fn is not None:
            throttle_fn()
        resp = fn(url, **kwargs)
        if resp.status_code == 429 and attempt < max_retries:
            wait = backoff_seconds(
                attempt, base=base_backoff, cap=max_backoff,
                retry_after=retry_after_seconds(resp), jitter=jitter, rng=rng,
            )
            notice(attempt + 1, max_retries, wait)
            sleep(wait)
            continue
        return resp
    return resp  # retries exhausted; caller inspects the (likely 429) response
