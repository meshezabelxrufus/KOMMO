"""
config/settings.py
==================
Secure, validated configuration management for the Kommo CRM integration.

DESIGN PRINCIPLES
─────────────────
  1. Single source of truth — all config flows through this module.
     No scattered os.environ.get() calls across the codebase.

  2. Fail fast — ValidationError is raised at startup if any required
     variable is missing or has an invalid value. Better to crash early
     than silently use a wrong value.

  3. Secrets never leak — KOMMO_CLIENT_SECRET and TOKEN_ENCRYPTION_KEY
     are stored as pydantic SecretStr. They are redacted in repr(),
     logs, and JSON serialisation.

  4. Type safety — pydantic coerces and validates all values at load time.
     `kommo_max_page_size: int = Field(..., ge=1, le=250)` will reject
     "banana" and 999 at startup, not at runtime.

  5. 12-Factor App compliant — all config comes from environment variables.
     No hardcoded values except sensible defaults.

USAGE
─────
    # Anywhere in the codebase:
    from config import settings

    print(settings.kommo_base_url)             # https://myco.kommo.com/api/v4
    print(settings.kommo_client_id)            # abc123
    print(settings.kommo_client_secret)        # SecretStr('**********')
    print(settings.kommo_client_secret.get_secret_value())  # actual secret

SECURITY RULES
──────────────
  - NEVER call .get_secret_value() in log statements
  - NEVER pass SecretStr fields as f-string arguments (use .get_secret_value() explicitly)
  - NEVER commit .env to git (it is in .gitignore)
  - ALWAYS use KOMMO_SUBDOMAIN (not a full URL) — the URL is computed at runtime
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import (
    Field,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application-wide, validated configuration.

    Loaded from environment variables and/or a .env file.
    All secret fields use pydantic's SecretStr to prevent accidental logging.

    Required variables (no defaults — startup fails if missing):
      KOMMO_CLIENT_ID
      KOMMO_CLIENT_SECRET
      KOMMO_REDIRECT_URI
      KOMMO_SUBDOMAIN
      TOKEN_ENCRYPTION_KEY

    All other fields have safe defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,       # KOMMO_CLIENT_ID == kommo_client_id
        extra="ignore",             # Silently ignore unknown env vars
        validate_default=True,      # Run validators even on default values
    )

    # ==========================================================================
    # Kommo OAuth 2.0 Credentials — ALL REQUIRED, NO DEFAULTS
    # ==========================================================================

    kommo_client_id: str = Field(
        ...,
        min_length=1,
        description=(
            "OAuth 2.0 Client ID from your Kommo Developer App. "
            "Source: https://kommo.com/developers/ → My Apps"
        ),
    )

    kommo_client_secret: SecretStr = Field(
        ...,
        description=(
            "OAuth 2.0 Client Secret. "
            "NEVER log or print this value. Stored as SecretStr."
        ),
    )

    kommo_redirect_uri: str = Field(
        ...,
        description=(
            "OAuth redirect URI. Must exactly match the URI registered "
            "in your Kommo Developer App (including trailing slash if any)."
        ),
    )

    kommo_subdomain: str = Field(
        ...,
        min_length=1,
        description=(
            "Your Kommo account subdomain (without .kommo.com). "
            "Example: if your URL is mycompany.kommo.com, set KOMMO_SUBDOMAIN=mycompany"
        ),
    )

    # ==========================================================================
    # Token Storage
    # ==========================================================================

    token_encryption_key: SecretStr = Field(
        ...,
        description=(
            "Fernet symmetric encryption key for the token store. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\". "
            "NEVER reuse across environments. Stored as SecretStr."
        ),
    )

    token_store_path: Path = Field(
        default=Path("auth/token_store.json"),
        description="Path to the OAuth token storage file.",
    )

    sync_state_path: Path = Field(
        default=Path("state/sync_state.json"),
        description="Path to the incremental sync state file.",
    )

    # ==========================================================================
    # API Behaviour
    # ==========================================================================

    kommo_max_page_size: int = Field(
        default=250,
        ge=1,
        le=250,
        description="Records per paginated API request. Max 250 (Kommo's limit).",
    )

    kommo_rate_limit_per_second: float = Field(
        default=7.0,
        ge=0.1,
        le=7.0,
        description=(
            "Client-side rate limit in requests/second. "
            "Kommo's documented limit is 7 req/s. "
            "Reduce this value if you share the limit with other integrations."
        ),
    )

    kommo_connect_timeout: float = Field(
        default=10.0,
        ge=1.0,
        description="Seconds to wait for TCP connection establishment.",
    )

    kommo_read_timeout: float = Field(
        default=30.0,
        ge=5.0,
        description="Seconds to wait for the response body after connection.",
    )

    kommo_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retry attempts for transient API failures.",
    )

    # ==========================================================================
    # Output
    # ==========================================================================

    output_dir: Path = Field(
        default=Path("outputs"),
        description="Root directory for all extracted JSON output files.",
    )

    # ==========================================================================
    # Logging
    # ==========================================================================

    log_level: str = Field(
        default="INFO",
        pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
        description="Minimum log level. Override with LOG_LEVEL env var.",
    )

    log_dir: Path = Field(
        default=Path("logs"),
        description="Directory for rotating log files.",
    )

    log_to_file: bool = Field(
        default=True,
        description="Write logs to rotating files in log_dir.",
    )

    log_max_bytes: int = Field(
        default=10 * 1024 * 1024,   # 10 MB
        ge=1024 * 1024,              # Min 1 MB
        description="Max size per log file before rotation.",
    )

    log_backup_count: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of rotated log file backups to retain.",
    )

    # ==========================================================================
    # Validators
    # ==========================================================================

    @field_validator("kommo_subdomain", mode="before")
    @classmethod
    def clean_subdomain(cls, v: Any) -> str:
        """
        Strip whitespace and remove any accidentally included full domain.

        Users sometimes paste the full URL (https://myco.kommo.com) instead
        of just the subdomain (myco). This validator handles both.
        """
        if not isinstance(v, str):
            raise ValueError("KOMMO_SUBDOMAIN must be a string")

        v = v.strip()

        # Strip protocol
        for prefix in ("https://", "http://"):
            if v.startswith(prefix):
                v = v[len(prefix):]

        # Strip .kommo.com suffix if included
        for suffix in (".kommo.com/api/v4", ".kommo.com"):
            if v.endswith(suffix):
                v = v[: -len(suffix)]

        # Strip trailing slashes
        v = v.rstrip("/")

        if not v:
            raise ValueError(
                "KOMMO_SUBDOMAIN is empty after cleaning. "
                "Set it to your Kommo subdomain (e.g. 'mycompany' for mycompany.kommo.com)"
            )

        return v

    @field_validator("kommo_redirect_uri", mode="before")
    @classmethod
    def validate_redirect_uri(cls, v: Any) -> str:
        """Ensure redirect URI starts with http:// or https://."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError("KOMMO_REDIRECT_URI must be a non-empty string")
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"KOMMO_REDIRECT_URI must start with http:// or https://. Got: {v!r}"
            )
        return v

    @field_validator("kommo_client_id", mode="before")
    @classmethod
    def validate_client_id(cls, v: Any) -> str:
        """Ensure client ID is not the placeholder from .env.example."""
        if not isinstance(v, str):
            raise ValueError("KOMMO_CLIENT_ID must be a string")
        v = v.strip()
        if v in ("your_client_id_here", ""):
            raise ValueError(
                "KOMMO_CLIENT_ID is still set to the placeholder value. "
                "Replace it with your actual Kommo OAuth client ID."
            )
        return v

    @model_validator(mode="after")
    def validate_encryption_key_format(self) -> "Settings":
        """
        Validate that TOKEN_ENCRYPTION_KEY is a valid base64 Fernet key.

        Fernet keys are exactly 44 base64url characters ending with '='.
        This check prevents silent failures when a corrupted key is used.
        """
        try:
            from cryptography.fernet import Fernet
            raw_key = self.token_encryption_key.get_secret_value().strip().encode()
            Fernet(raw_key)   # Raises ValueError if invalid
        except Exception as exc:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY is not a valid Fernet key. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                f"print(Fernet.generate_key().decode())\" — Error: {exc}"
            ) from exc
        return self

    # ==========================================================================
    # Computed Properties (URLs built at runtime from subdomain)
    # ==========================================================================

    @computed_field  # type: ignore[misc]
    @property
    def kommo_base_url(self) -> str:
        """Full Kommo API v4 base URL, e.g. https://myco.kommo.com/api/v4"""
        return f"https://{self.kommo_subdomain}.kommo.com/api/v4"

    @computed_field  # type: ignore[misc]
    @property
    def kommo_auth_url(self) -> str:
        """OAuth 2.0 authorization endpoint."""
        return f"https://{self.kommo_subdomain}.kommo.com/oauth2/authorize"

    @computed_field  # type: ignore[misc]
    @property
    def kommo_token_url(self) -> str:
        """OAuth 2.0 token exchange and refresh endpoint."""
        return f"https://{self.kommo_subdomain}.kommo.com/oauth2/access_token"

    # ==========================================================================
    # Utility methods
    # ==========================================================================

    def safe_repr(self) -> dict:
        """
        Return a safe dict of all settings with secrets redacted.

        Use this for logging the active configuration at startup.
        NEVER use model_dump() for logging — it may expose SecretStr values
        depending on the pydantic version and serialisation mode.

        Returns:
            Dict with all fields; secret fields shown as '***REDACTED***'.
        """
        result = {}
        for name, field_info in self.model_fields.items():
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                result[name] = "***REDACTED***"
            elif isinstance(value, Path):
                result[name] = str(value)
            else:
                result[name] = value

        # Include computed fields
        result["kommo_base_url"]   = self.kommo_base_url
        result["kommo_auth_url"]   = self.kommo_auth_url
        result["kommo_token_url"]  = self.kommo_token_url
        return result

    def validate_directories(self) -> None:
        """
        Ensure all required directories exist, creating them if needed.

        Call this at application startup after loading settings.
        Raises OSError if a directory cannot be created.
        """
        dirs = [
            self.output_dir,
            self.output_dir / "errors",
            self.log_dir,
            self.token_store_path.parent,
            self.sync_state_path.parent,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
