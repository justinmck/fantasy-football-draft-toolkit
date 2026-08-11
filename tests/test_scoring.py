import pandas as pd
import pytest

from src import scoring
from src.scoring import (
    DEFAULT_ROOKIE_RELIABILITY_FACTOR,
    add_vorp,
    add_vorp_z,
    adp_pressure,
    availability,
    availability_pressure,
    compute_baselines,
    confidence,
    model_confidence,
    need_weights,
    normalize_position,
    open_slots,
    position_reliability,
    position_spread,
    risk_multiplier,
    roster_urgency,
    score,
    starters_needed,
)


def test_normalize_position_aliases():
    assert normalize_position("D/ST") == "DST"
    assert normalize_position("DEF") == "DST"
    assert normalize_position(None) is None
    assert normalize_position("QB") == "QB"


def test_starters_needed_non_flex_position():
    roster_needs = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
    # 10 teams x 1 QB slot each = 10 starters, QB isn't FLEX-eligible
    assert starters_needed("QB", teams=10, roster_needs=roster_needs) == 10


def test_starters_needed_includes_flex_share():
    roster_needs = {"RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
    flex_eligible = ("RB", "WR", "TE")
    # RB: 10 teams x 2 base + a third of the FLEX slot spread across RB/WR/TE
    import src.scoring as scoring_mod

    orig_flex = scoring_mod.FLEX_ELIGIBLE
    scoring_mod.FLEX_ELIGIBLE = flex_eligible
    try:
        n = starters_needed("RB", teams=10, roster_needs=roster_needs)
    finally:
        scoring_mod.FLEX_ELIGIBLE = orig_flex
    assert n == round(10 * 2 + 10 * (1 / 3))


def test_compute_baselines_worst_starter():
    df = pd.DataFrame({
        "position": ["QB"] * 3,
        "projected_points": [300, 200, 100],
    })
    # 2 teams x 1 QB slot = 2 starters -> baseline is the 2nd-best QB (200)
    baselines = compute_baselines(df, teams=2, roster_needs={"QB": 1}, value_col="projected_points")
    assert baselines["QB"] == 200


def test_compute_baselines_zero_need_position():
    df = pd.DataFrame({"position": ["K"], "projected_points": [80]})
    baselines = compute_baselines(df, teams=10, roster_needs={"QB": 1}, value_col="projected_points")
    assert baselines["K"] == 0.0


def test_add_vorp_subtracts_baseline():
    df = pd.DataFrame({
        "position": ["QB", "QB"],
        "projected_points": [300, 100],
    })
    out = add_vorp(df, {"QB": 150.0}, value_col="projected_points")
    assert list(out["vorp"]) == [150.0, -50.0]


@pytest.mark.parametrize(
    "league_pick_est,current_pick,next_pick,expected",
    [
        (5, 10, 20, 1.12),   # already should've been picked -> available now
        (15, 10, 20, 1.22),  # will be gone before my next turn -> grab now
        (25, 10, 20, 1.05),  # gone soon after my next turn -> mild urgency
        (100, 10, 20, 0.95),  # not going anywhere soon -> can wait
    ],
)
def test_adp_pressure_thresholds(league_pick_est, current_pick, next_pick, expected):
    # The legacy step function is kept for comparison and must stay unchanged.
    assert adp_pressure(league_pick_est, current_pick, next_pick) == expected


# ---- Roster need ----

def test_open_slots_counts_unfilled_starters():
    roster_state = {
        "QB": {"have": 1, "need": 1},
        "RB": {"have": 0, "need": 2},
        "WR": {"have": 1, "need": 2},
    }
    assert open_slots(roster_state) == {"QB": 0, "RB": 2, "WR": 1}


def test_open_slots_spreads_flex_across_eligible_positions():
    roster_state = {
        "QB": {"have": 1, "need": 1},
        "RB": {"have": 2, "need": 2},
        "WR": {"have": 2, "need": 2},
        "TE": {"have": 1, "need": 1},
        "FLEX": {"have": 0, "need": 1},
    }
    opens = open_slots(roster_state)
    # Every direct slot is filled, but the open FLEX is still a real need -
    # split evenly across the three positions that can fill it.
    assert opens["QB"] == 0
    assert opens["RB"] == pytest.approx(1 / 3)
    assert opens["WR"] == pytest.approx(1 / 3)
    assert opens["TE"] == pytest.approx(1 / 3)
    assert "FLEX" not in opens


def test_open_slots_ignores_filled_flex():
    roster_state = {
        "RB": {"have": 2, "need": 2},
        "WR": {"have": 2, "need": 2},
        "TE": {"have": 1, "need": 1},
        "FLEX": {"have": 1, "need": 1},
    }
    assert set(open_slots(roster_state).values()) == {0}


def test_roster_urgency_is_flat_when_picks_remaining_unknown():
    roster_state = {"RB": {"have": 0, "need": 2}}
    assert roster_urgency(roster_state, None) == 1.0


def test_roster_urgency_rises_as_picks_run_out():
    roster_state = {"RB": {"have": 0, "need": 2}, "WR": {"have": 0, "need": 2}}
    roomy = roster_urgency(roster_state, picks_remaining=20)
    tight = roster_urgency(roster_state, picks_remaining=8)
    critical = roster_urgency(roster_state, picks_remaining=4)
    assert roomy < tight < critical
    assert critical == pytest.approx(2.0)  # 4 open slots, 4 picks left -> capped


def test_need_weights_prioritizes_unmet_needs():
    roster_state = {
        "QB": {"have": 1, "need": 1},
        "RB": {"have": 0, "need": 2},
    }
    w = need_weights(roster_state)
    assert w["QB"] == 1.0  # need already met
    assert w["RB"] == 1.0 + 0.5 * 2  # two open slots


def test_need_weights_escalate_when_picks_run_short():
    roster_state = {"QB": {"have": 0, "need": 1}, "RB": {"have": 0, "need": 2}}
    relaxed = need_weights(roster_state, picks_remaining=15)
    urgent = need_weights(roster_state, picks_remaining=3)
    assert urgent["RB"] > relaxed["RB"] > 1.0


# ---- Pick timing ----

def test_availability_falls_as_next_pick_passes_the_market_estimate():
    # A player the market takes around pick 20: very likely still there at 10,
    # a coin flip at 20, very unlikely at 40.
    assert availability(20, next_pick=10) > 0.9
    assert availability(20, next_pick=20) == pytest.approx(0.5)
    assert availability(20, next_pick=40) < 0.05


def test_availability_treats_missing_adp_as_available():
    # 999 is the "no market data" sentinel from load_candidates' COALESCE -
    # it must not be read as a genuine pick-999 estimate.
    assert availability(999.0, next_pick=50) == 1.0
    assert availability(float("nan"), next_pick=50) == 1.0


def test_availability_uncertainty_widens_deeper_into_the_draft():
    # Same 10-pick gap, but late-round estimates are far noisier, so the
    # player is less certain to be gone.
    early = availability(20, next_pick=30)
    late = availability(150, next_pick=160)
    assert late > early


def test_availability_pressure_is_bounded_and_monotonic():
    # Certain to be gone -> maximum urgency; certain to last -> none.
    gone = availability_pressure(5, current_pick=1, next_pick=40)
    safe = availability_pressure(200, current_pick=1, next_pick=40)
    assert gone == pytest.approx(1.25, abs=0.01)
    assert safe == pytest.approx(1.0, abs=0.01)
    assert 1.0 <= safe < gone <= 1.25


def test_availability_pressure_is_neutral_when_there_is_no_later_pick():
    # The old step function returned different multipliers here even though
    # "wait for my next turn" isn't an option, which distorted the ranking on
    # exactly the pick that matters most.
    assert availability_pressure(5, current_pick=10, next_pick=10) == 1.0
    assert availability_pressure(500, current_pick=10, next_pick=10) == 1.0


# ---- Risk / confidence ----

def test_model_confidence_is_share_of_projection_surviving_the_downside():
    df = pd.DataFrame({
        "predicted_vorp": [100.0, 100.0],
        "ci_low": [90.0, 40.0],
    })
    assert list(model_confidence(df)) == pytest.approx([0.9, 0.4])


def test_position_reliability_floors_negative_r2_at_zero():
    # A negative R^2 means "worse than guessing the mean" - that's still just
    # no information, not negative information.
    df = pd.DataFrame({"reliability": [0.52, 0.0, -0.03]})
    assert list(position_reliability(df)) == pytest.approx([0.52, 0.0, 0.0])


def test_confidence_blends_model_interval_and_position_reliability():
    df = pd.DataFrame({
        "predicted_vorp": [100.0],
        "ci_low": [80.0],       # model confidence 0.8
        "reliability": [0.5],   # position reliability 0.5
    })
    expected = 0.35 * 0.8 + 0.65 * 0.5
    assert confidence(df).iloc[0] == pytest.approx(expected)


def test_confidence_is_neutral_without_either_source():
    df = pd.DataFrame({"position": ["RB", "WR"]})
    assert list(confidence(df)) == [1.0, 1.0]


def test_confidence_falls_back_when_only_one_source_is_present():
    # The live tool must still rank sensibly before NB04/NB05 have been run,
    # so a missing table means "neutral", not "zero confidence".
    model_only = pd.DataFrame({"predicted_vorp": [100.0], "ci_low": [50.0]})
    assert confidence(model_only).iloc[0] == pytest.approx(0.35 * 0.5 + 0.65 * 1.0)

    position_only = pd.DataFrame({"reliability": [0.4]})
    assert confidence(position_only).iloc[0] == pytest.approx(0.35 * 1.0 + 0.65 * 0.4)


def test_confidence_handles_non_positive_and_missing_predictions():
    # Below-replacement players and players outside the model's pool have no
    # meaningful ratio - they should be neutral, not zero.
    df = pd.DataFrame({
        "predicted_vorp": [0.0, -50.0, None],
        "ci_low": [10.0, -80.0, 5.0],
    })
    assert list(model_confidence(df)) == [1.0, 1.0, 1.0]


def test_risk_multiplier_is_bounded_by_risk_aversion():
    df = pd.DataFrame({
        "predicted_vorp": [100.0, 100.0],
        "ci_low": [100.0, 0.0],
        "reliability": [1.0, 0.0],
    })
    mults = risk_multiplier(df, risk_aversion=0.20)
    assert mults.iloc[0] == pytest.approx(1.0)    # perfectly certain
    assert mults.iloc[1] == pytest.approx(0.80)   # worst case, capped at -20%


def test_risk_aversion_zero_disables_the_term():
    df = pd.DataFrame({
        "predicted_vorp": [100.0],
        "ci_low": [0.0],
        "reliability": [0.0],
    })
    assert risk_multiplier(df, risk_aversion=0.0).iloc[0] == 1.0


def test_unreliable_position_is_discounted_against_a_reliable_one():
    # A kicker's projection has explained ~0% of the variance in actual VORP
    # across six seasons; a QB's ~52%. Equal projected value should not mean
    # equal confidence.
    df = pd.DataFrame({
        "predicted_vorp": [100.0, 100.0],
        "ci_low": [90.0, 90.0],
        "reliability": [0.52, 0.0],
    })
    mults = risk_multiplier(df)
    assert mults.iloc[0] > mults.iloc[1]


def test_risk_only_reorders_players_the_board_already_rates_as_close():
    # A clearly better player must not be overtaken by a safer, worse one...
    df = pd.DataFrame({
        "position": ["RB", "WR"],
        "projected_points": [200, 200],
        "vorp_z": [100.0, 70.0],
        "predicted_vorp": [100.0, 70.0],
        "ci_low": [10.0, 70.0],   # RB very uncertain, WR perfectly certain
        "league_pick_est": [999, 999],
        "avg_last_year": [0, 0],
    })
    roster_state = {"RB": {"have": 0, "need": 2}, "WR": {"have": 0, "need": 2}}
    ranked = score(df, roster_state, current_pick=1, next_pick=14)
    assert ranked.iloc[0]["position"] == "RB"

    # ...but a near-tie should break toward the safer player.
    df.loc[1, ["vorp_z", "predicted_vorp", "ci_low"]] = [95.0, 95.0, 95.0]
    ranked = score(df, roster_state, current_pick=1, next_pick=14)
    assert ranked.iloc[0]["position"] == "WR"


def test_score_exposes_its_components_for_explanation():
    df = pd.DataFrame({
        "position": ["RB"],
        "projected_points": [200],
        "vorp_z": [100.0],
        "predicted_vorp": [100.0],
        "ci_low": [80.0],
        "league_pick_est": [12],
        "avg_last_year": [10],
    })
    ranked = score(df, {"RB": {"have": 0, "need": 2}}, current_pick=1, next_pick=14)
    for col in ("pos_weight", "adp_mult", "risk_mult", "availability", "confidence", "base_value"):
        assert col in ranked.columns
    row = ranked.iloc[0]
    # No `reliability` column here, so position reliability is neutral and
    # confidence is 0.35 * 0.8 (model) + 0.65 * 1.0.
    assert row["confidence"] == pytest.approx(0.93)
    assert row["risk_mult"] == pytest.approx(0.8 + 0.2 * 0.93)
    assert row["pos_weight"] == pytest.approx(2.0)
    assert row["base_value"] == pytest.approx(100.0)


def test_score_ranks_higher_vorp_and_need_first():
    df = pd.DataFrame({
        "position": ["QB", "RB"],
        "projected_points": [300, 250],
        "vorp": [50, 100],
        "vorp_z": [50, 100],
        "league_pick_est": [999, 999],
        "avg_last_year": [10, 10],
    })
    roster_state = {
        "QB": {"have": 1, "need": 1},  # filled already
        "RB": {"have": 0, "need": 2},  # needed
    }
    ranked = score(df, roster_state, current_pick=1, next_pick=14)
    assert ranked.iloc[0]["position"] == "RB"
    assert ranked.iloc[0]["utility"] > ranked.iloc[1]["utility"]


def test_score_clips_negative_vorp_contribution():
    df = pd.DataFrame({
        "position": ["QB"],
        "projected_points": [50],
        "vorp": [-30],
        "vorp_z": [-30],
        "league_pick_est": [999],
        "avg_last_year": [0],
    })
    roster_state = {"QB": {"have": 0, "need": 1}}
    ranked = score(df, roster_state, current_pick=1, next_pick=14)
    # negative vorp shouldn't push utility below the small baseline terms
    assert ranked.iloc[0]["utility"] >= 0.02 * 50


def test_score_falls_back_to_raw_vorp_when_vorp_z_missing():
    # Callers that haven't run add_vorp_z() yet (or don't need dampening)
    # should still get sane rankings off the raw `vorp` column.
    df = pd.DataFrame({
        "position": ["QB", "RB"],
        "projected_points": [300, 250],
        "vorp": [50, 100],
        "league_pick_est": [999, 999],
        "avg_last_year": [10, 10],
    })
    roster_state = {
        "QB": {"have": 0, "need": 1},
        "RB": {"have": 0, "need": 1},
    }
    ranked = score(df, roster_state, current_pick=1, next_pick=14)
    assert ranked.iloc[0]["position"] == "RB"


def test_score_prefers_vorp_z_over_raw_vorp_when_both_present():
    df = pd.DataFrame({
        "position": ["QB", "RB"],
        "projected_points": [300, 250],
        "vorp": [500, 10],   # raw vorp would favor QB heavily
        "vorp_z": [5, 10],   # dampened vorp_z favors RB instead
        "league_pick_est": [999, 999],
        "avg_last_year": [10, 10],
    })
    roster_state = {
        "QB": {"have": 0, "need": 1},
        "RB": {"have": 0, "need": 1},
    }
    ranked = score(df, roster_state, current_pick=1, next_pick=14)
    assert ranked.iloc[0]["position"] == "RB"


def test_position_spread_uses_startable_pool():
    df = pd.DataFrame({
        "position": ["QB", "QB", "QB"],
        "vorp": [100, 80, 10],
    })
    # starters_needed = 2 teams x 1 QB slot = 2 -> pool is top 2 by vorp: [100, 80]
    spreads = position_spread(df, teams=2, roster_needs={"QB": 1})
    assert spreads["QB"] == pytest.approx(10.0)


def test_position_spread_handles_small_pool():
    # Fewer players at a position than starters needed shouldn't error out.
    df = pd.DataFrame({
        "position": ["QB"],
        "vorp": [50],
    })
    spreads = position_spread(df, teams=2, roster_needs={"QB": 1})
    assert spreads["QB"] == 0.0


def test_add_vorp_z_dampens_high_spread_position():
    df = pd.DataFrame({
        "position": ["QB", "QB"] + ["RB"] * 6,
        "vorp": [300, 5, 50, 48, 46, 44, 42, 40],
    })
    roster_needs = {"QB": 1, "RB": 2, "FLEX": 1}
    out = add_vorp_z(df, teams=2, roster_needs=roster_needs)

    qb_rows = out[out.position == "QB"]
    rb_rows = out[out.position == "RB"]

    # RB anchors the FLEX-eligible reference spread -> left ~unchanged
    assert rb_rows["vorp_z"].tolist() == pytest.approx(rb_rows["vorp"].tolist(), rel=1e-6)

    # QB has a much wider spread -> dampened well below its raw vorp
    assert qb_rows["vorp_z"].max() < qb_rows["vorp"].max()

    # Raw vorp ranks the QB first; the dampened vorp_z should not, since the
    # gap is purely an artifact of QB's steep replacement cliff.
    assert out.sort_values("vorp", ascending=False).iloc[0]["position"] == "QB"
    assert out.sort_values("vorp_z", ascending=False).iloc[0]["position"] == "RB"

    # The old raw calculation is untouched, not overwritten.
    assert list(out["vorp"]) == [300, 5, 50, 48, 46, 44, 42, 40]


# ---- Rookie status ----

def test_rookies_are_less_trusted_than_veterans_at_the_same_position():
    # NB05 measures rookie projections at R^2 0.28 against 0.46 for veterans
    # over 2021-2025 - about 61% as informative.
    df = pd.DataFrame({
        "predicted_vorp": [100.0, 100.0],
        "ci_low": [90.0, 90.0],
        "reliability": [0.42, 0.42],   # same position
        "is_rookie": [0, 1],
        "rookie_factor": [0.61, 0.61],
    })
    rel = position_reliability(df)
    assert rel.iloc[0] == pytest.approx(0.42)
    assert rel.iloc[1] == pytest.approx(0.42 * 0.61)
    assert confidence(df).iloc[1] < confidence(df).iloc[0]


def test_rookie_and_position_reliability_compose_rather_than_override():
    # A rookie TE and a rookie RB must not collapse to the same number - the
    # two axes are independent and multiply.
    df = pd.DataFrame({
        "reliability": [0.52, 0.37],   # QB vs WR
        "is_rookie": [1, 1],
        "rookie_factor": [0.61, 0.61],
    })
    rel = position_reliability(df)
    assert rel.iloc[0] == pytest.approx(0.52 * 0.61)
    assert rel.iloc[1] == pytest.approx(0.37 * 0.61)
    assert rel.iloc[0] > rel.iloc[1]


def test_rookie_factor_falls_back_to_the_measured_default():
    # Databases where NB05 hasn't run still get the rookie adjustment.
    df = pd.DataFrame({"reliability": [0.42], "is_rookie": [1]})
    assert position_reliability(df).iloc[0] == pytest.approx(
        0.42 * DEFAULT_ROOKIE_RELIABILITY_FACTOR
    )


def test_veterans_are_untouched_by_the_rookie_adjustment():
    df = pd.DataFrame({
        "reliability": [0.42],
        "is_rookie": [0],
        "rookie_factor": [0.61],
    })
    assert position_reliability(df).iloc[0] == pytest.approx(0.42)


def test_missing_rookie_column_leaves_reliability_alone():
    df = pd.DataFrame({"reliability": [0.42]})
    assert position_reliability(df).iloc[0] == pytest.approx(0.42)


def test_rookie_status_lowers_score_only_through_confidence():
    # The rookie adjustment must express *uncertainty*, not a claim that
    # rookies are worse players - NB05 finds they out-deliver projections.
    # So with risk switched off, rookie and veteran must score identically.
    df = pd.DataFrame({
        "position": ["RB", "RB"],
        "projected_points": [200, 200],
        "vorp_z": [100.0, 100.0],
        "predicted_vorp": [100.0, 100.0],
        "ci_low": [90.0, 90.0],
        "reliability": [0.42, 0.42],
        "is_rookie": [0, 1],
        "rookie_factor": [0.61, 0.61],
        "league_pick_est": [999, 999],
        "avg_last_year": [0, 0],
    })
    roster_state = {"RB": {"have": 0, "need": 2}}

    off = score(df, roster_state, current_pick=1, next_pick=14, risk_aversion=0.0)
    assert off.iloc[0]["utility"] == pytest.approx(off.iloc[1]["utility"])

    on = score(df, roster_state, current_pick=1, next_pick=14)
    assert on.iloc[0]["is_rookie"] == 0  # veteran ranks first once risk is on


# ---------------------------------------------------------------------------
# Bench depth
# ---------------------------------------------------------------------------
#
# A 16-round draft with 9 starting slots is 7 bench picks - over a third of the
# draft. Before these, `need_weights` returned exactly 1.0 for every position
# the moment the starting lineup was full, so the board rated a backup RB and a
# second kicker identically for that whole stretch.

FULL_ROSTER = {
    "QB": {"have": 1, "need": 1}, "RB": {"have": 2, "need": 2},
    "WR": {"have": 2, "need": 2}, "TE": {"have": 1, "need": 1},
    "FLEX": {"have": 1, "need": 1}, "K": {"have": 1, "need": 1},
    "DST": {"have": 1, "need": 1},
}
EMPTY_ROSTER = {k: {"have": 0, "need": v["need"]} for k, v in FULL_ROSTER.items()}


class TestDepthNeeds:
    def test_ordered_by_injury_exposure(self):
        d = scoring.depth_needs()
        assert d["RB"] > d["WR"] > d["TE"] > d["QB"] > d["K"] > d["DST"]

    def test_normalised_to_one_at_the_peak(self):
        assert scoring.depth_needs()["RB"] == pytest.approx(1.0)

    def test_kicker_and_defense_depth_is_near_worthless(self):
        # The concrete thing this exists to prevent: a second K/DST reading as
        # comparable to a third RB.
        d = scoring.depth_needs()
        assert d["DST"] < 0.1 and d["K"] < 0.15

    def test_starting_more_of_a_position_raises_its_depth_need(self):
        one_rb = scoring.depth_needs(roster_needs={"RB": 1, "WR": 2, "FLEX": 0})
        two_rb = scoring.depth_needs(roster_needs={"RB": 2, "WR": 2, "FLEX": 0})
        assert two_rb["RB"] / two_rb["WR"] > one_rb["RB"] / one_rb["WR"]


class TestBenchWeights:
    def test_no_bench_room_means_no_bench_credit(self):
        assert scoring.bench_weights({}, bench_remaining=0) == {}
        assert scoring.bench_weights({}, bench_remaining=None) == {}

    def test_diminishing_returns_per_extra_body(self):
        first = scoring.bench_weights({}, bench_remaining=5)["RB"]
        third = scoring.bench_weights({"RB": 2}, bench_remaining=5)["RB"]
        assert third == pytest.approx(first / 3)

    def test_capped_below_a_starting_slot(self):
        # A backup must never outrank filling a hole in the lineup.
        assert max(scoring.bench_weights({}, bench_remaining=7).values()) < scoring.NEED_WEIGHT


class TestNeedWeightsWithBench:
    def test_full_roster_still_differentiates_positions(self):
        w = need_weights(FULL_ROSTER, picks_remaining=7, depth={}, bench_remaining=7)
        assert w["RB"] > w["WR"] > w["QB"] > w["DST"]
        assert w["DST"] == pytest.approx(1.0, abs=0.05)

    def test_stacking_a_position_hands_priority_to_the_next_one(self):
        w = need_weights(FULL_ROSTER, picks_remaining=5, depth={"RB": 2}, bench_remaining=5)
        assert w["WR"] > w["RB"]

    def test_open_starting_slot_always_beats_any_bench_need(self):
        w = need_weights(EMPTY_ROSTER, picks_remaining=16, depth={}, bench_remaining=7)
        # Even DST, the least valuable depth, outranks a full roster's best bench.
        full = need_weights(FULL_ROSTER, picks_remaining=7, depth={}, bench_remaining=7)
        assert w["DST"] > full["RB"]

    def test_omitting_bench_args_reproduces_previous_behaviour(self):
        assert need_weights(FULL_ROSTER, picks_remaining=7) == {
            p: 1.0 for p in ("QB", "RB", "WR", "TE", "K", "DST")
        }

    def test_bench_exhausted_collapses_to_flat(self):
        w = need_weights(FULL_ROSTER, picks_remaining=0, depth={}, bench_remaining=0)
        assert set(w.values()) == {1.0}
