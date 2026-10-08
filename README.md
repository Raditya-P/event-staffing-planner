# forecast-mcp

Audience forecasting and gate staffing for event planners, with two front doors on one server:

- **Dashboard** (`/`): planners view the forecast and its uncertainty, try what-if scenarios, pick a staffing plan and promote it.
- **MCP endpoint** (`/mcp`): Claude can show the same charts in the chat (MCP Apps panel), record planners' notes as constraints, and explain the results.

Both doors read and write one database, so a change made in either shows up in the other.

This is a prototype: the arrival history is **synthetic**, and the forecast model and optimizer are stand-ins behind stable contracts.

## Run it locally

```bash
uv sync
```

```bash
uv run forecast-mcp
```

Then open http://127.0.0.1:8000/. On first start the server creates the tables, generates demo data (one theme park with three gates and two event days) and trains the forecast model (about 10 seconds).

To try the chat panel without Claude, open http://127.0.0.1:8000/dev/panel. That page stands in for Claude: it calls the real tools and renders the same panel Claude would.

## Use Neon as the database

1. In the Neon console, create a project and copy its connection string (Connect → connection string).
2. Copy `.env.example` to `.env` and paste the string as `DATABASE_URL=...`. `.env` is git-ignored, so the password stays out of version control.
3. Restart the server. It prints `Database: Postgres (DATABASE_URL)`, applies the migrations and loads the demo data on first start.

Without `DATABASE_URL`, the server uses a local SQLite file (`local.db`). The tests always use a throwaway SQLite file.

The schema is managed by Alembic migrations in `src/forecast_mcp/migrations`, which the server applies at start-up. After changing a table in `db.py`, generate a migration:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

A test fails if `db.py` and the migrations disagree.

## Connect Claude

Claude reaches MCP servers over the internet, so the server needs a public HTTPS address: a deployment (below) or, for a quick test, a tunnel such as Cloudflare Tunnel or ngrok.

1. Set `PUBLIC_BASE_URL=https://your-host` in `.env` and restart.
2. In Claude, open Settings → Connectors and add a custom connector with the URL `https://your-host/mcp`. Custom connectors need a paid Claude plan.

With `AUTH_MODE=none`, anyone who has the URL can read and change the data. Use that only for a short test with synthetic data.

## Sign-in for a public server

With `AUTH_MODE=oidc`, people sign in through an OpenID Connect provider, and each new user gets a private workspace with a fresh copy of the demo.

- **Claude (`/mcp`):** the server answers unauthenticated requests with `401` and a pointer to `/.well-known/oauth-protected-resource/mcp`, which names your provider. Claude registers itself with the provider (Dynamic Client Registration or a Client ID Metadata Document), signs the user in, and sends an access token. The server checks the token's signature, issuer, expiry and audience.
- **Dashboard:** a normal "Sign in" button using the authorization-code flow with PKCE. The result is a signed session cookie.

What the provider must support:
- Dynamic Client Registration or Client ID Metadata Documents, S256 PKCE, and JWT access tokens published with a JWKS.
- The redirect URI `https://claude.ai/api/mcp/auth_callback` for Claude, plus `<PUBLIC_BASE_URL>/auth/callback` for the dashboard app.

WorkOS AuthKit, Descope, Clerk and Auth0 advertise MCP support. Check the current details before choosing.

