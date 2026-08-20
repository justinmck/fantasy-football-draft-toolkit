import logging
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from notebooks.config import CURRENT_SEASON, NEXT_SEASON
from src.settings import API_ORIGINS, TEAMS
from src.analysis import league_analysis
from src.db import engine
from src.indexes import ensure_indexes
from src.auth import credentials_for, forget, issue, issue_guest, principal_for, verify
from src.schemas import (
    AnalysisRunBody, AuthConnectBody, EspnConnectBody, EspnDisconnectBody, SessionCreate,
    PickBody, RecommendBody,
)
from src.state import SESSIONS, new_session, get_session
from src.principal import Principal, require_espn, require_principal
from src.leagues import assert_league
from src.recommender import recommend
from src.scoring import RISK_AVERSION
from src.biases import fit_league_bias, load_league_bias, persist_league_bias
from src.espn_history import available_seasons, pull_season, store_season
from src.jobs import find_active, get_for as get_job_for, start
from src.migrations import add_league_id, seasons_for_league
from src.espn_draft import (
    ESPN_SYNCS, DraftSync, EspnAuthError, EspnDraftClient, EspnUnavailable,
    all_teams, list_leagues, resolve_my_team_id, team_display,
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
    # Additive and idempotent: gives the league-scoped tables a league_id and
    # labels pre-existing rows with the configured league. Safe every boot.
    add_league_id(engine, os.getenv("LEAGUE_ID"))
except Exception as exc:  # pragma: no cover - depends on DB permissions
    log.warning("could not prepare database: %s", exc)

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
def analysis(league_id: str, year: int | None = None,
             session_id: str | None = None,
             p: Principal = Depends(require_espn)):
    """Retrospective league analysis for the UI's Analysis tab.

    `league_id` is required and checked against the leagues these credentials
    can reach. It used to be optional, falling back to "the only league with
    history" - which is either wrong or empty once more than one person's
    history shares a database, and let anyone read any league's managers,
    standings and per-person draft tendencies by guessing a number.

    `session_id` is how the analysis picks up the league's *settings*. A
    connected session has already adopted that league's `lineupSlotCounts`, and
    the lineup sets replacement level, so every VORP on the page moves with it -
    a two-QB league grades its quarterbacks against a completely different
    baseline. Without a session the default lineup is used, which is right for
    the notebooks and for a league nobody has connected to yet.

    Cached against the database file's identity (see `_db_version`), because the
    payload is expensive to build and does not change until a notebook rewrites
    a table. Falls back to computing every time when the version can't be
    determined.
    """
    league_id = assert_league(p, league_id, NEXT_SEASON)
    year = year or CURRENT_SEASON
    # Only the caller's own session may shape the analysis; otherwise a guessed
    # session id would leak another league's lineup through the baselines.
    session = SESSIONS.get(session_id) if session_id else None
    if session is not None and getattr(session, "owner", "") != p.user_id:
        session = None
    roster_need = dict(session.roster_need) if session else None

    def compute():
        return league_analysis(engine, year, league_id=league_id, roster_need=roster_need)

    version = _db_version()
    if version is None:
        return compute()

    # The lineup is part of the key: two leagues can share a season and a
    # database and still be owed different numbers.
    key = (year, league_id, version,
           tuple(sorted(roster_need.items())) if roster_need else None)
    if key not in _ANALYSIS_CACHE:
        # The database moved, so every previous entry is stale by definition.
        if not any(k[2] == version for k in _ANALYSIS_CACHE):
            _ANALYSIS_CACHE.clear()
        _ANALYSIS_CACHE[key] = compute()
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


def _league_has_history(league_id, fitted_league_id) -> bool:
    """Was the persisted bias fitted on this league?

    Both sides are stringified because the id arrives as a string from the API
    and may have been stored as an integer by the fit script.
    """
    if not fitted_league_id:
        # No fit on record: the constants in src/biases.py are McFL's, and
        # there is nothing to check them against. Treat as applicable, matching
        # the single-league behaviour this app has always had.
        return True
    return str(league_id) == str(fitted_league_id)


def _bias_for(session_id: str):
    """The league bias to score with, or None when it doesn't apply here.

    "This league reaches on Eagles" is a fact about *McFL's* drafters. Applying
    it to a different league's board would be asserting something never
    measured, so a session connected elsewhere gets market ADP only.

    A manual session keeps the fit: that path exists as the fallback for when
    ESPN sync breaks mid-draft, and stripping the signal exactly when the tool
    is already degraded would make the fallback worse. The legend says which
    league it was measured on.
    """
    sync = ESPN_SYNCS.get(session_id)
    if sync is None or sync.league_id is None:
        return BIAS
    fitted = (BIAS.get("meta") or {}).get("league_id")
    return BIAS if _league_has_history(sync.league_id, fitted) else None


def _sync_or_404(session_id: str) -> DraftSync:
    sync = ESPN_SYNCS.get(session_id)
    if sync is None:
        raise HTTPException(status_code=404, detail="not connected to ESPN")
    return sync


def _sync_payload(s, sync: DraftSync, new_picks=None) -> dict:
    ctx = sync.context()
    settings = sync.settings
    fitted_league = (BIAS.get("meta") or {}).get("league_id")
    return {
        "connected": True,
        "status": sync.status,
        "message": sync.message,
        "league": {
            "id": sync.league_id,
            "name": settings.name if settings else None,
            # `draft_at` absent is exactly "no date set" - see parse_settings.
            "draft_at": settings.draft_at if settings else None,
            "scheduled": bool(settings and settings.scheduled),
            # Whether this league is the one the bias was measured on. The UI
            # says so rather than quietly applying one league's habits to
            # another's drafters.
            "has_history": _league_has_history(sync.league_id, fitted_league),
        },
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


# --- sign in -------------------------------------------------------------
#
# The token identifies a device; the credentials stay on the server. Every
# ESPN-touching endpoint accepts the token via the X-Device-Token header and
# falls back to .env when there isn't one, so nothing breaks for a checkout
# that has never signed in.

# `_creds_for` used to live here. It returned the operator's own `.env`
# credentials whenever a request carried no token, which meant an
# unauthenticated stranger was served the operator's ESPN account. Requests now
# resolve through `require_principal`/`require_espn` or get a 401; there is no
# path from a request back to `.env`. Operator credentials remain available to
# the notebooks and CLI scripts, which run *as* the operator.


@app.post("/auth/connect")
def auth_connect(body: AuthConnectBody):
    """Verify a pair of ESPN cookies and remember them for this device."""
    year = body.year or NEXT_SEASON
    swid, espn_s2 = body.swid.strip(), body.espn_s2.strip()
    if not swid or not espn_s2:
        raise HTTPException(status_code=400, detail="Both SWID and espn_s2 are required.")
    try:
        leagues = verify(swid, espn_s2, year)
    except EspnAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except EspnUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not leagues:
        raise HTTPException(
            status_code=502,
            detail="Those credentials work, but no football leagues were found for this season.",
        )
    # Only ever returns the token - never the credentials it stands for.
    return {
        "token": issue(swid, espn_s2),
        "year": year,
        "leagues": [{"league_id": l.league_id, "name": l.name, "season": l.season}
                    for l in leagues],
    }


@app.get("/auth/session")
def auth_session(x_device_token: str | None = Header(default=None)):
    """Whether this device is still signed in."""
    creds = credentials_for(x_device_token)
    if not creds:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return {"signed_in": True}


@app.post("/auth/forget")
def auth_forget(x_device_token: str | None = Header(default=None)):
    return {"signed_in": False, "forgotten": forget(x_device_token)}


@app.get("/espn/leagues")
def espn_leagues(year: int | None = None, p: Principal = Depends(require_espn)):
    """Every football league these credentials can reach.

    Keyed on the SWID rather than on a league id, so nobody has to go and find
    league ids by hand — which is the whole reason multi-league is workable.
    """
    year = year or NEXT_SEASON
    creds = p.creds
    try:
        leagues = list_leagues(creds, year)
    except EspnAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except EspnUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    fitted = (BIAS.get("meta") or {}).get("league_id")
    out = []
    for lg in leagues:
        # One settings fetch each so the picker can show when every draft is,
        # rather than making the user pick blind and find out afterwards.
        # Three sequential calls at ~100ms; a league that fails to answer is
        # still listed, just without its date.
        draft_at, scheduled, unreadable = None, False, False
        try:
            settings = EspnDraftClient(creds, year, league_id=lg.league_id).settings()
            draft_at, scheduled = settings.draft_at, settings.scheduled
        except (EspnAuthError, EspnUnavailable):
            log.warning("could not read settings for league %s", lg.league_id)
            # "No date set" and "we couldn't read the settings" are completely
            # different facts, and collapsing them into one silent None is what
            # made a bad espn_s2 look like three unscheduled drafts. The UI says
            # "date unavailable" for this, never "not scheduled".
            unreadable = True
        out.append({
            "league_id": lg.league_id, "name": lg.name, "season": lg.season,
            "draft_at": draft_at, "scheduled": scheduled, "unreadable": unreadable,
            "has_history": _league_has_history(lg.league_id, fitted),
        })
    return {
        "year": year,
        "leagues": out,
        # Every league failing to answer is a credentials problem, not three
        # coincidences: the SWID alone is enough to list leagues, so this is
        # what a wrong espn_s2 looks like from here. The UI offers a re-sign-in
        # instead of showing an account that appears to work but can't load.
        "credentials_stale": bool(out) and all(l["unreadable"] for l in out),
        # Which league the persisted bias fit came from, so the UI can say
        # "measured on McFL" rather than implying it applies everywhere.
        "bias_league_id": fitted,
    }


@app.post("/espn/connect")
def espn_connect(body: EspnConnectBody, p: Principal = Depends(require_espn)):
    """Attach a session to the live ESPN draft.

    The league is checked against the ones these credentials can actually see,
    so a guessed league id can't be attached to.
    """
    s = _get_session_or_404(body.session_id, p)
    year = body.year or NEXT_SEASON
    creds = p.creds
    league_id = assert_league(p, body.league_id, year) if body.league_id else None
    try:
        client = EspnDraftClient(creds, year, league_id=league_id)
        teams_payload = client.team_payload()
        my_team_id = resolve_my_team_id(teams_payload, creds.swid)
        settings = client.settings()
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

    # ESPN knows the real league shape; the setup form's values were guesses.
    if snapshot.teams and snapshot.teams != s.teams:
        log.info("adopting ESPN team count %s over %s", snapshot.teams, s.teams)
        s.teams = snapshot.teams
    if snapshot.rounds:
        s.rounds = snapshot.rounds
    # Roster need is read from the league rather than assumed, because it sets
    # replacement level and therefore every VORP on the board. A league that
    # starts two quarterbacks would otherwise be scored against a one-QB
    # baseline. Rebuilt in place because the session was created before we knew.
    if settings.roster_need and settings.roster_need != s.roster_need:
        log.info("adopting ESPN roster need %s", settings.roster_need)
        s.roster_need = dict(settings.roster_need)
        s.roster_state = {k: {"have": 0, "need": v} for k, v in settings.roster_need.items()}
        s.depth = {}

    sync = DraftSync(session_id=body.session_id, client=client, engine=engine,
                     year=year, my_team_id=my_team_id,
                     team=team_display(teams_payload, my_team_id),
                     league_id=client.league_id, settings=settings)
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
        # Names for the order, so the Draft day view doesn't render fourteen
        # anonymous team ids.
        "teams_list": all_teams(teams_payload),
    }


@app.get("/espn/sync/{session_id}")
def espn_sync(session_id: str, p: Principal = Depends(require_espn)):
    """Poll ESPN and apply anything new. The workhorse.

    Deliberately does not re-check league reachability: that was proven at
    connect time, and a three-second poll must not wait on an ESPN round-trip.
    """
    s = _get_session_or_404(session_id, p)
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
def espn_disconnect(body: EspnDisconnectBody,
                    p: Principal = Depends(require_principal)):
    """Drop the sync but keep the session and everything drafted so far.

    The escape hatch: if ESPN breaks mid-draft the user must always be able to
    fall back to clicking picks in by hand. Ownership is checked because this
    used to validate nothing at all - anyone could disconnect anyone.
    """
    _get_session_or_404(body.session_id, p)
    ESPN_SYNCS.pop(body.session_id, None)
    return {"connected": False}


# --- per-league analysis ------------------------------------------------

@app.get("/analysis/status")
def analysis_status(league_id: str, year: int | None = None,
                    p: Principal = Depends(require_espn)):
    """What the Analysis tab needs to decide between the gate and the page.

    Cheap and local: reads what's stored rather than asking ESPN, so opening
    the tab costs nothing. Probing ESPN for which seasons *could* be pulled
    happens only when the user asks to run it.
    """
    league_id = assert_league(p, league_id, NEXT_SEASON)
    year = year or CURRENT_SEASON
    seasons = seasons_for_league(engine, league_id)
    running = find_active("history", league_id, p.user_id)
    return {
        "league_id": league_id,
        "seasons": seasons,
        "has_history": bool(seasons),
        # Fitted on whichever league was processed; the tab labels the shared
        # reliability tables as reference when it isn't this one.
        "reference_league_id": (BIAS.get("meta") or {}).get("league_id"),
        "job": running.to_dict() if running else None,
    }


@app.post("/analysis/run")
def analysis_run(body: AnalysisRunBody, p: Principal = Depends(require_espn)):
    """Pull this league's completed seasons, then refit its bias.

    A background job: five seasons is a couple of minutes of waiting on ESPN,
    and a blocking request risks timing out with the database half-populated.
    """
    year = CURRENT_SEASON
    league_id = assert_league(p, body.league_id, NEXT_SEASON)
    creds = p.creds

    first, last = body.since or 2020, body.through or CURRENT_SEASON

    def work(report):
        report(0.02, "Checking which seasons exist…")
        seasons = available_seasons(creds, league_id, range(first, last + 1))
        if not seasons:
            # Not a failure: a brand-new league genuinely has no history, and
            # the tab explains that rather than showing an error.
            return {"seasons": [], "written": {}, "reason": "no completed seasons"}

        written, skipped = {}, []
        for i, year in enumerate(seasons):
            report(0.05 + 0.8 * i / len(seasons), f"Pulling {year}…")
            try:
                frames = pull_season(creds, league_id, year)
            except EspnUnavailable:
                # One bad season shouldn't lose the four that worked.
                skipped.append(year)
                continue
            written[year] = store_season(engine, league_id, year, frames)

        report(0.9, "Measuring this league's draft habits…")
        fit = fit_league_bias(engine, league_id=league_id, permutations=0)
        persist_league_bias(engine, fit, league_id)

        # The payload is keyed on the database file's identity, which just
        # changed - drop it so the tab doesn't read the pre-pull version.
        _ANALYSIS_CACHE.clear()
        return {"seasons": sorted(written), "skipped": skipped,
                "written": {str(k): v for k, v in written.items()},
                "picks_fitted": fit["meta"].get("n_picks", 0)}

    return start("history", league_id, work, owner=p.user_id).to_dict()


@app.get("/analysis/job/{job_id}")
def analysis_job(job_id: str, p: Principal = Depends(require_principal)):
    job = get_job_for(job_id, p.user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.post("/session")
def create_session(cfg: SessionCreate,
                   x_device_token: str | None = Header(default=None)):
    """Start a board.

    Deliberately the one endpoint that does not demand an existing identity:
    drafting by hand is a legitimate way to use this, and shouldn't require
    handing over ESPN cookies. Callers without a token get a guest one back and
    are expected to send it from then on - the session still has an owner, so
    nobody else can read or write it.
    """
    p = principal_for(x_device_token)
    token = None
    if p is None:
        token = issue_guest()
        p = principal_for(token)
    sid = new_session(cfg.teams, cfg.roster_need, rounds=cfg.rounds, owner=p.user_id)
    out = {"session_id": sid}
    if token:
        out["token"] = token
    return out

def _get_session_or_404(session_id: str, p: Principal):
    """A session, but only for whoever owns it.

    404 rather than 403 for someone else's session, deliberately: a 403 would
    confirm the id exists, which is exactly the oracle this check closes. A
    session id used to be a bearer capability - it granted read, pick injection,
    ESPN polling on the owner's cookies, and disconnect, to anyone holding one.
    """
    session = SESSIONS.get(session_id)
    if session is None or getattr(session, "owner", "") != p.user_id:
        raise HTTPException(status_code=404, detail="session not found")
    return session

@app.post("/pick")
def pick(body: PickBody, p: Principal = Depends(require_principal)):
    s = _get_session_or_404(body.session_id, p)
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
def rec(body: RecommendBody, p: Principal = Depends(require_principal)):
    s = _get_session_or_404(body.session_id, p)
    df = recommend(engine,
                   year=body.year or NEXT_SEASON,
                   session=s,
                   current_pick=body.current_pick,
                   next_pick=body.next_pick,
                   bias=_bias_for(body.session_id),
                   # Which league's stored player-seasons to read. `players_stats`
                   # is per league now, so without this the pool joins against
                   # every league's copy and lists players twice.
                   league_id=getattr(ESPN_SYNCS.get(body.session_id), "league_id", None),
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