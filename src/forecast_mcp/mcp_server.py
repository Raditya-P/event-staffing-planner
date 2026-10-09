"""The MCP front door: tools Claude can call, plus the in-chat panel (MCP Apps).

What reaches whom (MCP Apps spec): a tool result's `content` is what Claude
reads; its `structuredContent` goes to the panel only. So every result carries a
plain-text summary for Claude and the full data for the chart.

Tools the panel's buttons call (confirm, reject, select, promote) are marked
app-only: the host hides them from Claude, so only a planner's click can
confirm a constraint or change the official plan.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Annotated

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import Field

from . import jobs
from . import services as S
from .accounts import actor_for_identity, local_actor
from .auth import JwtTokenVerifier, identity_from_token
from .config import settings
from .constraints import Constraint
from .db import session_scope
from .engines.contract import summarize_forecast

PANEL_URI = "ui://forecast-mcp/panel.html"
STATIC = Path(__file__).parent / "static"
RUN_WAIT_SECONDS = 25.0

INSTRUCTIONS = """\
Event Staffing Planner: arrival forecasts and gate staffing plans for event planners (research prototype; the event data is synthetic).

How to work with this server:
1. Call list_events to get event, scenario and gate ids. Never guess ids.
2. get_forecast shows arrivals per gate and hour with an 80% range, split into two kinds of uncertainty:
   day-to-day variation (covered by a staffing buffer) and limited history (reduced by the planner's knowledge).
   When it reports a gate with thin history, ask the planner what they know about it.
3. When the planner mentions roadworks, closures, fewer lanes, staff shortages, competing events or a changed
   show time, call propose_constraint with their exact words in source_text and your structured reading.
   Put anything you had to guess (times, sizes, which gate) in assumptions. The constraint stays PENDING until
   the planner presses Confirm in the panel; never say it is applied before that.
4. Confirmed constraints land in a what-if scenario and re-optimization starts automatically. Use
   get_scenario_result to read the cost-versus-waiting trade-off and explain it.
5. Only the planner can choose a plan and make it official, using the panel or the dashboard. Do not claim to
   have done either.
"""

apps = Apps()


def _actor(channel: str) -> S.Actor:
    """The one place that decides who is calling: the verified token's user and workspace, or the local user."""
    if not settings.auth_enabled:
        return local_actor(channel)
    token = get_access_token()
    if token is None or not token.subject:
        raise S.ServiceError("Sign-in required.", status=401)
    return actor_for_identity(*identity_from_token(token), channel)


def _ok(text: str, data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=text)], structured_content=data)


def _error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=message)], is_error=True)


def _wait(scenario_id: str) -> str | None:
    return jobs.wait_for(scenario_id, timeout=RUN_WAIT_SECONDS)


def _scenario_text(state: dict) -> str:
    sc, ev = state["scenario"], state["event"]
    lines = [f"Scenario '{sc['name']}' [{sc['id']}] ({sc['kind']}, {sc['status']}) for {ev['name']} on {ev['date']}."]
    run = state["run"]
    if run and run["status"] in ("queued", "running"):
        lines.append(f"Re-optimization is still running ({run['stage']}, {run['progress']:.0%}). "
                     "Call get_scenario_result again shortly.")
    if run and run["status"] == "failed":
        lines.append(f"The last run failed: {run['error']}")
    if state["constraints"]:
        lines.append("Constraints in this scenario:")
        for c in state["constraints"]:
            source = f' (planner: "{c["note"]["text"]}")' if c.get("note") else ""
            lines.append(f"  - [{c['id']}] {c['readback']}{source}")
    else:
        lines.append("No constraints in this scenario.")
    if state["pending"]:
        lines.append("Pending planner confirmation: " + "; ".join(f"[{c['id']}] {c['readback']}" for c in state["pending"]))
    result = state["result"]
    if result:
        if not result.get("service_cap_met", True):
            lines.append(f"WARNING: no plan keeps the expected wait under {result['max_expected_wait']:.0f} minutes "
                         "under these constraints; showing the best possible plans.")
        lines.append(f"Trade-off ({len(result['solutions'])} plans, staff cost vs. expected wait per guest):")
        for sol in result["solutions"]:
            tag = f" <- {sol['label']}" if sol["label"] else ""
            chosen = " [SELECTED]" if sol["id"] == sc["selected_solution_id"] else ""
            lines.append(f"  {sol['id']}: {sol['staff_hours']} staff-h, EUR {sol['staff_cost']:,.0f}, "
                         f"wait {sol['expected_wait']:.1f} min (90% of days < {sol['wait_p90']:.1f}){tag}{chosen}")
        if state["explanation"]:
            lines.append("Selected plan: " + state["explanation"])
        for eff in result["constraints_applied"]:
            lines.append(f"Optimizer effect: {eff['effect']}")
        cmp = state["comparison"]
        if cmp:
            lines.append(
                f"Versus the official plan's selection: {cmp['delta_staff_hours']:+d} staff-hours "
                f"(EUR {cmp['delta_staff_cost']:+,.0f}), expected wait {cmp['delta_expected_wait']:+.1f} min, "
                f"bad-day wait {cmp['delta_wait_p90']:+.1f} min, expected arrivals {cmp['delta_day_arrivals_p50']:+,}."
            )
        if state["result_is_stale"]:
            lines.append("Note: these numbers are from the previous run; a newer run is in progress.")
    if sc["kind"] == "what_if":
        lines.append("The planner can pick a plan and promote it to official in the panel or dashboard.")
    return "\n".join(lines)


