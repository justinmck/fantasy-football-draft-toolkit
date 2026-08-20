from starlette.testclient import TestClient

from src.api import app


# These endpoints are the manual-draft path, so a guest identity is the honest
# fixture: a session still needs an owner, but drafting by hand should never
# require handing over ESPN cookies.


def test_health(guest):
    res = guest.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_create_session(guest):
    res = guest.post("/session", json={"teams": 14, "roster_need": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}})
    assert res.status_code == 200
    assert "session_id" in res.json()


def test_recommend_handles_missing_prior_stats_and_pro_team(guest):
    # Regression test for the NaN-in-JSON crash: player_id 3 ("Rookie TE") has
    # no average_draft_position or players_stats row and a NULL pro_team, all
    # of which used to produce `nan` values that JSONResponse can't serialize.
    sid = guest.post("/session", json={}).json()["session_id"]
    res = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 15, "topn": 10,
    })
    assert res.status_code == 200
    results = res.json()["results"]
    player_ids = {r["player_id"] for r in results}
    assert {1, 2, 3, 4} <= player_ids
    rookie = next(r for r in results if r["player_id"] == 3)
    assert rookie["pro_team"] is not None


def test_pick_updates_roster_state_and_draft_log(guest):
    sid = guest.post("/session", json={}).json()["session_id"]
    res = guest.post("/pick", json={
        "session_id": sid, "player_id": 2, "position": "RB",
        "player_name": "Solid RB", "is_my_pick": True,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["roster_state"]["RB"]["have"] == 1
    assert 2 in body["drafted"]
    assert len(body["draft_log"]) == 1
    assert body["draft_log"][0]["player_name"] == "Solid RB"


def test_pick_then_recommend_excludes_drafted_player(guest):
    sid = guest.post("/session", json={}).json()["session_id"]
    guest.post("/pick", json={
        "session_id": sid, "player_id": 2, "position": "RB", "is_my_pick": False,
    })
    res = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 2, "next_pick": 16, "topn": 10,
    })
    player_ids = {r["player_id"] for r in res.json()["results"]}
    assert 2 not in player_ids


def test_unknown_session_returns_404(guest):
    res = guest.post("/pick", json={
        "session_id": "doesnotexist", "player_id": 1, "position": "WR",
    })
    assert res.status_code == 404

    res = guest.post("/recommend", json={
        "session_id": "doesnotexist", "current_pick": 1, "next_pick": 15,
    })
    assert res.status_code == 404


def test_recommend_returns_scoring_components_for_explanation(guest):
    # `utility` is a product of four terms; the API returns each one so the UI
    # can say *why* a player is being recommended rather than just how highly.
    sid = guest.post("/session", json={"teams": 14, "rounds": 16}).json()["session_id"]
    res = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 28, "topn": 5,
    })
    assert res.status_code == 200
    body = res.json()
    top = body["results"][0]
    for field in ("pos_weight", "adp_mult", "risk_mult", "availability",
                  "confidence", "base_value", "utility"):
        assert field in top, field
        assert top[field] is not None
    assert body["picks_remaining"] == 16
    assert body["roster_state"]["RB"]["need"] == 2


def test_recommend_returns_reach_fields_as_json_null_not_nan(guest):
    """The Reach column has to tell "no measured history" from "no effect".

    Both arrive as a missing float in pandas, and NaN is not valid JSON - it
    would either 500 the endpoint or reach the browser as a value that is
    neither null nor a number, which the column would render as a real finding.
    """
    sid = guest.post("/session", json={}).json()["session_id"]
    rows = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 15, "topn": 5,
    }).json()["results"]
    for r in rows:
        for field in ("bias_shift", "bias_player_shift", "bias_player_n"):
            assert field in r, field
            assert r[field] is None or isinstance(r[field], (int, float)), r[field]
            # NaN is the one float that isn't equal to itself.
            assert r[field] == r[field], f"{field} came back NaN"


def test_recommend_reports_which_season_of_adp_it_used(guest):
    # The fixture has both 2025 and 2026 ADP, so the requested year wins.
    sid = guest.post("/session", json={}).json()["session_id"]
    body = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 28,
    }).json()
    assert body["adp_year"] == 2026


def test_adp_falls_back_to_the_latest_season_on_record(guest):
    # Real-world offseason case: no ADP published yet for the upcoming season.
    # Without a fallback every player reads as "no market data", which makes
    # pick timing inert exactly when the tool is being used to prepare.
    from src.db import engine
    from src.recommender import resolve_adp_year

    assert resolve_adp_year(engine, 2026) == 2026
    assert resolve_adp_year(engine, 2027) == 2026  # nothing newer exists
    assert resolve_adp_year(engine, 2025) == 2025  # never looks into the future
    assert resolve_adp_year(engine, 2000) is None  # nothing at or before


def test_risk_aversion_zero_changes_ranking_versus_default(guest):
    sid = guest.post("/session", json={"teams": 14, "rounds": 16}).json()["session_id"]
    payload = {"session_id": sid, "current_pick": 1, "next_pick": 28, "topn": 5}

    default = guest.post("/recommend", json=payload).json()["results"]
    no_risk = guest.post("/recommend", json={**payload, "risk_aversion": 0.0}).json()["results"]

    # Turning the dial off must leave every risk multiplier at exactly 1.0.
    assert all(r["risk_mult"] == 1.0 for r in no_risk)
    assert any(r["risk_mult"] < 1.0 for r in default)


def test_risk_aversion_is_rejected_outside_zero_to_one(guest):
    sid = guest.post("/session", json={}).json()["session_id"]
    res = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 28, "risk_aversion": 1.5,
    })
    assert res.status_code == 422


def test_pick_reports_the_slot_it_filled(guest):
    sid = guest.post("/session", json={"teams": 14, "rounds": 16}).json()["session_id"]
    body = guest.post("/pick", json={
        "session_id": sid, "player_id": 2, "position": "RB", "is_my_pick": True,
    }).json()
    assert body["filled_slot"] == "RB"
    assert body["picks_remaining"] == 15


def test_recommend_returns_league_bias_columns(guest):
    """The board explains *why* a player will go early here, so the split has
    to survive the round trip - not just the total."""
    sid = guest.post("/session", json={}).json()["session_id"]
    res = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 15, "topn": 10,
    })
    assert res.status_code == 200
    for row in res.json()["results"]:
        for col in ("bias_shift", "bias_pos_shift", "bias_team_shift"):
            assert col in row and row[col] is not None
        # bias_reason is legitimately None below the reporting threshold.
        assert "bias_reason" in row


def test_recommend_payload_is_json_safe(guest):
    """NaN isn't valid JSON and 500s the endpoint; the bias columns are the
    newest place it could creep back in."""
    sid = guest.post("/session", json={}).json()["session_id"]
    res = guest.post("/recommend", json={
        "session_id": sid, "current_pick": 1, "next_pick": 15, "topn": 50,
    })
    assert "NaN" not in res.text
    assert "Infinity" not in res.text
