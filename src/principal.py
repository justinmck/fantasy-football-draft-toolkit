"""Who is making this request.

The app used to answer that question with "whoever the server operator is".
`_creds_for()` fell back to the SWID and espn_s2 in `.env` whenever a request
carried no device token, so an unauthenticated stranger hitting `/espn/leagues`
got the *operator's* leagues, fetched with the operator's cookies. That is
survivable for one person on a laptop and catastrophic the moment the app is
reachable from the internet.

Every request now resolves to a `Principal` or gets a 401. There is no third
outcome, and no path back to `.env` from inside a request.

Two kinds of principal:

  espn   - signed in with ESPN cookies; can reach leagues and history.
  guest  - has a token but no cookies. This is the manual-draft path: someone
           clicking picks by hand needs a session they own, but has no reason
           to hand over ESPN credentials to get one.

`user_id` identifies the *account*, not the device, and is deliberately derived
from the SWID rather than from the token. Tokens expire and rotate; a drafter
who re-signs in halfway through a draft must still own the session they were
in the middle of. Keying ownership on the token would orphan their board at the
worst possible moment.

It is an HMAC, not a plain hash, because a SWID is not a harmless identifier:
on its own it is enough to enumerate every league someone is in. A leaked table
of `user_id`s should not be a leaked table of SWIDs.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from src.espn_draft import EspnCredentials
from src.settings import APP_PEPPER

GUEST = "guest"
ESPN = "espn"


@dataclass(frozen=True)
class Principal:
    user_id: str
    kind: str
    creds: EspnCredentials | None = None
    token: str | None = None

    @property
    def is_espn(self) -> bool:
        return self.kind == ESPN and self.creds is not None


def user_id_for(swid: str) -> str:
    """A stable, non-reversible id for an ESPN account.

    Normalized first: ESPN is inconsistent about the braces and the case, and
    the same account arriving as two different ids would silently split one
    person's sessions in half.
    """
    norm = (swid or "").strip().strip("{}").upper()
    return hmac.new(APP_PEPPER, norm.encode(), hashlib.sha256).hexdigest()[:32]


def guest_user_id(token: str) -> str:
    """Guests have no account, so their identity is the token itself.

    Hashed for the same reason as above - the id ends up in log lines and in
    `DraftSession.owner`, and neither should carry something replayable.
    """
    return "g" + hmac.new(APP_PEPPER, token.encode(), hashlib.sha256).hexdigest()[:31]


def require_principal(x_device_token: str | None = Header(default=None)) -> Principal:
    """Resolve the token, or 401. The only way a request acquires an identity.

    `X-Auth-Code` tells the frontend which of the two it is: a missing token
    means "sign in", an unknown one means "you were signed out" - and the
    second must not throw away an in-progress draft, so the UI needs to tell
    them apart without parsing prose.
    """
    from src.auth import principal_for   # late: auth imports espn_draft

    if not x_device_token:
        raise HTTPException(
            status_code=401,
            detail="Sign in to continue.",
            headers={"X-Auth-Code": "no_token"},
        )
    principal = principal_for(x_device_token)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="Signed out. Sign in again to continue.",
            headers={"X-Auth-Code": "auth_expired"},
        )
    return principal


def require_espn(p: Principal = Depends(require_principal)) -> Principal:
    """For anything that spends the user's own ESPN cookies."""
    if not p.is_espn:
        raise HTTPException(
            status_code=403,
            detail="Connect your ESPN account to use this.",
            headers={"X-Auth-Code": "needs_espn"},
        )
    return p
