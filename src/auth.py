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

from src.espn_draft import EspnCredentials, list_leagues

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
        "swid": swid,
        "espn_s2": espn_s2,
        "created": int(time.time()),
    }
    _write(store)
    return token


def credentials_for(token: str | None) -> EspnCredentials | None:
    """The credentials behind a token, or None if it isn't one we issued."""
    if not token:
        return None
    entry = _read().get(token)
    if not entry:
        return None
    return EspnCredentials(
        league_id=str(os.getenv("LEAGUE_ID") or ""),
        swid=str(entry.get("swid") or ""),
        espn_s2=str(entry.get("espn_s2") or ""),
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
    """Prove the cookies work before storing them.

    Calling the fan endpoint does double duty: a bad cookie fails here, at
    sign-in, where the message can be clear - rather than later as a mysterious
    empty board - and the same call returns the league list the picker needs,
    so signing in costs one request rather than two.
    """
    return list_leagues(EspnCredentials(league_id="", swid=swid, espn_s2=espn_s2), year)
