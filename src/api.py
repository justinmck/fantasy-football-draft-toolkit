import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from notebooks.config import CURRENT_SEASON, NEXT_SEASON
from src.settings import API_ORIGINS, TEAMS
from src.analysis import league_analysis
from src.db import engine
from src.indexes import ensure_indexes
from src.schemas import (
    EspnConnectBody, EspnDisconnectBody, SessionCreate, PickBody, RecommendBody,
)
from src.state import SESSIONS, new_session, get_session
from src.recommender import recommend
from src.scoring import RISK_AVERSION
from src.biases import load_league_bias
from src.espn_draft import (
    ESPN_SYNCS, DraftSync, EspnAuthError, EspnDraftClient, EspnUnavailable,
    load_credentials, resolve_my_team_id, team_display,
)

log = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=API_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
# /recommend returns up to 300 players x 30 columns - ~240KB of highly
# repetitive JSON on every single pick, which compresses about 10:1. The
# threshold keeps the small responses (/health, /session) uncompressed.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Best-effort: a fresh clone gets the indexes without anyone remembering to run
# the script, and a read-only database degrades to slow-but-working rather than
# refusing to boot.
try:
    ensure_indexes(engine)
except Exception as exc:  # pragma: no cover - depends on DB permissions
    log.warning("could not ensure indexes: %s", exc)

# Read the persisted fit rather than computing it here. Fitting at import would
# need `teams`, `players` and `players_stats.pro_team`, which a partially built
# database (and the test fixture) doesn't have - and it would put a permutation
# test on the boot path. See notebooks/compute_league_bias.py.
BIAS = load_league_bias(engine)

@app.get("/health")
def health(): return {"ok": True}

_ANALYSIS_CACHE: dict[tuple, dict] = {}


def _db_version() -> tuple | None:
    """A fingerprint of the database file, or None when it isn't a local file.

    The original reason this endpoint refused to cache was sound: the notebooks
    drop and recreate these tables, so anything cached on a timer would happily
    serve numbers from a database that no longer exists. Keying on the file's
    own mtime and size satisfies that objection rather than ignoring it — the
    moment a notebook writes, the key changes and the entry is discarded.

    The `-wal`/`-shm` files are folded in because in WAL mode the main file's
    mtime lags behind writes. This database is in `delete` journal mode today,
    so they don't exist; including them costs three stat calls and closes the
    hole permanently if that ever changes.
    """
    url = str(engine.url)
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None  # in-memory, or some future non-file database: fail open
    path = url[len(prefix):].split("?")[0]
    if not path or not os.path.exists(path):
        return None
    parts = []
    for suffix in ("", "-wal", "-shm"):
        try:
            st = os.stat(path + suffix)
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


@app.get("/analysis")
def analysis(year: int | None = None):
    """Retrospective league analysis for the UI's Analysis tab.

    Cached against the database file's identity (see `_db_version`), because the
    payload is expensive to build and does not change until a notebook rewrites
    a table. Falls back to computing every time when the version can't be
    determined.
    """
    year = year or CURRENT_SEASON
    version = _db_version()
    if version is None:
        return league_analysis(engine, year)

    key = (year, version)
    if key not in _ANALYSIS_CACHE:
        # The database moved, so every previous entry is stale by definition.
        _ANALYSIS_CACHE.clear()
        _ANALYSIS_CACHE[key] = league_analysis(engine, year)
    return _ANALYSIS_CACHE[key]

# --- live ESPN draft sync -------------------------------------------------
#
# The frontend polls `/espn/sync`, which fetches from ESPN inside the request.
# There is no background worker on purpose: ESPN returns the entire pick list
# every time rather than a delta, so a missed poll costs nothing and the next
# one reconstructs identical state. A thread would need a lifecycle this app
# has no machinery for (SESSIONS is unbounded, --reload multiplies workers) and
# would turn the replay test into a timing test.

