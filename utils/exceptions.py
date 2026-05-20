"""
utils/exceptions.py
===================
Centralised exception hierarchy for the Kommo CRM integration.

HIERARCHY
─────────
  KommoBaseError
  ├── KommoConfigError          — Missing or invalid configuration
  ├── KommoAuthError
  │   ├── KommoTokenMissingError — Token file absent / unreadable
  │   ├── KommoTokenExpiredError  — Token expired and refresh failed
  │   └── KommoAuthorizationError — Kommo rejected the auth code / credentials
  ├── KommoAPIError
  │   ├── KommoNetworkError      — Connection, timeout, DNS failure
  │   ├── KommoRateLimitError    — 429 Too Many Requests
  │   ├── KommoNotFoundError     — 404 resource missing
  │   ├── KommoServerError       — 5xx Kommo server error
  │   ├── KommoRequestError      — Other 4xx client errors
  │   └── KommoMaxRetriesExceeded — All retry attempts exhausted
  └── KommoExtractionError
      ├── KommoValidationError   — Pydantic model validation failure
      └── KommoOutputError       — Failed to write output file

USAGE
─────
    from utils.exceptions import KommoAPIError, KommoNetworkError

    try:
        client.get("/leads")
    except KommoNetworkError:
        log.error("Network failure")
    except KommoAPIError as e:
        log.error("API error %s", e.status_code)
"""

from __future__ import annotations

from typing import Any


# =============================================================================
# Base
# =============================================================================

class KommoBaseError(Exception):
    """Root exception for all Kommo integration errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context

    def __str__(self) -> str:
        base = self.args[0]
        if self.context:
            ctx = " | ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base} [{ctx}]"
        return base


# =============================================================================
# Config errors
# =============================================================================

class KommoConfigError(KommoBaseError):
    """Missing or invalid configuration (env vars, settings)."""


# =============================================================================
# Auth errors
# =============================================================================

class KommoAuthError(KommoBaseError):
    """Base for all authentication and authorization errors."""


class KommoTokenMissingError(KommoAuthError):
    """Token store file does not exist or could not be read."""


class KommoTokenExpiredError(KommoAuthError):
    """Token has expired and automatic refresh has failed."""


class KommoAuthorizationError(KommoAuthError):
    """Kommo rejected the authorization code or credentials."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, **context)
        self.status_code = status_code
        self.error_code  = error_code


# =============================================================================
# API / HTTP errors
# =============================================================================

class KommoAPIError(KommoBaseError):
    """Base for all API communication errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: Any = None,
        request_id: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, **context)
        self.status_code   = status_code
        self.response_body = response_body
        self.request_id    = request_id

    def __str__(self) -> str:
        parts = [self.args[0]]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.request_id:
            parts.append(f"req={self.request_id}")
        return " | ".join(parts)


class KommoNetworkError(KommoAPIError):
    """Connection failure, timeout, or DNS resolution error."""


class KommoRateLimitError(KommoAPIError):
    """HTTP 429 — rate limit exceeded."""

    def __init__(
        self,
        message: str,
        retry_after: int = 10,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after = retry_after


class KommoNotFoundError(KommoAPIError):
    """HTTP 404 — requested resource does not exist."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, status_code=404, **kwargs)


class KommoServerError(KommoAPIError):
    """HTTP 5xx — Kommo server-side error (retryable)."""


class KommoRequestError(KommoAPIError):
    """HTTP 4xx — client-side request error (not retryable)."""


class KommoMaxRetriesExceeded(KommoAPIError):
    """All configured retry attempts have been exhausted."""

    def __init__(self, message: str, attempts: int = 0, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.attempts = attempts


# =============================================================================
# Extraction errors
# =============================================================================

class KommoExtractionError(KommoBaseError):
    """Base for errors occurring during data extraction."""


class KommoValidationError(KommoExtractionError):
    """
    A record failed Pydantic model validation.

    Note: Individual record failures do NOT raise this exception in
    normal operation — they are routed to dead-letter files instead.
    This exception is raised only for structural/schema failures that
    affect the entire extraction (e.g. completely unexpected API shape).
    """

    def __init__(
        self,
        message: str,
        entity: str | None = None,
        record_id: Any = None,
        errors: list | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, **context)
        self.entity    = entity
        self.record_id = record_id
        self.errors    = errors or []


class KommoOutputError(KommoExtractionError):
    """Failed to write output file (disk full, permission denied, etc.)."""

    def __init__(
        self,
        message: str,
        path: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, **context)
        self.path = path
