"""One league's fitted bias must survive another league being fitted.

`persist_league_bias` used to write six tables with `to_sql(if_exists="replace")`,
and none of them had a `league_id`. So running the analysis on league B dropped
league A's fit entirely — and because `league_bias_manager` carries real team
names, B was then served A's leaguemates. On one laptop that was lossy; with
more than one user it is cross-tenant data destruction.

The subtle part is the ordering: `replace` *drops and recreates* the table, so a
migration that only ran at boot would be undone by the next write. These pin
both the isolation and the ordering.
"""
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.biases import load_league_bias, persist_league_bias
from src.migrations import BIAS_TABLES, add_bias_league_id, fitted_leagues

LEAGUE_A = "780575"
LEAGUE_B = "197229335"


def _fit(position_shift: float, manager_name: str) -> dict:
    """A minimal fit in the shape `fit_league_bias` returns."""
    return {
        "tables": {
            "position": pd.DataFrame([
                {"position": "QB", "n": 100, "mean": position_shift,
                 "sd": 1.0, "t": -5.0, "shrunk": position_shift,
                 "seasons_same_sign": 5, "seasons": 5, "p_perm": 0.01},
            ]),
            "manager": pd.DataFrame([
                {"team_id": 1, "team_name": manager_name, "n": 50, "mean": 3.0,
                 "sd": 1.0, "t": 2.0, "shrunk": 2.5, "seasons_n": 5},
            ]),
            "player": pd.DataFrame([
                {"player_id": 1, "player_name": "A Player", "n": 3, "mean": -20.0,
                 "sd": 1.0, "seasons": "2020", "seasons_same_sign": 3,
                 "passes_strict_filter": 1},
            ]),
        },
        "meta": {"years": "2020-2025", "adp_cutoff": 180.0, "n_picks": 996,
                 "resid_sd": 19.6, "k_position": 7.9, "k_proteam": 32.7,
                 "k_manager": 19.9},
    }


@pytest.fixture
def engine():
    tmp = tempfile.TemporaryDirectory()
    eng = create_engine(f"sqlite:///{Path(tmp.name) / 'bias.db'}")
    eng._tmp = tmp
    return eng


def _rows(engine, table, league_id):
    with engine.begin() as conn:
        return conn.execute(
            text(f"SELECT * FROM {table} WHERE league_id = :l"),
            {"l": league_id}).fetchall()


class TestOneLeagueDoesNotDestroyAnother:
    def test_persisting_b_leaves_as_rows_intact(self, engine):
        """The direct regression. This used to wipe A completely."""
        persist_league_bias(engine, _fit(-11.1, "A's Manager"), LEAGUE_A)
        before = _rows(engine, "league_bias_position", LEAGUE_A)
        assert before, "league A should have rows to begin with"

        persist_league_bias(engine, _fit(-3.0, "B's Manager"), LEAGUE_B)

        assert _rows(engine, "league_bias_position", LEAGUE_A) == before

    def test_each_league_loads_its_own_numbers(self, engine):
        persist_league_bias(engine, _fit(-11.1, "A's Manager"), LEAGUE_A)
        persist_league_bias(engine, _fit(-3.0, "B's Manager"), LEAGUE_B)

        assert load_league_bias(engine, LEAGUE_A)["position"]["QB"] == pytest.approx(-11.1)
        assert load_league_bias(engine, LEAGUE_B)["position"]["QB"] == pytest.approx(-3.0)

    def test_a_leagues_manager_names_do_not_reach_b(self, engine):
        """`league_bias_manager` holds real people's team names."""
        persist_league_bias(engine, _fit(-11.1, "Ambler Thighs"), LEAGUE_A)
        persist_league_bias(engine, _fit(-3.0, "Someone Else"), LEAGUE_B)

        names_b = {r[2] for r in _rows(engine, "league_bias_manager", LEAGUE_B)}
        assert "Ambler Thighs" not in names_b

    def test_meta_holds_one_row_per_league(self, engine):
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        persist_league_bias(engine, _fit(-3.0, "B"), LEAGUE_B)
        assert sorted(fitted_leagues(engine)) == sorted([LEAGUE_A, LEAGUE_B])


