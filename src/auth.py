"""Sign in once on a device, and stay signed in.

Credentials used to come only from `.env`, which meant there was no sign-in
and nothing to remember. Now the user pastes their ESPN cookies once; the
server keeps them and hands the browser an opaque token.

**The browser never holds the credentials.** It stores only the token, so a
script on the page cannot read the session cookies, and they don't travel on
every request. That is the whole reason for the indirection - a simpler design
would put SWID and espn_s2 straight into `localStorage`.

Tokens are persisted to a file rather than an in-memory dict, because the point
of the feature is that reopening the app just works; an in-memory store would
sign the user out on every backend restart. The file is a credential store and
is treated like one: gitignored, created 0600, never logged, never returned.

`.env` remains a fallback so the notebooks and a fresh checkout keep working
with no sign-in at all.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

from src.espn_draft import (
    S2_MESSAGE,
    EspnAuthError,
    EspnCredentials,
    EspnDraftClient,
    list_leagues,
)

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = _REPO_ROOT / "data" / "device_tokens.json"

# Long enough that guessing is hopeless; this is the only thing standing
# between a request and someone's ESPN session.
_TOKEN_BYTES = 32


def _read() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    except OSError as exc:  # pragma: no cover - permissions
        log.warning("could not read device tokens: %s", exc)
        return {}


def _write(store: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions *before* anything is written, so the
    # credentials are never briefly world-readable.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(store, fh)


def issue(swid: str, espn_s2: str) -> str:
    """Store a credential pair and return the device token for it."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    store = _read()
    store[token] = {
        "kind": "espn",
        "swid": swid,
        "espn_s2": espn_s2,
        "created": int(time.time()),
    }
    _write(store)
    return token


def issue_guest() -> str:
    """A token for someone drafting by hand, with no ESPN account attached.

    The manual path - create a session, click picks - needs an owner so one
    person's board isn't reachable by anyone who guesses a session id. It does
    not need, and should not demand, ESPN cookies to get one.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    store = _read()
    store[token] = {"kind": "guest", "created": int(time.time())}
    _write(store)
    return token


def credentials_for(token: str | None) -> EspnCredentials | None:
    """The ESPN credentials behind a token, or None.

    None for a guest token as well as for an unknown one: neither can talk to
    ESPN. Callers that need to tell those apart want `principal_for`.
    """
    p = principal_for(token)
    return p.creds if p else None


def principal_for(token: str | None):
    """Resolve a token to a Principal, or None if we never issued it."""
    from src.principal import ESPN, GUEST, Principal, guest_user_id, user_id_for

    if not token:
        return None
    entry = _read().get(token)
    if not entry:
        return None

    if entry.get("kind") == "guest":
        return Principal(user_id=guest_user_id(token), kind=GUEST, token=token)

    swid = str(entry.get("swid") or "")
    espn_s2 = str(entry.get("espn_s2") or "")
    if not (swid and espn_s2):
        return None
    return Principal(
        user_id=user_id_for(swid),
        kind=ESPN,
        # `league_id` is deliberately empty. It used to be filled from the
        # operator's LEAGUE_ID, which stamped one person's league onto every
        # other user's credentials; every caller passes the league explicitly.
        creds=EspnCredentials(league_id="", swid=swid, espn_s2=espn_s2),
        token=token,
    )


def forget(token: str | None) -> bool:
    if not token:
        return False
    store = _read()
    if token not in store:
        return False
    store.pop(token)
    _write(store)
    return True


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
