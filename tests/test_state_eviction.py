"""Sessions have to be reclaimed, and reclaiming them must not be a weapon.

`SESSIONS` was an unbounded dict that was never evicted, so `POST /session` was
a memory-exhaustion primitive. The obvious fix - a global LRU - is itself a
cross-tenant denial of service: one person opening sessions in a loop would
push everyone else's live draft off the board. Hence a per-user cap as well,
which is the property the middle test here pins.
"""
import pytest

from src import state
from src.state import SESSIONS, new_session, sweep

NEED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}


@pytest.fixture(autouse=True)
def clean():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


def _mk(owner, created=None, last_seen=None):
    sid = new_session(14, dict(NEED), rounds=16, owner=owner)
    if created is not None:
        SESSIONS[sid].created = created
    if last_seen is not None:
        SESSIONS[sid].last_seen = last_seen
    return sid


class TestIdleSweep:
    def test_an_idle_session_is_dropped(self):
        sid = _mk("user-a", last_seen=0.0)
        assert sweep() >= 1
        assert sid not in SESSIONS

    def test_a_live_session_survives(self):
        sid = _mk("user-a")
        sweep()
        assert sid in SESSIONS

    def test_a_swept_session_is_gone_not_broken(self):
        """The endpoint turns a missing session into 404, not a 500."""
        sid = _mk("user-a", last_seen=0.0)
        sweep()
        assert SESSIONS.get(sid) is None


class TestPerUserCap:
    def test_the_cap_evicts_only_that_users_oldest(self):
        """The cross-tenant regression.

        A global-only cap would let one user's loop evict everybody else. B's
        single session must survive A opening far more than the limit.
        """
        b = _mk("user-b", created=1000.0)
        a_sessions = [_mk("user-a", created=float(i)) for i in range(10)]

        sweep()

        assert b in SESSIONS, "another user's session was evicted"
        surviving_a = [s for s in a_sessions if s in SESSIONS]
        assert len(surviving_a) == state.MAX_PER_USER
        # Oldest first: the survivors are the most recently created.
        assert surviving_a == a_sessions[-state.MAX_PER_USER:]

    def test_two_users_each_keep_their_own(self):
        a = [_mk("user-a", created=float(i)) for i in range(3)]
        b = [_mk("user-b", created=float(i)) for i in range(3)]
        sweep()
        assert all(s in SESSIONS for s in a + b)


class TestEvictionHook:
    def test_dropping_a_session_drops_its_espn_sync(self):
        """Otherwise the sync keeps a live client - and the user's cookies -
        alive for a board nobody can reach any more."""
        seen = []
        state.on_evict.append(seen.append)
        try:
            sid = _mk("user-a", last_seen=0.0)
            sweep()
            assert sid in seen
        finally:
            state.on_evict.remove(seen.append)

    def test_a_failing_hook_does_not_stop_eviction(self):
        def boom(sid):
            raise RuntimeError("hook is broken")

        state.on_evict.append(boom)
        try:
            sid = _mk("user-a", last_seen=0.0)
            sweep()
            assert sid not in SESSIONS
        finally:
            state.on_evict.remove(boom)


class TestGlobalCap:
    def test_the_global_ceiling_holds(self, monkeypatch):
        monkeypatch.setattr(state, "MAX_SESSIONS", 6)
        monkeypatch.setattr(state, "MAX_PER_USER", 1000)  # isolate the global cap
        for i in range(12):
            _mk(f"user-{i}", created=float(i))
        sweep()
        assert len(SESSIONS) <= 6
