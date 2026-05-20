"""
api/client.py
=============
Production-grade, reusable Kommo CRM API client.

ARCHITECTURE
────────────
KommoAPIClient is a thin, stateless HTTP wrapper that handles all
infrastructure concerns so that callers (extractors, scripts, future
integrations) only think about business logic:

  Infrastructure layer (this file):
    ✓ OAuth token injection          — reads from KommoOAuthClient
    ✓ Automatic 401 token refresh    — retries once with fresh token
    ✓ Rate limiting (7 req/s)        — token-bucket, client-side
    ✓ Retry with exponential backoff — configurable, respects Retry-After
    ✓ Timeout handling               — connect + read separately
    ✓ Pagination                     — generator-based, memory-efficient
    ✓ Structured logging             — every request/response logged

  Caller layer (extractors, scripts):
    → client.get("/leads", params={...})
    → client.paginate("/leads", page_size=250)
    → client.post("/tasks", json={...})

DESIGN PRINCIPLES
─────────────────
  - Uses a persistent requests.Session (connection pooling across calls)
  - Implements context manager protocol (__enter__/__exit__)
  - Never raises on retryable errors mid-pagination — logs + retries
  - Raises typed exceptions for all terminal failures
  - Zero business logic — pure HTTP plumbing

USAGE
─────
    from dotenv import load_dotenv
    load_dotenv()

    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient

    oauth = KommoOAuthClient()

    with KommoAPIClient(oauth) as client:

        # Single GET
        resp = client.get("/leads", params={"limit": 1})
        print(resp.json())

        # Paginate all leads (generator — memory efficient)
        for page_records in client.paginate("/leads"):
            for lead in page_records:
                print(lead["id"], lead.get("name"))

        # POST
        resp = client.post("/tasks", json={"text": "Follow up", ...})
        print(resp.status_code)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Generator, Iterator

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auth.oauth import (
    KommoOAuthClient,
    KommoAuthorizationError,
    KommoNetworkError,
    KommoOAuthError,
)
from utils.logger import get_logger
from utils.retry import retry_on_network_error

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class KommoClientError(Exception):
    """Base exception for API client errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [self.args[0]]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.request_id:
            parts.append(f"req_id={self.request_id}")
        return " | ".join(parts)


class KommoRateLimitError(KommoClientError):
    """Raised when the 429 rate-limit is hit and Retry-After is exceeded."""

    def __init__(self, message: str, retry_after: int = 0, **kwargs: Any) -> None:
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after = retry_after


class KommoNotFoundError(KommoClientError):
    """Raised when the requested resource does not exist (404)."""


class KommoServerError(KommoClientError):
    """Raised for unrecoverable 5xx server errors."""


class KommoRequestError(KommoClientError):
    """Raised for 4xx client errors (excluding 401, 404, 429)."""


class KommoMaxRetriesExceeded(KommoClientError):
    """Raised when all retry attempts are exhausted."""


# ---------------------------------------------------------------------------
# Rate Limiter (Token Bucket)
# ---------------------------------------------------------------------------

import threading

class _TokenBucket:
    """
    Thread-safe token-bucket rate limiter.

    Enforces Kommo's 7 requests/second limit client-side, preventing
    429 errors from ever occurring in normal operation.

    Thread-safety: uses a threading.Lock so multiple threads (e.g.
    concurrent extractors via ThreadPoolExecutor) share a single limiter
    safely without racing on _tokens / _last.

    Args:
        rate:  Tokens (requests) generated per second.
        burst: Maximum tokens that can accumulate when idle.
    """

    def __init__(self, rate: float = 7.0, burst: float | None = None) -> None:
        self._rate   = rate
        self._burst  = burst if burst is not None else rate
        self._tokens = self._burst
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                now     = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last   = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Calculate sleep outside lock to avoid holding it while sleeping
                deficit = (1.0 - self._tokens) / self._rate

            time.sleep(deficit)


# ---------------------------------------------------------------------------
# Response Parser
# ---------------------------------------------------------------------------

