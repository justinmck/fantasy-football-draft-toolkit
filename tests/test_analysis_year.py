"""Which season the analysis describes, now that the reader picks it.

The Analysis tab used to show one season - whichever the league last drafted
in - so the year argument was barely exercised. With a season switcher on the
page it decides what every retrospective section says, and the rule it follows
matters: honour the request when the league played that season, fall back
rather than render an empty page when it didn't.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from src.analysis import league_analysis

SCHEMA = """
CREATE TABLE drafts (
    player_id INTEGER, year INTEGER, league_id TEXT,
    overallPickNumber INTEGER, roundId INTEGER, team_id INTEGER
);
"""

# Two seasons for one league, one for another, so "this league's seasons" is a
# real filter rather than "every season in the table".
ROWS = [
    (1, 2022, "780575", 1, 1, 10),
    (2, 2022, "780575", 2, 1, 11),
    (1, 2024, "780575", 1, 1, 10),
    (2, 2024, "780575", 2, 1, 11),
    (1, 2023, "197229335", 1, 1, 20),
]


@pytest.fixture
def engine():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "years.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO drafts VALUES (?,?,?,?,?,?)", ROWS)
    conn.commit()
    conn.close()
    eng = create_engine(f"sqlite:///{path}")
    eng._tmp = tmp
    yield eng


class TestYearSelection:
    def test_a_requested_season_is_honoured(self, engine):
        """The switcher's whole job. 2022 must not silently become 2024."""
        assert league_analysis(engine, 2022, league_id="780575")["year"] == 2022

    def test_every_stored_season_is_reachable(self, engine):
        for y in (2022, 2024):
            assert league_analysis(engine, y, league_id="780575")["year"] == y

    def test_the_seasons_offered_are_this_league_s_own(self, engine):
        """The switcher renders one chip per entry here, so a season belonging
        to another league appearing in this list would offer a year that has
        nothing behind it."""
        assert league_analysis(engine, None, league_id="780575")["seasons"] == [2022, 2024]
        assert league_analysis(engine, None, league_id="197229335")["seasons"] == [2023]

    def test_no_year_asked_for_gives_the_latest_the_league_drafted(self, engine):
        assert league_analysis(engine, None, league_id="780575")["year"] == 2024

    def test_a_season_the_league_didnt_play_falls_back(self, engine):
        """2023 exists in the table, but not for this league - McFL skipped it.

        Rendering an empty page for a year the user can't have selected from
        the switcher is worse than showing the nearest real season.
        """
        assert league_analysis(engine, 2023, league_id="780575")["year"] == 2024

    def test_a_league_with_no_history_keeps_the_year_it_was_asked_for(self, engine):
        """Nothing to fall back to, and the year still labels the sections
        that don't need draft history."""
        out = league_analysis(engine, 2025, league_id="99999")
        assert out["year"] == 2025
        assert out["seasons"] == [] and out["has_history"] is False
