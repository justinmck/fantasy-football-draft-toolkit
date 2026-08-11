"""Build the ESPN-shaped draft fixture the live-sync tests replay.

Synthesised from the `drafts` table rather than recorded from ESPN. That table
already holds every field the parser reads - `overallPickNumber`, `teamId`,
`playerId`, `roundId`, `roundPickNumber`, `autoDraftTypeId` - and holds **no
`memberId` and no SWID**. So the scrubbing problem doesn't exist by
construction, which is a much better position than redacting a recorded
response: redaction is something you only have to get wrong once, and git
remembers forever.

    python notebooks/export_draft_fixture.py [--year 2025]

Note the emitted `draftDetail.drafted` is `true`, deliberately. The parser
ignores it, and the test asserts that it does - that flag being treated as
"are there any picks" is precisely the bug that makes `espn_api`'s wrapper
blind to a draft in progress.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "notebooks"))

from src.db import engine  # noqa: E402

OUT = _REPO_ROOT / "tests" / "fixtures" / "espn_draft_2025.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    df = pd.read_sql(
        "SELECT overallPickNumber, roundId, roundPickNumber, team_id, player_id, "
        f"autoDraftTypeId FROM drafts WHERE year = {int(args.year)} "
        "ORDER BY overallPickNumber",
        engine,
    )
    if df.empty:
        print(f"No drafts rows for {args.year} — has NB02 been run?", file=sys.stderr)
        return 1

    picks = [{
        "overallPickNumber": int(r.overallPickNumber),
        "roundId": int(r.roundId),
        "roundPickNumber": int(r.roundPickNumber),
        "teamId": int(r.team_id),
        "playerId": int(r.player_id),
        "autoDraftTypeId": int(r.autoDraftTypeId or 0),
        "keeper": False,
    } for r in df.itertuples()]

    # Name and position for every drafted player, so the replay test can build
    # its own in-memory database and stay independent of the real one. Without
    # this the test would need `data/fantasy_data.db`, which conftest.py
    # deliberately swaps out for a fixture.
    ids = ",".join(str(int(p["playerId"])) for p in picks)
    lookup = pd.read_sql(
        "SELECT player_id, player_name, position FROM next_season_projections "
        f"WHERE player_id IN ({ids}) GROUP BY player_id",
        engine,
    )
    players = {str(int(r.player_id)): [r.player_name, r.position] for r in lookup.itertuples()}
    missing = {int(p["playerId"]) for p in picks} - {int(k) for k in players}
    if missing:
        print(f"warning: {len(missing)} drafted players have no projection row", file=sys.stderr)

    round_one = [p["teamId"] for p in picks if p["roundId"] == 1]
    payload = {
        "draftDetail": {
            # True on purpose - the parser must ignore it. See the docstring.
            "drafted": True,
            "inProgress": False,
            "picks": picks,
        },
        "settings": {"draftSettings": {"pickOrder": round_one, "type": "SNAKE"}},
        # Not part of ESPN's response - test scaffolding, kept alongside the
        # picks so the two can never drift apart.
        "_players": players,
    }

    out = args.out or OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))

    teams = len({p["teamId"] for p in picks})
    print(f"Wrote {out.relative_to(_REPO_ROOT)} — {len(picks)} picks, "
          f"{teams} teams, {len(picks) // teams} rounds, "
          f"{out.stat().st_size // 1024} KB")
    # Sanity check the invariant the tests depend on.
    assert not any("memberId" in p for p in picks), "fixture must carry no member ids"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