Settings: `AUTH_MODE=oidc`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` (the dashboard app), `MCP_AUDIENCE` (usually `<PUBLIC_BASE_URL>/mcp`), `SESSION_SECRET`. See `.env.example`.

## Deploy

The server is one always-on process: web server, MCP endpoint and a background worker for forecast and optimization runs. The `Dockerfile` builds it for any container host. Pick a region close to your Neon database (yours is in Singapore, `ap-southeast-1`). Set the environment variables from `.env.example` in the host's dashboard, with `ENV=production`.

### Deploy on Azure Container Apps (Azure for Students, no card)

`deploy/azure.ps1` sets up a resource group, a Basic container registry (about USD 5 a month from the student credit), and a Container Apps environment and app in Malaysia West, the closest region to Neon's Singapore that Azure for Students allows. The app scales to zero when idle, which keeps it inside the monthly free allowance. Secrets go from `.env` straight into Azure and GitHub secrets.

Student subscriptions don't let Azure build images, so GitHub Actions builds the image after the tests pass and uploads it to the registry. You don't need Docker locally.

1. Log in to Azure:

   ```bash
   az login
   ```

2. Run this once. It creates the registry, stores its credentials as GitHub secrets, and starts a build:

   ```bash
   powershell -ExecutionPolicy Bypass -File .\deploy\azure.ps1 -ConnectGitHub
   ```

3. When the GitHub workflow has passed, deploy the image of your latest pushed commit. Repeat this after each push:

   ```bash
   powershell -ExecutionPolicy Bypass -File .\deploy\azure.ps1
   ```

4. Once WorkOS is set up, add `-EnableSignIn`. Use `-PruneImages` now and then to keep only the 5 newest images.

### Deploy on Render (free) with WorkOS sign-in

`render.yaml` describes the whole service: free plan, Singapore region (next to Neon), the light engine profile and sign-in switched on. GitHub Actions also builds the Docker image and smoke-tests it on every push. Render deploys a commit only after those checks pass.

**1. WorkOS (sign-in): use the free staging environment.**
1. Create a WorkOS account and stay in the **Staging** environment. Production asks for billing details.
2. Under AuthKit, note your AuthKit domain, e.g. `https://something.authkit.app`. That is `OIDC_ISSUER`.
3. Enable **Dynamic Client Registration**. Claude registers itself through it.
4. Add the redirect URI `https://claude.ai/api/mcp/auth_callback`.
5. Create an **OAuth application** for the dashboard: a confidential app with redirect URI `https://<your-service>.onrender.com/auth/callback`. Its client ID and secret are `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`.

**2. Render (hosting).**
1. In the Render dashboard, choose New → Blueprint and pick this repository. Give Render access to the private repo when GitHub asks.
2. Choose the free plan if asked.
3. Fill in the secrets it asks for: `DATABASE_URL` (the Neon string), the three `OIDC_*` values, and `CONTACT_EMAIL`. `SESSION_SECRET` is generated for you.
4. Wait for the first deploy, then open `https://<your-service>.onrender.com`. Sign in, and the demo appears in your own workspace.
5. If the service name differs from the redirect URI you gave WorkOS, update it in WorkOS.

**3. Claude.** Add a custom connector with `https://<your-service>.onrender.com/mcp` and sign in when asked.

The free service sleeps after 15 minutes without traffic. The first request after that waits about a minute while it starts and retrains the model, so open the dashboard before a session to wake it.

### Running it for free

- **Database:** Neon's free plan gives 0.5 GB of storage and a monthly compute allowance per project. The server is built to stay inside it:
  - The idle worker checks the database only every 10 minutes, so Neon can pause.
  - Old runs are deleted, keeping the two latest per scenario.
  - The dashboard stops polling while its tab is hidden.
  - Each user's demo copy takes roughly 1 MB.
- **Sign-in:** WorkOS AuthKit is free up to a million monthly users and supports the registration Claude uses. Enable Dynamic Client Registration in its dashboard. Leave `MCP_AUDIENCE` empty, since its issuer is dedicated to this app. If the dashboard login rejects PKCE, set `OIDC_PKCE=0`.
- **Host:** the server needs about 250 MB of memory.
  - On hosts with a fraction of a CPU, set `ENGINE_PROFILE=light`. Expect slower runs, plus a delay while a sleeping service wakes and retrains the model.
  - For short studies, running on your laptop behind a free tunnel works too.

Several instances can share one database. Runs are claimed with `FOR UPDATE SKIP LOCKED`, and migrations take a lock. Rate limits are counted per instance.

At start-up the server prints a warning for every unsafe production setting (no sign-in, `/dev` pages on, a weak session secret). With `AUTH_MODE=oidc` and missing settings, it refuses to start.

