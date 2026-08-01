"""Tests for live draft session state.

`roster_state` is what `src/scoring.py`'s need weighting reads, so how picks
are allocated to slots here directly drives what the tool recommends next.
"""
import pytest

from src.state import DraftSession

ROSTER_NEED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}


def make_session(rounds=None):
    return DraftSession(teams=14, roster_need=dict(ROSTER_NEED), rounds=rounds)


def test_pick_fills_the_positions_own_slot_first():
    s = make_session()
    assert s.pick(1, "RB", mine=True) == "RB"
    assert s.roster_state["RB"]["have"] == 1
    assert s.roster_state["FLEX"]["have"] == 0


def test_third_rb_spills_into_flex():
    # Two RB starting slots, then the third RB fills FLEX rather than being
    # counted as a third RB starter.
    s = make_session()
    for i in range(2):
        assert s.pick(i, "RB", mine=True) == "RB"
    assert s.pick(99, "RB", mine=True) == "FLEX"
    assert s.roster_state["RB"]["have"] == 2
    assert s.roster_state["FLEX"]["have"] == 1


def test_fourth_rb_is_bench_depth_and_does_not_inflate_starters():
    s = make_session()
    for i in range(3):
        s.pick(i, "RB", mine=True)  # RB, RB, FLEX
    assert s.pick(100, "RB", mine=True) is None
    assert s.roster_state["RB"]["have"] == 2  # never exceeds need
    assert s.roster_state["FLEX"]["have"] == 1
    assert s.depth["RB"] == 1


def test_non_flex_eligible_position_never_fills_flex():
    s = make_session()
    assert s.pick(1, "QB", mine=True) == "QB"
    # QB slot is now full; a second QB is bench depth, not a FLEX starter.
    assert s.pick(2, "QB", mine=True) is None
    assert s.roster_state["FLEX"]["have"] == 0
    assert s.depth["QB"] == 1


def test_defense_position_alias_is_normalized():
    s = make_session()
    assert s.pick(1, "D/ST", mine=True) == "DST"
    assert s.roster_state["DST"]["have"] == 1


def test_other_teams_picks_remove_the_player_without_touching_my_roster():
    s = make_session()
    assert s.pick(7, "RB", mine=False) is None
    assert 7 in s.drafted_ids
    assert s.roster_state["RB"]["have"] == 0
    assert s.draft_log[0]["is_my_pick"] is False


def test_draft_log_records_which_slot_was_filled():
    s = make_session()
    s.pick(1, "RB", mine=True)
    s.pick(2, "RB", mine=True)
    s.pick(3, "WR", mine=True)
    s.pick(4, "RB", mine=True)
    assert [p["filled_slot"] for p in s.draft_log] == ["RB", "RB", "WR", "FLEX"]


def test_picks_remaining_is_unknown_without_a_round_count():
    s = make_session(rounds=None)
    assert s.picks_remaining() is None


def test_picks_remaining_counts_down_only_on_my_picks():
    s = make_session(rounds=16)
    s.pick(1, "RB", mine=True)
    s.pick(2, "WR", mine=False)  # someone else's pick
    s.pick(3, "TE", mine=True)
    assert s.my_picks_made == 2
    assert s.picks_remaining() == 14


def test_picks_remaining_never_goes_negative():
    s = make_session(rounds=2)
    for i in range(5):
        s.pick(i, "RB", mine=True)
    assert s.picks_remaining() == 0
