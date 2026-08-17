"""Tests for league draft bias.

`league_pick_est` feeds the availability and urgency multipliers, so a wrong
number here silently changes what the board recommends without changing any
value it displays. These pin the arithmetic, the guards, and — most importantly
— the lineup-slot bug that made the previous implementation fit on the wrong
population.
"""
import json
import re
import math

import numpy as np
import pandas as pd
import pytest

from src.biases import (
    DEFAULT_LEAGUE_BIAS,
    JUNK_PRO_TEAMS,
    MIN_REPORTABLE_SHIFT,
    _empirical_bayes_k,
    _shrink,
    apply_league_bias,
    load_league_bias,
)
from src.db import engine
from src.scoring import NO_ADP_SENTINEL, availability


def pool(**overrides):
    """A minimal candidate pool in the shape `apply_league_bias` receives."""
    base = {
        "player_id": [1, 2, 3],
        "player_name": ["A QB", "A WR", "No Market"],
        "position": ["QB", "WR", "RB"],
        "pro_team": ["BUF", "PHI", "FA"],
        "adp": [20.0, 40.0, 999.0],
    }
    base.update(overrides)
    return pd.DataFrame(base)


class TestApplyLeagueBias:
    def test_empty_bias_leaves_adp_untouched(self):
        # Matches the `bias or` fallback branch in src/recommender.py, which
        # assigns league_pick_est = adp when there's no fit at all.
        out = apply_league_bias(pool(), {})
        assert out["league_pick_est"].tolist() == [20.0, 40.0, 999.0]

    def test_an_unfitted_league_reports_no_shift_rather_than_zero(self):
        """The Reach column's dash.

        Zero would read as "your league drafts everyone at market", which is a
        measurement nobody took. Scoring is unaffected either way - the pick
        estimate stays at ADP - but the number is reported to the user.
        """
        out = apply_league_bias(pool(), {})
        assert out["bias_shift"].isna().all()
        assert out["bias_pos_shift"].isna().all()
        assert out["bias_team_shift"].isna().all()
        assert out["bias_reason"].isna().all()

    def test_a_fitted_league_keeps_its_real_zeroes(self):
        """A position with no measurable habit genuinely shifts by nothing.

        That's a finding, not an absence, and must stay distinguishable from
        the case above.
        """
        out = apply_league_bias(pool(), {"position": {"QB": -10.0}})
        assert out.loc[0, "bias_shift"] == pytest.approx(-10.0)
        assert out.loc[1, "bias_shift"] == 0.0        # WR: measured, no effect
        assert not out["bias_shift"].isna().any()

    def test_position_shift_is_additive(self):
        out = apply_league_bias(pool(), {"position": {"QB": -10.0}})
        assert out.loc[0, "league_pick_est"] == pytest.approx(10.0)

    def test_position_and_team_shifts_compose(self):
        out = apply_league_bias(
            pool(), {"position": {"WR": 3.0}, "pro_team": {"PHI": -9.0}}
        )
        assert out.loc[1, "bias_pos_shift"] == pytest.approx(3.0)
        assert out.loc[1, "bias_team_shift"] == pytest.approx(-9.0)
        assert out.loc[1, "league_pick_est"] == pytest.approx(34.0)

    @pytest.mark.parametrize("team", sorted(JUNK_PRO_TEAMS) + ["ZZZ"])
    def test_junk_and_unknown_pro_teams_shift_by_zero(self, team):
        # "FA" is what load_candidates fills for free agents - a real, common
        # value, not an edge case.
        df = pool(pro_team=[team, team, team], adp=[20.0, 40.0, 60.0])
        out = apply_league_bias(df, {"pro_team": {"PHI": -9.0}})
        assert out["bias_team_shift"].tolist() == [0.0, 0.0, 0.0]
        assert not out["bias_team_shift"].isna().any()

    def test_missing_pro_team_column_is_tolerated(self):
        df = pool().drop(columns="pro_team")
        out = apply_league_bias(df, {"position": {"QB": -10.0}, "pro_team": {"PHI": -9.0}})
        assert out.loc[0, "league_pick_est"] == pytest.approx(10.0)

    def test_never_estimates_a_pick_before_the_first(self):
        df = pool(position=["QB"], player_id=[1], player_name=["Early QB"],
                  pro_team=["BUF"], adp=[3.0])
        out = apply_league_bias(df, {"position": {"QB": -11.1}})
        assert out.loc[0, "league_pick_est"] == 1.0

    def test_no_market_players_are_not_shifted(self):
        """A sentinel is not a pick number, so it must not move.

        Shifting it happens to stay above the threshold today, which would make
        this work by coincidence and couple two unrelated constants.
        """
        out = apply_league_bias(pool(), {"position": {"RB": -50.0}})
        assert out.loc[2, "league_pick_est"] == pytest.approx(999.0)
        assert out.loc[2, "bias_shift"] == 0.0
        assert availability(out.loc[2, "league_pick_est"], next_pick=30) == 1.0

    def test_player_effects_are_off_by_default(self):
        bias = {"player": {1: -25.0}}
        # Nothing applied, so nothing measured to report - see the dash test above.
        assert pd.isna(apply_league_bias(pool(), bias).loc[0, "bias_shift"])
        assert apply_league_bias(pool(), bias, include_player=True).loc[0, "bias_shift"] == -25.0


