"""
auth/token_manager.py
=====================
Encrypted token persistence — load, save, and auto-refresh.

Responsibilities:
  - Encrypt token data using Fernet (AES-128) before writing to disk
  - Decrypt and validate tokens on load
  - Detect token expiry and trigger refresh automatically
  - Write atomically (temp file → rename) to prevent corruption
  - Ensure the refresh token (single-use) is ALWAYS persisted before
    returning a refreshed access token

Security properties:
  - Token file encrypted with a key stored only in .env
  - File written with restricted permissions (0o600)
  - Tokens never logged (only expiry timestamps are logged)

Usage:
    from auth.token_manager import TokenManager
    from config import settings

    manager = TokenManager(settings)
    access_token = manager.get_valid_access_token()
    # Returns a fresh access token, refreshing automatically if needed
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from auth.models import TokenData
from config.settings import Settings
from utils.exceptions import KommoAuthError, KommoTokenExpiredError
from utils.logger import get_logger

log = get_logger(__name__)


class TokenManager:
    """
    Manages encrypted token storage and automatic refresh.

    Args:
        settings: Application settings (token_file_path, encryption_key, etc.)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_file = settings.token_file_path
        self._fernet = Fernet(
            settings.token_encryption_key.get_secret_value().encode()
        )

    # ------------------------------------------------------------------
    # Public Interface
    # ------------------------------------------------------------------

    def get_valid_access_token(self) -> str:
        """
        Return a valid access token, refreshing automatically if expired.

        This is the primary method used by the API client.

        Returns:
            A valid access token string.

        Raises:
            KommoAuthError:         Token file missing (run run_auth.py first).
            KommoTokenExpiredError: Both access and refresh tokens have expired.
        """
        # TODO: Implement token retrieval + auto-refresh logic
        # 1. load_tokens()
        # 2. if token_data.is_expired → refresh and save
        # 3. return token_data.access_token
        raise NotImplementedError("get_valid_access_token — to be implemented in Phase 2")

    def save_tokens(self, token_data: TokenData) -> None:
        """
        Encrypt and atomically write token data to disk.

        Uses a temp file + os.rename() pattern to prevent partial writes
        from corrupting the token file.

        Args:
            token_data: Token data to persist.
        """
        # TODO: Implement atomic encrypted file write
        # 1. Serialise TokenData to JSON
        # 2. Encrypt with Fernet
        # 3. Write to .tmp file
        # 4. os.rename() to final path (atomic on POSIX)
        # 5. chmod 0o600
        raise NotImplementedError("save_tokens — to be implemented in Phase 2")

    def load_tokens(self) -> TokenData:
        """
        Decrypt and load token data from disk.

        Returns:
            Validated TokenData instance.

        Raises:
            KommoAuthError: File missing, corrupted, or decryption fails.
        """
        # TODO: Implement encrypted file read + decryption
        # 1. Read raw bytes from token_file_path
        # 2. Decrypt with Fernet
        # 3. Parse JSON → TokenData
        raise NotImplementedError("load_tokens — to be implemented in Phase 2")

    def tokens_exist(self) -> bool:
        """Return True if an encrypted token file exists on disk."""
        return self._token_file.exists()
