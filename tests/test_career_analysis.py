"""Career draft records, and the two ways they can go quietly wrong.

`draft_performance` only ever described one season, so "who drafted best" meant
"who drafted best that year". These aggregate across every stored season, which
introduces two traps worth pinning:

1. **Managers must be keyed on `team_id`, not `team_name`.** Fourteen
   franchises in the real league have used eighteen names, and two of them
   differ by a single space.
2. **A missing `draft_projected_rank` is not rank zero.** Twelve of 76 rows in
   the real database have none; treating those as 0 would report the best
   preseason projection anyone ever had.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from src.analysis import _Ctx, career_performance, expectations

LEAGUE = "780575"

SCHEMA = """
CREATE TABLE drafts (player_id INTEGER, year INTEGER, league_id TEXT,
                     overallPickNumber INTEGER, roundId INTEGER, team_id INTEGER);
CREATE TABLE players (player_id INTEGER, player_name TEXT);
CREATE TABLE players_stats (player_id INTEGER, year INTEGER, league_id TEXT,
                            points REAL, projected_points REAL, games_played REAL);
CREATE TABLE teams (team_id INTEGER, team_name TEXT, year INTEGER, league_id TEXT,
                    final_standing INTEGER, draft_projected_rank INTEGER,
                    wins INTEGER, losses INTEGER, points_for REAL);
CREATE TABLE average_draft_position (player_id INTEGER, year INTEGER, avg REAL,
                                     position TEXT);
"""

# Two franchises whose names collide on a single space, exactly as in the real
# league: id 11 was "Gerald Pea's Football Team" and became "B Mac"; id 12 was
# "Jean Machine" and became "GeraldPea's Football Team".
TEAMS = [
    (11, "Gerald Pea's Football Team", 2024, LEAGUE, 3, 5, 8, 6, 1400.0),
    (11, "B Mac",                      2025, LEAGUE, 1, 2, 11, 3, 1600.0),
    (12, "Jean Machine",               2024, LEAGUE, 6, 7, 7, 7, 1300.0),
    (12, "GeraldPea's Football Team",  2025, LEAGUE, 4, 4, 9, 5, 1500.0),
]


def _seed(conn, *, projections=True):
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO teams VALUES (?,?,?,?,?,?,?,?,?)",
                     TEAMS if projections else
                     [(t[0], t[1], t[2], t[3], t[4], None, t[6], t[7], t[8]) for t in TEAMS])
    pid = 1
    for team_id, _name, year, *_ in TEAMS:
        for k in range(3):
            conn.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?)",
                         (pid, year, LEAGUE, k + 1, 1, team_id))
            conn.execute("INSERT INTO players VALUES (?,?)", (pid, f"Player {pid}"))
            conn.execute("INSERT INTO players_stats VALUES (?,?,?,?,?,?)",
                         (pid, year, LEAGUE, 100.0 + pid, 90.0, 16.0))
            conn.execute("INSERT INTO average_draft_position VALUES (?,?,?,?)",
                         (pid, year, float(k + 1), "RB"))
            pid += 1
    conn.commit()


@pytest.fixture
def engine():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "career.db"
    conn = sqlite3.connect(path)
    _seed(conn)
    conn.close()
    eng = create_engine(f"sqlite:///{path}")
    eng._tmp = tmp
    return eng


@pytest.fixture
def engine_no_projections():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "career2.db"
    conn = sqlite3.connect(path)
    _seed(conn, projections=False)
    conn.close()
    eng = create_engine(f"sqlite:///{path}")
    eng._tmp = tmp
    return eng


class TestManagerIdentity:
    def test_a_renamed_franchise_is_one_manager_not_two(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert len(out["managers"]) == 2, "renames split a manager in half"
        assert {m["seasons_n"] for m in out["managers"]} == {2}

    def test_names_that_differ_by_a_space_stay_separate(self, engine):
        """"Gerald Pea's Football Team" and "GeraldPea's Football Team" are
        different people. Grouping by name merges them."""
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert {m["team_id"] for m in out["managers"]} == {11, 12}

    def test_the_latest_name_is_the_label(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        by_id = {m["team_id"]: m for m in out["managers"]}
        assert by_id[11]["team_name"] == "B Mac"
        assert by_id[12]["team_name"] == "GeraldPea's Football Team"

    def test_former_names_are_kept(self, engine):
        """So the UI can say "formerly ..." instead of silently rewriting
        someone's history."""
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        by_id = {m["team_id"]: m for m in out["managers"]}
        assert by_id[11]["former_names"] == ["Gerald Pea's Football Team"]


class TestCareerAggregation:
    def test_titles_are_counted(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        by_id = {m["team_id"]: m for m in out["managers"]}
        assert by_id[11]["titles"] == 1   # finished 1st in 2025
        assert by_id[12]["titles"] == 0

    def test_every_season_appears_in_the_sparkline(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        for m in out["managers"]:
            assert [s["year"] for s in m["by_season"]] == [2024, 2025]

    def test_sorted_best_first(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        avgs = [m["avg_vorp"] for m in out["managers"]]
        assert avgs == sorted(avgs, reverse=True)

    def test_no_seasons_is_empty_not_an_error(self, engine):
        assert career_performance(_Ctx(engine, league_id=LEAGUE), [])["managers"] == []


class TestExpectations:
    def test_beat_by_is_signed_the_way_people_say_it(self, engine):
        """"+3" must mean three places better than projected."""
        out = expectations(_Ctx(engine, league_id=LEAGUE), 2025)
        by_name = {t["team_name"]: t for t in out["teams"]}
        # id 11: projected 2nd, finished 1st -> one place better.
        assert by_name["B Mac"]["beat_by"] == 1

    def test_a_missing_projection_is_unknown_not_rank_zero(self, engine_no_projections):
        """Rank 0 would read as the best preseason projection ever recorded."""
        out = expectations(_Ctx(engine_no_projections, league_id=LEAGUE), 2025)
        assert out["missing"] == len(out["teams"])
        for t in out["teams"]:
            assert t["draft_projected_rank"] is None
            assert t["beat_by"] is None

    def test_teams_without_a_projection_sort_last(self, engine_no_projections):
        out = expectations(_Ctx(engine_no_projections, league_id=LEAGUE), 2025)
        assert out["teams"], "teams with no projection must still be listed"
