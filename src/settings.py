import os
import secrets
from pathlib import Path

DB_URL = os.getenv("DATABASE_URL", "sqlite:///data/fantasy_data.db")
API_ORIGINS = os.getenv("API_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
TEAMS = int(os.getenv("LEAGUE_TEAMS", "14"))

# "prod" makes the operator's own `.env` credentials unreachable from a request
# path and refuses to boot without a real pepper - see below and
# `src/espn_draft.load_credentials`.
APP_ENV = os.getenv("APP_ENV", "dev")

_PEPPER_FILE = Path(__file__).resolve().parent.parent / "data" / "runtime" / "pepper"


def _load_pepper() -> bytes:
    """The key behind `user_id`, which is what owns every session.

    Deliberately its own secret rather than sharing one with credential
    encryption: rotating an encryption key should not re-derive every user id
    and sign everyone out mid-season.

    Generated once into a gitignored file in development so a fresh checkout
    just works. In production it must be supplied, because a pepper that
    changes on redeploy silently orphans every session it ever issued.
    """
    env = os.getenv("APP_PEPPER")
    if env:
        return env.encode()
    if APP_ENV == "prod":
        raise RuntimeError(
            "APP_PEPPER must be set in production. Without it every session id "
            "is re-derived on restart and every signed-in user loses their draft."
        )
    if _PEPPER_FILE.exists():
        return _PEPPER_FILE.read_bytes()
    _PEPPER_FILE.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    fd = os.open(_PEPPER_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(value)
    return value


APP_PEPPER = _load_pepper()
