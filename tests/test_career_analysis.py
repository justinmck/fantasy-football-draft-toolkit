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

from src.analysis import _Ctx, career_performance, expectations, trophy_case

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


class TestTrophyCase:
    """Championships, read from `teams` rather than from the draft join.

    `career_performance` already counts titles, but off the *draft* frame - so a
    season a franchise played and has no picks stored for is invisible to it. A
    championship is the one number in this league nobody will accept being
    quietly wrong, so it comes from the standings table directly.
    """

    def test_a_title_won_under_an_old_name_belongs_to_the_current_one(self, engine):
        """Team 11 won in 2025 as "B Mac" having been "Gerald Pea's Football
        Team" in 2024. One franchise, one trophy, listed under today's name."""
        out = trophy_case(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        champs = [f for f in out["franchises"] if f["titles"]]
        assert len(champs) == 1
        assert champs[0]["team_name"] == "B Mac"
        assert champs[0]["former_names"] == ["Gerald Pea's Football Team"]
        assert champs[0]["title_years"] == [2025]

    def test_names_that_differ_by_a_space_stay_separate(self, engine):
        out = trophy_case(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert {f["team_id"] for f in out["franchises"]} == {11, 12}

    def test_a_champion_with_no_scoring_rows_still_counts(self, engine):
        """`luck` filters on `points_for > 0`; copying that filter here would
        drop the champion of any season stored without scoring totals."""
        import sqlite3
        path = engine.url.database
        conn = sqlite3.connect(path)
        conn.execute("UPDATE teams SET points_for = NULL WHERE year = 2025")
        conn.commit()
        conn.close()
        out = trophy_case(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert [f["title_years"] for f in out["franchises"] if f["titles"]] == [[2025]]

    def test_runner_ups_outrank_never_placed(self, engine):
        """A franchise with no title but a second-place finish sorts above one
        that has never been near it - the ordering is the finding."""
        out = trophy_case(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert out["franchises"][0]["titles"] == 1
        assert out["never_won"] == 1

    def test_no_repeat_champion_is_reported_as_such(self, engine):
        out = trophy_case(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert out["distinct_champions"] == 1
        assert out["repeat_champion"] is False

    def test_a_league_with_no_standings_is_empty_not_an_error(self, engine):
        import sqlite3
        conn = sqlite3.connect(engine.url.database)
        conn.execute("UPDATE teams SET final_standing = NULL")
        conn.commit()
        conn.close()
        assert trophy_case(_Ctx(engine, league_id=LEAGUE), [2024, 2025])["franchises"] == []


class TestAllTimeCoverage:
    """Grading a draft must not require the market price it went against.

    ESPN publishes no average draft position before 2020, and `_DRAFT_SEASON_SQL`
    joined it as an inner join - so eight of this league's fourteen seasons were
    invisible to every section built on that frame, and a leaderboard captioned
    "all time" silently meant "since 2020". What a pick scored and what position
    the player played both live on `players_stats`; ADP only answers whether the
    pick was a *bargain*, which is a different question.
    """

    def test_a_season_with_no_adp_is_still_graded(self, engine):
        import sqlite3
        conn = sqlite3.connect(engine.url.database)
        # `players_stats` in this fixture has no position column, so add one:
        # that is what carries a pre-ADP season.
        conn.execute("ALTER TABLE players_stats ADD COLUMN position TEXT")
        conn.execute("UPDATE players_stats SET position = 'RB'")
        conn.execute("DELETE FROM average_draft_position WHERE year = 2024")
        conn.commit()
        conn.close()
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert 2024 in out["seasons"], "a season without ADP must still be graded"
        for m in out["managers"]:
            assert m["seasons_n"] == 2

    def test_without_a_position_column_it_degrades_rather_than_failing(self, engine):
        """A partially built database is a legitimate state, not a crash."""
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        assert out["managers"], "should still load using the ADP position"


class TestSmallSamplesDoNotWinTheLeaderboard:
    """A franchise with eight graded picks is not the best drafter in the league.

    Opening the range to the whole history brought in teams that played two early
    seasons, and on a raw average three of them took the top three places ahead
    of managers with a hundred and fifty picks. Each average is now pulled toward
    the league average in proportion to how little supports it - the same
    empirical-Bayes estimator `src/biases.py` uses for draft habits.
    """

    def test_the_raw_average_is_still_reported(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        for m in out["managers"]:
            assert "avg_vorp" in m and "avg_vorp_shrunk" in m

    def test_shrinking_moves_estimates_toward_the_league_average(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        mean = out["league_avg_vorp"]
        for m in out["managers"]:
            # Never past the mean, and never further from it than the raw value.
            assert abs(m["avg_vorp_shrunk"] - mean) <= abs(m["avg_vorp"] - mean) + 1e-9

    def test_ranking_follows_the_shrunk_value(self, engine):
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        vals = [m["avg_vorp_shrunk"] for m in out["managers"]]
        assert vals == sorted(vals, reverse=True)

    def test_a_thin_record_cannot_outrank_a_deep_one_on_noise(self, engine):
        """The property that matters, stated directly: give one manager a huge
        average off very few picks and a second a good average off many, and the
        second must still rank higher."""
        import sqlite3
        path = engine.url.database
        conn = sqlite3.connect(path)
        # Team 12 keeps three ordinary picks; team 11 gets many solid ones.
        conn.execute("DELETE FROM drafts WHERE team_id = 12 AND overallPickNumber > 1")
        pid = 500
        for year in (2024, 2025):
            for k in range(40):
                conn.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?)",
                             (pid, year, LEAGUE, k + 10, 2, 11))
                conn.execute("INSERT INTO players VALUES (?,?)", (pid, f"Filler {pid}"))
                conn.execute("INSERT INTO players_stats VALUES (?,?,?,?,?,?)",
                             (pid, year, LEAGUE, 150.0, 90.0, 16.0))
                conn.execute("INSERT INTO average_draft_position VALUES (?,?,?,?)",
                             (pid, year, float(k + 10), "RB"))
                pid += 1
        conn.commit()
        conn.close()
        out = career_performance(_Ctx(engine, league_id=LEAGUE), [2024, 2025])
        by_id = {m["team_id"]: m for m in out["managers"]}
        assert by_id[11]["picks"] > by_id[12]["picks"]
        order = [m["team_id"] for m in out["managers"]]
        assert order.index(11) < order.index(12), \
            "the manager with the deep record must outrank the thin one"


class TestLeagueSize:
    """Replacement level is a function of how many teams there are.

    The section reporting it says the number comes from the league's own
    settings, but it was computed from a module default of fourteen - so a
    ten-team league was shown a fourteen-team baseline and told it was theirs.
    This league itself ran eight teams for its first seven seasons.
    """

    def test_it_reads_the_season_not_the_default(self, engine):
        from src.analysis import league_size
        # The fixture has two franchises per season.
        assert league_size(_Ctx(engine, league_id=LEAGUE), 2025) == 2

    def test_a_season_with_no_teams_falls_back_rather_than_failing(self, engine):
        from notebooks.config import TEAMS
        from src.analysis import league_size
        assert league_size(_Ctx(engine, league_id=LEAGUE), 1999) == TEAMS

    def test_replacement_level_reports_that_size(self, engine):
        from src.analysis import replacement_levels
        out = replacement_levels(_Ctx(engine, league_id=LEAGUE), 2025)
        assert out["teams"] == 2, "the page states this number as the league's own"

    def test_a_smaller_league_sets_a_higher_bar(self, engine):
        """Fewer teams means fewer starters, so the last startable player is a
        better one - replacement level goes up, and VORP goes down."""
        from src.scoring import compute_baselines
        import pandas as pd
        df = pd.DataFrame({"position": ["RB"] * 10,
                           "points": [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]})
        small = compute_baselines(df, teams=2, value_col="points")["RB"]
        big = compute_baselines(df, teams=8, value_col="points")["RB"]
        assert small > big
