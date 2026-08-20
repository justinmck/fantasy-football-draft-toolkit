"""Sign in once on a device, and stay signed in.

Credentials used to come only from `.env`, which meant there was no sign-in
and nothing to remember. Now the user pastes their ESPN cookies once; the
server keeps them and hands the browser an opaque token.

**The browser never holds the credentials.** It stores only the token, so a
script on the page cannot read the session cookies, and they don't travel on
every request. That is the whole reason for the indirection - a simpler design
would put SWID and espn_s2 straight into `localStorage`.

This module is now the *policy* layer: what a sign-in requires, what a token
means, when to refuse one. The storage underneath it is `src/authdb.py` - an
encrypted SQLite file with hashed tokens and expiry, which replaced a plaintext
JSON file that was rewritten whole on every write.

A short in-process cache sits in front of it: the board polls every three
seconds, and decrypting two cookies per poll for every connected user is work
nobody needs. Short enough that a sign-out takes effect promptly.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from src import authdb
from src.espn_draft import (
    S2_MESSAGE,
    EspnAuthError,
    EspnCredentials,
    EspnDraftClient,
    list_leagues,
)

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Only still referenced to migrate off it; see `authdb.import_legacy`.
LEGACY_TOKEN_FILE = _REPO_ROOT / "data" / "device_tokens.json"

_CACHE_TTL = 60.0
_CACHE_MAX = 2000
_cache: dict = {}
_cache_lock = threading.Lock()


def _cache_get(token: str):
    with _cache_lock:
        hit = _cache.get(token)
    if hit and time.monotonic() - hit[1] < _CACHE_TTL:
        return hit[0]
    return None


def _cache_put(token: str, principal) -> None:
    with _cache_lock:
        if len(_cache) > _CACHE_MAX:
            _cache.clear()
        _cache[token] = (principal, time.monotonic())


def _cache_drop(token: str) -> None:
    with _cache_lock:
        _cache.pop(token, None)


def migrate_legacy_store() -> int:
    """Carry any pre-existing tokens into the encrypted store. Idempotent."""
    try:
        return authdb.import_legacy(LEGACY_TOKEN_FILE)
    except Exception as exc:  # pragma: no cover - never block boot on this
        log.warning("could not migrate legacy token store: %s", exc)
        return 0


def issue(swid: str, espn_s2: str) -> str:
    """Store a credential pair and return the device token for it."""
    from src.principal import user_id_for
    return authdb.insert(user_id=user_id_for(swid), kind="espn",
                         swid=swid, espn_s2=espn_s2)


def issue_guest() -> str:
    """A token for someone drafting by hand, with no ESPN account attached.

    The manual path - create a session, click picks - needs an owner so one
    person's board isn't reachable by anyone who guesses a session id. It does
    not need, and should not demand, ESPN cookies to get one.
    """
    import secrets
    return authdb.insert(user_id="guest:" + secrets.token_hex(8), kind="guest",
                         swid=None, espn_s2=None)


def credentials_for(token: str | None) -> EspnCredentials | None:
    """The ESPN credentials behind a token, or None.

    None for a guest token as well as for an unknown one: neither can talk to
    ESPN. Callers that need to tell those apart want `principal_for`.
    """
    p = principal_for(token)
    return p.creds if p else None


def principal_for(token: str | None):
    """Resolve a token to a Principal, or None if it isn't a live one.

    None covers unknown, expired, idle-expired and undecryptable alike. They
    are the same event from the user's side - sign in again - and collapsing
    them here means no caller can accidentally leak which it was.
    """
    from src.principal import ESPN, GUEST, Principal

    if not token:
        return None
    cached = _cache_get(token)
    if cached is not None:
        return cached

    row = authdb.lookup(token)
    if row is None:
        _cache_drop(token)
        return None

    if row["kind"] == "guest":
        p = Principal(user_id=row["user_id"], kind=GUEST, token=token)
    else:
        p = Principal(
            user_id=row["user_id"],
            kind=ESPN,
            # `league_id` is deliberately empty. It used to be filled from the
            # operator's LEAGUE_ID, which stamped one person's league onto every
            # other user's credentials; every caller passes the league explicitly.
            creds=EspnCredentials(league_id="", swid=row["swid"],
                                  espn_s2=row["espn_s2"]),
            token=token,
        )
    _cache_put(token, p)
    return p


def forget(token: str | None) -> bool:
    _cache_drop(token)
    return authdb.delete(token)


def verify(swid: str, espn_s2: str, year: int) -> list:
    """Prove *both* cookies work before storing them.

    Calling the fan endpoint does double duty: a bad SWID fails here, at
    sign-in, where the message can be clear - rather than later as a mysterious
    empty board - and the same call returns the league list the picker needs,
    so signing in costs one request rather than two.

    But the fan endpoint carries the SWID in its URL path and is satisfied by
    it alone, so it cannot tell us anything about `espn_s2`. A pair with a good
    SWID and a junk `espn_s2` passed sign-in, listed every league by name, and
    then failed on every per-league read - which surfaced as three leagues all
    claiming their draft wasn't scheduled. Reading one league's settings is the
    cheapest call that genuinely needs the second cookie, so it's the one that
    decides whether this sign-in is accepted.
    """
    creds = EspnCredentials(league_id="", swid=swid, espn_s2=espn_s2)
    leagues = list_leagues(creds, year)
    if leagues:
        try:
            EspnDraftClient(creds, year, league_id=leagues[0].league_id).settings()
        except EspnAuthError:
            raise EspnAuthError(S2_MESSAGE) from None
    return leagues