# Below this, serve the last snapshot rather than hitting ESPN again. React
# StrictMode double-mounts effects in development and a user may have two tabs
# open; without it the request rate is several times what was designed for.
_MIN_POLL_SECONDS = 3.0
# Consecutive failures back off, so a dead connection doesn't hammer ESPN.
_BACKOFF = (3.0, 10.0, 30.0)


def _sync_or_404(session_id: str) -> DraftSync:
    sync = ESPN_SYNCS.get(session_id)
    if sync is None:
        raise HTTPException(status_code=404, detail="not connected to ESPN")
    return sync


def _sync_payload(s, sync: DraftSync, new_picks=None) -> dict:
    ctx = sync.context()
    return {
        "connected": True,
        "status": sync.status,
        "message": sync.message,
        "age_seconds": (round(time.time() - sync.last_ok, 1) if sync.last_ok else None),
        "version": sync.version,
        "rebuilt": sync.rebuilt,
        "team": sync.team,
        **ctx,
        # `new_picks` is the event stream the ticker animates; the full log is
        # the authority that makes a missed poll or a page refresh self-healing.
        "new_picks": new_picks if new_picks is not None else sync.last_new,
        "draft_log": s.draft_log,
        "roster_state": s.roster_state,
        "depth": s.depth,
        "picks_remaining": s.picks_remaining(),
        "bench_slots": s.bench_slots(),
        "bench_filled": s.bench_filled,
        "drafted_count": len(s.drafted_ids),
        "unresolved": sync.unresolved,
    }


@app.post("/espn/connect")
def espn_connect(body: EspnConnectBody):
    """Attach a session to the live ESPN draft.

    Credentials are read here, never at import: `src/api.py` is imported by the
    whole test suite with no ESPN variables set, and loading them at module
    scope would fail collection for every test in the project.
    """
    s = _get_session_or_404(body.session_id)
    year = body.year or NEXT_SEASON
    try:
        creds = load_credentials()
        client = EspnDraftClient(creds, year)
        teams_payload = client.team_payload()
        my_team_id = resolve_my_team_id(teams_payload, creds.swid)
        snapshot = client.draft_snapshot()
    except EspnAuthError as exc:
        # The message is a constant from src/espn_draft; never the response body.
        raise HTTPException(status_code=502, detail=str(exc))
    except EspnUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if my_team_id is None:
        raise HTTPException(
            status_code=502,
            detail="Could not find a team owned by this SWID in that league.",
        )
    if snapshot is None:
        raise HTTPException(status_code=503, detail="ESPN returned no draft data.")

    # ESPN knows the real league size; the setup form's value was a guess.
    if snapshot.teams and snapshot.teams != s.teams:
        log.info("adopting ESPN team count %s over %s", snapshot.teams, s.teams)
        s.teams = snapshot.teams
    if snapshot.rounds:
        s.rounds = snapshot.rounds

    sync = DraftSync(session_id=body.session_id, client=client, engine=engine,
                     year=year, my_team_id=my_team_id,
                     team=team_display(teams_payload, my_team_id))
    sync.snapshot = snapshot
    # Records the attempt as well as the success: connect has just talked to
    # ESPN, so the poll the frontend fires immediately afterwards should be
    # served from this snapshot rather than fetching the same thing again.
    sync.status = "ok"
    sync.last_ok = sync.last_attempt = time.time()
    # Rejoining a draft already in progress is a first-class case.
    updated, new_picks = sync.apply(s, snapshot)
    SESSIONS[body.session_id] = updated
    ESPN_SYNCS[body.session_id] = sync

    return {
        **_sync_payload(updated, sync, new_picks),
        "year": year,
        "teams": snapshot.teams,
        "rounds": snapshot.rounds,
        "total_slots": snapshot.total_slots,
        "pick_order": list(snapshot.order),
    }