# ---------------------------------------------------------------- model-visible tools


def list_events() -> CallToolResult:
    """List the planner's upcoming events with their gates (ids, names, lanes), opening hours and scenarios.
    Call this first to get the ids every other tool needs."""
    actor = _actor("chat")
    with session_scope() as s:
        events = S.list_events(s, actor)
    lines = []
    for ev in events:
        lines.append(f"{ev['name']} [{ev['id']}] on {ev['weekday']} {ev['date']} at {ev['venue']}, "
                     f"open {ev['opening_hours']} ({ev['timezone']}), weather '{ev['weather']}', show {ev['show_start']}.")
        lines.append("  Gates: " + ", ".join(f"{g['name']} [{g['id']}] {g['lanes']} lanes" for g in ev["gates"]))
        for sc in ev["scenarios"]:
            lines.append(f"  Scenario {sc['name']} [{sc['id']}] ({sc['kind']}), run {sc['run_status']}")
        if ev["pending_constraints"]:
            lines.append(f"  {ev['pending_constraints']} constraint(s) waiting for planner confirmation.")
    return _ok("\n".join(lines) or "No events yet.", {"view": "events", "events": events})


@apps.tool(resource_uri=PANEL_URI, annotations=ToolAnnotations(read_only_hint=True), meta={"ui/resourceUri": PANEL_URI})
def get_forecast(
    event_id: Annotated[str, Field(description="Event id from list_events.")],
    scenario_id: Annotated[str | None, Field(description="Scenario id; omit for the official plan.")] = None,
) -> CallToolResult:
    """Show the arrival forecast per gate and hour as a chart in the chat, with the 80% range and its split into
    day-to-day variation versus limited history. Returns a text summary for you to explain."""
    actor = _actor("chat")
    try:
        with session_scope() as s:
            sid = scenario_id or S.official_scenario(s, event_id).id
            S.scenario_state(s, actor, sid)  # validates access
        _wait(sid)
        with session_scope() as s:
            state = S.scenario_state(s, actor, sid)
    except S.ServiceError as exc:
        return _error(str(exc))
    if state["forecast"] is None:
        return _error("The forecast is still being computed; try again in a few seconds.")
    text = summarize_forecast(state["forecast"], state["event"], state["scenario"]["name"])
    return _ok(text, {"view": "forecast", **state})


@apps.tool(resource_uri=PANEL_URI, meta={"ui/resourceUri": PANEL_URI})
def propose_constraint(
    event_id: Annotated[str, Field(description="Event id from list_events.")],
    source_text: Annotated[str, Field(description="The planner's own words, quoted exactly, that this constraint comes from.")],
    constraint: Annotated[Constraint, Field(description="Your structured reading of the planner's words. Choose the type that fits; use note_only if none does.")],
    interpretation: Annotated[str, Field(description="One plain sentence saying how you read the planner's words.")],
    assumptions: Annotated[list[str], Field(description="Everything you had to guess or fill in (times, sizes, which gate). Empty if nothing.")] = [],
    scenario_id: Annotated[str | None, Field(description="What-if scenario to add it to; omit to start a new what-if from the official plan.")] = None,
) -> CallToolResult:
    """Record a planner's note as a structured constraint, PENDING their confirmation. The chat panel shows the
    planner's words, your reading and the formal constraint with Confirm / Reject buttons. Nothing changes until
    the planner confirms; after that a what-if scenario is re-optimized automatically."""
    actor = _actor("chat")
    try:
        with session_scope() as s:
            row = S.propose_constraint(
                s, actor, event_id, source_text, constraint.model_dump(), interpretation, assumptions, scenario_id
            )
            ev = S.event_detail(s, actor, event_id)
            view = S.constraint_view(s, row, {g["id"]: g["name"] for g in ev["gates"]})
            target = S.scenario_brief(s, s.get(S.Scenario, row.scenario_id)) if row.scenario_id else None
            official = S.scenario_brief(s, S.official_scenario(s, event_id))
    except S.ServiceError as exc:
        return _error(str(exc))
    where = f"what-if '{target['name']}'" if target else "a new what-if scenario based on the official plan"
    text = (
        f"Proposed constraint [{view['id']}], PENDING planner confirmation: {view['readback']}\n"
        f"It acts on the {view['acts_on']}. On confirmation it goes into {where} and re-optimization starts.\n"
        "The panel shows the planner your reading with Confirm / Reject. Ask them to check it; do not say it is applied."
    )
    event = {k: v for k, v in ev.items() if k not in ("pending", "scenarios")}
    return _ok(text, {"view": "proposal", "event": event, "constraint": view, "target": target, "official": official})


