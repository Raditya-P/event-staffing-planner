"""The dashboard's JSON API (the second front door)."""

import pytest
from starlette.testclient import TestClient

from forecast_mcp.web import create_app


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(seed=False)) as c:
        yield c


def test_pages(client):
    assert "Event forecast" in client.get("/").text
    panel = client.get("/dev/panel.html").text
    assert "__CHARTS_CSS__" not in panel and "renderForecast" in panel
    assert client.get("/static/charts.js").status_code == 200


def test_dashboard_flow(client, event_id):
    detail = client.get(f"/api/events/{event_id}").json()
    rev = client.get(f"/api/events/{event_id}/revision").json()["revision"]
    assert detail["gates"] and rev == detail["revision"]

    sid = client.post("/api/scenarios", json={"event_id": event_id, "name": "Dashboard test"}).json()["id"]
    bad = client.post(f"/api/scenarios/{sid}/constraints", json={"constraint": {"type": "gate_closed", "gate_id": "north",
                                                                               "start": "17:30", "end": "20:00"}})
    assert bad.status_code == 400 and "pattern" in bad.json()["error"]

    ok = client.post(f"/api/scenarios/{sid}/constraints", json={
        "constraint": {"type": "staff_limit", "max_staff": 15, "start": "09:00", "end": "12:00"},
        "note_text": "Only 15 people on the early shift",
    })
    assert ok.status_code == 200
    st = client.get(f"/api/scenarios/{sid}").json()
    assert st["constraints"][0]["note"]["text"] == "Only 15 people on the early shift"
    assert st["run"]["status"] == "done" and st["result"]["solutions"]
    assert client.get(f"/api/events/{event_id}/revision").json()["revision"] > rev

    first = st["result"]["solutions"][0]["id"]
    assert client.post(f"/api/scenarios/{sid}/select", json={"solution_id": first}).status_code == 200
    assert client.post(f"/api/scenarios/{sid}/undo").json()["undone"] == "select_plan"
    official = next(s for s in detail["scenarios"] if s["kind"] == "official" and s["status"] == "active")
    assert client.post(f"/api/scenarios/{official['id']}/undo").status_code == 409

    audit = client.get(f"/api/events/{event_id}/audit").json()
    assert {"add_constraint", "select_plan", "undo"} <= {a["action"] for a in audit}


def test_unknown_ids_are_404(client):
    assert client.get("/api/scenarios/scn_nope").status_code == 404
    assert client.get("/api/events/evt_nope").status_code == 404