class TestPlayerHistoryIsEvidenceNotScore:
    """The Reach column's dot: this player's own record here.

    It's reported so a drafter can see what backs the estimate, and kept out of
    the estimate because two or three drafts against a ~19-pick spread is not
    enough to move a pick prediction with.
    """

    HISTORY = {"player_history": {1: {"mean": -30.0, "n": 3}}}

    def test_history_is_reported(self):
        out = apply_league_bias(pool(), self.HISTORY)
        assert out["bias_player_shift"].tolist()[0] == -30.0
        assert out["bias_player_n"].tolist()[0] == 3

    def test_history_does_not_move_the_pick_estimate(self):
        out = apply_league_bias(pool(), self.HISTORY)
        assert out["league_pick_est"].tolist() == [20.0, 40.0, 999.0]

    def test_history_alone_is_not_a_measured_league(self):
        """Player rows exist but nothing is applied, so Reach still shows a dash.

        Otherwise the 25 players with a record would read as measured while
        everyone else read as "par", from the same empty fit.
        """
        out = apply_league_bias(pool(), self.HISTORY)
        assert out["bias_shift"].isna().all()

    def test_history_shows_beside_a_real_fit(self):
        out = apply_league_bias(pool(), {**self.HISTORY, "position": {"QB": -10.0}})
        assert out.loc[0, "bias_shift"] == pytest.approx(-10.0)   # what's applied
        assert out.loc[0, "bias_player_shift"] == -30.0           # what backs it

    def test_players_without_a_record_report_nothing(self):
        """Missing, not zero - "never measured" isn't "measured as no effect".

        It leaves here as NaN, which pandas produces for a float column with
        holes; `recommend` turns that into JSON null on the way out, and the UI
        distinguishes null from 0 to decide whether to show the dot at all.
        """
        out = apply_league_bias(pool(), self.HISTORY)
        assert out["bias_player_shift"].isna().tolist() == [False, True, True]
        assert (out["bias_player_shift"].fillna(0) != 0).tolist() == [True, False, False]

    def test_a_fit_with_no_player_table_still_returns_the_columns(self):
        """The response shape can't depend on which league you're looking at."""
        out = apply_league_bias(pool(), {"position": {"QB": -10.0}})
        assert out["bias_player_shift"].isna().all()
        assert out["bias_player_n"].isna().all()

    def test_load_exposes_only_strict_filter_players(self):
        """The unfiltered table has n=1 deltas of 100+ picks - pure noise."""
        loaded = load_league_bias(engine)
        history = loaded.get("player_history", {})
        assert all(v["n"] >= 2 for v in history.values()), "n=1 rows must not reach the UI"
        assert set(history) == set(loaded.get("player", {}))


class TestBiasReason:
    def test_none_not_nan_below_the_reporting_threshold(self):
        out = apply_league_bias(pool(), {"position": {"QB": -1.0}})
        assert out.loc[0, "bias_reason"] is None

    def test_reason_names_both_components(self):
        out = apply_league_bias(
            pool(), {"position": {"WR": 3.0}, "pro_team": {"PHI": -9.0}}
        )
        reason = out.loc[1, "bias_reason"]
        assert "PHI" in reason and "WR" in reason
        assert "earlier" in reason and "later" in reason

    @pytest.mark.parametrize("shift", [-30.0, -11.1, -6.2, -2.0, -1.6, 1.6, 2.0, 6.1, 7.1, 30.0])
    def test_pick_counts_are_never_mis_pluralised(self, shift):
        """No "1 picks" at any magnitude.

        The singular branch is currently unreachable — components below 1.5
        picks aren't named at all, so the smallest number that ever prints is 2
        — but it stays in place so lowering that threshold can't quietly
        introduce a grammar bug.
        """
        df = pool(position=["QB"], player_id=[1], player_name=["X"],
                  pro_team=["BUF"], adp=[80.0])
        reason = apply_league_bias(df, {"position": {"QB": shift}}).loc[0, "bias_reason"]
        if reason:
            assert not re.search(r"\b1 picks\b", reason)
            assert re.search(r"\b\d+ picks?\b", reason)

    def test_components_below_a_pick_and_a_half_are_not_named(self):
        """The total clears the reporting threshold; one component doesn't.

        A 1.2-pick nudge rounds to "about 1 pick", which claims precision the
        estimate doesn't have — so the sentence names the receiver effect and
        stays quiet about the team.
        """
        out = apply_league_bias(
            pool(), {"position": {"WR": 4.0}, "pro_team": {"PHI": -1.2}}
        )
        assert out.loc[1, "bias_shift"] == pytest.approx(2.8)
        reason = out.loc[1, "bias_reason"]
        assert "WR" in reason and "PHI" not in reason

    def test_payload_is_json_safe(self):
        """NaN isn't valid JSON and 500s /recommend - the exact bug already
        fixed once for rookies with no prior stats."""
        out = apply_league_bias(pool(), {"position": {"QB": -11.1}})
        cols = ["bias_shift", "bias_pos_shift", "bias_team_shift", "bias_reason"]
        records = out[cols].replace({np.nan: None}).to_dict(orient="records")
        assert "NaN" not in json.dumps(records)


