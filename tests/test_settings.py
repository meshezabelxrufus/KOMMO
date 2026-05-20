"""
tests/test_settings.py
=======================
Unit tests for config/settings.py — Settings validation and security.

All tests use isolated env patches — no .env file or live credentials needed.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_env(overrides: dict | None = None) -> dict:
    """Return a minimal valid environment dict."""
    from cryptography.fernet import Fernet
    base = {
        "KOMMO_CLIENT_ID":      "test-client-123",
        "KOMMO_CLIENT_SECRET":  "test-secret-abc",
        "KOMMO_REDIRECT_URI":   "http://localhost:8000/callback",
        "KOMMO_SUBDOMAIN":      "mycompany",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    if overrides:
        base.update(overrides)
    return base


def _make_settings(env: dict):
    """Create a Settings instance with a patched environment (no .env file)."""
    from config.settings import Settings
    with patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None)   # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Happy path — all required fields present
# ---------------------------------------------------------------------------

class TestValidSettings:
    def test_loads_all_required_fields(self):
        s = _make_settings(_valid_env())
        assert s.kommo_client_id     == "test-client-123"
        assert s.kommo_subdomain     == "mycompany"
        assert s.kommo_redirect_uri  == "http://localhost:8000/callback"

    def test_client_secret_is_secretstr(self):
        from pydantic import SecretStr
        s = _make_settings(_valid_env())
        assert isinstance(s.kommo_client_secret, SecretStr)

    def test_token_encryption_key_is_secretstr(self):
        from pydantic import SecretStr
        s = _make_settings(_valid_env())
        assert isinstance(s.token_encryption_key, SecretStr)

    def test_secret_not_in_repr(self):
        s = _make_settings(_valid_env())
        assert "test-secret-abc" not in repr(s)
        assert "test-secret-abc" not in str(s)

    def test_defaults_applied(self):
        s = _make_settings(_valid_env())
        assert s.kommo_max_page_size         == 250
        assert s.kommo_rate_limit_per_second == 7.0
        assert s.kommo_max_retries           == 3
        assert s.log_level                   == "INFO"

    def test_output_dir_is_path(self):
        s = _make_settings(_valid_env())
        assert isinstance(s.output_dir, Path)

    def test_log_dir_is_path(self):
        s = _make_settings(_valid_env())
        assert isinstance(s.log_dir, Path)


# ---------------------------------------------------------------------------
# Computed URL properties
# ---------------------------------------------------------------------------

class TestComputedURLs:
    def test_base_url_uses_subdomain(self):
        s = _make_settings(_valid_env())
        assert s.kommo_base_url  == "https://mycompany.kommo.com/api/v4"

    def test_auth_url_uses_subdomain(self):
        s = _make_settings(_valid_env())
        assert s.kommo_auth_url  == "https://mycompany.kommo.com/oauth2/authorize"

    def test_token_url_uses_subdomain(self):
        s = _make_settings(_valid_env())
        assert s.kommo_token_url == "https://mycompany.kommo.com/oauth2/access_token"

    def test_custom_subdomain_reflected_in_urls(self):
        s = _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "acmecorp"}))
        assert "acmecorp.kommo.com" in s.kommo_base_url
        assert "acmecorp.kommo.com" in s.kommo_auth_url


# ---------------------------------------------------------------------------
# Subdomain validator
# ---------------------------------------------------------------------------

class TestSubdomainValidator:
    def test_strips_https_prefix(self):
        s = _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "https://myco.kommo.com"}))
        assert s.kommo_subdomain == "myco"

    def test_strips_dot_kommo_com_suffix(self):
        s = _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "myco.kommo.com"}))
        assert s.kommo_subdomain == "myco"

    def test_strips_full_api_url(self):
        s = _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "myco.kommo.com/api/v4"}))
        assert s.kommo_subdomain == "myco"

    def test_strips_whitespace(self):
        s = _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "  myco  "}))
        assert s.kommo_subdomain == "myco"

    def test_plain_subdomain_unchanged(self):
        s = _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "acmecorp"}))
        assert s.kommo_subdomain == "acmecorp"

    def test_empty_subdomain_raises(self):
        with pytest.raises(ValidationError, match="KOMMO_SUBDOMAIN"):
            _make_settings(_valid_env({"KOMMO_SUBDOMAIN": ""}))

    def test_whitespace_only_subdomain_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_SUBDOMAIN": "   "}))


# ---------------------------------------------------------------------------
# Redirect URI validator
# ---------------------------------------------------------------------------

class TestRedirectURIValidator:
    def test_http_uri_accepted(self):
        s = _make_settings(_valid_env({"KOMMO_REDIRECT_URI": "http://localhost:8000/callback"}))
        assert s.kommo_redirect_uri.startswith("http://")

    def test_https_uri_accepted(self):
        s = _make_settings(_valid_env({"KOMMO_REDIRECT_URI": "https://myapp.com/callback"}))
        assert s.kommo_redirect_uri.startswith("https://")

    def test_missing_scheme_raises(self):
        with pytest.raises(ValidationError, match="KOMMO_REDIRECT_URI"):
            _make_settings(_valid_env({"KOMMO_REDIRECT_URI": "localhost/callback"}))

    def test_empty_uri_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_REDIRECT_URI": ""}))


# ---------------------------------------------------------------------------
# Client ID validator
# ---------------------------------------------------------------------------

class TestClientIDValidator:
    def test_placeholder_raises(self):
        with pytest.raises(ValidationError, match="placeholder"):
            _make_settings(_valid_env({"KOMMO_CLIENT_ID": "your_client_id_here"}))

    def test_empty_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_CLIENT_ID": ""}))

    def test_real_id_accepted(self):
        s = _make_settings(_valid_env({"KOMMO_CLIENT_ID": "abc123def456"}))
        assert s.kommo_client_id == "abc123def456"


# ---------------------------------------------------------------------------
# Fernet key validator
# ---------------------------------------------------------------------------

class TestFernetKeyValidator:
    def test_valid_fernet_key_accepted(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        s = _make_settings(_valid_env({"TOKEN_ENCRYPTION_KEY": key}))
        assert s.token_encryption_key.get_secret_value() == key

    def test_invalid_key_raises(self):
        with pytest.raises(ValidationError, match="Fernet"):
            _make_settings(_valid_env({"TOKEN_ENCRYPTION_KEY": "not-a-valid-key"}))

    def test_placeholder_key_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"TOKEN_ENCRYPTION_KEY": "your_fernet_key_here"}))

    def test_empty_key_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"TOKEN_ENCRYPTION_KEY": ""}))


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------

class TestMissingRequiredFields:
    @pytest.mark.parametrize("missing_field", [
        "KOMMO_CLIENT_ID",
        "KOMMO_CLIENT_SECRET",
        "KOMMO_REDIRECT_URI",
        "KOMMO_SUBDOMAIN",
        "TOKEN_ENCRYPTION_KEY",
    ])
    def test_missing_field_raises_validation_error(self, missing_field):
        env = _valid_env()
        del env[missing_field]
        with pytest.raises(ValidationError):
            _make_settings(env)


# ---------------------------------------------------------------------------
# Field range validators
# ---------------------------------------------------------------------------

class TestFieldRanges:
    def test_page_size_above_250_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_MAX_PAGE_SIZE": "251"}))

    def test_page_size_zero_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_MAX_PAGE_SIZE": "0"}))

    def test_rate_limit_above_7_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_RATE_LIMIT_PER_SECOND": "8"}))

    def test_rate_limit_below_0_1_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"KOMMO_RATE_LIMIT_PER_SECOND": "0"}))

    def test_invalid_log_level_raises(self):
        with pytest.raises(ValidationError):
            _make_settings(_valid_env({"LOG_LEVEL": "VERBOSE"}))

    def test_valid_log_levels_accepted(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            s = _make_settings(_valid_env({"LOG_LEVEL": level}))
            assert s.log_level == level


# ---------------------------------------------------------------------------
# safe_repr — security audit
# ---------------------------------------------------------------------------

class TestSafeRepr:
    def test_client_secret_redacted(self):
        s = _make_settings(_valid_env())
        d = s.safe_repr()
        assert d.get("kommo_client_secret") == "***REDACTED***"

    def test_token_key_redacted(self):
        s = _make_settings(_valid_env())
        d = s.safe_repr()
        assert d.get("token_encryption_key") == "***REDACTED***"

    def test_non_secret_fields_visible(self):
        s = _make_settings(_valid_env())
        d = s.safe_repr()
        assert d["kommo_client_id"]  == "test-client-123"
        assert d["kommo_subdomain"]  == "mycompany"
        assert d["kommo_base_url"]   == "https://mycompany.kommo.com/api/v4"

    def test_raw_secret_value_not_in_safe_repr_values(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        env = _valid_env({"TOKEN_ENCRYPTION_KEY": key,
                          "KOMMO_CLIENT_SECRET": "super-secret-value"})
        s = _make_settings(env)
        d = s.safe_repr()
        values_str = str(d.values())
        assert "super-secret-value" not in values_str
        assert key not in values_str
