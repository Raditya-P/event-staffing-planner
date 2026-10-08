"""The dashboard's JSON API (the second front door)."""

import pytest
from starlette.testclient import TestClient

from forecast_mcp.web import create_app


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(seed=False)) as c:
        yield c


def test_pages(client):
    # Without sign-in the app opens straight away; the guided welcome is always at /welcome.
    app_page = client.get("/")
    assert "Staffing Planner" in app_page.text and "/static/app.js" in app_page.text
    assert "default-src 'self'" in app_page.headers["content-security-policy"]
    welcome = client.get("/welcome").text
    assert "About this prototype" in welcome and "The park is made up" in welcome
    assert "<script>" not in welcome  # strict CSP: no inline scripts on our own pages
    panel = client.get("/dev/panel.html").text
    assert "__APP_CSS__" not in panel and "__CHARTS_JS__" not in panel and "renderForecast" in panel
    assert "earth-dark" in panel  # the website's theme is inlined into the chat panel
    for asset in ("app.css", "app.js", "charts.js", "welcome.js", "privacy.js"):
        assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/static/fonts/ibm-plex-sans-latin-wght.woff2").headers["content-type"] == "font/woff2"
    assert client.get("/privacy").status_code == 200


def test_public_facts_come_from_a_computed_forecast(client):
    facts = client.get("/api/facts").json()
    assert facts["available"] is True
    assert 0 < facts["coverage"] <= 1 and facts["gates"] == 3 and facts["history_days"] == 120
    assert facts["thin_gates"] == ["East Gate (new car park)"]


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
