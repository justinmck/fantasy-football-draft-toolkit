"""Measure how often startable players at each position actually miss games.

This is the evidence behind how much a *bench* spot is worth. A backup only
ever pays off in the weeks your starter isn't playing, so the rate at which
starters miss time is the honest driver of depth need — and it separates the
positions sharply: running backs miss roughly eight times as much of the season
as defenses do, which is why a fourth RB is a reasonable pick and a second
kicker never is.

Measured on the top `starters_needed(pos)` players by season points in each
year — the same replacement-level tier `src/scoring.py` draws the VORP baseline
from. Restricting to that tier matters: including every player on record would
drag the rate toward the deep-bench players who never appear at all, which
measures irrelevance rather than injury.

    python notebooks/compute_availability.py [--since 2020]

Writes the `position_availability` table. `src/recommender.py` reads it behind
an existence check and falls back to the constants in `src/scoring.py`, so this
is an optional refinement rather than a required pipeline step.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "notebooks"))

from notebooks.config import POSITIONS, TEAMS  # noqa: E402
from src.db import engine  # noqa: E402
from src.scoring import normalize_position, starters_needed  # noqa: E402

GAMES_IN_SEASON = 17


def compute(engine, since: int) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT player_id, year, position, points, games_played FROM players_stats "
        f"WHERE year >= {int(since)}",
        engine,
    )
    df["position"] = df["position"].map(normalize_position)
    df = df[df["position"].isin(set(POSITIONS))]

    rows = []
    for pos, grp in df.groupby("position"):
        n = starters_needed(pos, teams=TEAMS)
        # Per season, so a year with more recorded players doesn't dominate.
        tiers = [g.nlargest(n, "points") for _, g in grp.groupby("year")]
        if not tiers:
            continue
        games = pd.concat(tiers)["games_played"].fillna(0)
        rows.append({
            "position": pos,
            "starters": n,
            "n": int(len(games)),
            "mean_games": round(float(games.mean()), 3),
            "missed_game_rate": round(1 - float(games.mean()) / GAMES_IN_SEASON, 4),
            "pct_under_14_games": round(float((games < 14).mean()), 4),
            "seasons": ",".join(str(y) for y in sorted(grp["year"].unique())),
        })
    return pd.DataFrame(rows).sort_values("missed_game_rate", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", type=int, default=2020)
    args = ap.parse_args()

    out = compute(engine, args.since)
    if out.empty:
        print("No player-season rows found — has NB02 been run?", file=sys.stderr)
        return 1

    out.to_sql("position_availability", engine, if_exists="replace", index=False)
    print(f"Wrote {len(out)} rows to position_availability")
    print(out[["position", "starters", "n", "mean_games", "missed_game_rate"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