@apps.tool(resource_uri=PANEL_URI, meta={"ui/resourceUri": PANEL_URI})
def create_what_if(
    event_id: Annotated[str, Field(description="Event id from list_events.")],
    name: Annotated[str, Field(description="Short name for the scenario, e.g. 'Show at 20:00'.")],
    base_scenario_id: Annotated[str | None, Field(description="Scenario to copy; omit to copy the official plan.")] = None,
) -> CallToolResult:
    """Start a what-if scenario as a copy of the official plan (or another scenario). It is optimized automatically.
    Add changes to it with propose_constraint(scenario_id=...). It never changes the official plan by itself."""
    actor = _actor("chat")
    try:
        with session_scope() as s:
            sc = S.create_what_if(s, actor, event_id, name, base_scenario_id)
            sid = sc.id
        _wait(sid)
        with session_scope() as s:
            state = S.scenario_state(s, actor, sid)
    except S.ServiceError as exc:
        return _error(str(exc))
    return _ok(f"Created what-if scenario [{sid}].\n" + _scenario_text(state), {"view": "scenario", **state})


@apps.tool(resource_uri=PANEL_URI, annotations=ToolAnnotations(read_only_hint=True), meta={"ui/resourceUri": PANEL_URI})
def get_scenario_result(
    scenario_id: Annotated[str, Field(description="Scenario id from list_events or an earlier tool result.")],
) -> CallToolResult:
    """Show a scenario's staffing trade-off (cost vs. expected waiting) as a chart in the chat, with its constraints,
    the selected plan, robustness on bad days and the comparison with the official plan. Waits briefly if a
    re-optimization is still running."""
    actor = _actor("chat")
    try:
        with session_scope() as s:
            S.scenario_state(s, actor, scenario_id)
        _wait(scenario_id)
        with session_scope() as s:
            state = S.scenario_state(s, actor, scenario_id)
    except S.ServiceError as exc:
        return _error(str(exc))
    return _ok(_scenario_text(state), {"view": "scenario", **state})


# ---------------------------------------------------------------- panel-only tools (hidden from Claude)

APP_ONLY = ["app"]


def _panel_state(scenario_id: str, text: str, actor: S.Actor | None = None) -> CallToolResult:
    actor = actor or _actor("chat_panel")
    with session_scope() as s:
        state = S.scenario_state(s, actor, scenario_id)
    return _ok(text, {"view": "scenario", **state})


@apps.tool(resource_uri=PANEL_URI, visibility=APP_ONLY)
def confirm_constraint(constraint_id: str) -> CallToolResult:
    """Planner confirms a pending constraint (panel button)."""
    try:
        actor = _actor("chat_panel")
        with session_scope() as s:
            row, sc = S.confirm_constraint(s, actor, constraint_id)
            sid, name = sc.id, sc.name
        return _panel_state(sid, actor=actor, text=f"Planner confirmed constraint {constraint_id}; it is now in '{name}' [{sid}], "
                                 "which is being re-optimized.")
    except S.ServiceError as exc:
        return _error(str(exc))


@apps.tool(resource_uri=PANEL_URI, visibility=APP_ONLY)
def reject_constraint(constraint_id: str, reason: str = "") -> CallToolResult:
    """Planner rejects a pending constraint (panel button)."""
    try:
        actor = _actor("chat_panel")
        with session_scope() as s:
            row = S.reject_constraint(s, actor, constraint_id, reason)
            view = S.constraint_view(s, row, S.gate_names(s.get(S.Event, row.event_id)))
        return _ok(f"Planner rejected constraint {constraint_id}.", {"view": "proposal", "constraint": view})
    except S.ServiceError as exc:
        return _error(str(exc))


