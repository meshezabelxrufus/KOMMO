"""
run_auth.py
===========
One-time OAuth 2.0 authorization flow for Kommo CRM.

WHAT THIS DOES
──────────────
1. Reads credentials from .env
2. Prints a Kommo authorization URL — open it in your browser
3. You grant access → Kommo redirects to your redirect_uri?code=...
4. Paste the `code` value here
5. Tokens are saved to auth/token_store.json

PREREQUISITES
─────────────
  - .env configured with KOMMO_CLIENT_ID, KOMMO_CLIENT_SECRET,
    KOMMO_REDIRECT_URI, KOMMO_ACCOUNT_DOMAIN
  - pip install -r requirements.txt

USAGE
─────
    python run_auth.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Load .env before anything else
from dotenv import load_dotenv
load_dotenv()

from auth.oauth import (
    KommoOAuthClient,
    KommoAuthorizationError,
    KommoNetworkError,
    KommoOAuthError,
)

# ---------------------------------------------------------------------------
# Logging — human-readable for CLI usage
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> int:
    """
    Execute the Kommo OAuth 2.0 authorization flow.

    Returns:
        0 on success, 1 on failure.
    """
    print("\n" + "=" * 60)
    print("  Kommo OAuth 2.0 Authorization")
    print("=" * 60 + "\n")

    # ------------------------------------------------------------------
    # 1. Initialise the OAuth client (validates env vars immediately)
    # ------------------------------------------------------------------
    try:
        client = KommoOAuthClient()
    except EnvironmentError as exc:
        logger.critical("Configuration error: %s", exc)
        print(f"\n❌  Configuration error: {exc}")
        print("    Check your .env file.\n")
        return 1

    # ------------------------------------------------------------------
    # 2. Warn if tokens already exist
    # ------------------------------------------------------------------
    if client.tokens_exist():
        print("⚠️  Existing tokens found in token store.")
        answer = input("   Overwrite and re-authorize? [y/N]: ").strip().lower()
        if answer != "y":
            print("\n✅  Existing tokens kept. Run run_extraction.py to start extraction.\n")
            return 0

    # ------------------------------------------------------------------
    # 3. Generate and display authorization URL
    # ------------------------------------------------------------------
    auth_url = client.get_authorization_url()

    print("📋  Step 1 — Open this URL in your browser:\n")
    print(f"    {auth_url}\n")
    print("📋  Step 2 — Grant access to your Kommo account.\n")
    print("📋  Step 3 — Copy the `code` value from the redirect URL.")
    print("            The redirect URL will look like:")
    print("            http://localhost:8000/callback?code=<THIS_PART>&...\n")

    # ------------------------------------------------------------------
    # 4. Receive authorization code from user
    # ------------------------------------------------------------------
    code = input("🔑  Paste the authorization code here: ").strip()

    if not code:
        print("\n❌  No code entered. Aborting.\n")
        return 1

    # ------------------------------------------------------------------
    # 5. Exchange code for tokens
    # ------------------------------------------------------------------
    print("\n⏳  Exchanging authorization code for tokens...\n")

    try:
        token_data = client.exchange_code_for_tokens(code=code)
    except KommoAuthorizationError as exc:
        logger.error("Authorization failed: %s", exc)
        print(f"\n❌  Authorization failed: {exc}")
        print("    The code may have expired (10-minute window) or already been used.")
        print("    Please restart this script and try again.\n")
        return 1
    except KommoNetworkError as exc:
        logger.error("Network error during token exchange: %s", exc)
        print(f"\n❌  Network error: {exc}")
        print("    Check your internet connection and try again.\n")
        return 1
    except KommoOAuthError as exc:
        logger.error("Unexpected OAuth error: %s", exc)
        print(f"\n❌  Unexpected error: {exc}\n")
        return 1

    # ------------------------------------------------------------------
    # 6. Confirm success
    # ------------------------------------------------------------------
    import time
    from datetime import datetime, timezone

    expires_dt = datetime.fromtimestamp(token_data["expires_at"], tz=timezone.utc)

    print("✅  Authorization successful! Tokens saved.\n")
    print(f"   Account:      {token_data['account_domain']}.kommo.com")
    print(f"   Token type:   {token_data['token_type']}")
    print(f"   Expires at:   {expires_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Token file:   {client._token_path}\n")
    print("🚀  You can now run: python run_extraction.py\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
