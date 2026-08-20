"""A minimal background job runner for work too slow to hold a request open.

Pulling a league's history is one request per view per season — for five
seasons that's a couple of minutes, mostly waiting on ESPN. A blocking
endpoint would risk a browser or proxy timeout part-way through, leaving the
database half-populated and the user with a spinner that never resolves.

Deliberately small: a thread per job, an in-memory registry, and progress the
job reports itself. There is no queue, no persistence and no cancellation,
because there is exactly one user and one job that matters. The alternative -
Celery, a broker, a worker process - would be more machinery than the whole
rest of the backend.

Jobs are *not* persisted: a backend restart loses them. That is acceptable
because the work is idempotent and the result lives in the database, so
re-running after a restart costs time rather than correctness.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Finished jobs are kept this long so a poll that arrives after completion
# still sees the result rather than a confusing 404.
_RETAIN_SECONDS = 30 * 60


@dataclass
class Job:
    id: str
    kind: str
    key: str                      # what it's about, e.g. a league id
    owner: str = ""               # Principal.user_id that started it
    status: str = "running"       # running | done | failed
    progress: float = 0.0         # 0..1
    message: str = ""
    result: dict | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self) -> dict:
        return {
            # `owner` is deliberately absent: it identifies an account, and
            # this dict goes to the browser.
            "id": self.id, "kind": self.kind, "key": self.key,
            "status": self.status, "progress": round(self.progress, 3),
            "message": self.message, "result": self.result, "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


JOBS: dict[str, Job] = {}

# A pull is minutes of waiting on ESPN, so these are deliberately small: the
# work is I/O bound and the constraint is ESPN's patience, not the CPU.
MAX_CONCURRENT = 4
MAX_QUEUED = 8
MAX_PER_USER = 1

_POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="job")
_LOCK = threading.Lock()


def _reap() -> None:
    """Drop long-finished jobs so the registry can't grow without bound."""
    cutoff = time.time() - _RETAIN_SECONDS
    for job_id in [j.id for j in JOBS.values()
                   if j.finished and j.finished < cutoff]:
        JOBS.pop(job_id, None)


def find_active(kind: str, key: str, owner: str = "") -> Job | None:
    """A running job for the same thing, so a double-click doesn't start two.

    Scoped by owner as well as key: two users pulling the same league must get
    two jobs, because each runs under its own ESPN cookies. Sharing one would
    hand user B a job performed with user A's credentials.
    """
    with _LOCK:
        for job in JOBS.values():
            if (job.kind == kind and job.key == str(key)
                    and job.owner == owner and job.status == "running"):
                return job
    return None


def get_for(job_id: str, owner: str) -> Job | None:
    """A job, but only for whoever started it."""
    job = JOBS.get(job_id)
    return job if job is not None and job.owner == owner else None


def start(kind: str, key: str, fn, owner: str = "") -> Job:
    """Run `fn(report)` on the pool. `report(progress, message)` drives the UI.

    Returns the existing job if one is already running for this key - clicking
    "Run analysis" twice should watch one pull, not start a second one against
    the same league.

    Raises 429 when the caller already has a job running, or the pool is
    saturated. Each job is minutes of outbound ESPN traffic on one user's
    cookies, so an unbounded thread-per-job was a way to turn this deployment
    into a source of ESPN load.
    """
    existing = find_active(kind, key, owner)
    if existing:
        return existing

    with _LOCK:
        mine = [j for j in JOBS.values()
                if j.owner == owner and j.status == "running"]
        if owner and len(mine) >= MAX_PER_USER:
            raise HTTPException(
                status_code=429,
                detail="You already have an analysis running. Wait for it to finish.")
        running = sum(1 for j in JOBS.values() if j.status == "running")
        if running >= MAX_CONCURRENT + MAX_QUEUED:
            raise HTTPException(
                status_code=429,
                detail="The server is busy running analyses. Try again shortly.")

    job = Job(id=uuid.uuid4().hex[:12], kind=kind, key=str(key), owner=owner)
    with _LOCK:
        _reap()
        JOBS[job.id] = job

    def report(progress: float, message: str = "") -> None:
        job.progress = max(0.0, min(1.0, float(progress)))
        if message:
            job.message = message

    def run() -> None:
        try:
            job.result = fn(report)
            job.status, job.progress = "done", 1.0
            job.message = job.message or "Finished"
        except Exception as exc:  # noqa: BLE001 - the message is shown to the user
            # str(exc) is safe here only because everything this runs raises the
            # constant-message exceptions from src/espn_draft.py. Anything that
            # could interpolate a credential must never be raised into a job.
            job.status, job.error = "failed", str(exc)
            job.message = "Failed"
            log.warning("job %s (%s/%s) failed: %s", job.id, kind, key, exc)
        finally:
            job.finished = time.time()

    _POOL.submit(run)
    return job


def get(job_id: str) -> Job | None:
    return JOBS.get(job_id)
