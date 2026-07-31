from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from notebooks.config import NEXT_SEASON
from src.settings import API_ORIGINS, TEAMS
from src.db import engine
from src.schemas import SessionCreate, PickBody, RecommendBody
from src.state import new_session, get_session
from src.recommender import recommend
from src.biases import fit_league_bias

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=API_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

BIAS = fit_league_bias(engine)  # compute once at startup

@app.get("/health")
def health(): return {"ok": True}

@app.post("/session")
def create_session(cfg: SessionCreate):
    sid = new_session(cfg.teams, cfg.roster_need)
    return {"session_id": sid}

def _get_session_or_404(session_id: str):
    try:
        return get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")

@app.post("/pick")
def pick(body: PickBody):
    s = _get_session_or_404(body.session_id)
    s.pick(body.player_id, body.position, body.is_my_pick, body.player_name)
    return {
        "ok": True,
        "roster_state": s.roster_state,
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
                   topn=body.topn)
    return {"results": df.to_dict(orient="records")}