"""Rate limiting, in process, with no new dependency.

There was none anywhere. `/auth/connect` in particular accepted unlimited
attempts, and *each one makes an outbound request to ESPN carrying
attacker-supplied cookies* - so an open endpoint here is both a credential
oracle and a way to get the deployment's IP throttled by ESPN.

A token bucket rather than a fixed window: a fixed window lets someone spend
the whole budget in the last second of one window and again in the first second
of the next, which is exactly the burst this is meant to stop.

**In process, so the budget multiplies by worker count.** That is why the
deployment notes pin `--workers 1` - `SESSIONS`, `ESPN_SYNCS` and `JOBS` are all
in-process for the same reason. Running several workers would need Redis, and
that is not worth building for a tool with a dozen users.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

# Swappable so tests can advance the clock instead of sleeping.
now = time.monotonic

_MAX_BUCKETS = 20_000


@dataclass
class _Bucket:
    tokens: float
    last: float


_BUCKETS: dict[tuple[str, str], _Bucket] = {}
_LOCK = threading.Lock()


def _sweep(t: float) -> None:
    """Drop buckets that have refilled completely; they carry no state."""
    stale = [k for k, b in _BUCKETS.items() if t - b.last > 3600]
    for k in stale:
        _BUCKETS.pop(k, None)
    if len(_BUCKETS) > _MAX_BUCKETS:
        _BUCKETS.clear()


def client_ip(request: Request) -> str:
    """The caller's address, trusting X-Forwarded-For only behind a proxy.

    Reading the header unconditionally would make every limit here bypassable
    by setting it, which is worse than having no limiter at all - it would look
    protected. `TRUSTED_PROXY=1` is opt-in, and the hop is counted from the
    right because everything to the left is attacker-controlled.
    """
    if os.getenv("TRUSTED_PROXY") == "1":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            hops = [h.strip() for h in fwd.split(",") if h.strip()]
            depth = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))
            if len(hops) >= depth:
                return hops[-depth]
    return request.client.host if request.client else "unknown"


def check(scope: str, key: str, rate_per_sec: float, burst: float) -> None:
    """Spend one token, or raise 429 with a Retry-After the client can use."""
    t = now()
    with _LOCK:
        if len(_BUCKETS) > _MAX_BUCKETS:
            _sweep(t)
        bucket = _BUCKETS.get((scope, key))
        if bucket is None:
            bucket = _BUCKETS[(scope, key)] = _Bucket(burst, t)
        bucket.tokens = min(burst, bucket.tokens + (t - bucket.last) * rate_per_sec)
        bucket.last = t
        if bucket.tokens < 1.0:
            wait = int((1.0 - bucket.tokens) / rate_per_sec) + 1
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Try again shortly.",
                headers={"Retry-After": str(wait)},
            )
        bucket.tokens -= 1.0


def reset() -> None:
    """Drop all state. For tests."""
    with _LOCK:
        _BUCKETS.clear()


# --- budgets -----------------------------------------------------------------
#
# `/auth/connect` is the tight one, because each attempt costs an outbound ESPN
# call. Five in a burst covers someone fumbling their cookies a few times; one
# per three minutes after that is far too slow to enumerate anything.

def limit_auth_connect(request: Request) -> None:
    check("auth_connect", client_ip(request), rate_per_sec=1 / 180, burst=5)
    # A global ceiling as well, so a botnet spread across addresses still can't
    # turn this deployment into a source of ESPN traffic.
    check("auth_connect_global", "*", rate_per_sec=1 / 60, burst=60)


def limit_analysis_run(user_id: str) -> None:
    """A pull is minutes of ESPN traffic; three an hour is generous."""
    check("analysis_run", user_id, rate_per_sec=1 / 1200, burst=3)


def limit_default(request: Request) -> None:
    """A ceiling on everything else. Well above the board's 3s poll."""
    check("default", client_ip(request), rate_per_sec=4.0, burst=120)
