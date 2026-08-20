"""The credential store: SQLite, encrypted, in a file of its own.

Replaces a flat JSON file that had four problems, each of which matters more
once there is more than one user:

1. **Plaintext ESPN cookies at rest**, indefinitely. See `src/secretbox.py`.
2. **Read-modify-write of the whole file with no lock and no atomic rename.**
   Two concurrent sign-ins lost one of the tokens, and a crash mid-`json.dump`
   truncated *every* user's credentials at once.
3. **Re-read and re-parsed on every authenticated request.** The board polls
   every three seconds.
4. **No expiry.** A token issued once was valid forever.

**Its own file, not `data/fantasy_data.db`.** That one is tracked in git and has
had runtime writes committed to it eight times. Putting credentials there means
the next `git add data/` publishes everyone's ESPN session. `data/runtime/` is
gitignored precisely so this cannot happen.

The bearer token is stored **hashed**. A stolen copy of this database therefore
cannot be replayed against the API directly - the attacker gets the encrypted
cookies and a set of hashes, not working tokens. That matters because the
failure mode being defended is the file being copied somewhere it shouldn't.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

from sqlalchemy import create_engine, text

from src.secretbox import seal, unseal

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT = _REPO_ROOT / "data" / "runtime" / "auth.db"

AUTH_DB_URL = os.getenv("AUTH_DB_URL", f"sqlite:///{_DEFAULT}")

# Long enough that guessing is hopeless; this is the only thing standing between
# a request and someone's ESPN session.
TOKEN_BYTES = 32

# Absolute alone would sign people out mid-season; sliding alone means a stolen
# token lives forever. Both, so neither failure mode applies.
MAX_AGE = 30 * 24 * 3600
MAX_IDLE = 14 * 24 * 3600
GUEST_MAX_AGE = 24 * 3600

# `last_used` is only written when it has drifted this far, so a three-second
# poll doesn't turn into twenty writes a minute.
_TOUCH_EVERY = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tokens (
  token_hash  TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'espn',
  swid_enc    BLOB,
  espn_s2_enc BLOB,
  created     INTEGER NOT NULL,
  last_used   INTEGER NOT NULL,
  expires     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_device_tokens_expires ON device_tokens(expires);
"""

_lock = threading.Lock()
_engine = None


def engine():
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                if AUTH_DB_URL.startswith("sqlite:///"):
                    path = Path(AUTH_DB_URL[len("sqlite:///"):])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # Owner-only *before* anything is written to it.
                    if not path.exists():
                        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
                _engine = create_engine(
                    AUTH_DB_URL, future=True,
                    connect_args={"check_same_thread": False},
                )
                with _engine.begin() as conn:
                    # WAL so a reader never blocks the writer; this is on the
                    # request path for every authenticated call.
                    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                    for stmt in _SCHEMA.strip().split(";"):
                        if stmt.strip():
                            conn.exec_driver_sql(stmt)
    return _engine


def hash_token(token: str) -> str:
    """SHA-256 of the bearer token. Not a password hash on purpose.

    A slow KDF is for low-entropy secrets. This is 32 random bytes, so there is
    nothing to brute-force, and the lookup is on the hot path.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def insert(*, user_id: str, kind: str, swid: str | None, espn_s2: str | None) -> str:
    """Store a credential pair (or a guest marker) and return its bearer token."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = int(time.time())
    ttl = GUEST_MAX_AGE if kind == "guest" else MAX_AGE
    with engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO device_tokens "
            "(token_hash, user_id, kind, swid_enc, espn_s2_enc, created, last_used, expires) "
            "VALUES (:h, :u, :k, :s, :e, :c, :c, :x)"),
            {"h": hash_token(token), "u": user_id, "k": kind,
             "s": seal(swid) if swid else None,
             "e": seal(espn_s2) if espn_s2 else None,
             "c": now, "x": now + ttl})
    return token


def lookup(token: str | None) -> dict | None:
    """The stored row for a token, decrypted, or None.

    None covers all of: never issued, expired, idle too long, and encrypted
    under a key that is no longer configured. The caller turns every one of
    those into the same 401 - which is correct, because from the user's side
    they are the same event: sign in again.
    """
    if not token:
        return None
    now = int(time.time())
    with engine().begin() as conn:
        row = conn.execute(text(
            "SELECT user_id, kind, swid_enc, espn_s2_enc, created, last_used, expires "
            "FROM device_tokens WHERE token_hash = :h"),
            {"h": hash_token(token)}).mappings().first()
        if row is None:
            return None
        if now > row["expires"] or now - row["last_used"] > MAX_IDLE:
            conn.execute(text("DELETE FROM device_tokens WHERE token_hash = :h"),
                         {"h": hash_token(token)})
            return None
        if now - row["last_used"] > _TOUCH_EVERY:
            conn.execute(text(
                "UPDATE device_tokens SET last_used = :n WHERE token_hash = :h"),
                {"n": now, "h": hash_token(token)})

    out = {"user_id": row["user_id"], "kind": row["kind"], "swid": None, "espn_s2": None}
    if row["kind"] != "guest":
        out["swid"] = unseal(row["swid_enc"])
        out["espn_s2"] = unseal(row["espn_s2_enc"])
        # Undecryptable means the key rotated out from under it. Treat as gone.
        if not (out["swid"] and out["espn_s2"]):
            return None
    return out


def delete(token: str | None) -> bool:
    if not token:
        return False
    with engine().begin() as conn:
        res = conn.execute(text("DELETE FROM device_tokens WHERE token_hash = :h"),
                           {"h": hash_token(token)})
    return bool(res.rowcount)


def sweep(now: int | None = None) -> int:
    """Drop rows past either deadline. Cheap, indexed, safe to call often."""
    now = int(now if now is not None else time.time())
    with engine().begin() as conn:
        res = conn.execute(text(
            "DELETE FROM device_tokens WHERE expires < :n OR last_used < :idle"),
            {"n": now, "idle": now - MAX_IDLE})
    return res.rowcount or 0


def count() -> int:
    with engine().begin() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM device_tokens")).scalar_one()


def import_legacy(path: Path) -> int:
    """Carry tokens over from the old JSON file, once.

    Without this the switch signs everyone out, which is a rotten way to ship a
    security fix - people would re-paste their cookies and reasonably conclude
    the app had broken. The file is renamed rather than deleted so the plaintext
    is out of the way but recoverable if the import went wrong.
    """
    import json

    from src.principal import user_id_for

    if not path.exists():
        return 0
    try:
        store = json.loads(path.read_text())
    except (ValueError, OSError):
        return 0

    now, moved = int(time.time()), 0
    with engine().begin() as conn:
        for token, entry in store.items():
            swid = str(entry.get("swid") or "")
            espn_s2 = str(entry.get("espn_s2") or "")
            if not (swid and espn_s2):
                continue
            created = int(entry.get("created") or now)
            conn.execute(text(
                "INSERT OR IGNORE INTO device_tokens "
                "(token_hash, user_id, kind, swid_enc, espn_s2_enc, created, last_used, expires) "
                "VALUES (:h, :u, 'espn', :s, :e, :c, :n, :x)"),
                {"h": hash_token(token), "u": user_id_for(swid),
                 "s": seal(swid), "e": seal(espn_s2),
                 "c": created, "n": now, "x": created + MAX_AGE})
            moved += 1
    path.rename(path.with_suffix(".json.migrated"))
    return moved
