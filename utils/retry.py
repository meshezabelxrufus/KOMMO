"""
utils/retry.py
==============
Production-grade retry decorators using Tenacity with exponential backoff.

DECORATORS
──────────
  @retry_on_network_error  — ConnectionError, Timeout, OSError
  @retry_on_server_error   — KommoServerError (5xx)
  @retry_on_rate_limit     — KommoRateLimitError (429), respects Retry-After
  @retry_api_call          — Combined: all of the above (use this by default)

BACKOFF SCHEDULE (defaults)
────────────────────────────
  Attempt 1 → immediate
  Attempt 2 → ~1s  + jitter
  Attempt 3 → ~2s  + jitter
  Attempt 4 → ~4s  + jitter
  Give up   → raises last exception

USAGE
─────
    from utils.retry import retry_api_call

    @retry_api_call
    def fetch_leads(client, page):
        return client.get("/leads", params={"page": page})
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_INITIAL_WAIT = 1.0
_MAX_WAIT     = 60.0
_MULTIPLIER   = 2.0


# ---------------------------------------------------------------------------
# Before-sleep callback
# ---------------------------------------------------------------------------

def _log_retry(retry_state: RetryCallState) -> None:
    exc      = retry_state.outcome.exception() if retry_state.outcome else None
    nxt      = retry_state.next_action.sleep if retry_state.next_action else 0.0
    fn_name  = getattr(retry_state.fn, "__name__", "unknown")
    log.warning(
        "Retry %d/%d — %s — sleeping %.1fs — %s",
        retry_state.attempt_number,
        _MAX_ATTEMPTS,
        fn_name,
        nxt,
        type(exc).__name__ if exc else "no exception",
        extra={
            "retry_attempt": retry_state.attempt_number,
            "retry_sleep_s": round(nxt, 2),
            "exc_type":      type(exc).__name__ if exc else None,
        },
    )


def _log_exhausted(retry_state: RetryCallState) -> None:
    exc     = retry_state.outcome.exception() if retry_state.outcome else None
    fn_name = getattr(retry_state.fn, "__name__", "unknown")
    log.error(
        "All %d retries exhausted for %s — %s",
        retry_state.attempt_number,
        fn_name,
        repr(exc) if exc else "unknown error",
        extra={"retries_exhausted": retry_state.attempt_number},
    )


# ---------------------------------------------------------------------------
# Shared backoff wait
# ---------------------------------------------------------------------------

def _exponential_wait(retry_state: RetryCallState) -> float:
    attempt = retry_state.attempt_number
    backoff = min(_MAX_WAIT, _INITIAL_WAIT * (_MULTIPLIER ** attempt))
    return backoff + random.uniform(0, 1.0)


def _rate_limit_wait(retry_state: RetryCallState) -> float:
    """Prefer Retry-After header; fall back to exponential backoff."""
    from api.client import KommoRateLimitError
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, KommoRateLimitError) and getattr(exc, "retry_after", 0):
        wait = min(exc.retry_after, _MAX_WAIT)
        log.warning("Rate limited — honouring Retry-After: %ds", wait)
        return wait
    return _exponential_wait(retry_state)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def retry_on_network_error(func):
    """Retry on transient network failures (connection reset, timeout, DNS)."""
    return retry(
        retry=retry_if_exception_type((
            requests.ConnectionError,
            requests.Timeout,
            ConnectionResetError,
            TimeoutError,
            OSError,
        )),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=wait_exponential_jitter(initial=_INITIAL_WAIT, max=_MAX_WAIT,
                                     exp_base=_MULTIPLIER, jitter=1.0),
        before_sleep=_log_retry,
        retry_error_callback=_log_exhausted,
    )(func)


def retry_on_server_error(func):
    """Retry on Kommo 5xx server errors."""
    from api.client import KommoServerError
    return retry(
        retry=retry_if_exception_type(KommoServerError),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=wait_exponential_jitter(initial=_INITIAL_WAIT, max=_MAX_WAIT,
                                     exp_base=_MULTIPLIER, jitter=1.0),
        before_sleep=_log_retry,
        retry_error_callback=_log_exhausted,
    )(func)


def retry_on_rate_limit(func):
    """Retry on 429 — respects Retry-After header."""
    from api.client import KommoRateLimitError
    return retry(
        retry=retry_if_exception_type(KommoRateLimitError),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=_rate_limit_wait,
        before_sleep=_log_retry,
        retry_error_callback=_log_exhausted,
    )(func)


def retry_api_call(func):
    """
    Combined retry decorator: network errors + 5xx + 429.
    Use this as the default on any function that calls the Kommo API.
    """
    from api.client import KommoServerError, KommoRateLimitError
    return retry(
        retry=retry_if_exception_type((
            requests.ConnectionError,
            requests.Timeout,
            ConnectionResetError,
            TimeoutError,
            OSError,
            KommoServerError,
            KommoRateLimitError,
        )),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=_rate_limit_wait,
        before_sleep=_log_retry,
        retry_error_callback=_log_exhausted,
    )(func)


# ---------------------------------------------------------------------------
# Manual backoff helper
# ---------------------------------------------------------------------------

def sleep_with_backoff(attempt: int, base: float = _INITIAL_WAIT,
                       cap: float = _MAX_WAIT, multiplier: float = _MULTIPLIER) -> float:
    """
    Sleep for exponentially backed-off + jitter duration. Returns actual sleep time.

    Use in manual loops where decorators aren't applicable.

    Example:
        for attempt in range(3):
            try:
                do_thing()
                break
            except SomeError:
                slept = sleep_with_backoff(attempt)
    """
    sleep_s = min(cap, base * (multiplier ** attempt)) + random.uniform(0, 1)
    time.sleep(sleep_s)
    return sleep_s
