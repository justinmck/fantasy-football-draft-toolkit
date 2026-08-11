"""Rebuild `next_season_projections` from the raw projections CSV.

This is NB02's projection step, extracted so it can be re-run on its own after
a fresh `pull_projections.py`. Re-running the whole notebook would drop and
repopulate every table in the database — including the ADP table, whose insert
is explicitly documented as not safe to repeat — when the only thing that has
actually changed is this one file.

Two differences from the original notebook cell, both fixes:

1. Position falls back to ESPN's `eligible_slots` when a player has no ADP or
   prior-season history to look it up from. The notebook dropped those rows,
   which meant incoming rookies — the exact players a draft board most needs to
   surface, and the ones the "Unproven" flag exists for — never reached the
   board at all.
2. Players are deduplicated on `player_id`, since a duplicate row here becomes
   a duplicated player on the board.

    python notebooks/rebuild_projections.py [--year 2026]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "notebooks"))

from notebooks.config import NEXT_SEASON, POSITIONS  # noqa: E402
from notebooks.utils import position_from_eligible_slots  # noqa: E402
from src.db import engine  # noqa: E402
from src.scoring import normalize_position  # noqa: E402

_COLS = ["player_id", "player_name", "position", "pro_team", "projected_points", "year"]


def build_position_lookup(conn) -> pd.DataFrame:
    """Most recently known real position per player, from ADP and stats history.

    The valid-position filter is kept from the notebook: ADP occasionally
    carries labels this project doesn't model (e.g. "DT"), and a garbage label
    shouldn't win the most-recent tiebreak against a real one.
    """
    frames = []
    for table in ("average_draft_position", "players_stats"):
        try:
            frames.append(pd.read_sql(text(f"SELECT player_id, year, position FROM {table}"), conn))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=["player_id", "position"])

    lookup = pd.concat(frames, ignore_index=True).dropna(subset=["player_id", "position"])
    lookup["player_id"] = pd.to_numeric(lookup["player_id"], errors="coerce")
    lookup["position"] = lookup["position"].map(normalize_position)
    lookup = lookup[lookup["position"].isin(set(POSITIONS))]
    return (
        lookup.sort_values("year", ascending=False)
        .drop_duplicates(subset="player_id", keep="first")[["player_id", "position"]]
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=NEXT_SEASON)
    args = ap.parse_args()

    csv = _REPO_ROOT / "data" / "raw" / str(args.year) / "projections" / "espn_proj" / f"{args.year}_proj_stats.csv"
    if not csv.exists():
        print(f"No projections CSV at {csv}\nRun: python notebooks/pull_projections.py --year {args.year}", file=sys.stderr)
        return 1

    df = pd.read_csv(csv)
    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce")
    df = df.dropna(subset=["player_id"]).drop_duplicates(subset="player_id", keep="first")
    df["year"] = args.year
    print(f"Read {len(df)} projected players from {csv.name}")

    with engine.connect() as conn:
        lookup = build_position_lookup(conn)
    df = df.merge(lookup, on="player_id", how="left")
    from_history = df["position"].notna().sum()

    # Fall back to eligible_slots for anyone with no history to look up.
    missing = df["position"].isna()
    if missing.any() and "eligible_slots" in df.columns:
        df.loc[missing, "position"] = (
            df.loc[missing, "eligible_slots"].map(position_from_eligible_slots)
        )
    df["position"] = df["position"].map(normalize_position)
    from_slots = int(df["position"].notna().sum() - from_history)
    print(f"  position: {from_history} from ADP/stats history, {from_slots} recovered from eligible_slots")

    df = df[df["position"].isin(set(POSITIONS))]
    df = df.dropna(subset=["projected_points"])
    df["player_id"] = df["player_id"].astype(int)
    df["pro_team"] = df["pro_team"].fillna("FA")
    out = df[_COLS]

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM next_season_projections WHERE year = :y"), {"y": args.year})
    out.to_sql("next_season_projections", engine, if_exists="append", index=False)

    print(f"Wrote {len(out)} rows to next_season_projections for {args.year}")
    print(out["position"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
