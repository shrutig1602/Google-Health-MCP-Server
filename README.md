# Google Health MCP Server

> A production-ready MCP server that exposes the entire Google Health API as tools for AI agents — with automatic token refresh, CrewAI integration, and zero manual auth management.

**Model Context Protocol · Streamable HTTP Transport**

| MCP Tools | Health Scopes | Transport |
|-----------|---------------|-----------|-----------|
| 40+ | 4 | HTTP |

---

## Overview

This project bridges the **Google Health API** and any AI agent framework (CrewAI, LangChain) via the **Model Context Protocol**. Your agent calls tools like `get_steps_daily_rollup` or `get_sleep` — the server handles OAuth, token refresh, and HTTP transparently.

**Stack:** Python 3.11+ · FastMCP · CrewAI · LangChain · Google Health API

---

## Architecture

```
AI Agent (CrewAI)
      │
      ▼
MCP Client (MCPServerAdapter)
      │
      ▼
MCP Server (FastMCP · :8000/mcp)
      │
      ▼
Google Health API (health.googleapis.com/v4)
```

### Key Features

- **Proactive Auto-Refresh** — Tokens are refreshed 120 seconds before expiry. No tool call ever hits a live 401. Thread-safe `TOKEN_STATE` with `threading.Lock` ensures concurrent safety.
- **Streamable HTTP / SSE** — Uses MCP's Streamable HTTP transport at `/mcp`. A separate `/health/token-status` endpoint lets clients verify token validity before starting a session.
- **Full OAuth Flow Built-in** — Server exposes `/auth/url` and `/auth/exchange` HTTP endpoints. Client opens browser, pastes redirect URL — tokens are stored server-side automatically.
- **4 Google Health Scopes** — Activity & fitness, health metrics & measurements, sleep, and nutrition — requested with `access_type=offline` for long-lived refresh tokens.
- **Daily Rollups & Custom Ranges** — Every data type supports `dailyRollUp` (civil-day aggregates) and `rollUp` (arbitrary time ranges) in addition to raw data point listing.
- **Generic Passthrough Tools** — `list_data_points`, `daily_rollup`, and `rollup` tools accept any data type string — future-proof against new Google Health endpoints.

---

## Token Lifecycle

| State | Condition | Behaviour |
|-------|-----------|-----------|
| ✅ Fresh | expiry > 120s away | Token returned immediately — zero API calls, fast path |
| ⚠️ Buffer Window | 0s < expiry ≤ 120s | Proactive refresh triggered inline before returning the token |
| ❌ Expired | expiry ≤ 0s | Refresh grant executed using stored `refresh_token` |

```python
def _token() -> str:
    secs = _seconds_until_expiry()

    if secs is not None and secs > REFRESH_BUFFER_SECS:
        # ✓ Fresh — fast path, no API call needed
        return TOKEN_STATE["access_token"]

    if secs is not None and secs <= 0:
        _do_refresh()           # Fully expired → must refresh
    elif secs is None:
        if has_refresh_token():
            _do_refresh()       # Unknown expiry + refresh token → refresh
    else:
        _do_refresh()           # Within 120s buffer → proactive refresh

    return TOKEN_STATE["access_token"]
```

---

## OAuth Flow

| Step | Action |
|------|--------|
| 1 | Client checks `GET /health/token-status` → `{ valid, expires_in, has_refresh_token }` |
| 2 | If invalid → `GET /auth/url` → returns Google OAuth authorization URL |
| 3 | Browser opens, user approves 4 health scopes on Google's consent screen |
| 4 | User pastes the full redirect URL into the terminal |
| 5 | `POST /auth/exchange` extracts the code, calls Google Token URL, stores tokens in `TOKEN_STATE` |
| 6 | MCP session starts — agent calls health tools transparently, auto-refresh runs as needed |

```python
def run_oauth_flow():
    # 1. Get the auth URL from the server
    resp = requests.get(f"{BASE}/auth/url", timeout=5)
    url  = resp.json()["url"]

    # 2. Open browser for user consent
    webbrowser.open(url)
    redirect_url = input("Paste the full redirect URL here: ")

    # 3. Exchange code for tokens (server handles extraction)
    requests.post(
        f"{BASE}/auth/exchange",
        json={"authorization_code": redirect_url},
    )
    # Tokens stored in TOKEN_STATE — all tools work now ✓
```

---

## MCP Tools Reference

### 🔐 OAuth

| Tool | Description |
|------|-------------|
| `get_authorization_url` | Generate Google OAuth 2.0 URL with all health scopes |
| `exchange_code_for_tokens` | Exchange auth code or redirect URL for access + refresh tokens |
| `refresh_access_token` | Manually trigger refresh grant using stored `refresh_token` |
| `set_tokens` | Inject externally-obtained tokens into server `TOKEN_STATE` |

### 🏃 Activity & Fitness

