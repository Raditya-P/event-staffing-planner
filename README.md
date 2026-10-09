<div align="center">

<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/lockup-dark.png">
    <img alt="Event Staffing Planner" src="docs/brand/lockup-light.png" width="440">
  </picture>
</h1>

**Arrival forecasts and staffing plans for event entrances, available in Claude and on the web.**

[![Version](https://img.shields.io/badge/version-0.1.0-2f7fd0)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-1f7a4d)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-555)](pyproject.toml)

[Overview](#overview) · [How it works](#how-it-works) · [Using it in Claude](#using-it-in-claude) · [Getting started](#getting-started) · [Live demo](https://forecast-mcp.ashyground-d96d5f06.malaysiawest.azurecontainerapps.io/)

</div>

---

<p align="center">
  <img src="docs/screenshots/forecast.png" alt="Arrival forecast for one entrance, with the median and the 80% range per hour" width="860">
</p>

## Overview

Event Staffing Planner helps event planners decide how many staff to assign to each entrance, hour by hour. It forecasts guest arrivals, explains the uncertainty in each forecast, and proposes staffing plans that balance labour cost against guest waiting time.

The tool is a research prototype. It addresses two questions: whether explaining the sources of forecast uncertainty helps planners act on a forecast, and whether planners' knowledge, stated in plain language, can be turned into constraints that an optimizer follows once the planner confirms them.

All data describes a fictional venue, Parkland Theme Park, with three entrances and 120 days of synthetic visitor history. Forecasts and staffing plans are computed from this history.

## Features

- Forecasts arrivals per entrance and hour with a median and an 80% range, and separates the uncertainty into day-to-day variation and limited history.
- Generates a set of staffing plans from lowest cost to shortest queues, and reports each plan's 90th-percentile wait across sampled days.
- Records changes described to Claude, such as an entrance closure, as constraints that take effect only after the planner confirms them.
- Lets planners test changes in scenarios without affecting the official plan, and logs every decision with its author, time and source.
- Explains each forecast and plan step by step: why, how sure, what would change it and, for plans, a stress test against higher or lower arrivals. Explanations can be shown as a chart, as text or both, in plain or technical wording.
- Provides the same functions on the website and in Claude, where an MCP Apps panel displays the charts in the conversation. The [references page](https://forecast-mcp.ashyground-d96d5f06.malaysiawest.azurecontainerapps.io/references) lists the research behind each method.

## How it works

### Forecasting

A bootstrapped ensemble of gradient-boosted quantile models produces the median and the 80% range for each entrance and hour. Using the law of total variance, the spread is divided into the average variance within ensemble members (day-to-day variation) and the variance between members (limited history). The 80% ranges are validated on days held out from training.

### Optimization

NSGA-II, a multi-objective evolutionary algorithm (pymoo), selects the number of lanes to staff at each entrance and hour, trading staff cost against expected waiting time. Waiting times are estimated with a fluid backlog model and Sakasegawa's M/M/c approximation.

### Constraint workflow

1. The planner describes a change in Claude, for example "roadworks close the north road from 5 to 8 pm".
2. Claude calls `propose_constraint` with the planner's wording, a structured constraint and any assumptions. The constraint is pending.
3. The panel shows Claude's interpretation with Confirm and Reject buttons. These buttons call tools that are hidden from Claude, so only the planner can confirm.
4. A confirmed constraint is added to a scenario, and the forecast and optimization run again.
5. The planner selects a plan and adopts the scenario as the official plan. The activity log records each step.

<p align="center">
  <img src="docs/screenshots/plans.png" alt="Staffing plans: staff cost against expected waiting time" width="430">
  <img src="docs/screenshots/chat-panel.png" alt="The chat panel showing the forecast for all three entrances" width="430">
</p>

## Using it in Claude

1. In Claude, open Settings > Connectors and add a custom connector with the address `https://forecast-mcp.ashyground-d96d5f06.malaysiawest.azurecontainerapps.io/mcp`. Custom connectors require a paid Claude plan.
2. Sign in when prompted. Each user receives a private copy of the demo venue.
3. Ask in plain language, for example "Show me the forecast for Halloween Night".

| Tool | Purpose |
| --- | --- |
| `list_events` | Lists events with their entrances, opening hours and scenarios |
| `get_forecast` | Shows arrivals per entrance and hour, with the 80% range and its sources of uncertainty |
| `propose_constraint` | Records a planner's change as a pending constraint |
| `create_what_if` | Creates a scenario as a copy of the official plan |
| `get_scenario_result` | Shows the cost and waiting-time trade-off, the selected plan and the comparison with the official plan |

## Getting started

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run forecast-mcp
```

Open http://127.0.0.1:8000/. On first start, the server creates the database, generates the demo data and trains the forecast model, which takes about 10 seconds. Without sign-in settings, it runs for a single local user. To test the Claude panel without Claude, open http://127.0.0.1:8000/dev/panel.

### Configuration

Settings are read from environment variables or a `.env` file. See `.env.example` for the full list.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. Without it, a local SQLite file is used. |
| `PUBLIC_BASE_URL` | Public HTTPS address of the server, required for Claude. |
| `AUTH_MODE` | `oidc` to require sign-in through an OpenID Connect provider; `none` for local use. |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | Identity provider settings for sign-in. |
| `MCP_AUDIENCE` | Expected audience of access tokens, usually `<PUBLIC_BASE_URL>/mcp`. |
| `SESSION_SECRET` | Key for signing website sessions. |
| `ENGINE_PROFILE` | `light` for hosts with limited CPU. |

The identity provider must support Dynamic Client Registration or Client ID Metadata Documents, PKCE (S256) and JWT access tokens. Register `https://claude.ai/api/mcp/auth_callback` and `<PUBLIC_BASE_URL>/auth/callback` as redirect URIs. The live demo uses WorkOS AuthKit.

### Deployment

The `Dockerfile` builds a single container that serves the website, the MCP endpoint and a background worker for forecast and optimization runs. GitHub Actions runs the tests and builds the image on every push. `deploy/azure.ps1` deploys the image to Azure Container Apps.

## Project structure

| Path | Contents |
| --- | --- |
| `src/forecast_mcp/engines/` | Synthetic data, forecasting model, forecast contract and optimizer |
| `src/forecast_mcp/constraints.py` | Constraint definitions and validation |
| `src/forecast_mcp/services.py` | Shared rules: confirmation, scenarios, adoption, activity log and limits |
| `src/forecast_mcp/jobs.py` | Run queue and background worker |
| `src/forecast_mcp/mcp_server.py` | MCP tools and the chat panel resource |
| `src/forecast_mcp/web.py` | Website pages and JSON API |
| `src/forecast_mcp/auth.py`, `accounts.py` | Sign-in and per-user workspaces |
| `src/forecast_mcp/static/` | Website, chat panel and shared chart code |
| `src/forecast_mcp/migrations/` | Database migrations (Alembic) |

## Testing

```bash
uv run pytest
```

The tests cover the forecasting and optimization engines, constraint validation, confirmation and adoption rules, workspace isolation, sign-in, database migrations, security headers and rate limits, the MCP protocol and the website API.

## Limitations

- The ensemble understates uncertainty where data is scarce. A separate limited-history indicator flags entrances with few days of data.
- Waiting times come from an hourly approximation. They are suitable for comparing plans, not for predicting exact queue lengths.
- Every workspace uses the demo venue. Importing a venue's own data is not yet supported.

## Citation

To cite this software, use the metadata in [CITATION.cff](CITATION.cff).

## License

Released under the [MIT License](LICENSE). The bundled fonts are licensed under the SIL Open Font License.