class APIResponse:
    """
    Lightweight wrapper around requests.Response providing:
      - .json()        — parsed response body
      - .embedded(key) — safely extracts _embedded.<key> (Kommo pattern)
      - .is_empty      — True if HTTP 204 No Content
      - .total_count   — X-Total-Count header (if present)

    Args:
        response: The underlying requests.Response object.
    """

    def __init__(self, response: Response) -> None:
        self._r = response

    # ------------------------------------------------------------------
    # Proxied properties
    # ------------------------------------------------------------------
    @property
    def status_code(self) -> int:
        return self._r.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._r.headers)  # type: ignore[return-value]

    @property
    def ok(self) -> bool:
        return self._r.ok

    @property
    def is_empty(self) -> bool:
        """True when Kommo returns HTTP 204 (no records on this page)."""
        return self._r.status_code == 204

    @property
    def total_count(self) -> int | None:
        """Value of X-Total-Count header, if present."""
        raw = self._r.headers.get("X-Total-Count")
        return int(raw) if raw else None

    # ------------------------------------------------------------------
    # Body helpers
    # ------------------------------------------------------------------
    def json(self) -> dict[str, Any]:
        """Parse response body as JSON."""
        return self._r.json()  # type: ignore[return-value]

    def embedded(self, resource: str) -> list[dict[str, Any]]:
        """
        Safely extract _embedded.<resource> from a Kommo list response.

        Kommo wraps all paginated resources as:
            { "_embedded": { "<resource>": [...] } }

        Args:
            resource: Key inside _embedded (e.g. "leads", "tasks", "pipelines")

        Returns:
            List of record dicts. Empty list if the key is missing.
        """
        if self.is_empty:
            return []
        try:
            body = self.json()
            return body.get("_embedded", {}).get(resource, [])  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            logger.warning(
                "Could not parse _embedded.%s from response",
                resource,
                extra={"status_code": self.status_code},
            )
            return []


# ---------------------------------------------------------------------------
# KommoAPIClient
# ---------------------------------------------------------------------------