@apps.tool(resource_uri=PANEL_URI, visibility=APP_ONLY)
def select_plan(scenario_id: str, solution_id: str) -> CallToolResult:
    """Planner picks a plan on a what-if's trade-off (panel click)."""
    try:
        actor = _actor("chat_panel")
        with session_scope() as s:
            S.select_plan(s, actor, scenario_id, solution_id)
        return _panel_state(scenario_id, actor=actor, text=f"Planner selected plan {solution_id} in scenario {scenario_id}.")
    except S.ServiceError as exc:
        return _error(str(exc))


@apps.tool(resource_uri=PANEL_URI, visibility=APP_ONLY)
def promote_scenario(scenario_id: str, solution_id: str) -> CallToolResult:
    """Planner makes a what-if (with the chosen plan) the official plan (panel button, after a confirm step)."""
    try:
        actor = _actor("chat_panel")
        with session_scope() as s:
            S.promote_scenario(s, actor, scenario_id, solution_id)
        return _panel_state(scenario_id, actor=actor, text=f"Planner promoted scenario {scenario_id} with plan {solution_id} to official.")
    except S.ServiceError as exc:
        return _error(str(exc))


@apps.tool(resource_uri=PANEL_URI, visibility=APP_ONLY, annotations=ToolAnnotations(read_only_hint=True))
def get_scenario_state(scenario_id: str) -> CallToolResult:
    """Current state of a scenario, for the panel to refresh itself (run progress, changes made elsewhere)."""
    try:
        return _panel_state(scenario_id, "state")
    except S.ServiceError as exc:
        return _error(str(exc))


def panel_html() -> str:
    html = (STATIC / "panel.html").read_text(encoding="utf-8")
    charts = (STATIC / "charts.js").read_text(encoding="utf-8")
    styles = (STATIC / "app.css").read_text(encoding="utf-8")
    # The panel runs in Claude's sandbox, not on this site, so the fonts travel inside the stylesheet.
    for name in ("ibm-plex-sans-latin-wght.woff2", "instrument-sans-latin-wght.woff2"):
        font = base64.b64encode((STATIC / "fonts" / name).read_bytes()).decode()
        styles = styles.replace(f"/static/fonts/{name}", f"data:font/woff2;base64,{font}")
    return html.replace("/*__CHARTS_JS__*/", charts).replace("/*__APP_CSS__*/", styles)


def server_icons() -> list[Icon]:
    """The logo, for clients that show an icon next to the connector. Served from this site when it has a
    public address; otherwise sent inline so a local server still has one."""
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
        return [Icon(src=f"{base}/static/brand/logo.svg", mime_type="image/svg+xml", sizes=["any"]),
                Icon(src=f"{base}/static/brand/icon-512.png", mime_type="image/png", sizes=["512x512"])]
    svg = base64.b64encode((STATIC / "brand" / "logo.svg").read_bytes()).decode()
    return [Icon(src=f"data:image/svg+xml;base64,{svg}", mime_type="image/svg+xml", sizes=["any"])]


apps.add_html_resource(
    PANEL_URI,
    panel_html(),
    name="Event Staffing Planner panel",
    description="Interactive forecast and trade-off charts with confirm and promote buttons.",
    csp=ResourceCsp(resource_domains=["https://unpkg.com"]),
    prefers_border=True,
)

def _auth_kwargs() -> dict:
    """With sign-in on, /mcp answers 401 + a pointer to the protected-resource metadata, which names the
    identity provider; Claude then runs OAuth with that provider and comes back with a token we verify."""
    if not settings.auth_enabled:
        return {}
    return {
        "auth": AuthSettings(
            issuer_url=settings.oidc_issuer,
            resource_server_url=settings.mcp_url,
            required_scopes=settings.mcp_required_scopes or None,
            validate_token_resource=False,  # JwtTokenVerifier checks the audience itself (MCP_AUDIENCE)
        ),
        "token_verifier": JwtTokenVerifier(),
    }


def build_server(auth: AuthSettings | None = None, token_verifier=None) -> MCPServer:
    # Extensions are read when the server is built, so this runs after every @apps.tool above.
    server = MCPServer(
        name="event-staffing-planner",
        title="Event Staffing Planner",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        website_url=settings.public_base_url or None,
        icons=server_icons(),
        extensions=[apps],
        auth=auth,
        token_verifier=token_verifier,
    )
    server.add_tool(list_events, annotations=ToolAnnotations(read_only_hint=True))
    return server


mcp = build_server(**_auth_kwargs())
