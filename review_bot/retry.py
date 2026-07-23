"""HTTP retry with exponential backoff.

Transient failures (a vLLM pod restarting, a GitLab 502, a rate limit) used to
fail the whole job. Because the CI job is declared `allow_failure: true`, that
surfaced as no review at all rather than as a visible error.

Retrying a POST is not automatically safe: if a request reached the server and
only the response was lost, replaying it posts a duplicate comment. So callers
declare whether the request is idempotent, and non-idempotent requests are only
replayed when the server clearly refused to process them (429/503) or the
connection failed before a response existed.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

log = logging.getLogger("review_bot.retry")

# Safe to replay for any request: the server said it did not handle this one.
_REFUSED_STATUSES = frozenset({429, 503})
# Additionally replayable when the request is idempotent.
_TRANSIENT_STATUSES = frozenset({500, 502, 504})

MAX_BACKOFF_SECONDS = 30.0


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; fall back to computed backoff


def _sleep_for(attempt: int, response: Optional[requests.Response]) -> float:
    if response is not None:
        advised = _retry_after_seconds(response)
        if advised is not None:
            return min(advised, MAX_BACKOFF_SECONDS)
    # Exponential backoff with jitter, so parallel jobs do not retry in lockstep.
    return min(2.0**attempt, MAX_BACKOFF_SECONDS) * (0.5 + random.random() / 2)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    idempotent: bool,
    attempts: int = 4,
    **kwargs: Any,
) -> requests.Response:
    """Perform an HTTP request, retrying transient failures.

    Returns the final response, including an error response once retries are
    exhausted; the caller still decides how to treat its status code.
    """
    retryable = _REFUSED_STATUSES | (_TRANSIENT_STATUSES if idempotent else frozenset())
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            response = session.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            # A connection error means no response was produced. A read timeout
            # on a non-idempotent call might still have been applied server
            # side, so only replay those when the call is idempotent.
            replayable = idempotent or isinstance(exc, requests.ConnectionError)
            if is_last or not replayable:
                raise
            last_error = exc
            delay = _sleep_for(attempt, None)
            log.warning(
                "%s %s failed (%s), retrying in %.1fs",
                method,
                url,
                type(exc).__name__,
                delay,
                extra={"fields": {"attempt": attempt + 1, "attempts": attempts}},
            )
            time.sleep(delay)
            continue

        if response.status_code in retryable and not is_last:
            delay = _sleep_for(attempt, response)
            log.warning(
                "%s %s returned HTTP %d, retrying in %.1fs",
                method,
                url,
                response.status_code,
                delay,
                extra={
                    "fields": {
                        "attempt": attempt + 1,
                        "attempts": attempts,
                        "status": response.status_code,
                    }
                },
            )
            time.sleep(delay)
            continue

        return response

    # Only reachable if the loop exhausted on an exception path.
    raise last_error if last_error else RuntimeError("retry loop exhausted")
