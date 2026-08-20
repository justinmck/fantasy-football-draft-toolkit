"""One row per player on the board, whatever else is in the database.

`players_stats` became per-league (scoring settings differ, so a point total
isn't portable between leagues). That turned the recommender's join one-to-many
the moment a second league was stored: every player who appeared in both showed
up twice on the board. These pin the fix, and the fallbacks around it.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from src.recommender import load_candidates, resolve_stats_league

SCHEMA = """
CREATE TABLE next_season_projections (
    player_id INTEGER, player_name TEXT, position TEXT,
    pro_team TEXT, projected_points REAL, year INTEGER
);
CREATE TABLE average_draft_position (player_id INTEGER, year INTEGER, avg REAL);
CREATE TABLE players_stats (
    player_id INTEGER, year INTEGER, league_id TEXT, avg_points REAL,
    points REAL, games_played REAL,
    actual_receivingTargets REAL, actual_rushingAttempts REAL,
    actual_passingAttempts REAL
);
"""

PLAYERS = [
    (1, "Jahmyr Gibbs", "RB", "DET", 250.0, 2026),
    (2, "Puka Nacua", "WR", "LAR", 240.0, 2026),
]
# Same two players, two leagues, one season - and different points, because
# that's the whole reason the table is scoped.
STATS = [
    (1, 2025, "780575", 15.0, 240.0, 16.0, 0.0, 200.0, 0.0),
    (2, 2025, "780575", 14.0, 224.0, 16.0, 150.0, 0.0, 0.0),
    (1, 2025, "197229335", 18.0, 288.0, 16.0, 0.0, 200.0, 0.0),
    (2, 2025, "197229335", 17.0, 272.0, 16.0, 150.0, 0.0, 0.0),
]


def _engine(stats=STATS, with_league_column=True):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "pool.db"
    conn = sqlite3.connect(path)
    schema = SCHEMA if with_league_column else SCHEMA.replace(" league_id TEXT,", "")
    conn.executescript(schema)
    conn.executemany("INSERT INTO next_season_projections VALUES (?,?,?,?,?,?)", PLAYERS)
    cols = 9 if with_league_column else 8
    rows = stats if with_league_column else [r[:2] + r[3:] for r in stats]
    conn.executemany(f"INSERT INTO players_stats VALUES ({','.join('?' * cols)})", rows)
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{path}")
    engine._tmp = tmp   # keep the directory alive for the test's lifetime
    return engine


class TestNoDuplicates:
    def test_two_leagues_still_give_one_row_per_player(self):
        pool = load_candidates(_engine(), 2026, drafted_ids=set(), league_id="780575")
        assert list(pool["player_id"]) == [1, 2]

    def test_the_named_league_supplies_the_numbers(self):
        """Not just deduplicated - deduplicated to the *right* row."""
        pool = load_candidates(_engine(), 2026, drafted_ids=set(), league_id="197229335")
        assert pool.set_index("player_id")["points_last_year"].to_dict() == {1: 288.0, 2: 272.0}

    def test_naming_no_league_still_returns_one_row_each(self):
        """The notebooks call this with no league at all."""
        pool = load_candidates(_engine(), 2026, drafted_ids=set())
        assert list(pool["player_id"]) == [1, 2]


class TestFallbacks:
    def test_a_league_with_no_stored_seasons_borrows_the_fullest(self):
        """A brand-new league has no history of its own.

        Borrowing beats an empty join, which would silently mark every
        established player a rookie with no production to reason about.
        """
        engine = _engine(stats=STATS[:2] + STATS[2:3])   # 780575 has 2 rows, other has 1
        pool = load_candidates(engine, 2026, drafted_ids=set(), league_id="99999")
        assert list(pool["player_id"]) == [1, 2]
        assert pool.set_index("player_id")["points_last_year"][1] == 240.0
        assert (pool["is_rookie"] == 0).all()

    def test_a_database_without_the_column_is_left_unfiltered(self):
        """Fixtures and pre-migration databases must keep working."""
        engine = _engine(with_league_column=False)
        assert resolve_stats_league(engine, 2025, "780575") is None
        # The query runs unfiltered, which is the old behavior exactly. A
        # pre-migration database holds one league, so that's one row per
        # player; this fixture holds two leagues' rows with nothing to tell
        # them apart, and duplicating is the honest result of that.
        pool = load_candidates(engine, 2026, drafted_ids=set(), league_id="780575")
        assert len(pool) == 4

    def test_no_stored_seasons_at_all_is_not_an_error(self):
        engine = _engine(stats=[])
        assert resolve_stats_league(engine, 2025, "780575") is None
        pool = load_candidates(engine, 2026, drafted_ids=set(), league_id="780575")
        assert list(pool["player_id"]) == [1, 2]
        assert (pool["is_rookie"] == 1).all()


@pytest.mark.parametrize("asked,expected", [
    ("780575", "780575"),        # stored: use it
    ("197229335", "197229335"),
    ("99999", "780575"),         # not stored: the fullest league
    (None, "780575"),            # unnamed: same
])
def test_resolve_stats_league(asked, expected):
    engine = _engine(stats=STATS[:2] + STATS[2:3])
    assert resolve_stats_league(engine, 2025, asked) == expected