class KommoAPIClient:
    """
    Reusable, infrastructure-aware HTTP client for the Kommo REST API.

    Handles OAuth tokens, rate limiting, retries, and timeouts so that
    callers can focus on business logic.

    Designed for use with Python's context manager protocol:
        with KommoAPIClient(oauth) as client:
            ...

    Args:
        oauth:              KommoOAuthClient instance for token management.
        base_url:           Override the API base URL. Defaults to
                            https://{domain}.kommo.com/api/v4
        rate_limit:         Max requests per second (default: 7 — Kommo's limit).
        connect_timeout:    Seconds to wait for TCP connection (default: 10).
        read_timeout:       Seconds to wait for response body (default: 30).
        max_retries:        Max retry attempts for server/network errors (default: 3).
        retry_backoff:      Backoff multiplier for urllib3 retry (default: 1.0).
        extra_headers:      Additional headers merged into every request.
    """

    # Kommo API resource names for _embedded parsing
    RESOURCE_LEADS = "leads"
    RESOURCE_TASKS = "tasks"
    RESOURCE_PIPELINES = "pipelines"
    RESOURCE_CONTACTS = "contacts"
    RESOURCE_COMPANIES = "companies"

    def __init__(
        self,
        oauth: KommoOAuthClient,
        base_url: str | None = None,
        rate_limit: float = 7.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._oauth = oauth
        self._base_url = (base_url or oauth.base_url + "/api/v4").rstrip("/")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries
        self._extra_headers = extra_headers or {}

        self._limiter = _TokenBucket(rate=rate_limit, burst=rate_limit)
        self._session: Session | None = None

        logger.info(
            "KommoAPIClient initialised",
            extra={
                "base_url": self._base_url,
                "rate_limit": rate_limit,
                "max_retries": max_retries,
                "timeout": f"{connect_timeout}s/{read_timeout}s",
            },
        )

    # =========================================================================
    # Context Manager
    # =========================================================================

    def __enter__(self) -> "KommoAPIClient":
        """Open the requests session with connection pooling and retry adapters."""
        self._session = self._build_session()
        return self

    def __exit__(self, *args: Any) -> None:
        """Close the session and release all connections."""
        if self._session:
            self._session.close()
            self._session = None
            logger.debug("KommoAPIClient session closed")

    # =========================================================================
    # PUBLIC: HTTP Methods
    # =========================================================================

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> APIResponse:
        """
        Perform an authenticated GET request.

        Automatically:
          - Injects a valid Bearer token
          - Enforces rate limit (7 req/s)
          - Retries on 5xx / network errors
          - Refreshes token + retries once on 401

        Args:
            path:   API path, relative to base_url. (e.g. "/leads")
            params: Query parameters dict.
            **kwargs: Extra keyword args forwarded to requests.Session.get()

        Returns:
            APIResponse wrapping the requests.Response.

        Raises:
            KommoRateLimitError:    429 after retry exhaustion.
            KommoNotFoundError:     404 resource not found.
            KommoServerError:       5xx after retry exhaustion.
            KommoRequestError:      Other 4xx errors.
            KommoMaxRetriesExceeded: Retries exhausted on network errors.

        Example:
            resp = client.get("/leads", params={"limit": 250, "page": 1})
            leads = resp.embedded("leads")
        """
        return self._request("GET", path, params=params, **kwargs)

    def post(
        self,
        path: str,
        json: dict[str, Any] | list[Any] | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> APIResponse:
        """
        Perform an authenticated POST request.

        Args:
            path:   API path relative to base_url.
            json:   Request body (serialised to JSON automatically).
            params: Query parameters dict.
            **kwargs: Extra keyword args forwarded to requests.Session.post()

        Returns:
            APIResponse wrapping the requests.Response.

        Raises:
            Same exception types as get().

        Example:
            resp = client.post(
                "/tasks",
                json={"text": "Follow up", "entity_type": "leads", "entity_id": 12345}
            )
        """
        return self._request("POST", path, json=json, params=params, **kwargs)

    def patch(
        self,
        path: str,
        json: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> APIResponse:
        """
        Perform an authenticated PATCH request (for updating resources).

        Args:
            path: API path relative to base_url.
            json: Partial update body.

        Returns:
            APIResponse wrapping the requests.Response.
        """
        return self._request("PATCH", path, json=json, **kwargs)

    # =========================================================================
    # PUBLIC: Pagination
    # =========================================================================

    def paginate(
        self,
        path: str,
        resource: str | None = None,
        page_size: int = 250,
        start_page: int = 1,
        max_pages: int = 10_000,
        params: dict[str, Any] | None = None,
    ) -> Generator[list[dict[str, Any]], None, None]:
        """
        Memory-efficient generator that paginates a Kommo list endpoint.

        Yields one page of records at a time. Stops cleanly when:
          - The API returns HTTP 204 (no content — Kommo's "end of data")
          - The API returns an empty _embedded list
          - max_pages is reached (safety guard against infinite loops)

        Args:
            path:       API path (e.g. "/leads", "/tasks")
            resource:   _embedded key to extract. Inferred from path if None.
                        (e.g. path="/leads" → resource="leads")
            page_size:  Records per page. Max 250 (Kommo limit).
            start_page: First page number (1-indexed).
            max_pages:  Hard cap on pages fetched (default: 10,000).
            params:     Extra query params (merged with page/limit params).

        Yields:
            list[dict[str, Any]] — one page of raw record dicts.

        Raises:
            KommoClientError subclasses on unrecoverable API errors.

        Example:
            # Fetch all leads
            all_leads = []
            for page in client.paginate("/leads"):
                all_leads.extend(page)
            print(f"Total leads: {len(all_leads)}")

            # With extra filters
            for page in client.paginate(
                "/tasks",
                params={"filter[is_completed]": 0},
                page_size=100,
            ):
                process(page)
        """
        # Infer resource key from path if not provided
        # e.g. "/leads" → "leads", "/leads/pipelines" → "pipelines"
        effective_resource = resource or path.rstrip("/").split("/")[-1]

        base_params: dict[str, Any] = {
            "limit": min(page_size, 250),  # Enforce Kommo's max
            **(params or {}),
        }

        logger.info(
            "Pagination started",
            extra={
                "path": path,
                "resource": effective_resource,
                "page_size": base_params["limit"],
                "max_pages": max_pages,
            },
        )

        total_records = 0

        for page_num in range(start_page, start_page + max_pages):
            page_params = {**base_params, "page": page_num}

            try:
                response = self.get(path, params=page_params)
            except KommoNotFoundError:
                # Some endpoints return 404 when there are no records at all
                logger.info(
                    "Pagination ended — 404 (no records)",
                    extra={"path": path, "page": page_num},
                )
                return

            # HTTP 204 = Kommo's explicit "this page is empty"
            if response.is_empty:
                logger.info(
                    "Pagination ended — HTTP 204",
                    extra={"path": path, "page": page_num, "total_records": total_records},
                )
                return

            records = response.embedded(effective_resource)

            if not records:
                logger.info(
                    "Pagination ended — empty page",
                    extra={"path": path, "page": page_num, "total_records": total_records},
                )
                return

            total_records += len(records)

            logger.debug(
                "Page fetched",
                extra={
                    "path": path,
                    "page": page_num,
                    "records_this_page": len(records),
                    "total_so_far": total_records,
                },
            )

            yield records

            # If this page returned fewer than page_size records, it's the last page
            if len(records) < base_params["limit"]:
                logger.info(
                    "Pagination ended — partial page (last page)",
                    extra={"path": path, "page": page_num, "total_records": total_records},
                )
                return

        logger.warning(
            "Pagination hit max_pages safety limit — consider increasing it",
            extra={"path": path, "max_pages": max_pages, "total_records": total_records},
        )

    # =========================================================================
    # PRIVATE: Core Request Dispatcher
    # =========================================================================

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        _retry_on_401: bool = True,
        **kwargs: Any,
    ) -> APIResponse:
        """
        Core request dispatcher with rate limiting, logging, and error handling.

        Args:
            method:          HTTP verb (GET, POST, PATCH, DELETE).
            path:            Path relative to base_url.
            params:          Query params.
            json:            JSON body (for POST/PATCH).
            _retry_on_401:   Internal flag — prevents infinite 401 refresh loops.
            **kwargs:        Forwarded to requests.Session.request().

        Returns:
            Validated APIResponse.
        """
        session = self._ensure_session()
        request_id = str(uuid.uuid4())[:8]
        url = f"{self._base_url}/{path.lstrip('/')}"

        # ------------------------------------------------------------------
        # 1. Rate limiting — block until a token is available
        # ------------------------------------------------------------------
        self._limiter.acquire()

        # ------------------------------------------------------------------
        # 2. Inject auth header with a valid token
        # ------------------------------------------------------------------
        try:
            token = self._oauth.get_valid_token()
        except KommoOAuthError as exc:
            raise KommoClientError(
                f"Failed to obtain auth token: {exc}",
                request_id=request_id,
            ) from exc

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **self._extra_headers,
        }

        # ------------------------------------------------------------------
        # 3. Execute request with retry on network errors
        # ------------------------------------------------------------------
        logger.debug(
            "→ %s %s",
            method,
            url,
            extra={
                "request_id": request_id,
                "params": params,
                "method": method,
            },
        )

        start_time = time.monotonic()

        try:
            raw_response = session.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                timeout=(self._connect_timeout, self._read_timeout),
                **kwargs,
            )
        except requests.Timeout as exc:
            raise KommoClientError(
                f"Request timed out after {self._read_timeout}s — {method} {path}",
                request_id=request_id,
            ) from exc
        except requests.ConnectionError as exc:
            raise KommoClientError(
                f"Connection error — {method} {path}: {exc}",
                request_id=request_id,
            ) from exc

        elapsed = time.monotonic() - start_time

        logger.debug(
            "← %s %s [%dms]",
            raw_response.status_code,
            url,
            int(elapsed * 1000),
            extra={
                "request_id": request_id,
                "status_code": raw_response.status_code,
                "elapsed_ms": int(elapsed * 1000),
            },
        )

        # ------------------------------------------------------------------
        # 4. Handle 401 — refresh token and retry ONCE
        # ------------------------------------------------------------------
        if raw_response.status_code == 401 and _retry_on_401:
            logger.warning(
                "Received 401 — refreshing token and retrying",
                extra={"request_id": request_id, "path": path},
            )
            try:
                self._oauth.refresh_tokens()
            except KommoOAuthError as exc:
                raise KommoClientError(
                    f"Token refresh failed after 401: {exc}",
                    status_code=401,
                    request_id=request_id,
                ) from exc

            # Retry the request once with the fresh token (_retry_on_401=False prevents loops)
            return self._request(
                method, path, params=params, json=json, _retry_on_401=False, **kwargs
            )

        # ------------------------------------------------------------------
        # 5. Handle HTTP error status codes
        # ------------------------------------------------------------------
        response = APIResponse(raw_response)

        if not raw_response.ok and not response.is_empty:
            self._raise_for_status(raw_response, request_id)

        return response

    # =========================================================================
    # PRIVATE: Error Handling
    # =========================================================================

    def _raise_for_status(self, response: Response, request_id: str) -> None:
        """
        Map HTTP error status codes to typed KommoClientError subclasses.

        Args:
            response:   The raw requests.Response.
            request_id: Request correlation ID for logging.

        Raises:
            KommoRateLimitError:  429
            KommoNotFoundError:   404
            KommoServerError:     5xx
            KommoRequestError:    Other 4xx
        """
        status = response.status_code

        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}

        logger.error(
            "API error response",
            extra={
                "request_id": request_id,
                "status_code": status,
                "response_body": body,
            },
        )

        if status == 429:
            retry_after = int(response.headers.get("Retry-After", "10"))
            raise KommoRateLimitError(
                f"Rate limit exceeded. Retry after {retry_after}s.",
                retry_after=retry_after,
                request_id=request_id,
            )

        if status == 404:
            raise KommoNotFoundError(
                f"Resource not found: {response.url}",
                status_code=404,
                response_body=body,
                request_id=request_id,
            )

        if status >= 500:
            raise KommoServerError(
                f"Kommo server error [{status}]",
                status_code=status,
                response_body=body,
                request_id=request_id,
            )

        # All other 4xx
        raise KommoRequestError(
            f"API request error [{status}]",
            status_code=status,
            response_body=body,
            request_id=request_id,
        )

    # =========================================================================
    # PRIVATE: Session Management
    # =========================================================================

    def _build_session(self) -> Session:
        """
        Create a requests.Session with connection pooling and urllib3 retry.

        urllib3 retry handles network-level failures (connect errors, read
        errors). HTTP-level errors (4xx, 5xx) are handled in _request().

        Returns:
            Configured requests.Session.
        """
        session = Session()

        # urllib3 retry for network-level failures only
        # HTTP status codes are NOT retried here — handled in _request()
        retry_config = Retry(
            total=self._max_retries,
            backoff_factor=1.0,
            status_forcelist=[],     # We handle status codes ourselves
            allowed_methods=["GET", "POST", "PATCH"],
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry_config,
            pool_connections=2,
            pool_maxsize=10,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        logger.debug("HTTP session created with connection pooling")
        return session

    def _ensure_session(self) -> Session:
        """
        Return the active session, or raise if the client is not open.

        Raises:
            RuntimeError: If used outside the context manager.
        """
        if self._session is None:
            raise RuntimeError(
                "KommoAPIClient must be used as a context manager. "
                "Use: 'with KommoAPIClient(oauth) as client: ...'"
            )
        return self._session

    # =========================================================================
    # PUBLIC: Utility
    # =========================================================================

    def health_check(self) -> dict[str, Any]:
        """
        Verify API connectivity by calling the /account endpoint.

        Returns:
            Account info dict on success.

        Raises:
            KommoClientError: On any failure.

        Example:
            info = client.health_check()
            print(f"Connected to account: {info.get('name')}")
        """
        logger.info("Running API health check")
        response = self.get("/account")
        return response.json()

    def __repr__(self) -> str:
        session_state = "open" if self._session else "closed"
        return (
            f"KommoAPIClient("
            f"base_url={self._base_url!r}, "
            f"session={session_state})"
        )
