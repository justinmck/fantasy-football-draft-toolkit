"""Which leagues a signed-in user is allowed to touch.

`/analysis`, `/analysis/status`, `/analysis/run` and `/espn/connect` all take a
league id from the request. Without a check, any signed-in user could read any
other league's draft history, final standings, per-manager reach tendencies and
team names by guessing a numeric id - and league ids are small integers.

The rule is simply: you may touch a league your own credentials can see. ESPN
already answers that question through the fan endpoint, which `list_leagues`
wraps, so this is a cache in front of a call the app was making anyway.

Two deliberate choices:

  * **404, never 403.** A 403 confirms the league exists, which hands back
    exactly the information the check is meant to withhold.
  * **Stale is better than locked out.** If ESPN blips mid-draft, serving a
    slightly old league list is far better than telling a drafter they no
    longer have access to their own league while they are on the clock.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from fastapi import HTTPException

from src.espn_draft import EspnUnavailable, list_leagues

# A league list changes a few times a year, so this could be much longer. Five
# minutes keeps "I just joined a league" from needing a restart.
TTL = 300.0
# How long a cached answer keeps being served after ESPN starts failing.
STALE_GRACE = 1800.0
MAX_ENTRIES = 5000

_CACHE: "OrderedDict[tuple[str, int], tuple[float, frozenset[str]]]" = OrderedDict()
_LOCK = threading.Lock()

# Module attribute rather than a direct call so tests can advance the clock.
_now = time.monotonic


def clear() -> None:
    with _LOCK:
        _CACHE.clear()


def reachable(principal, year: int) -> frozenset[str]:
    """Every league id these credentials can see in `year`."""
    key = (principal.user_id, int(year))
    now = _now()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]

    try:
        ids = frozenset(str(l.league_id) for l in list_leagues(principal.creds, year))
    except EspnUnavailable:
        if hit and now - hit[0] < STALE_GRACE:
            return hit[1]
        raise

    with _LOCK:
        _CACHE[key] = (now, ids)
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return ids


def assert_league(principal, league_id, year: int) -> str:
    """Return the league id, or 404 if it isn't one this user can reach."""
    lid = str(league_id or "").strip()
    if not lid or lid not in reachable(principal, year):
        raise HTTPException(status_code=404, detail="league not found")
    return lid
