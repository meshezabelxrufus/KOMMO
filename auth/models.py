"""
auth/models.py
==============
Pydantic models for OAuth 2.0 token data.

TokenData is the canonical representation of Kommo credentials in memory
and on disk (encrypted). Using Pydantic ensures tokens are always
structurally valid before being persisted or used.

Key design decisions:
  - `expires_at` stores an absolute Unix timestamp (not expires_in).
    This is calculated at the moment tokens are received so expiry
    checks are always reliable regardless of when the file is read.
  - `access_token` and `refresh_token` are plain str (not SecretStr)
    because they must be serialised to encrypted storage. Encryption
    is handled separately in token_manager.py.
  - `is_expired` includes a 5-minute buffer to refresh proactively.
"""

from __future__ import annotations

import time
from pydantic import BaseModel, Field


# Pre-refresh buffer: refresh 5 minutes before actual expiry
_EXPIRY_BUFFER_SECONDS = 300


class TokenData(BaseModel):
    """
    Represents a Kommo OAuth 2.0 token pair.

    Attributes:
        access_token:   Bearer token for API requests (valid 24h).
        refresh_token:  Token used to obtain a new access token (valid 3 months).
        token_type:     Always "Bearer" for Kommo.
        expires_at:     Absolute Unix timestamp when access_token expires.
        account_domain: Kommo subdomain (e.g. "mycompany"). Stored here
                        so token file is self-contained.
    """

    access_token: str = Field(..., description="OAuth 2.0 Bearer access token")
    refresh_token: str = Field(..., description="OAuth 2.0 refresh token (single-use)")
    token_type: str = Field(default="Bearer")
    expires_at: float = Field(..., description="Unix timestamp when access_token expires")
    account_domain: str = Field(..., description="Kommo account subdomain")

    # ------------------------------------------------------------------
    # Computed helpers
    # ------------------------------------------------------------------

    @property
    def is_expired(self) -> bool:
        """
        Returns True if the access token has expired or will expire
        within the next 5 minutes (proactive refresh buffer).
        """
        return time.time() >= (self.expires_at - _EXPIRY_BUFFER_SECONDS)

    @property
    def seconds_until_expiry(self) -> float:
        """Returns seconds remaining before access token expires."""
        return max(0.0, self.expires_at - time.time())

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_api_response(
        cls,
        response: dict[str, object],
        account_domain: str,
    ) -> "TokenData":
        """
        Construct a TokenData from a raw Kommo token API response.

        The API returns `expires_in` (seconds). We convert this to an
        absolute `expires_at` timestamp immediately.

        Args:
            response:       Raw JSON dict from Kommo /oauth2/access_token
            account_domain: Kommo subdomain (for storage)

        Returns:
            Validated TokenData instance.
        """
        expires_in: int = int(response.get("expires_in", 86400))  # Default: 24h
        return cls(
            access_token=str(response["access_token"]),
            refresh_token=str(response["refresh_token"]),
            token_type=str(response.get("token_type", "Bearer")),
            expires_at=time.time() + expires_in,
            account_domain=account_domain,
        )
