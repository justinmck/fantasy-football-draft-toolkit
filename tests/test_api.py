from starlette.testclient import TestClient

from src.api import app

client = TestClient(app)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_create_session():
    res = client.post("/session", json={"teams": 14, "roster_need": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}})
    assert res.status_code == 200
    assert "session_id" in res.json()


def test_recommend_handles_missing_prior_stats_and_pro_team():
    # Regression test for the NaN-in-JSON crash: player_id 3 ("Rookie TE") has
    # no average_draft_position or players_stats row and a NULL pro_team, all
    # of which used to produce `nan` values that JSONResponse can't serialize.
    sid = client.post("/session", json={}).json()["session_id"]
    res = client.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 15, "topn": 10,
    })
    assert res.status_code == 200
    results = res.json()["results"]
    player_ids = {r["player_id"] for r in results}
    assert {1, 2, 3, 4} <= player_ids
    rookie = next(r for r in results if r["player_id"] == 3)
    assert rookie["pro_team"] is not None


def test_pick_updates_roster_state_and_draft_log():
    sid = client.post("/session", json={}).json()["session_id"]
    res = client.post("/pick", json={
        "session_id": sid, "player_id": 2, "position": "RB",
        "player_name": "Solid RB", "is_my_pick": True,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["roster_state"]["RB"]["have"] == 1
    assert 2 in body["drafted"]
    assert len(body["draft_log"]) == 1
    assert body["draft_log"][0]["player_name"] == "Solid RB"


def test_pick_then_recommend_excludes_drafted_player():
    sid = client.post("/session", json={}).json()["session_id"]
    client.post("/pick", json={
        "session_id": sid, "player_id": 2, "position": "RB", "is_my_pick": False,
    })
    res = client.post("/recommend", json={
        "session_id": sid, "current_pick": 2, "next_pick": 16, "topn": 10,
    })
    player_ids = {r["player_id"] for r in res.json()["results"]}
    assert 2 not in player_ids


def test_unknown_session_returns_404():
    res = client.post("/pick", json={
        "session_id": "doesnotexist", "player_id": 1, "position": "WR",
    })
    assert res.status_code == 404

    res = client.post("/recommend", json={
        "session_id": "doesnotexist", "current_pick": 1, "next_pick": 15,
    })
    assert res.status_code == 404
