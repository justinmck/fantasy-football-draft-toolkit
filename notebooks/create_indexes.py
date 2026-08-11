"""Create the database's query indexes.

Run after any notebook that rewrites tables — `to_sql(if_exists="replace")`
drops each table along with its indexes, so they have to be recreated after the
last write, not before it. NB02's final cell and the API's startup both call the
same `ensure_indexes`, so this script is for the case where you've rebuilt part
of the database by hand.

    python notebooks/create_indexes.py

See `src/indexes.py` for what each index is for and why the expression index on
`average_draft_position` is fragile.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "notebooks"))

from src.db import engine  # noqa: E402
from src.indexes import ensure_indexes  # noqa: E402


def main() -> int:
    print("Creating indexes…")
    created = ensure_indexes(engine, verbose=True)
    if not created:
        print("No tables found to index — has NB02 been run?", file=sys.stderr)
        return 1
    print(f"{len(created)} indexes present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
