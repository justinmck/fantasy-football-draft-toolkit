"""Sets up a tiny fixture SQLite DB and points the app at it via
DATABASE_URL *before* src.settings/src.db/src.api are ever imported, since
those modules read the env var and build a SQLAlchemy engine (and, in
src.api's case, fit the league bias) at import time.

conftest.py is collected by pytest before test modules in the same
directory, so setting the env var here (at module import time, not inside a
fixture) guarantees it's in place before `from src.api import app` runs in
test_api.py.
"""
import os
import sqlite3
import tempfile

_tmpdir = tempfile.TemporaryDirectory()
_DB_PATH = os.path.join(_tmpdir.name, "fixture.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

_conn = sqlite3.connect(_DB_PATH)
_conn.executescript(
    """
    CREATE TABLE next_season_projections (
        player_id INTEGER, player_name TEXT, position TEXT,
        pro_team TEXT, projected_points REAL, year INTEGER
    );
    CREATE TABLE average_draft_position (
        player_id INTEGER, year INTEGER, avg REAL
    );
    CREATE TABLE players_stats (
        player_id INTEGER, year INTEGER, avg_points REAL
    );
    CREATE TABLE drafts (
        player_id INTEGER, year INTEGER, overallPickNumber INTEGER, position TEXT
    );
    """
)
_conn.executemany(
    "INSERT INTO next_season_projections VALUES (?, ?, ?, ?, ?, ?)",
    [
        (1, "Established Vet WR", "WR", "KC", 250.0, 2026),
        (2, "Solid RB", "RB", "SF", 220.0, 2026),
        (3, "Rookie TE", "TE", None, 90.0, 2026),  # no prior-year stats, no pro_team -> NaN regression case
        (4, "Backup QB", "QB", "NYJ", 180.0, 2026),
    ],
)
_conn.executemany(
    "INSERT INTO average_draft_position VALUES (?, ?, ?)",
    [
        (1, 2026, 8.0),
        (2, 2026, 12.0),
        (4, 2026, 40.0),
        # player 3 (rookie) intentionally has no ADP row
        (1, 2025, 9.0),
        (2, 2025, 11.0),
        (4, 2025, 38.0),
    ],
)
_conn.executemany(
    "INSERT INTO players_stats VALUES (?, ?, ?)",
    [
        (1, 2025, 18.5),
        (2, 2025, 15.0),
        # player 3 (rookie) intentionally has no prior-season row -> NaN avg_last_year
        (4, 2025, 12.0),
    ],
)
_conn.executemany(
    "INSERT INTO drafts VALUES (?, ?, ?, ?)",
    [
        (1, 2025, 7, "WR"),
        (2, 2025, 13, "RB"),
        (4, 2025, 41, "QB"),
    ],
)
_conn.commit()
_conn.close()
