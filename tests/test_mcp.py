"""The MCP front door, exercised through a real MCP client (in-process and over HTTP)."""

import socket
import threading
import time

import pytest
import uvicorn
from mcp import Client

from forecast_mcp.mcp_server import PANEL_URI, mcp

pytestmark = pytest.mark.anyio

MODEL_TOOLS = {"list_events", "get_forecast", "propose_constraint", "create_what_if", "get_scenario_result"}
APP_TOOLS = {"confirm_constraint", "reject_constraint", "select_plan", "promote_scenario", "get_scenario_state"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def text(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


async def test_tool_list_and_visibility():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert MODEL_TOOLS | APP_TOOLS <= set(tools)
    for name in APP_TOOLS:
        assert tools[name].meta["ui"]["visibility"] == ["app"]
    for name in ("get_forecast", "propose_constraint", "create_what_if", "get_scenario_result"):
        assert tools[name].meta["ui"]["resourceUri"] == PANEL_URI
    schema = tools["propose_constraint"].input_schema
    assert "constraint" in schema["properties"] and "source_text" in schema["required"]


async def test_panel_resource_is_an_mcp_app():
    async with Client(mcp) as client:
        res = await client.read_resource(PANEL_URI)
    body = res.contents[0]
    assert body.mime_type == "text/html;profile=mcp-app"
    assert "ext-apps" in body.text and "__CHARTS_JS__" not in body.text and "renderTradeoff" in body.text


async def test_forecast_and_propose_confirm_round_trip(event_id):
    async with Client(mcp) as client:
        events = (await client.call_tool("list_events", {})).structured_content["events"]
        assert events[0]["gates"]
        fc = await client.call_tool("get_forecast", {"event_id": event_id})
        assert not fc.is_error and fc.structured_content["view"] == "forecast"
        assert "THIN HISTORY" in text(fc) and "Ask the planner" in text(fc)

        bad = await client.call_tool("propose_constraint", {
            "event_id": event_id, "source_text": "close the west gate", "interpretation": "x",
            "constraint": {"type": "gate_closed", "gate_id": "west", "start": "17:00", "end": "20:00"},
        })
        assert bad.is_error and "Unknown gate_id" in text(bad)

        prop = await client.call_tool("propose_constraint", {
            "event_id": event_id,
            "source_text": "There's a big concert in town from 7pm",
            "interpretation": "A major competing event 19:00-23:00 draws visitors away.",
            "assumptions": ["'big' read as major impact", "Assumed it runs until closing"],
            "constraint": {"type": "competing_event", "name": "Concert in town", "impact": "major", "start": "19:00", "end": "23:00"},
        })
        assert not prop.is_error and "PENDING" in text(prop)
        cid = prop.structured_content["constraint"]["id"]

        # The panel's Confirm button (an app-only tool; the host hides it from Claude).
        conf = await client.call_tool("confirm_constraint", {"constraint_id": cid})
        assert not conf.is_error and conf.structured_content["view"] == "scenario"
        sid = conf.structured_content["scenario"]["id"]

        res = await client.call_tool("get_scenario_result", {"scenario_id": sid})
        body = text(res)
        assert "Trade-off" in body and "Versus the official plan" in body and "Concert in town" in body


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_streamable_http_endpoint():
    from forecast_mcp.web import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(seed=False), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert MODEL_TOOLS <= names
            result = await client.call_tool("list_events", {})
            assert not result.is_error
    finally:
        server.should_exit = True
        thread.join(timeout=10)
