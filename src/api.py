import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from notebooks.config import CURRENT_SEASON, NEXT_SEASON
from src.settings import API_ORIGINS, TEAMS
from src.analysis import league_analysis
from src.db import engine
from src.indexes import ensure_indexes
from src.schemas import SessionCreate, PickBody, RecommendBody
from src.state import new_session, get_session
from src.recommender import recommend
from src.scoring import RISK_AVERSION
from src.biases import load_league_bias

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