@app.get("/espn/sync/{session_id}")
def espn_sync(session_id: str):
    """Poll ESPN and apply anything new. The workhorse."""
    s = _get_session_or_404(session_id)
    sync = _sync_or_404(session_id)

    # One session, one in-flight apply. Two overlapping polls could both read
    # the same high-water mark and both apply the same picks, and
    # DraftSession.pick double-counts a roster slot when a pick is replayed.
    with sync.lock:
        now = time.time()
        wait = _BACKOFF[min(sync.failures, len(_BACKOFF) - 1)] if sync.failures else _MIN_POLL_SECONDS
        if now - sync.last_attempt < wait:
            return _sync_payload(s, sync, new_picks=[])

        sync.last_attempt = now
        try:
            snapshot = sync.client.draft_snapshot()
            sync.failures = 0
        except EspnAuthError as exc:
            # The sync failed, not the request - keep returning state so the UI
            # can show a banner instead of losing the board.
            sync.status, sync.message = "auth", str(exc)
            return _sync_payload(s, sync, new_picks=[])
        except EspnUnavailable as exc:
            sync.failures += 1
            sync.status, sync.message = "stale", str(exc)
            return _sync_payload(s, sync, new_picks=[])

        sync.status, sync.message, sync.last_ok = "ok", None, now
        if snapshot is None:
            # 304 Not Modified: nothing changed. The cheapest path, and the one
            # most polls take.
            return _sync_payload(s, sync, new_picks=[])

        sync.snapshot = snapshot
        updated, new_picks = sync.apply(s, snapshot)
        SESSIONS[session_id] = updated
        return _sync_payload(updated, sync, new_picks)


@app.post("/espn/disconnect")
def espn_disconnect(body: EspnDisconnectBody):
    """Drop the sync but keep the session and everything drafted so far.

    The escape hatch: if ESPN breaks mid-draft the user must always be able to
    fall back to clicking picks in by hand.
    """
    ESPN_SYNCS.pop(body.session_id, None)
    return {"connected": False}


@app.post("/session")
def create_session(cfg: SessionCreate):
    sid = new_session(cfg.teams, cfg.roster_need, rounds=cfg.rounds)
    return {"session_id": sid}

def _get_session_or_404(session_id: str):
    try:
        return get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")

@app.post("/pick")
def pick(body: PickBody):
    s = _get_session_or_404(body.session_id)
    filled = s.pick(body.player_id, body.position, body.is_my_pick, body.player_name)
    return {
        "ok": True,
        # `filled_slot` is which roster slot the pick actually consumed - the
        # position's own slot, "FLEX", or None for bench depth. The UI shows it
        # so it's obvious when a third RB stopped filling a starting slot.
        "filled_slot": filled,
        "roster_state": s.roster_state,
        "depth": s.depth,
        "picks_remaining": s.picks_remaining(),
        # Bench is a third of a 16-round draft, so the UI shows it as real
        # roster capacity rather than leaving those picks unaccounted for.
        "bench_slots": s.bench_slots(),
        "bench_filled": s.bench_filled,
        "drafted": list(s.drafted_ids),
        "draft_log": s.draft_log,
    }

@app.post("/recommend")
def rec(body: RecommendBody):
    s = _get_session_or_404(body.session_id)
    df = recommend(engine,
                   year=body.year or NEXT_SEASON,
                   session=s,
                   current_pick=body.current_pick,
                   next_pick=body.next_pick,
                   bias=BIAS,
                   topn=body.topn,
                   risk_aversion=(RISK_AVERSION if body.risk_aversion is None
                                  else body.risk_aversion))
    return {
        "results": df.to_dict(orient="records"),
        # Echoed back so the UI can render roster/timing context alongside the
        # board without a second round-trip.
        "roster_state": s.roster_state,
        "picks_remaining": s.picks_remaining(),
        "depth": s.depth,
        "bench_slots": s.bench_slots(),
        "bench_filled": s.bench_filled,
        # Which season's ADP the timing signal came from. During the offseason
        # this is last season's, and the UI says so rather than presenting it
        # as the current market.
        "adp_year": df.attrs.get("adp_year"),
        "year": body.year or NEXT_SEASON,
    }