"""Configuration that refuses to boot rather than failing quietly.

`API_ORIGINS` was split on commas and handed straight to the CORS middleware.
Each of the cases below produced a deployment that *looked* configured and
wasn't - which is the worst outcome for a security control, because nobody
investigates a thing that appears to work.
"""
import pytest

from src.settings import parse_origins


class TestOrigins:
    def test_a_normal_list_parses(self):
        assert parse_origins("https://a.com,https://b.com") == [
            "https://a.com", "https://b.com"]

    def test_whitespace_is_stripped(self):
        """`"a, b"` used to yield a second entry of `" b"`, which matches no
        Origin header - so that origin silently stopped working."""
        assert parse_origins("https://a.com, https://b.com") == [
            "https://a.com", "https://b.com"]

    def test_empty_entries_are_dropped(self):
        assert parse_origins("https://a.com,,") == ["https://a.com"]

    def test_star_with_credentials_refuses(self):
        """Not a valid CORS configuration - browsers ignore it. A deployment
        setting this believes it is permissive and is actually broken."""
        with pytest.raises(RuntimeError, match="not a valid CORS"):
            parse_origins("*", allow_credentials=True)

    def test_star_without_credentials_is_allowed(self):
        assert parse_origins("*", allow_credentials=False) == ["*"]

    def test_a_path_is_rejected(self):
        """An Origin header only ever carries scheme, host and port, so an
        entry with a path can never match."""
        with pytest.raises(RuntimeError, match="bare scheme"):
            parse_origins("https://a.com/app")

    def test_a_bare_host_is_rejected(self):
        with pytest.raises(RuntimeError, match="bare scheme"):
            parse_origins("a.com")

    def test_an_empty_config_is_rejected(self):
        with pytest.raises(RuntimeError, match="empty"):
            parse_origins("  ,  ")

    def test_a_port_is_kept(self):
        assert parse_origins("http://localhost:5173") == ["http://localhost:5173"]


class TestProdGuards:
    def test_prod_rejects_a_plaintext_origin(self, monkeypatch):
        """ESPN session cookies over plaintext HTTP is game over regardless of
        everything else in the app."""
        import src.settings as settings
        monkeypatch.setattr(settings, "APP_ENV", "prod")
        with pytest.raises(RuntimeError, match="non-https"):
            settings.parse_origins("http://example.com")

    def test_prod_accepts_https(self, monkeypatch):
        import src.settings as settings
        monkeypatch.setattr(settings, "APP_ENV", "prod")
        assert settings.parse_origins("https://example.com") == ["https://example.com"]


class TestReferenceDatabase:
    def test_the_shipped_database_carries_no_account_ids(self):
        """The build script vacuums, but the file is what ships - so the test
        checks the bytes, not the script's intentions."""
        import re

        from src.settings import REFERENCE_DB

        if not REFERENCE_DB.exists():
            pytest.skip("reference database not built")
        raw = REFERENCE_DB.read_bytes()
        assert not re.search(rb"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-", raw)

    def test_the_shipped_database_has_no_league_scoped_tables(self):
        import sqlite3

        from src.settings import REFERENCE_DB

        if not REFERENCE_DB.exists():
            pytest.skip("reference database not built")
        conn = sqlite3.connect(REFERENCE_DB)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        # `teams` and `league_bias_manager` are the sharp ones: team names, and
        # a per-person "reaches 7.8 picks early" score.
        for forbidden in ("drafts", "teams", "players_stats", "league_bias_manager"):
            assert forbidden not in names, f"{forbidden} must never ship"
