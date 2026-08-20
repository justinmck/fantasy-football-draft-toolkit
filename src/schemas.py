from pydantic import BaseModel, Field
from typing import Dict, Optional

# Unbounded fields on a public endpoint are a denial-of-service surface: a
# 40-team, 400-round session allocates real memory, and a 2 MB cookie string
# gets encrypted and stored. These ceilings are all far above any real league.
class SessionCreate(BaseModel):
    teams: int = Field(default=14, ge=2, le=32)
    roster_need: Dict[str, int] = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
    # Total rounds in the draft. Optional: when set, need weighting escalates
    # as the drafter's remaining picks run out (src/scoring.roster_urgency).
    # When omitted, need weighting stays flat for the whole draft.
    rounds: Optional[int] = Field(default=None, ge=1, le=40)

class PickBody(BaseModel):
    session_id: str
    player_id: int
    position: str
    player_name: Optional[str] = None
    is_my_pick: bool = True

class EspnConnectBody(BaseModel):
    session_id: str
    # The draft's season, not the season being scored. Defaults to the upcoming
    # draft, which is the only one anyone connects to live.
    year: Optional[int] = None
    # Which of the user's leagues to attach to. Optional so an existing caller
    # (and the notebooks' single-league assumption) still works: falls back to
    # LEAGUE_ID from the environment.
    league_id: Optional[str] = None


class AuthConnectBody(BaseModel):
    # A SWID is a braced GUID (38 chars) and espn_s2 a few hundred characters.
    # The ceilings stop someone posting megabytes to be encrypted and stored.
    swid: str = Field(max_length=128)
    espn_s2: str = Field(max_length=4096)
    year: Optional[int] = Field(default=None, ge=2000, le=2100)


class AnalysisRunBody(BaseModel):
    # ESPN league ids are numeric. Constraining the shape here means the value
    # can't be anything exotic by the time it reaches a query or a URL.
    league_id: str = Field(pattern=r"^[0-9]{1,12}$")
    # Which seasons to look for. Defaults cover every season ESPN is likely to
    # still serve; seasons the league didn't exist for are skipped, not errors.
    since: Optional[int] = Field(default=None, ge=2000, le=2100)
    through: Optional[int] = Field(default=None, ge=2015, le=2100)


class EspnDisconnectBody(BaseModel):
    session_id: str


class RecommendBody(BaseModel):
    session_id: str
    year: Optional[int] = None
    current_pick: int
    # The drafter's *next* turn after the one being decided now. Pick timing is
    # a "grab him now or wait?" trade-off, so this must be a later pick than
    # current_pick; when they're equal there is no wait option and
    # src/scoring.availability_pressure returns a neutral 1.0 for everyone.
    next_pick: int
    topn: int = Field(default=10, ge=1, le=300)
    # How much uncertainty is allowed to discount a player, 0-1. 0 ranks purely
    # on value/need/timing; higher values increasingly prefer the safer player
    # when the board is close. Defaults to src/scoring.RISK_AVERSION.
    risk_aversion: Optional[float] = Field(default=None, ge=0.0, le=1.0)