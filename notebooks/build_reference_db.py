"""Build the reference database that ships with the repo.

The app used to ship `data/fantasy_data.db` itself — the same file it writes to
at runtime. Two problems with that, and they compound:

1. It carried two real leagues' **team and manager names**, plus
   `league_bias_manager`, which pairs named individuals with a measured
   behavioural profile. None of those people agreed to be in a repo.
2. Being both tracked and written-to guaranteed a dirty tree, merge conflicts
   between anyone running it, and — demonstrably — eight commits of accumulated
   runtime data, 6.6 MB each.

So the shipped file is a *subset*: the tables that are league-independent and
carry no personal data. Everything league-scoped is rebuilt locally by NB01/NB02
or pulled per user by the app.

    python notebooks/build_reference_db.py

Writes `data/reference.db`, which `src/settings.py` copies to
`data/runtime/fantasy_data.db` on first boot.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "runtime" / "fantasy_data.db"
FALLBACK = ROOT / "data" / "fantasy_data.db"
TARGET = ROOT / "data" / "reference.db"

# League-independent, no personal data. The national ADP market, ESPN's
# projections, the player list, and the fitted reliability tables.
SHIP = [
    "average_draft_position",
    "next_season_projections",
    "players",
    "model_projections",
    "position_reliability",
    "rookie_reliability",
    "position_availability",
    "model_report",
    "model_ablation",
    "feature_vif",
]

# Columns inside otherwise-shippable tables that still name people.
# `players.current_team_name` is the fantasy team currently rostering a player -
# so a shipped copy carries a dozen real managers' team names even though the
# table itself is the national player list. Nothing in `src/` reads it; only
# NB02, which writes it.
SCRUB_COLUMNS = {
    "players": ["current_team_name"],
}

# Every one of these is either league-scoped or names real people. `teams` and
# `league_bias_manager` are the sharp ones: team names, and a per-person
# "reaches 7.8 picks early" score.
NEVER_SHIP = [
    "drafts", "teams", "players_stats",
    "league_bias_position", "league_bias_proteam", "league_bias_manager",
    "league_bias_player", "league_bias_position_season",
    "league_bias_proteam_season", "league_bias_meta",
]


def main() -> int:
    source = SOURCE if SOURCE.exists() else FALLBACK
    if not source.exists():
        print(f"no source database at {SOURCE} or {FALLBACK}", file=sys.stderr)
        return 1

    if TARGET.exists():
        TARGET.unlink()
    shutil.copy2(source, TARGET)

    conn = sqlite3.connect(TARGET)
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    dropped, kept = [], []
    for table in sorted(present):
        if table.startswith("sqlite_"):
            continue
        if table in SHIP:
            kept.append(table)
        else:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            dropped.append(table)
    scrubbed = []
    for table, columns in SCRUB_COLUMNS.items():
        if table not in kept:
            continue
        have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for col in columns:
            if col in have:
                conn.execute(f'UPDATE "{table}" SET "{col}" = NULL')
                scrubbed.append(f"{table}.{col}")
    conn.commit()
    # Actually reclaim the space rather than leaving the pages on the free list,
    # where dropped rows and overwritten values are still readable with a hex
    # editor. Without this the VACUUM is the only thing standing between a
    # "scrubbed" file and the original bytes.
    conn.execute("VACUUM")
    conn.close()

    print(f"kept     ({len(kept)}): {', '.join(kept)}")
    print(f"dropped  ({len(dropped)}): {', '.join(dropped)}")
    if scrubbed:
        print(f"scrubbed ({len(scrubbed)}): {', '.join(scrubbed)}")
    print(f"\n{TARGET.relative_to(ROOT)}  {TARGET.stat().st_size / 1e6:.1f} MB")

    leaked = [t for t in NEVER_SHIP if t in kept]
    if leaked:
        print(f"\nREFUSING: {leaked} must never ship", file=sys.stderr)
        return 1

    # Check the bytes, not the schema. Dropping a table and nulling a column
    # both leave the original values on free pages until VACUUM runs, and a
    # build script that only checked its own intentions would have shipped
    # them - this is exactly how `players.current_team_name` was nearly missed.
    raw = TARGET.read_bytes()
    if re.search(rb"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-", raw):
        print("\nREFUSING: ESPN account ids present in the output", file=sys.stderr)
        return 1
    print("verified: no account ids, no league-scoped tables in the output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
