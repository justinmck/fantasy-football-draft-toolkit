"""Encryption for the ESPN cookies held on the server.

**What this does and does not buy.** The server has to decrypt these to use
them, so a compromised *application* can read them regardless — this is not a
defence against that, and pretending otherwise would be worse than not doing it.

What it does defend is a database file that *leaves the machine*: a git commit,
a backup, a laptop snapshot, an `scp` of `data/`. That is not hypothetical here.
This repo committed a 6.6 MB SQLite file eight times, and published sixteen real
ESPN account ids to a public repo, before anyone noticed. The credential store
is the one file where that mistake is unrecoverable, so it does not sit in
plaintext.

Two secrets, deliberately separate:

- `APP_SECRET` encrypts the cookies. Rotating it re-encrypts rows lazily.
- `APP_PEPPER` derives `user_id` from a SWID. It must **never** rotate: the
  owner of a draft session is a `user_id`, so changing the pepper orphans every
  session and signs everyone out mid-draft.

Sharing one secret for both would couple those lifecycles, which is why they
aren't.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEV_SECRET = _REPO_ROOT / "data" / "runtime" / "dev_secret"
_DEV_PEPPER = _REPO_ROOT / "data" / "runtime" / "dev_pepper"

# Set APP_ENV=prod on a deployed host. It turns three things from warnings into
# refusals: a generated dev secret, operator credentials from .env, and a
# non-https CORS origin.
APP_ENV = os.getenv("APP_ENV", "dev")


class SecretsMissing(RuntimeError):
    """No usable APP_SECRET in an environment that requires one."""


def _read_or_create(path: Path, env_name: str) -> str:
    """A stable dev secret, generated once, 0600, gitignored.

    Generating a *fresh* one per boot would silently invalidate every stored
    token on restart, which looks like a bug rather than a configuration
    problem. In production this path is never taken.
    """
    if APP_ENV == "prod":
        raise SecretsMissing(
            f"{env_name} must be set in production. Generate one with:\n"
            f"  python -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\""
        )
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = Fernet.generate_key().decode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    log.warning("%s not set; generated a development secret at %s", env_name, path)
    return value


def _fernet() -> MultiFernet:
    """Current key first, then any retired ones from APP_SECRET_OLD.

    MultiFernet decrypts with whichever key works and always *encrypts* with
    the first, so re-saving a row rotates it forward.
    """
    primary = os.getenv("APP_SECRET") or _read_or_create(_DEV_SECRET, "APP_SECRET")
    keys = [Fernet(primary.encode())]
    for old in (os.getenv("APP_SECRET_OLD") or "").split(","):
        old = old.strip()
        if old:
            keys.append(Fernet(old.encode()))
    return MultiFernet(keys)


def pepper() -> bytes:
    """The HMAC pepper for `user_id`. Never rotate this - see the module docstring."""
    return (os.getenv("APP_PEPPER") or _read_or_create(_DEV_PEPPER, "APP_PEPPER")).encode()


def seal(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def unseal(blob) -> str | None:
    """Decrypt, or None if no key fits.

    Never raises. A row encrypted under a key that is no longer configured is
    unreadable, and the caller's job is to sign that device out and ask for the
    cookies again - not to 500 in the middle of a draft.
    """
    if blob is None:
        return None
    try:
        return _fernet().decrypt(bytes(blob)).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
