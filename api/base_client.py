"""
api/base_client.py
==================
Generic HTTP client wrapping httpx.Client.

Responsibilities:
  - Manages a persistent httpx.Client session (connection pooling)
  - Injects Authorization: Bearer header on every request
  - Handles 401 Unauthorized → triggers token refresh → retries once
  - Enforces client-side rate limiting (7 req/s via RateLimiter)
  - Applies global timeout settings
  - Logs all requests at DEBUG level (no sensitive data)
  - Raises typed KommoAPIError subclasses for HTTP errors

Usage:
    from api.base_client import BaseClient
    from auth.token_manager import TokenManager
    from config import settings
    from utils.rate_limiter import RateLimiter

    token_mgr = TokenManager(settings)
    limiter = RateLimiter(rate=settings.kommo_rate_limit_per_second)

    with BaseClient(settings, token_mgr, limiter) as client:
        response = client.get("/leads", params={"page": 1, "limit": 250})
"""

from __future__ import annotations

from typing import Any

import httpx

from auth.token_manager import TokenManager
from config.settings import Settings
from utils.exceptions import (
    KommoAPIError,
    KommoNotFoundError,
    KommoRateLimitError,
    KommoServerError,
)
from utils.logger import get_logger
from utils.rate_limiter import RateLimiter

log = get_logger(__name__)


class BaseClient:
    """
    Thread-safe HTTP client for the Kommo API.

    Implements context manager protocol for proper resource cleanup.

    Args:
        settings:    Application settings (base_url, timeouts, etc.)
        token_mgr:   TokenManager for injecting + refreshing auth tokens.
        limiter:     RateLimiter enforcing Kommo's 7 req/s limit.
    """

    def __init__(
        self,
        settings: Settings,
        token_mgr: TokenManager,
        limiter: RateLimiter,
    ) -> None:
        self._settings = settings
        self._token_mgr = token_mgr
        self._limiter = limiter
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------
    # Context Manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "BaseClient":
        self._client = httpx.Client(
            base_url=self._settings.kommo_base_url,
            timeout=httpx.Timeout(
                connect=10.0,
                read=float(self._settings.kommo_request_timeout_seconds),
                write=10.0,
                pool=5.0,
            ),
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
        )
        log.debug("HTTP client session opened", base_url=self._settings.kommo_base_url)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._client:
            self._client.close()
            log.debug("HTTP client session closed")

    # ------------------------------------------------------------------
    # Public HTTP Methods
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """
        Perform a rate-limited, authenticated GET request.

        Args:
            path:   API path relative to base URL (e.g. "/leads")
            params: Query parameters dict.

        Returns:
            httpx.Response (already validated — no 4xx/5xx will be returned)

        Raises:
            KommoRateLimitError:  API returned 429.
            KommoNotFoundError:   API returned 404.
            KommoServerError:     API returned 5xx.
            KommoAPIError:        Other non-2xx responses.
        """
        # TODO: Implement GET with:
        # 1. self._limiter.acquire()
        # 2. Inject Authorization header
        # 3. Execute request
        # 4. Handle 401 → refresh token → retry once
        # 5. _raise_for_status(response)
        # 6. Return response
        raise NotImplementedError("BaseClient.get — to be implemented in Phase 3")

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_auth_header(self) -> dict[str, str]:
        """Build the Authorization header using the current valid access token."""
        # TODO: Call self._token_mgr.get_valid_access_token()
        raise NotImplementedError

    def _raise_for_status(self, response: httpx.Response) -> None:
        """
        Inspect the response status and raise an appropriate typed exception.

        Args:
            response: httpx.Response to inspect.

        Raises:
            KommoRateLimitError: 429
            KommoNotFoundError:  404
            KommoServerError:    5xx
            KommoAPIError:       All other non-2xx
        """
        # TODO: Implement status code → exception mapping
        raise NotImplementedError
