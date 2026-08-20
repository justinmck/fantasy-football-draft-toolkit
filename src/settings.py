import os
import secrets
import shutil
from pathlib import Path
from urllib.parse import urlparse

# "prod" makes the operator's own `.env` credentials unreachable from a request
# path, refuses to boot without a real pepper or APP_SECRET, and rejects a
# non-https CORS origin. Read first because everything below gates on it.
APP_ENV = os.getenv("APP_ENV", "dev")

# The shipped reference database is read-only; the app writes to a copy under
# data/runtime/ that is gitignored. Before this split the app wrote to a file
# tracked in git, which is how eight commits of runtime data - and two real
# leagues' team and manager names - ended up in version control.
# Built by notebooks/build_reference_db.py: the ten league-independent tables,
# with `players.current_team_name` nulled and the file vacuumed. 0.4 MB against
# the 6.6 MB working database, and carries no real names.
REFERENCE_DB = Path(__file__).resolve().parent.parent / "data" / "reference.db"
RUNTIME_DB = Path(__file__).resolve().parent.parent / "data" / "runtime" / "fantasy_data.db"


def _default_db_url() -> str:
    """Seed the runtime database from the shipped one, once."""
    if not RUNTIME_DB.exists() and REFERENCE_DB.exists():
        RUNTIME_DB.parent.mkdir(parents=True, exist_ok=True)
        tmp = RUNTIME_DB.with_suffix(".seeding")
        shutil.copy2(REFERENCE_DB, tmp)
        # Atomic, so two workers starting together can't see a half-copy.
        os.replace(tmp, RUNTIME_DB)
    return f"sqlite:///{RUNTIME_DB}"


DB_URL = os.getenv("DATABASE_URL") or _default_db_url()
TEAMS = int(os.getenv("LEAGUE_TEAMS", "14"))


def parse_origins(raw: str, *, allow_credentials: bool = True) -> list[str]:
    """Validate CORS origins at import, so a bad config refuses to boot.

    `API_ORIGINS` was split on commas and handed straight to the middleware.
    Three ways that goes wrong quietly:

    - `"a, b"` yields a second entry of `" b"`, which matches nothing, so the
      origin silently stops working.
    - `"*"` with `allow_credentials=True` is not a valid CORS configuration and
      browsers ignore it - the deployment looks permissive and is actually
      broken.
    - An entry with a path (`https://x.com/app`) never matches an Origin header,
      which only ever carries scheme, host and port.

    Failing loudly at startup beats any of those.
    """
    out = []
    for item in raw.split(","):
        origin = item.strip()
        if not origin:
            continue
        if origin == "*":
            if allow_credentials:
                raise RuntimeError(
                    "API_ORIGINS='*' with credentials is not a valid CORS "
                    "configuration and is ignored by browsers. List the origins "
                    "explicitly.")
            out.append(origin)
            continue
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
            raise RuntimeError(
                f"API_ORIGINS entry is not a bare scheme://host[:port]: {origin!r}")
        if APP_ENV == "prod" and parsed.scheme != "https":
            raise RuntimeError(f"non-https origin in production: {origin!r}")
        out.append(f"{parsed.scheme}://{parsed.netloc}")
    if not out:
        raise RuntimeError("API_ORIGINS is empty")
    return out


API_ORIGINS = parse_origins(
    os.getenv("API_ORIGINS", "http://localhost:5173,http://localhost:3000"))

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
