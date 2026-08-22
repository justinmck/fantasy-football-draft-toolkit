"""Seeding the working database, on the path a deployment actually takes.

`DATABASE_URL` used to short-circuit seeding entirely: the reference copy only
happened on the default in-repo path. That is exactly backwards for a container,
where pointing the app at a mounted volume is the one case where the file is
guaranteed not to exist yet — so first boot would come up with no tables, no
error, and an Analysis tab that silently had nothing in it.
"""
import sqlite3
from pathlib import Path

import pytest

from src import settings


def _tables(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]


class TestSeeding:
    def test_a_configured_sqlite_path_is_seeded(self, tmp_path, monkeypatch):
        target = tmp_path / "vol" / "fantasy_data.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
        url = settings._resolve_db_url()
        assert url.endswith(str(target))
        assert target.exists(), "a mounted volume starts empty; this is the case that matters"
        assert _tables(target) > 0, "seeded from the reference database, not just touched"

    def test_it_creates_the_parent_directory(self, tmp_path, monkeypatch):
        """A fresh volume is an empty mount point, not a prepared tree."""
        target = tmp_path / "a" / "b" / "fantasy_data.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
        settings._resolve_db_url()
        assert target.exists()

    def test_an_existing_database_is_never_overwritten(self, tmp_path, monkeypatch):
        """Second boot must not wipe the league history the first one pulled."""
        target = tmp_path / "fantasy_data.db"
        with sqlite3.connect(target) as conn:
            conn.execute("CREATE TABLE mine (x INTEGER)")
            conn.execute("INSERT INTO mine VALUES (42)")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
        settings._resolve_db_url()
        with sqlite3.connect(target) as conn:
            assert conn.execute("SELECT x FROM mine").fetchone()[0] == 42

    def test_a_non_sqlite_url_is_passed_through_untouched(self, monkeypatch):
        """Postgres has no file to seed, and must not be mistaken for a path."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://user@host/db")
        assert settings._resolve_db_url() == "postgresql://user@host/db"

    def test_no_leftover_partial_file_is_left_behind(self, tmp_path, monkeypatch):
        """The copy goes via a temp name and is renamed into place."""
        target = tmp_path / "fantasy_data.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
        settings._resolve_db_url()
        assert not list(tmp_path.glob("*.seeding"))

    def test_the_default_path_still_works(self, monkeypatch):
        """The in-repo path is what development uses and must not have regressed."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert settings._resolve_db_url().startswith("sqlite:///")
