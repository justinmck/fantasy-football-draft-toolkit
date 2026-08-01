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
    -- Mirrors the columns src/recommender.py actually selects. The real table
    -- is ~200 columns wide; only last season's production is needed here, since
    -- that's what feeds the confidence term and the UI's player detail panel.
    CREATE TABLE players_stats (
        player_id INTEGER, year INTEGER, avg_points REAL,
        points REAL, games_played REAL,
        actual_receivingTargets REAL,
        actual_rushingAttempts REAL,
        actual_passingAttempts REAL
    );
    CREATE TABLE drafts (
        player_id INTEGER, year INTEGER, overallPickNumber INTEGER, position TEXT
    );
    -- Written by NB04 (bootstrap prediction intervals) and NB05 (historical
    -- per-position accuracy). Both are optional at runtime - the recommender
    -- guards each join with a table-existence check - but they're present here
    -- so the risk/confidence path is actually exercised by the API tests.
    CREATE TABLE model_projections (
        player_id INTEGER, year INTEGER,
        predicted_vorp REAL, ci_low REAL, ci_high REAL,
        ci_width REAL, confidence REAL
    );
    CREATE TABLE position_reliability (
        position TEXT, n INTEGER, rmse REAL, mae REAL, r2 REAL,
        reliability REAL, seasons TEXT
    );
    """
)
_conn.executemany(
    "INSERT INTO model_projections VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        # Wide interval -> low model confidence.
        (1, 2026, 100.0, 40.0, 160.0, 120.0, 0.40),
        # Tight interval -> high model confidence.
        (2, 2026, 90.0, 81.0, 99.0, 18.0, 0.90),
        (4, 2026, 60.0, 45.0, 75.0, 30.0, 0.75),
        # player 3 (rookie) has no interval row -> neutral confidence
    ],
)
_conn.executemany(
    "INSERT INTO position_reliability VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        # Roughly the real NB05 values, so tests reflect the true spread:
        # QB projections explain about half the variance, K/DST essentially none.
        ("QB", 277, 79.2, 62.6, 0.52, 0.52, "2020,2021,2022,2024,2025"),
        ("RB", 550, 60.0, 46.9, 0.42, 0.42, "2020,2021,2022,2024,2025"),
        ("WR", 714, 53.8, 42.3, 0.37, 0.37, "2020,2021,2022,2024,2025"),
        ("TE", 311, 39.0, 30.1, 0.37, 0.37, "2020,2021,2022,2024,2025"),
        ("K", 162, 43.6, 31.8, -0.03, 0.0, "2020,2021,2022,2024,2025"),
        ("DST", 160, 35.5, 28.6, 0.00, 0.0, "2020,2021,2022,2024,2025"),
    ],
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
    "INSERT INTO players_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    [
        #  id  year  avg   pts   gp   targets  rush  pass
        (1, 2025, 18.5, 296.0, 16.0, 140.0, 4.0, 0.0),
        (2, 2025, 15.0, 255.0, 17.0, 52.0, 260.0, 0.0),
        # player 3 (rookie) intentionally has no prior-season row -> is_rookie=1
        (4, 2025, 12.0, 180.0, 15.0, 0.0, 30.0, 480.0),
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
