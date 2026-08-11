"""Pull next-season projections for *every* player, not just free agents.

Why this exists as a script rather than a notebook cell: NB01's projection pull
used `league.free_agents(size=500)` alone, which is correct only while the
league is in its pre-draft state. Run at any other time — mid-season, or after
the draft — `free_agents()` structurally excludes every rostered player, which
is exactly the elite tier. The 2025 pull was made mid-season, so the resulting
projections file contained none of the top 50 players by ADP, and the draft
board could only ever recommend from what was left.

Unioning free agents with every team's roster makes the pull correct whenever
it happens to be run, which matters because the whole point of this file is to
be re-run each August.

    python notebooks/pull_projections.py [--year 2026] [--size 1500]

Writes data/raw/<year>/projections/espn_proj/<year>_proj_stats.csv in the same
shape NB02 expects, so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "notebooks"))

from espn_api.football import League  # noqa: E402

from notebooks.config import NEXT_SEASON  # noqa: E402
from notebooks.utils import process_projections  # noqa: E402

# Column order NB02 reads positionally-ish; kept identical to NB01's original
# export so the two paths produce interchangeable files.
_FRONT_COLS = ["player_name", "player_id", "pro_team", "projected_points", "year", "eligible_slots"]


def all_players(league, size: int):
    """Every player with a projection: free agents plus all rostered players.

    Deduplicated on `playerId` because a player can legitimately appear in both
    lists depending on when the league state was last refreshed, and a
    duplicate row here becomes a duplicate row on the draft board.
    """
    players, seen = [], set()

    for p in league.free_agents(size=size):
        pid = getattr(p, "playerId", None)
        if pid is not None and pid not in seen:
            seen.add(pid)
            players.append(p)
    n_fa = len(players)

    for team in league.teams:
        for p in team.roster:
            pid = getattr(p, "playerId", None)
            if pid is not None and pid not in seen:
                seen.add(pid)
                players.append(p)

    print(f"  free agents: {n_fa}, rostered (not already seen): {len(players) - n_fa}")
    return players


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=NEXT_SEASON)
    ap.add_argument("--size", type=int, default=1500, help="free-agent page size")
    args = ap.parse_args()

    load_dotenv(_REPO_ROOT / ".env")
    league_id, swid, espn_s2 = os.getenv("LEAGUE_ID"), os.getenv("SWID"), os.getenv("ESPN_S2")
    if not all([league_id, swid, espn_s2]):
        print("Missing LEAGUE_ID / SWID / ESPN_S2 in .env", file=sys.stderr)
        return 1

    print(f"Fetching {args.year} projections…")
    league = League(league_id=int(league_id), year=args.year, swid=swid, espn_s2=espn_s2)
    players = all_players(league, args.size)

    df = process_projections(players)
    if df.empty:
        print("No projections returned — is the season published yet?", file=sys.stderr)
        return 1

    df["year"] = args.year
    other = [c for c in df.columns if c not in _FRONT_COLS]
    df = df[_FRONT_COLS + other]

    # A player with no projection is noise on a draft board, not a candidate.
    before = len(df)
    df = df[df["projected_points"].notna()]
    print(f"  {len(df)} players with projections ({before - len(df)} dropped as unprojected)")

    out = _REPO_ROOT / "data" / "raw" / str(args.year) / "projections" / "espn_proj"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{args.year}_proj_stats.csv"
    df.to_csv(path)
    print(f"Wrote {path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
