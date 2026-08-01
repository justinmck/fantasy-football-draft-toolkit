from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from notebooks.config import NEXT_SEASON
from src.settings import API_ORIGINS, TEAMS
from src.db import engine
from src.schemas import SessionCreate, PickBody, RecommendBody
from src.state import new_session, get_session
from src.recommender import recommend
from src.scoring import RISK_AVERSION
from src.biases import fit_league_bias

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=API_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

BIAS = fit_league_bias(engine)  # compute once at startup

@app.get("/health")
def health(): return {"ok": True}

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
        # Which season's ADP the timing signal came from. During the offseason
        # this is last season's, and the UI says so rather than presenting it
        # as the current market.
        "adp_year": df.attrs.get("adp_year"),
        "year": body.year or NEXT_SEASON,
    }