class TestShrink:
    def test_zero_n_contributes_nothing(self):
        assert _shrink(50.0, 0, 8.0) == 0.0

    def test_k_zero_is_a_no_op(self):
        assert _shrink(12.0, 30, 0.0) == pytest.approx(12.0)

    def test_infinite_k_shrinks_to_zero(self):
        assert _shrink(12.0, 30, float("inf")) == 0.0

    def test_monotone_in_sample_size(self):
        vals = [_shrink(10.0, n, 20.0) for n in (5, 20, 100, 500)]
        assert vals == sorted(vals)
        assert vals[-1] < 10.0

    def test_small_groups_are_muted_more_than_large_ones(self):
        # The property that makes this preferable to a |t| > 2 gate.
        assert abs(_shrink(-17.0, 11, 33.0)) < abs(_shrink(-17.0, 41, 33.0))


class TestEmpiricalBayesK:
    def test_returns_inf_when_spread_is_pure_noise(self):
        # Must not divide by zero: identical group means is a legitimate result.
        assert math.isinf(_empirical_bayes_k([0.0, 0.0, 0.0], [10, 10, 10], 100.0))

    def test_real_spread_gives_finite_k(self):
        k = _empirical_bayes_k([-15.0, 0.0, 12.0], [40, 40, 40], 100.0)
        assert math.isfinite(k) and k > 0

    def test_degenerate_inputs_do_not_raise(self):
        assert math.isinf(_empirical_bayes_k([], [], 10.0))
        assert math.isinf(_empirical_bayes_k([1.0], [5], 10.0))
        assert math.isinf(_empirical_bayes_k([1.0, 2.0], [5, 5], 0.0))


class TestLoadLeagueBias:
    def test_falls_back_without_raising_on_a_bare_database(self):
        """The fixture has no league_bias_* tables, no `teams` and no `players`.

        This is why src/api.py loads rather than fits at import - a live fit
        would break every API test at collection time.
        """
        bias = load_league_bias(engine)
        assert bias["position"] == DEFAULT_LEAGUE_BIAS["position"]
        assert bias["pro_team"] == {}

    def test_default_carries_the_headline_effect(self):
        # QB is the strongest, most consistent finding in the league's history.
        assert DEFAULT_LEAGUE_BIAS["position"]["QB"] < -MIN_REPORTABLE_SHIFT


class TestLineupSlotRegression:
    """The bug the rewrite exists to fix.

    The old fit grouped on `drafts.position`, which is ESPN's *lineup slot*.
    358 of 800 training rows arrive as "BE", plus more as "RB/WR/TE"; those keys
    never match a pool position, so they were silently dropped. The RB effect
    was therefore fitted only on RBs who happened to start at RB — excluding the
    late and bench picks where reaching actually shows up.
    """

    def test_fit_reads_position_from_adp_not_from_the_draft_slot(self):
        import inspect

        import src.biases as biases

        sql = biases._FIT_SQL
        assert "a.position" in sql, "position must come from average_draft_position"
        assert "d.position" not in sql, "drafts.position is the lineup slot, not the position"

        # And nothing in the module groups by a draft-slot column.
        src = inspect.getsource(biases)
        assert 'groupby("lineupSlot' not in src

    def test_late_and_bench_picks_are_not_excluded_from_the_fit(self):
        """Every drafted RB counts, whichever slot they landed in.

        Built as a frame rather than a DB so the assertion is about the
        grouping logic, not about SQL.
        """
        from src.biases import _group_stats

        df = pd.DataFrame({
            # Same players the old code would have split by lineup slot.
            "position": ["RB"] * 6,
            "lineup_slot": ["RB", "RB", "BE", "BE", "RB/WR/TE", "BE"],
            "delta_y": [-2.0, -4.0, -30.0, -25.0, -18.0, -22.0],
        })
        stats = _group_stats(df, "position", "delta_y")
        assert stats.loc[0, "n"] == 6, "all six picks must be in the sample"
        # Grouping by lineup slot would have reported the RB-slot mean of -3;
        # the true effect across every RB taken is far larger.
        assert stats.loc[0, "mean"] == pytest.approx(-16.833, abs=0.01)