## How a planner's note flows

1. The planner tells Claude, for example: "roadworks close the north road from 5 to 8 pm".
2. Claude calls `propose_constraint` with the planner's exact words, a structured constraint (`gate_closed`, north, 17:00–20:00) and any assumptions it made. The constraint is **pending**.
3. The panel shows the reading with Confirm and Reject buttons. Those buttons call app-only tools that the host hides from Claude, so only the planner can confirm.
4. On confirmation the constraint goes into a what-if scenario. The forecast and the NSGA-II optimizer rerun automatically, and the panel shows the cost-versus-waiting trade-off.
5. The planner picks a plan and promotes the what-if. Only then does the official plan change. Every step is in the audit log, with who did it, when, via which door and from which note.

## Code map

| Path | What it does |
|---|---|
| `src/forecast_mcp/constraints.py` | The one shared constraint definition and its validation |
| `src/forecast_mcp/engines/synthetic.py` | Synthetic history with controlled volatility and a thin-history gate |
| `src/forecast_mcp/engines/forecast.py` | Bootstrapped ensemble of quantile models; out-of-bag backtest |
| `src/forecast_mcp/engines/contract.py` | The forecast contract (p10/p50/p90, uncertainty split, totals, notes) and Claude's text summary |
| `src/forecast_mcp/engines/optimizer.py` | NSGA-II staffing (pymoo), queue model, robustness on sampled days |
| `src/forecast_mcp/services.py` | The rules both doors share: pending, confirm, undo, promote, audit, limits |
| `src/forecast_mcp/jobs.py` | The run queue and its worker: forecast, then optimize |
| `src/forecast_mcp/auth.py`, `accounts.py` | Sign-in (token checks, dashboard login) and one workspace per user |
| `src/forecast_mcp/middleware.py` | Rate limits and security headers |
| `src/forecast_mcp/migrations/` | Database migrations (Alembic) |
| `src/forecast_mcp/mcp_server.py` | MCP tools and the `ui://` panel resource |
| `src/forecast_mcp/web.py` | Dashboard JSON API, pages, and mounting of `/mcp` |
| `src/forecast_mcp/static/` | Dashboard, chat panel, shared chart code |

## Tests

```bash
uv run pytest
```

The tests cover engine behaviour, constraint validation, the confirm and promote rules, workspace isolation, sign-in (with a locally generated signing key), migrations matching the models, rate limits and headers, the MCP protocol (in-process and over HTTP) and the dashboard API.

## Public-release status

Done:
- Sign-in (OIDC) for Claude and the dashboard, with a private workspace per user.
- Workspace checks on every lookup, plus limits on what-ifs, pending notes, text sizes and request rate.
- Migrations, and a multi-instance-safe run queue.
- Docker image, security headers, and a landing page and privacy page.
- Tests run on GitHub Actions.

Still needed before inviting the public:
- **Choices only you can make:** the identity provider, the host, and a license.
- **Approvals:** confirm with your supervisor, university and industry partners that the code and any data may be public. If you collect planners' use of the tool for your research, you need ethics approval and an informed-consent step.
- **Real data:** a way for users to bring their own venue and history (CSV import). Today every workspace holds the demo park.
- **Operations:** error monitoring, database backups (Neon has point-in-time restore), and someone reachable at `CONTACT_EMAIL`.

## Known limitations of the stand-ins

- **Underestimated uncertainty.** Bootstrap ensembles tend to understate model uncertainty where data is scarce or conditions are new. East Gate's range is attributed mostly to day-to-day variation even though it has only 6 days of history. The "thin history" flag is therefore a separate, explicit signal.
- **Simplified waiting model.** Waiting times use a fluid backlog plus Sakasegawa's M/M/c approximation per hour. That is fine for comparing plans, not for predicting exact queue lengths.
- **Polling, not push.** Views learn about changes by polling a revision number every 2 seconds. Runs execute one at a time.
