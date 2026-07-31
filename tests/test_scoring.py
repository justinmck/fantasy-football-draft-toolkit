import pandas as pd
import pytest

from src.scoring import (
    add_vorp,
    add_vorp_z,
    adp_pressure,
    compute_baselines,
    need_weights,
    normalize_position,
    position_spread,
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


def test_need_weights_prioritizes_unmet_needs():
    roster_state = {
        "QB": {"have": 1, "need": 1},
        "RB": {"have": 0, "need": 2},
    }
    w = need_weights(roster_state)
    assert w["QB"] == 1.0  # need already met
    assert w["RB"] == 1.0 + 0.5 * 2  # two open slots


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
    assert adp_pressure(league_pick_est, current_pick, next_pick) == expected


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
