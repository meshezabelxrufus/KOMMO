"""
utils/rate_limiter.py
=====================
Token-bucket rate limiter enforcing Kommo's 7 requests/second API limit.

Why client-side limiting?
  - Kommo's 429 errors incur penalty delays and consume retry budget.
  - Proactive limiting keeps extraction smooth and predictable.
  - Thread-safe implementation for potential parallel extractors (M2+).

Usage:
    from utils.rate_limiter import RateLimiter

    limiter = RateLimiter(rate=7)   # 7 requests per second

    def make_request():
        limiter.acquire()           # Blocks until a token is available
        return httpx_client.get(...)

    # Or use as a context manager:
    with limiter:
        response = httpx_client.get(...)
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """
    Thread-safe token-bucket rate limiter.

    Args:
        rate: Maximum number of requests allowed per second.
        burst: Maximum burst size (defaults to `rate`). Controls
               how many tokens can accumulate when idle.
    """

    def __init__(self, rate: int = 7, burst: int | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")

        self.rate = rate
        self.burst = burst if burst is not None else rate

        self._tokens: float = float(self.burst)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public Interface
    # ------------------------------------------------------------------

    def acquire(self, tokens: int = 1) -> None:
        """
        Block until `tokens` tokens are available, then consume them.

        Args:
            tokens: Number of tokens to consume (default: 1 per request).
        """
        with self._lock:
            self._refill()
            while self._tokens < tokens:
                # Calculate how long to sleep until enough tokens arrive
                deficit = tokens - self._tokens
                sleep_time = deficit / self.rate
                self._lock.release()
                try:
                    time.sleep(sleep_time)
                finally:
                    self._lock.acquire()
                self._refill()
            self._tokens -= tokens

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        pass  # Nothing to release — tokens are already consumed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.rate
        self._tokens = min(self.burst, self._tokens + new_tokens)
        self._last_refill = now
