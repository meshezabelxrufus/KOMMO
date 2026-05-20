"""
api package
===========
HTTP client layer for the Kommo REST API.

Primary export:
  KommoAPIClient — reusable, rate-limited, auto-refreshing API client

Usage:
    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient

    oauth = KommoOAuthClient()

    with KommoAPIClient(oauth) as client:
        resp = client.get("/leads", params={"limit": 1})
        for page in client.paginate("/leads"):
            ...
"""

from api.client import (
    KommoAPIClient,
    APIResponse,
    KommoClientError,
    KommoNotFoundError,
    KommoRateLimitError,
    KommoRequestError,
    KommoServerError,
    KommoMaxRetriesExceeded,
)

__all__ = [
    "KommoAPIClient",
    "APIResponse",
    "KommoClientError",
    "KommoNotFoundError",
    "KommoRateLimitError",
    "KommoRequestError",
    "KommoServerError",
    "KommoMaxRetriesExceeded",
]
