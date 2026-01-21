from pydantic import BaseModel
from typing import Dict, Optional

class SessionCreate(BaseModel):
    teams: int = 14
    roster_need: Dict[str, int] = {"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":1}

class PickBody(BaseModel):
    session_id: str
    player_id: int
    position: str
    is_my_pick: bool = True

class RecommendBody(BaseModel):
    session_id: str
    year: int
    current_pick: int
    next_pick: int
    topn: int = 10