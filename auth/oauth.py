"""
auth/oauth.py
=============
Production-grade Kommo OAuth 2.0 implementation.

FLOW OVERVIEW
─────────────
Step 1 — Authorization Request
    Your app redirects the user to Kommo's OAuth consent screen.
    URL: https://{domain}.kommo.com/oauth2/authorize?client_id=...&redirect_uri=...

Step 2 — Authorization Code
    Kommo redirects back to your redirect_uri with ?code=<auth_code>
    This code is short-lived (~10 minutes). Use it ONCE.

Step 3 — Token Exchange
    POST the code to Kommo's token endpoint.
    Kommo returns: access_token, refresh_token, expires_in, token_type.

Step 4 — Secure Storage
    Tokens are saved to auth/token_store.json with:
      - access_token  : Used in Authorization: Bearer header
      - refresh_token : Used to get a new access_token when it expires
      - expires_at    : Absolute Unix timestamp (time.time() + expires_in)

Step 5 — Automatic Refresh
    Before every API call, get_valid_token() checks if expires_at is
    within the next 5 minutes. If so, it silently calls refresh_tokens()
    and re-saves before returning the fresh access_token.

    ⚠️  CRITICAL: Kommo refresh tokens are SINGLE-USE.
    After a refresh, the old refresh_token is immediately invalidated.
    The new token pair is always saved to disk before use.

USAGE
─────
    from auth.oauth import KommoOAuthClient

    client = KommoOAuthClient()

    # One-time setup — exchange auth code
    client.exchange_code_for_tokens(code="abc123")

    # Every API call — always use this, never access tokens directly
    access_token = client.get_valid_token()

    # Check token health
    info = client.token_info()
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from requests import Response

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Refresh proactively 5 minutes before actual expiry
_REFRESH_BUFFER_SECONDS: int = 300

# Kommo access token lifetime in seconds (24 hours)
_DEFAULT_EXPIRES_IN: int = 86_400


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class KommoOAuthError(Exception):
    """Base exception for all OAuth-related errors."""

    def __init__(self, message: str, status_code: int | None = None, response_body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        parts = [self.args[0]]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        return " | ".join(parts)


class KommoTokenMissingError(KommoOAuthError):
    """Raised when no stored token is found. Run authorization first."""


class KommoTokenExpiredError(KommoOAuthError):
    """Raised when both access and refresh tokens have expired."""


class KommoAuthorizationError(KommoOAuthError):
    """Raised when Kommo rejects an authorization code or refresh token."""


class KommoNetworkError(KommoOAuthError):
    """Raised for network-level failures (timeouts, connection refused)."""


# ---------------------------------------------------------------------------
# KommoOAuthClient
# ---------------------------------------------------------------------------

class KommoOAuthClient:
    """
    Manages the full Kommo OAuth 2.0 lifecycle:
      - Authorization URL generation
      - Authorization code → token exchange
      - Token persistence (JSON file)
      - Automatic token refresh on expiry

    All sensitive values are loaded exclusively from environment variables.
    Token data is persisted to a JSON file (path configurable via env var).

    Args:
        token_store_path: Override the default path for token_store.json.
                          Defaults to the value of TOKEN_STORE_PATH env var,
                          or "auth/token_store.json" if unset.

    Environment Variables Required:
        KOMMO_CLIENT_ID       : OAuth application Client ID
        KOMMO_CLIENT_SECRET   : OAuth application Client Secret
        KOMMO_REDIRECT_URI    : Registered redirect URI (must match exactly)
        KOMMO_SUBDOMAIN       : Your Kommo subdomain (e.g. "mycompany" for mycompany.kommo.com)
                                 Alias: KOMMO_ACCOUNT_DOMAIN (accepted for backward compatibility)

    Environment Variables Optional:
        TOKEN_STORE_PATH      : Path to token JSON file (default: auth/token_store.json)
    """

    def __init__(self, token_store_path: str | Path | None = None) -> None:
        # ------------------------------------------------------------------
        # Load configuration from environment
        # ------------------------------------------------------------------
        self.client_id: str = self._require_env("KOMMO_CLIENT_ID")
        self.client_secret: str = self._require_env("KOMMO_CLIENT_SECRET")
        self.redirect_uri: str = self._require_env("KOMMO_REDIRECT_URI")
        # Support both KOMMO_SUBDOMAIN (preferred) and KOMMO_ACCOUNT_DOMAIN (legacy)
        self.account_domain: str = (
            os.environ.get("KOMMO_SUBDOMAIN", "").strip()
            or os.environ.get("KOMMO_ACCOUNT_DOMAIN", "").strip()
        )
        if not self.account_domain:
            raise EnvironmentError(
                "Required environment variable 'KOMMO_SUBDOMAIN' is not set or is empty. "
                "Set KOMMO_SUBDOMAIN=yoursubdomain in your .env file."
            )

        # ------------------------------------------------------------------
        # Derived URLs (computed from domain — never hardcoded)
        # ------------------------------------------------------------------
        self.base_url: str = f"https://{self.account_domain}.kommo.com"
        self.auth_url: str = f"{self.base_url}/oauth2/authorize"
        self.token_url: str = f"{self.base_url}/oauth2/access_token"

        # ------------------------------------------------------------------
        # Token file path
        # ------------------------------------------------------------------
        _default_path = os.environ.get("TOKEN_STORE_PATH", "auth/token_store.json")
        self._token_path: Path = Path(token_store_path or _default_path)

        logger.info(
            "KommoOAuthClient initialised",
            extra={
                "account_domain": self.account_domain,
                "token_store": str(self._token_path),
            },
        )

    # =========================================================================
    # PUBLIC: Authorization Flow
    # =========================================================================

    def get_authorization_url(self, state: str | None = None) -> str:
        """
        Build the Kommo OAuth 2.0 authorization URL.

        Direct your user's browser to this URL. After granting access,
        Kommo will redirect to `redirect_uri?code=<auth_code>`.

        Args:
            state: Optional CSRF token. Recommended for production.
                   If provided, verify it matches in the callback.

        Returns:
            Full authorization URL string.

        Example:
            >>> url = client.get_authorization_url(state="random-csrf-token")
            >>> print(url)
            https://mycompany.kommo.com/oauth2/authorize?client_id=...
        """
        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
        }
        if state:
            params["state"] = state

        url = f"{self.auth_url}?{urlencode(params)}"
        logger.info("Authorization URL generated", extra={"url": url})
        return url

    def exchange_code_for_tokens(self, code: str) -> dict[str, Any]:
        """
        Exchange an authorization code for access + refresh tokens.

        Call this ONCE after the user completes the OAuth consent screen.
        Tokens are automatically saved to the token store file.

        Args:
            code: The `code` query parameter from Kommo's redirect callback.

        Returns:
            Saved token dict with keys:
              access_token, refresh_token, expires_at, token_type, account_domain

        Raises:
            KommoAuthorizationError: Kommo rejected the code (expired, invalid, already used).
            KommoNetworkError:       Network failure during the request.

        Example:
            >>> tokens = client.exchange_code_for_tokens(code="abc123xyz")
            >>> print(tokens["expires_at"])
            1718000000.0
        """
        logger.info("Exchanging authorization code for tokens")

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }

        response = self._post_to_token_endpoint(payload)
        token_data = self._parse_and_save_token_response(response)

        logger.info(
            "Token exchange successful",
            extra={"expires_at": token_data["expires_at"]},
        )
        return token_data

    # =========================================================================
    # PUBLIC: Token Access
    # =========================================================================

    def get_valid_token(self) -> str:
        """
        Return a valid access token, refreshing automatically if needed.

        This is the ONLY method API callers should use to obtain a token.
        It handles expiry detection and silent refresh transparently.

        Returns:
            A valid access token string.

        Raises:
            KommoTokenMissingError:  No token file found (run exchange first).
            KommoTokenExpiredError:  Refresh token itself has expired.
            KommoAuthorizationError: Kommo rejected the refresh token.

        Example:
            >>> token = client.get_valid_token()
            >>> headers = {"Authorization": f"Bearer {token}"}
        """
        token_data = self._load_tokens()

        if self._is_token_expired(token_data):
            logger.info(
                "Access token expired or near expiry — refreshing",
                extra={"expires_at": token_data.get("expires_at")},
            )
            token_data = self.refresh_tokens(token_data)

        return token_data["access_token"]

    def token_info(self) -> dict[str, Any]:
        """
        Return a safe summary of the current token status.

        Never returns the actual token values — only metadata.

        Returns:
            Dict with: is_expired, expires_at, seconds_until_expiry,
                       has_refresh_token, account_domain

        Raises:
            KommoTokenMissingError: No token file found.

        Example:
            >>> info = client.token_info()
            >>> print(f"Expires in: {info['seconds_until_expiry']:.0f}s")
        """
        token_data = self._load_tokens()
        expires_at: float = token_data.get("expires_at", 0.0)
        seconds_left = max(0.0, expires_at - time.time())

        return {
            "account_domain": token_data.get("account_domain"),
            "is_expired": self._is_token_expired(token_data),
            "expires_at": expires_at,
            "seconds_until_expiry": seconds_left,
            "has_refresh_token": bool(token_data.get("refresh_token")),
            "token_type": token_data.get("token_type", "Bearer"),
        }

    def tokens_exist(self) -> bool:
        """Return True if a token store file exists on disk."""
        return self._token_path.exists()

    # =========================================================================
    # PUBLIC: Token Refresh
    # =========================================================================

    def refresh_tokens(self, token_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Obtain a new access token using the stored refresh token.

        Can be called manually to force a refresh, or is called automatically
        by get_valid_token() when the access token is near expiry.

        ⚠️  Kommo refresh tokens are SINGLE-USE.
            The new token pair is ALWAYS saved to disk before returning.
            Never use the old refresh_token after calling this method.

        Args:
            token_data: Existing token dict. If None, loads from disk.

        Returns:
            New token dict with fresh access_token and refresh_token.

        Raises:
            KommoTokenMissingError:  No token data found (run exchange first).
            KommoAuthorizationError: Kommo rejected the refresh token
                                     (likely expired after 3 months).
            KommoNetworkError:       Network failure.

        Example:
            >>> new_tokens = client.refresh_tokens()
            >>> print("Refreshed. New expiry:", new_tokens["expires_at"])
        """
        if token_data is None:
            token_data = self._load_tokens()

        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise KommoTokenMissingError(
                "No refresh token found in token store. "
                "Please re-run the authorization flow."
            )

        logger.info("Refreshing access token using refresh token")

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": self.redirect_uri,
        }

        response = self._post_to_token_endpoint(payload)

        # ⚠️  Save immediately — the old refresh_token is NOW invalidated
        new_token_data = self._parse_and_save_token_response(response)

        logger.info(
            "Token refresh successful",
            extra={"new_expires_at": new_token_data["expires_at"]},
        )
        return new_token_data

    # =========================================================================
    # PRIVATE: Token Validation
    # =========================================================================

    def _is_token_expired(self, token_data: dict[str, Any]) -> bool:
        """
        Check whether the access token has expired or will expire soon.

        Uses a 5-minute buffer to refresh proactively, preventing API calls
        from failing mid-flight due to token expiry.

        Args:
            token_data: Token dict with 'expires_at' Unix timestamp.

        Returns:
            True if the token should be refreshed.
        """
        expires_at: float = float(token_data.get("expires_at", 0))
        return time.time() >= (expires_at - _REFRESH_BUFFER_SECONDS)

    # =========================================================================
    # PRIVATE: Token Persistence
    # =========================================================================

    def _load_tokens(self) -> dict[str, Any]:
        """
        Load and parse token data from the JSON file.

        Returns:
            Token dict with access_token, refresh_token, expires_at, etc.

        Raises:
            KommoTokenMissingError: File does not exist or is unreadable.
        """
        if not self._token_path.exists():
            raise KommoTokenMissingError(
                f"Token store not found at '{self._token_path}'. "
                "Please run the authorization flow first (run_auth.py)."
            )

        try:
            raw = self._token_path.read_text(encoding="utf-8")
            data: dict[str, Any] = json.loads(raw)
            logger.debug(
                "Tokens loaded from disk",
                extra={"path": str(self._token_path)},
            )
            return data
        except (json.JSONDecodeError, OSError) as exc:
            raise KommoTokenMissingError(
                f"Failed to read token store at '{self._token_path}': {exc}"
            ) from exc

    def _save_tokens(self, token_data: dict[str, Any]) -> None:
        """
        Atomically write token data to the JSON file.

        Uses a temp-file + rename pattern so a crash during write never
        leaves a corrupted (partially written) token file.

        Args:
            token_data: Token dict to persist.
        """
        # Ensure parent directory exists
        self._token_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = self._token_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(token_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            # Atomic rename — POSIX guarantees this is atomic
            tmp_path.replace(self._token_path)
            logger.debug(
                "Tokens saved to disk",
                extra={"path": str(self._token_path)},
            )
        except OSError as exc:
            # Clean up temp file if rename failed
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise KommoOAuthError(
                f"Failed to save tokens to '{self._token_path}': {exc}"
            ) from exc

    # =========================================================================
    # PRIVATE: HTTP
    # =========================================================================

    def _post_to_token_endpoint(self, payload: dict[str, str]) -> Response:
        """
        POST to Kommo's token endpoint with proper error handling.

        Args:
            payload: Form data dict for the token request.

        Returns:
            requests.Response with a 2xx status code.

        Raises:
            KommoAuthorizationError: 4xx response from Kommo.
            KommoNetworkError:       Timeout or connection failure.
        """
        logger.debug(
            "POST to token endpoint",
            extra={"url": self.token_url, "grant_type": payload.get("grant_type")},
        )

        try:
            response = requests.post(
                url=self.token_url,
                json=payload,                     # Kommo expects JSON body
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=30,
            )
        except requests.Timeout as exc:
            raise KommoNetworkError(
                f"Token endpoint timed out after 30s: {exc}"
            ) from exc
        except requests.ConnectionError as exc:
            raise KommoNetworkError(
                f"Failed to connect to Kommo token endpoint: {exc}"
            ) from exc

        # Handle error responses
        if not response.ok:
            self._raise_for_token_error(response)

        return response

    def _raise_for_token_error(self, response: Response) -> None:
        """
        Parse a non-2xx token response and raise an appropriate exception.

        Args:
            response: The failed requests.Response.

        Raises:
            KommoAuthorizationError: Always (this method never returns normally).
        """
        try:
            body = response.json()
            hint = body.get("hint") or body.get("message") or body.get("error_description", "")
            error_code = body.get("error", "unknown_error")
        except ValueError:
            body = response.text
            hint = ""
            error_code = "unparseable_response"

        logger.error(
            "Token endpoint returned error",
            extra={
                "status_code": response.status_code,
                "error": error_code,
                "hint": hint,
            },
        )

        raise KommoAuthorizationError(
            message=f"Kommo token error [{error_code}]: {hint or 'No details provided'}",
            status_code=response.status_code,
            response_body=body,
        )

    def _parse_and_save_token_response(self, response: Response) -> dict[str, Any]:
        """
        Parse a successful token response and save it to disk.

        Converts `expires_in` (relative seconds) → `expires_at` (absolute
        Unix timestamp) so expiry checks are reliable at any future point.

        Args:
            response: Successful requests.Response from the token endpoint.

        Returns:
            Normalised token dict saved to disk.
        """
        try:
            data = response.json()
        except ValueError as exc:
            raise KommoOAuthError(
                "Kommo returned non-JSON response from token endpoint"
            ) from exc

        expires_in: int = int(data.get("expires_in", _DEFAULT_EXPIRES_IN))
        expires_at: float = time.time() + expires_in

        token_data: dict[str, Any] = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "token_type": data.get("token_type", "Bearer"),
            "expires_at": expires_at,         # ← absolute timestamp
            "expires_in_original": expires_in,
            "account_domain": self.account_domain,
            "saved_at": time.time(),
        }

        # ⚠️  Save BEFORE returning — especially critical on refresh
        self._save_tokens(token_data)
        return token_data

    # =========================================================================
    # PRIVATE: Utilities
    # =========================================================================

    @staticmethod
    def _require_env(key: str) -> str:
        """
        Read a required environment variable or raise immediately.

        Args:
            key: Environment variable name.

        Returns:
            The value of the environment variable.

        Raises:
            EnvironmentError: If the variable is not set or is empty.
        """
        value = os.environ.get(key, "").strip()
        if not value:
            raise EnvironmentError(
                f"Required environment variable '{key}' is not set or is empty. "
                f"Ensure it is defined in your .env file."
            )
        return value