| Tool | Description |
|------|-------------|
| `get_exercises` | List exercise sessions, optionally filtered by start time |
| `get_single_exercise` | Retrieve one exercise session by data point ID |
| `export_exercise_tcx` | Export exercise session as TCX (Training Center XML) format |
| `get_steps` | List intraday step count data points |
| `get_steps_daily_rollup` | Daily step totals per civil day for a date range |
| `get_distance` | List distance data points (values in millimeters) |
| `get_distance_daily_rollup` | Daily distance aggregates |
| `get_calories` | List total calorie expenditure data points |
| `get_calories_daily_rollup` | Daily calorie expenditure totals per civil day |
| `get_active_minutes` | List active minutes data points |
| `get_active_zone_minutes` | List active zone minutes data points |
| `get_active_zone_minutes_daily_rollup` | Daily active zone minutes aggregated per civil day |
| `get_activity_level` | Sedentary / light / moderate / vigorous intervals |
| `get_sedentary_periods` | List sedentary period data points |
| `get_floors_daily_rollup` | Daily floors climbed — defaults to last 7 days |
| `get_altitude` | List altitude data points |
| `get_altitude_daily_rollup` | Daily altitude data aggregated per civil day |
| `get_daily_vo2_max` | Daily VO₂ max estimates |
| `get_calories_in_heart_rate_zone` | Calories burned within each heart rate zone |
| `get_time_in_heart_rate_zone` | Time spent in each heart rate training zone |

### 💓 Health Metrics & Measurements

| Tool | Description |
|------|-------------|
| `get_heart_rate` | Intraday heart rate samples (beats per minute) |
| `get_heart_rate_daily_rollup` | Daily heart rate statistics — min, max, avg per day |
| `get_daily_resting_heart_rate` | Daily resting heart rate values |
| `get_heart_rate_variability` | Intraday HRV samples |
| `get_daily_hrv` | Daily heart rate variability summaries |
| `get_oxygen_saturation` | Blood oxygen (SpO₂) samples |
| `get_daily_oxygen_saturation` | Daily oxygen saturation summaries |
| `get_daily_respiratory_rate` | Daily respiratory rate (breaths per minute) |
| `get_respiratory_rate_sleep_summary` | Respiratory rate summaries derived from sleep sessions |
| `get_weight` | Body weight measurements |
| `get_weight_daily_rollup` | Daily weight statistics aggregated per civil day |
| `get_single_weight` | Single weight measurement by data point ID |
| `get_body_fat` | Body fat percentage measurements |
| `get_body_fat_daily_rollup` | Daily body fat statistics aggregated per civil day |
| `get_height` | Body height measurements |
| `get_daily_heart_rate_zones` | Daily heart rate zone boundary data |
| `get_daily_sleep_temperature_derivations` | Daily sleep temperature derivation data points |

### 😴 Sleep

| Tool | Description |
|------|-------------|
| `get_sleep` | List all sleep session data points |
| `get_single_sleep` | Retrieve one sleep session by data point ID |

### 💧 Nutrition

| Tool | Description |
|------|-------------|
| `get_hydration` | Hydration log entries (fluid intake in milliliters) |
| `get_hydration_daily_rollup` | Daily hydration totals aggregated per civil day |
| `get_single_hydration` | Single hydration log entry by data point ID |

### 🔧 Generic / Advanced

| Tool | Description |
|------|-------------|
| `list_data_points` | Any data type with optional AIP-160 filter expression |
| `daily_rollup` | Daily aggregation for any data type over a date range |
| `rollup` | Aggregate over arbitrary ISO 8601 time range |
| `reconcile_data_points` | Merge data from multiple sources into one consistent stream |
| `get_user_identity` | Authenticated user's unique Google Health identity |
| `get_user_profile` | Basic Google profile info via userinfo endpoint |

---

## Setup & Installation

### 1. Install dependencies

```bash
# Install directly
pip install mcp requests crewai crewai-tools langchain-groq python-dotenv uvicorn starlette

# Or use requirements.txt
pip install -r requirements.txt
```

### 2. Configure environment variables

```env
GOOGLE_HEALTH_CLIENT_ID=your-client-id
GOOGLE_HEALTH_CLIENT_SECRET=your-secret
GOOGLE_HEALTH_REDIRECT_URI=http://localhost

AZURE_API_KEY=your-azure-key
AZURE_API_BASE=https://your-endpoint.openai.azure.com
AZURE_API_VERSION=2023-05-15
```

### 3. Start the MCP server

```bash
python server.py
# → Starting on http://127.0.0.1:8000
# → MCP endpoint: http://127.0.0.1:8000/mcp
# → Token status: GET /health/token-status
```

### 4. Run the CrewAI client

```bash
python client.py
# If no token: browser opens automatically
# Paste redirect URL → tokens stored
# Chat starts immediately
```

Example queries:
```
You: Show my step counts for this week
You: What was my average resting heart rate in May?
You: Summarise my sleep from last 7 days
```

### Google Cloud Console Setup

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Google Health API** (`health.googleapis.com`)
3. Create OAuth 2.0 credentials → Desktop App type
4. Add `https://www.google.com` to Authorized Redirect URIs
5. Add test users under OAuth consent screen if the app is in testing mode

---

## HTTP Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/mcp` | MCP Streamable HTTP transport |
| `GET` | `/health/token-status` | Token validity check — used by client during handshake |
| `GET` | `/auth/url` | Returns Google OAuth authorization URL |
| `POST` | `/auth/exchange` | Exchanges authorization code for tokens |

---

## Links

- [Google Health API](https://health.googleapis.com)
- [Model Context Protocol Docs](https://modelcontextprotocol.io)
- [CrewAI Docs](https://docs.crewai.com)
