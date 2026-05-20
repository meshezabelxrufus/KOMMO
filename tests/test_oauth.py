"""
tests/test_oauth.py
===================
Unit tests for auth/oauth.py — KommoOAuthClient.

All HTTP interactions are patched — no live Kommo account needed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _env(tmp_path: Path, overrides: dict | None = None) -> dict:
    base = {
        "KOMMO_CLIENT_ID":      "test-client-id",
        "KOMMO_CLIENT_SECRET":  "test-client-secret",
        "KOMMO_REDIRECT_URI":   "http://localhost:8000/callback",
        "KOMMO_ACCOUNT_DOMAIN": "testaccount",
        "TOKEN_STORE_PATH":     str(tmp_path / "tokens.json"),
    }
    if overrides:
        base.update(overrides)
    return base


def _make_client(tmp_path: Path, overrides: dict | None = None):
    from auth.oauth import KommoOAuthClient
    with patch.dict(os.environ, _env(tmp_path, overrides), clear=True):
        return KommoOAuthClient()


def _dummy_tokens(tmp_path: Path) -> dict:
    return {
        "access_token": "acc_abc123", "refresh_token": "ref_xyz789",
        "token_type": "Bearer", "expires_at": time.time() + 3600,
        "account_domain": "testaccount", "saved_at": time.time(),
    }


def _mock_http(status: int = 200, body: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.ok = status < 400
    m.status_code = status
    m.json.return_value = body or {
        "access_token": "new_access", "refresh_token": "new_refresh",
        "token_type": "Bearer", "expires_in": 86400,
    }
    return m


# ---------------------------------------------------------------------------
# Init & env validation
# ---------------------------------------------------------------------------

class TestInit:
    def test_success(self, tmp_path):
        c = _make_client(tmp_path)
        assert c.client_id == "test-client-id"
        assert "testaccount.kommo.com" in c.base_url

    def test_missing_env_raises(self, tmp_path):
        from auth.oauth import KommoOAuthClient
        env = _env(tmp_path)
        del env["KOMMO_CLIENT_ID"]
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvironmentError, match="KOMMO_CLIENT_ID"):
                KommoOAuthClient()

    def test_blank_env_raises(self, tmp_path):
        from auth.oauth import KommoOAuthClient
        with patch.dict(os.environ, _env(tmp_path, {"KOMMO_CLIENT_ID": "  "}), clear=True):
            with pytest.raises(EnvironmentError, match="KOMMO_CLIENT_ID"):
                KommoOAuthClient()

    def test_urls_computed_from_domain(self, tmp_path):
        c = _make_client(tmp_path)
        assert c.auth_url  == "https://testaccount.kommo.com/oauth2/authorize"
        assert c.token_url == "https://testaccount.kommo.com/oauth2/access_token"

    def test_custom_token_path(self, tmp_path):
        from auth.oauth import KommoOAuthClient
        custom = tmp_path / "custom.json"
        with patch.dict(os.environ, _env(tmp_path), clear=True):
            c = KommoOAuthClient(token_store_path=custom)
        assert c._token_path == custom


# ---------------------------------------------------------------------------
# Token expiry
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_fresh_not_expired(self, tmp_path):
        c = _make_client(tmp_path)
        assert c._is_token_expired({"expires_at": time.time() + 3600}) is False

    def test_past_is_expired(self, tmp_path):
        c = _make_client(tmp_path)
        assert c._is_token_expired({"expires_at": time.time() - 60}) is True

    def test_within_5min_buffer_is_expired(self, tmp_path):
        c = _make_client(tmp_path)
        assert c._is_token_expired({"expires_at": time.time() + 120}) is True

    def test_beyond_5min_buffer_not_expired(self, tmp_path):
        c = _make_client(tmp_path)
        assert c._is_token_expired({"expires_at": time.time() + 600}) is False

    def test_missing_field_is_expired(self, tmp_path):
        c = _make_client(tmp_path)
        assert c._is_token_expired({}) is True


# ---------------------------------------------------------------------------
# Save / Load round-trip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_round_trip(self, tmp_path):
        c = _make_client(tmp_path)
        tokens = _dummy_tokens(tmp_path)
        c._save_tokens(tokens)
        loaded = c._load_tokens()
        assert loaded["access_token"]  == tokens["access_token"]
        assert loaded["refresh_token"] == tokens["refresh_token"]

    def test_creates_parent_dirs(self, tmp_path):
        from auth.oauth import KommoOAuthClient
        deep = tmp_path / "a" / "b" / "tokens.json"
        with patch.dict(os.environ, _env(tmp_path, {"TOKEN_STORE_PATH": str(deep)}), clear=True):
            c = KommoOAuthClient()
        c._save_tokens(_dummy_tokens(tmp_path))
        assert deep.exists()

    def test_load_missing_raises(self, tmp_path):
        from auth.oauth import KommoTokenMissingError
        c = _make_client(tmp_path, {"TOKEN_STORE_PATH": str(tmp_path / "no.json")})
        with pytest.raises(KommoTokenMissingError):
            c._load_tokens()

    def test_load_corrupt_raises(self, tmp_path):
        from auth.oauth import KommoTokenMissingError
        f = tmp_path / "tokens.json"
        f.write_text("{{BAD JSON}}")
        c = _make_client(tmp_path)
        with pytest.raises(KommoTokenMissingError):
            c._load_tokens()

    def test_tokens_exist_false_before_save(self, tmp_path):
        c = _make_client(tmp_path, {"TOKEN_STORE_PATH": str(tmp_path / "no.json")})
        assert c.tokens_exist() is False

    def test_tokens_exist_true_after_save(self, tmp_path):
        c = _make_client(tmp_path)
        c._save_tokens(_dummy_tokens(tmp_path))
        assert c.tokens_exist() is True


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------

class TestAuthURL:
    def test_contains_client_id(self, tmp_path):
        c = _make_client(tmp_path)
        assert "client_id=test-client-id" in c.get_authorization_url()

    def test_contains_response_type_code(self, tmp_path):
        c = _make_client(tmp_path)
        assert "response_type=code" in c.get_authorization_url()

    def test_state_included_when_provided(self, tmp_path):
        c = _make_client(tmp_path)
        assert "state=csrf-abc" in c.get_authorization_url(state="csrf-abc")

    def test_state_absent_when_not_provided(self, tmp_path):
        c = _make_client(tmp_path)
        assert "state=" not in c.get_authorization_url()


# ---------------------------------------------------------------------------
# Token exchange (mocked HTTP)
# ---------------------------------------------------------------------------

class TestTokenExchange:
    def test_exchange_saves_tokens(self, tmp_path):
        c = _make_client(tmp_path)
        with patch("requests.post", return_value=_mock_http()):
            result = c.exchange_code_for_tokens("auth-code")
        assert result["access_token"] == "new_access"
        assert c.tokens_exist()

    def test_exchange_computes_absolute_expires_at(self, tmp_path):
        c = _make_client(tmp_path)
        before = time.time()
        with patch("requests.post", return_value=_mock_http()):
            result = c.exchange_code_for_tokens("code")
        assert result["expires_at"] >= before + 86400

    def test_exchange_4xx_raises_auth_error(self, tmp_path):
        from auth.oauth import KommoAuthorizationError
        c = _make_client(tmp_path)
        with patch("requests.post", return_value=_mock_http(401, {"error": "invalid_code"})):
            with pytest.raises(KommoAuthorizationError):
                c.exchange_code_for_tokens("bad-code")

    def test_exchange_timeout_raises_network_error(self, tmp_path):
        import requests as req
        from auth.oauth import KommoNetworkError
        c = _make_client(tmp_path)
        with patch("requests.post", side_effect=req.Timeout("timeout")):
            with pytest.raises(KommoNetworkError):
                c.exchange_code_for_tokens("code")


# ---------------------------------------------------------------------------
# Token info
# ---------------------------------------------------------------------------

class TestTokenInfo:
    def test_does_not_expose_secret_values(self, tmp_path):
        c = _make_client(tmp_path)
        c._save_tokens(_dummy_tokens(tmp_path))
        info = c.token_info()
        assert "access_token"  not in info
        assert "refresh_token" not in info
        assert "acc_abc123"    not in str(info)

    def test_returns_correct_metadata(self, tmp_path):
        c = _make_client(tmp_path)
        c._save_tokens(_dummy_tokens(tmp_path))
        info = c.token_info()
        assert info["is_expired"]        is False
        assert info["has_refresh_token"] is True
        assert info["seconds_until_expiry"] == pytest.approx(3600, abs=10)
