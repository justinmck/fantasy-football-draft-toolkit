"""Measure how this league drafts differently from the national market.

Fits the effects described in `src/biases.py` and persists them so the live
board and the Analysis tab both read one set of numbers. Nothing here is
computed at request time — the permutation tests alone would put seconds on the
boot path.

    python notebooks/compute_league_bias.py [--since 2020] [--adp-max 180]
                                            [--permutations 2000]

Writes five tables:

  league_bias_position   applied
  league_bias_proteam    applied
  league_bias_manager    persisted and displayed, NOT applied (a manager effect
                         attaches to a drafter, and the tool doesn't know who
                         is on the clock)
  league_bias_player     persisted and displayed, NOT applied (n is 2-4 against
                         a per-pick spread of ~19 picks)
  league_bias_meta       provenance: seasons, cutoff, sample size, shrinkage
                         constants

`src/biases.py::load_league_bias` reads these behind an existence check and
falls back to measured constants, so the tool still runs if this never executes.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "notebooks"))

from notebooks.config import CURRENT_SEASON  # noqa: E402
from src.biases import DEFAULT_ADP_MAX, fit_league_bias  # noqa: E402
from src.db import engine  # noqa: E402


def _manager_names(engine) -> dict:
    """Most recent season's name per `team_id`, for display only.

    Grouping is always on the id: four franchises renamed themselves, and
    "Gerald Pea's Football Team" (id 11) and "GeraldPea's Football Team" (id 12)
    are different franchises one space apart.
    """
    try:
        t = pd.read_sql("SELECT team_id, team_name, year FROM teams", engine)
    except Exception:
        return {}
    latest = t.sort_values("year").drop_duplicates("team_id", keep="last")
    return dict(zip(latest["team_id"], latest["team_name"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", type=int, default=2020)
    ap.add_argument("--adp-max", type=float, default=DEFAULT_ADP_MAX)
    ap.add_argument("--permutations", type=int, default=2000,
                    help="0 to skip the family-wise significance tests")
    args = ap.parse_args()

    years = range(args.since, CURRENT_SEASON + 1)
    print(f"Fitting league bias over {args.since}-{CURRENT_SEASON} (adp <= {args.adp_max:g})…")
    fit = fit_league_bias(engine, years=years, adp_max=args.adp_max,
                          permutations=args.permutations)

    meta = fit["meta"]
    if not meta.get("n_picks"):
        print("No draft picks with a market price found — has NB02 been run?", file=sys.stderr)
        return 1
    tables = fit["tables"]

    names = _manager_names(engine)
    mgr = tables["manager"]
    if len(mgr):
        mgr = mgr.copy()
        mgr["team_name"] = mgr["team_id"].map(names)

    tables["position"].to_sql("league_bias_position", engine, if_exists="replace", index=False)
    if len(tables["pro_team"]):
        tables["pro_team"].to_sql("league_bias_proteam", engine, if_exists="replace", index=False)
    if len(mgr):
        mgr.to_sql("league_bias_manager", engine, if_exists="replace", index=False)
    if len(tables["player"]):
        tables["player"].to_sql("league_bias_player", engine, if_exists="replace", index=False)
    # Per-season breakdowns: consistency is the argument, and an aggregate mean
    # can't distinguish a habit from one wild season.
    if len(tables["position_season"]):
        tables["position_season"].to_sql("league_bias_position_season", engine,
                                         if_exists="replace", index=False)
    if len(tables["proteam_season"]):
        tables["proteam_season"].to_sql("league_bias_proteam_season", engine,
                                        if_exists="replace", index=False)

    # Which league this was fitted on. Without it the app can't tell whether
    # the fit applies to whatever league is being drafted, and "this league
    # reaches on Eagles" would silently be asserted about other people's
    # drafters. src/api.py::_bias_for gates on exactly this.
    load_dotenv(_REPO_ROOT / ".env")
    pd.DataFrame([{
        "fit_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "league_id": str(os.getenv("LEAGUE_ID") or ""),
        "years": meta["years"],
        "adp_cutoff": meta["adp_cutoff"],
        "n_picks": meta["n_picks"],
        "resid_sd": round(meta["resid_sd"], 3),
        "k_position": round(meta["k_position"], 2),
        "k_proteam": round(meta["k_proteam"], 2),
        "k_manager": round(meta["k_manager"], 2),
        "permutations": args.permutations,
    }]).to_sql("league_bias_meta", engine, if_exists="replace", index=False)

    # --- report ---
    print(f"\n{meta['n_picks']} picks with a market price · residual sd {meta['resid_sd']:.1f} picks")
    print(f"shrinkage k: position {meta['k_position']:.1f}, "
          f"pro-team {meta['k_proteam']:.1f}, manager {meta['k_manager']:.1f}\n")

    cols = ["n", "mean", "sd", "t", "shrunk"]
    print("POSITION (applied)")
    print(tables["position"].set_index("position")[cols].round(2).to_string())

    if len(tables["pro_team"]):
        team = tables["pro_team"].reindex(
            tables["pro_team"]["shrunk"].abs().sort_values(ascending=False).index)
        print("\nNFL TEAM (applied) — top 8 by |shrunk|")
        print(team.set_index("pro_team")[cols].head(8).round(2).to_string())

    if len(mgr):
        m = mgr.reindex(mgr["shrunk"].abs().sort_values(ascending=False).index)
        print("\nMANAGER (not applied) — top 5 by |shrunk|")
        print(m.set_index("team_name")[["n", "mean", "t", "shrunk", "seasons_n"]].head(5).round(2).to_string())

    strict = tables["player"][tables["player"]["passes_strict_filter"] == 1]
    print(f"\nPER-PLAYER (not applied): {len(strict)} of {len(tables['player'])} pass the strict filter")
    if len(strict):
        s = strict.reindex(strict["mean"].abs().sort_values(ascending=False).index)
        print(s.set_index("player_name")[["n", "mean", "sd"]].head(8).round(1).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
