"""
utils package
=============
Shared production utilities for the Kommo CRM integration.

Logging:
    configure_logging  — Set up rotating file + console handlers
    get_logger         — Named logger factory

Retry:
    retry_api_call          — Combined: network + 5xx + 429
    retry_on_network_error  — Connection / timeout only
    retry_on_server_error   — 5xx only
    retry_on_rate_limit     — 429 + Retry-After aware
    sleep_with_backoff      — Manual exponential backoff

State:
    StateManager       — Per-entity incremental sync state
    load_state         — Load full state dict
    save_state         — Merge + persist state updates

Exceptions:
    KommoBaseError, KommoConfigError,
    KommoAuthError, KommoTokenMissingError, KommoTokenExpiredError,
    KommoAuthorizationError,
    KommoAPIError, KommoNetworkError, KommoRateLimitError,
    KommoNotFoundError, KommoServerError, KommoRequestError,
    KommoMaxRetriesExceeded,
    KommoExtractionError, KommoValidationError, KommoOutputError
"""

from utils.logger import configure_logging, get_logger
from utils.retry import (
    retry_api_call,
    retry_on_network_error,
    retry_on_server_error,
    retry_on_rate_limit,
    sleep_with_backoff,
)
from utils.state_manager import StateManager, load_state, save_state
from utils.exceptions import (
    KommoBaseError,
    KommoConfigError,
    KommoAuthError,
    KommoTokenMissingError,
    KommoTokenExpiredError,
    KommoAuthorizationError,
    KommoAPIError,
    KommoNetworkError,
    KommoRateLimitError,
    KommoNotFoundError,
    KommoServerError,
    KommoRequestError,
    KommoMaxRetriesExceeded,
    KommoExtractionError,
    KommoValidationError,
    KommoOutputError,
)

__all__ = [
    # Logging
    "configure_logging", "get_logger",
    # Retry
    "retry_api_call", "retry_on_network_error", "retry_on_server_error",
    "retry_on_rate_limit", "sleep_with_backoff",
    # State
    "StateManager", "load_state", "save_state",
    # Exceptions
    "KommoBaseError", "KommoConfigError",
    "KommoAuthError", "KommoTokenMissingError", "KommoTokenExpiredError",
    "KommoAuthorizationError",
    "KommoAPIError", "KommoNetworkError", "KommoRateLimitError",
    "KommoNotFoundError", "KommoServerError", "KommoRequestError",
    "KommoMaxRetriesExceeded",
    "KommoExtractionError", "KommoValidationError", "KommoOutputError",
]
