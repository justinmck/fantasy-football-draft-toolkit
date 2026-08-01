"""Tests for notebooks/utils.py helpers that the data pipeline depends on.

`notebooks/` isn't a package, so it's added to sys.path the same way the
notebooks themselves do it.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

NOTEBOOKS_DIR = Path(__file__).resolve().parents[1] / "notebooks"
if str(NOTEBOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(NOTEBOOKS_DIR))

from utils import clean_adp, position_from_eligible_slots  # noqa: E402


def test_wide_receiver_not_misread_as_running_back():
    # The combined "RB/WR" slot must not win over the singular "WR" slot -
    # matching is exact, so the shared-slot labels are ignored entirely.
    slots = ["RB/WR", "WR", "WR/TE", "RB/WR/TE"]
    assert position_from_eligible_slots(slots) == "WR"


def test_running_back():
    assert position_from_eligible_slots(["RB", "RB/WR", "RB/WR/TE"]) == "RB"


def test_tight_end_not_misread_as_wide_receiver():
    assert position_from_eligible_slots(["WR/TE", "TE", "RB/WR/TE"]) == "TE"


def test_quarterback_with_bench_and_ir_noise():
    assert position_from_eligible_slots(["QB", "OP", "BE", "IR"]) == "QB"


def test_kicker():
    assert position_from_eligible_slots(["K", "BE", "IR"]) == "K"


def test_defense_normalized_to_dst():
    assert position_from_eligible_slots(["D/ST", "BE", "IR"]) == "DST"


def test_accepts_json_string_as_stored_in_the_database():
    assert position_from_eligible_slots('["RB", "RB/WR", "RB/WR/TE"]') == "RB"


def test_accepts_python_repr_string_as_stored_in_the_raw_csvs():
    assert position_from_eligible_slots("['WR/TE', 'TE', 'RB/WR/TE', 'OP', 'BE', 'IR']") == "TE"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        '["P"]',            # punter - a real player, but not a fantasy position
        '["BE", "IR"]',     # bench/IR only, no position information
        "not a list",
        42,
    ],
)
def test_unresolvable_values_return_none(value):
    assert position_from_eligible_slots(value) is None


def _write_adp(tmp_path, header, rows):
    p = tmp_path / "adp.csv"
    p.write_text("\n".join([header, *rows]) + "\n")
    return str(p)


def test_clean_adp_reads_the_legacy_separate_column_format(tmp_path):
    csv = _write_adp(
        tmp_path,
        '"Rank","Player","Team","Bye","POS","ESPN","Sleeper","AVG"',
        ['1,"Jahmyr Gibbs","DET",6,"RB1",1,1,1.0'],
    )
    df = clean_adp(csv)
    assert df.loc[0, "player_name"] == "Jahmyr Gibbs"
    assert df.loc[0, "team_name"] == "DET"
    assert df.loc[0, "position"] == "RB"
    assert df.loc[0, "pos_rank"] == 1.0


def test_clean_adp_splits_the_2026_combined_player_bye_column(tmp_path):
    # FantasyPros merged Player/Team/Bye into one column from 2026 on.
    csv = _write_adp(
        tmp_path,
        "Rank,Player (Bye),POS,ESPN,Sleeper,AVG",
        ["1,Jahmyr Gibbs   DET (6),RB1,1,1,1.0"],
    )
    df = clean_adp(csv)
    assert df.loc[0, "player_name"] == "Jahmyr Gibbs"
    assert df.loc[0, "team_name"] == "DET"
    assert df.loc[0, "bye"] == 6
    assert "Player (Bye)" not in df.columns


def test_clean_adp_maps_defenses_the_same_way_in_the_new_format(tmp_path):
    # Defenses carry the literal team code "DST", matching how earlier seasons
    # recorded them, so the existing defense mapping keeps working.
    csv = _write_adp(
        tmp_path,
        "Rank,Player (Bye),POS,ESPN,Sleeper,AVG",
        ["146,Houston Texans DST   (8),DST1,81,—,106.5"],
    )
    df = clean_adp(csv)
    assert df.loc[0, "player_name"] == "Texans D/ST"
    assert df.loc[0, "position"] == "DST"


def test_clean_adp_keeps_players_with_no_team_or_bye(tmp_path):
    # Free agents have no team and no bye week; dropping them would silently
    # remove real, draftable players from the market data.
    csv = _write_adp(
        tmp_path,
        "Rank,Player (Bye),POS,ESPN,Sleeper,AVG",
        ["142,Stefon Diggs,WR57,170,125,152.8"],
    )
    df = clean_adp(csv)
    assert df.loc[0, "player_name"] == "Stefon Diggs"
    assert pd.isna(df.loc[0, "team_name"])
    assert df.loc[0, "avg"] == 152.8


def test_clean_adp_coerces_em_dash_placeholders_to_missing(tmp_path):
    # "—" means "this source didn't rank them"; left as text it poisons the
    # column dtype and lands in the database as a string.
    csv = _write_adp(
        tmp_path,
        "Rank,Player (Bye),POS,ESPN,Sleeper,AVG",
        ["294,Austin Ekeler,RB81,—,—,165.0"],
    )
    df = clean_adp(csv)
    assert pd.isna(df.loc[0, "ESPN"])
    assert df["avg"].dtype.kind == "f"