class TestIdempotence:
    def test_persisting_twice_does_not_duplicate(self, engine):
        """Re-running an analysis must replace, not accumulate - the same
        property `store_season` guarantees for pulled history."""
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        first = len(_rows(engine, "league_bias_position", LEAGUE_A))
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        assert len(_rows(engine, "league_bias_position", LEAGUE_A)) == first
        assert fitted_leagues(engine) == [LEAGUE_A]

    def test_a_refit_updates_the_numbers(self, engine):
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        persist_league_bias(engine, _fit(-8.0, "A"), LEAGUE_A)
        assert load_league_bias(engine, LEAGUE_A)["position"]["QB"] == pytest.approx(-8.0)


class TestTheReplaceTrap:
    def test_the_league_id_column_survives_a_write(self, engine):
        """`to_sql(if_exists="replace")` drops and recreates the table.

        A migration that ran only at boot would therefore be silently undone by
        the very next persist, and the DELETE that scopes the write would have
        no column to filter on. `persist_league_bias` re-runs the migration for
        exactly this reason.
        """
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        for table in BIAS_TABLES:
            with engine.begin() as conn:
                exists = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
                    {"n": table}).first()
                if not exists:
                    continue
                cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            assert "league_id" in cols, f"{table} lost its league_id"


class TestMigration:
    def test_backfills_from_meta_not_the_environment(self, engine, monkeypatch):
        """The fit recorded which league it measured; LEAGUE_ID may say otherwise.

        They genuinely differ on the real database - the last fit there was a
        second league, not the one in the environment - so trusting the env var
        would relabel one league's rows as another's.
        """
        monkeypatch.setenv("LEAGUE_ID", "999999")
        # A legacy shape: rows with no league_id, and a meta row that knows.
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE league_bias_position "
                              "(position TEXT, shrunk REAL)"))
            conn.execute(text("INSERT INTO league_bias_position VALUES ('QB', -11.1)"))
            conn.execute(text("CREATE TABLE league_bias_meta (league_id TEXT)"))
            conn.execute(text(f"INSERT INTO league_bias_meta VALUES ('{LEAGUE_A}')"))

        add_bias_league_id(engine)

        assert _rows(engine, "league_bias_position", LEAGUE_A)
        assert not _rows(engine, "league_bias_position", "999999")

    def test_is_idempotent(self, engine):
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        before = add_bias_league_id(engine)
        after = add_bias_league_id(engine)
        assert after["added"] == [], "a second run must add nothing"

    def test_a_pre_migration_database_still_loads(self, engine):
        """A database from before this change has no league_id to filter on;
        reading it whole is better than returning nothing."""
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE league_bias_position "
                              "(position TEXT, shrunk REAL)"))
            conn.execute(text("INSERT INTO league_bias_position VALUES ('QB', -11.1)"))
        assert load_league_bias(engine, LEAGUE_A)["position"]["QB"] == pytest.approx(-11.1)


class TestRefusesToGuess:
    def test_several_leagues_and_none_named_is_neutral(self, engine):
        """Picking one arbitrarily would label a league's drafters with another
        league's habits - the exact error the scoping exists to prevent."""
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        persist_league_bias(engine, _fit(-3.0, "B"), LEAGUE_B)

        out = load_league_bias(engine)
        # Falls back to the measured defaults rather than either league's fit.
        assert out["meta"].get("source") == "default"

    def test_a_single_stored_league_is_used_when_unnamed(self, engine):
        """Single-league databases - the notebooks, a fresh checkout - keep
        behaving exactly as they did."""
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        assert load_league_bias(engine)["position"]["QB"] == pytest.approx(-11.1)

    def test_a_league_with_no_fit_gets_nothing(self, engine):
        """Empty, not another league's numbers - which the board renders as a
        dash in the Reach column rather than a fabricated zero."""
        persist_league_bias(engine, _fit(-11.1, "A"), LEAGUE_A)
        assert load_league_bias(engine, "555555")["position"] == {}
