"""
config/__init__.py
==================
Exposes a single `settings` singleton loaded at import time.

USAGE
─────
    from config import settings

    # Safe — returns plain string
    print(settings.kommo_client_id)
    print(settings.kommo_base_url)

    # Safe — returns SecretStr (redacted in repr/logs)
    secret = settings.kommo_client_secret

    # Use .get_secret_value() only when you ACTUALLY need the raw value
    # (e.g. sending it in an HTTP request body — never in a log statement)
    raw = settings.kommo_client_secret.get_secret_value()

STARTUP BEHAVIOUR
─────────────────
  - `settings` is instantiated once at module load time.
  - If any required env var is missing or invalid, a `ValidationError`
    is raised immediately at startup — before any extraction runs.
  - This is intentional: fail fast and clearly, not silently at runtime.

ENVIRONMENT RESOLUTION ORDER
─────────────────────────────
  1. Actual OS environment variables (highest priority)
  2. .env file in the project root
  3. Pydantic field defaults (lowest priority)

  This means you can override any .env value by setting the env var
  directly — useful in CI/CD pipelines and container deployments.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import ValidationError

from config.settings import Settings
from utils.exceptions import KommoConfigError

log = logging.getLogger(__name__)


def _load_settings() -> Settings:
    """
    Load and validate settings, providing clear error messages on failure.

    Raises KommoConfigError (not SystemExit) so that callers — including
    tests and library users — can handle the failure gracefully.

    main.py catches KommoConfigError at startup and calls sys.exit(2) there.

    Returns:
        Validated Settings instance.

    Raises:
        KommoConfigError: On missing or invalid environment variables.
    """
    try:
        return Settings()
    except ValidationError as exc:
        # Build a human-readable error message from pydantic's error list
        field_errors = []
        for error in exc.errors():
            field   = " → ".join(str(loc) for loc in error["loc"])
            message = error["msg"]
            field_errors.append(f"{field}: {message}")

        summary = "\n  ".join(field_errors)
        raise KommoConfigError(
            f"Configuration validation failed — {len(field_errors)} error(s):\n  {summary}\n"
            "Copy .env.example → .env and fill in all required values."
        ) from exc


def _print_config_error(exc: ValidationError) -> None:
    """Print a human-readable configuration error report to stderr."""
    import sys

    print("\n" + "=" * 65, file=sys.stderr)
    print("  ❌  CONFIGURATION ERROR", file=sys.stderr)
    print("=" * 65, file=sys.stderr)
    print(
        "  One or more required environment variables are missing or invalid.\n"
        "  Copy .env.example → .env and fill in all required values.\n",
        file=sys.stderr,
    )

    for error in exc.errors():
        field   = " → ".join(str(loc) for loc in error["loc"])
        message = error["msg"]
        etype   = error["type"]
        print(f"  Field : {field}", file=sys.stderr)
        print(f"  Error : {message}", file=sys.stderr)
        print(f"  Type  : {etype}", file=sys.stderr)
        print(file=sys.stderr)

    print(
        "  Docs  : See .env.example for all required variables.\n"
        "  Setup : Run ./setup.sh to generate your .env automatically.\n",
        file=sys.stderr,
    )
    print("=" * 65 + "\n", file=sys.stderr)


# ---------------------------------------------------------------------------
# The singleton — import this everywhere
# ---------------------------------------------------------------------------
settings: Settings = _load_settings()


# ---------------------------------------------------------------------------
# Public helper: log active config at startup (secrets redacted)
# ---------------------------------------------------------------------------

def log_active_config() -> None:
    """
    Log all active configuration values at INFO level with secrets redacted.

    Call this once at application startup to create a permanent audit
    trail of what config values were in effect for each run.

    Example:
        from config import log_active_config
        log_active_config()
    """
    safe = settings.safe_repr()

    log.info("Active configuration loaded:")
    for key, value in safe.items():
        log.info("  %-40s = %s", key, value)
