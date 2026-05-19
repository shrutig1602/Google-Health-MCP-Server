"""
Google Health MCP Server — HTTP Transport (Streamable HTTP )
=================================================================
Exposes every Google Health API helper as an MCP tool over HTTP.

Start:
    pip install mcp requests
    python server.py

MCP client connects to:
    http://localhost:8000/mcp  (Streamable HTTP / SSE)

Token-status endpoint (used by client during handshake):
    GET http://localhost:8000/health/token-status

Environment variables (or edit CONFIG below):
    GOOGLE_HEALTH_CLIENT_ID
    GOOGLE_HEALTH_CLIENT_SECRET
    GOOGLE_HEALTH_REDIRECT_URI
    GOOGLE_HEALTH_ACCESS_TOKEN   ← set after OAuth
    GOOGLE_HEALTH_REFRESH_TOKEN  ← set after OAuth

Token lifecycle
───────────────
• Google access tokens live ~3600 seconds (1 hour).
• TOKEN_STATE tracks expiry_at (epoch float).
• _token() proactively auto-refreshes when ≤ REFRESH_BUFFER_SECS remain
  so no tool call ever hits a live 401.
• GET /health/token-status lets the client verify token validity before
  (and during) every MCP session — it returns JSON with `valid`, `expires_in`,
  `has_refresh_token`, and `expires_at` (ISO-8601 UTC).
"""

import os
import time
import threading
import json
from datetime import date, timedelta, datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from mcp.server.fastmcp import FastMCP

# ──────────────────────────────────────────────
# CONFIG  — override via env vars or edit here
# ──────────────────────────────────────────────
CONFIG = {
    "client_id":     os.getenv("GOOGLE_HEALTH_CLIENT_ID",""),
    "client_secret": os.getenv("GOOGLE_HEALTH_CLIENT_SECRET", ""),
    "redirect_uri":  os.getenv("GOOGLE_HEALTH_REDIRECT_URI",  ""),
}

AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE_URL  = "https://health.googleapis.com/v4"

# SCOPES = [
#     "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
#     "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
#     "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
#     "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
# ]
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
]

# How many seconds before actual expiry we proactively refresh.
REFRESH_BUFFER_SECS = 120  # 2 minutes

# ──────────────────────────────────────────────
# TOKEN STATE  — single source of truth
# ──────────────────────────────────────────────
TOKEN_STATE: dict = {
    "access_token":  os.getenv("GOOGLE_HEALTH_ACCESS_TOKEN",  ""),
    "refresh_token": os.getenv("GOOGLE_HEALTH_REFRESH_TOKEN", ""),
    # epoch float; None = unknown expiry (treat as expired)
    "expiry_at":     None,
}
_token_lock = threading.Lock()


def _store_token_response(resp_json: dict) -> None:
    """Persist a token response dict into TOKEN_STATE (thread-safe)."""
    with _token_lock:
        TOKEN_STATE["access_token"]  = resp_json.get("access_token", "")
        if resp_json.get("refresh_token"):          # refresh_token absent on refresh grants
            TOKEN_STATE["refresh_token"] = resp_json["refresh_token"]
        expires_in = resp_json.get("expires_in", 3600)
        TOKEN_STATE["expiry_at"] = time.time() + expires_in


def _seconds_until_expiry() -> float | None:
    """Return seconds remaining on the current token, or None if unknown."""
    with _token_lock:
        if TOKEN_STATE["expiry_at"] is None:
            return None
        return TOKEN_STATE["expiry_at"] - time.time()


def _do_refresh() -> None:
    """Perform a refresh-grant and update TOKEN_STATE. Raises on failure."""
    with _token_lock:
        rt = TOKEN_STATE["refresh_token"]
    if not rt:
        raise ValueError("No refresh_token available — full OAuth required.")
    payload = {
        "client_id":     CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"],
        "refresh_token": rt,
        "grant_type":    "refresh_token",
    }
    resp = requests.post(TOKEN_URL, data=payload, timeout=10)
    resp.raise_for_status()
    _store_token_response(resp.json())


def _token() -> str:
    """
    Return a valid access token.

    Logic:
      1. If expiry is known and > REFRESH_BUFFER_SECS away → return as-is.
      2. If expiry is unknown or ≤ REFRESH_BUFFER_SECS → proactively refresh.
      3. If no token at all → raise with clear instructions.
    """
    secs = _seconds_until_expiry()

    if secs is not None and secs > REFRESH_BUFFER_SECS:
        # Token is fresh — fast path.
        with _token_lock:
            return TOKEN_STATE["access_token"]

    if secs is not None and secs <= 0:
        # Fully expired → must refresh.
        _do_refresh()
    elif secs is None:
        # Expiry unknown: refresh if we have a refresh token, else trust the token.
        with _token_lock:
            has_rt = bool(TOKEN_STATE["refresh_token"])
        if has_rt:
            _do_refresh()
    else:
        # Within buffer window → proactive refresh.
        _do_refresh()

    with _token_lock:
        tok = TOKEN_STATE["access_token"]
    if not tok:
        raise ValueError(
            "No access_token set. Use the 'get_authorization_url' tool to start "
            "OAuth, then 'exchange_code_for_tokens' to store tokens."
        )
    return tok


# ──────────────────────────────────────────────
# MCP server instance
# ──────────────────────────────────────────────
mcp = FastMCP("google-health", host="127.0.0.1", port=8000)


# ──────────────────────────────────────────────
# /health/token-status  — plain HTTP endpoint
# Mounted on the same Starlette/uvicorn app that FastMCP uses.
# The client calls this during handshake and periodically thereafter.
# ──────────────────────────────────────────────
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse


async def token_status(request: Request) -> JSONResponse:
    """
    GET /health/token-status

    Returns:
        {
          "valid":             bool,   # true if token exists and not expired
          "expires_in":        int,    # seconds remaining (negative = expired)
          "expires_at":        str,    # ISO-8601 UTC, or null
          "has_access_token":  bool,
          "has_refresh_token": bool,
          "server_time":       str     # ISO-8601 UTC now
        }
    """
    with _token_lock:
        at  = TOKEN_STATE["access_token"]
        rt  = TOKEN_STATE["refresh_token"]
        exp = TOKEN_STATE["expiry_at"]

    now = time.time()
    if exp is not None:
        secs_left   = int(exp - now)
        valid       = bool(at) and secs_left > 0
        expires_at  = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        expires_in  = secs_left
    else:
        # expiry unknown — report as valid if token string is present
        valid      = bool(at)
        expires_at = None
        expires_in = None

    return JSONResponse({
        "valid":             valid,
        "expires_in":        expires_in,
        "expires_at":        expires_at,
        "has_access_token":  bool(at),
        "has_refresh_token": bool(rt),
        "server_time":       datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    })


def _get(path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    resp = requests.get(f"{BASE_URL}/{path}", headers=headers, params=params or {})
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    resp = requests.post(f"{BASE_URL}/{path}", headers=headers, json=body or {})
    resp.raise_for_status()
    return resp.json()


def _list(data_type: str, extra_params: dict = None) -> dict:
    return _get(f"users/me/dataTypes/{data_type}/dataPoints", extra_params)


def _daily_rollup(data_type: str, start_date: str, end_date: str) -> dict:
    body = {"aggregationPeriod": {"startDate": start_date, "endDate": end_date}}
    return _post(f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", body)


def _rollup(data_type: str, start_time: str, end_time: str) -> dict:
    body = {"aggregationPeriod": {"startTime": start_time, "endTime": end_time}}
    return _post(f"users/me/dataTypes/{data_type}/dataPoints:rollUp", body)


# ──────────────────────────────────────────────
# OAUTH TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def get_authorization_url() -> str:
    """
    Generate the Google OAuth 2.0 authorization URL.
    Direct the user to this URL in a browser. After consent they are
    redirected to the REDIRECT_URI with a `code` query parameter.
    """
    params = {
        "client_id":     CONFIG["client_id"],
        "redirect_uri":  CONFIG["redirect_uri"],
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


@mcp.tool()
def exchange_code_for_tokens(authorization_code: str) -> dict:
    """
    Exchange an authorization code (or the full redirect URL) for
    access_token + refresh_token. Stores tokens in the server CONFIG
    so subsequent tool calls work immediately.

    Args:
        authorization_code: The `code` value from the redirect URL,
                            or the entire redirect URL pasted from the browser.
    """
    # Accept either the raw code or the full redirect URL
    if "code=" in authorization_code:
        parsed = urlparse(authorization_code)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise ValueError("Could not extract 'code' from the provided URL.")
    else:
        code = authorization_code

    payload = {
        "code":          code,
        "client_id":     CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"],
        "redirect_uri":  CONFIG["redirect_uri"],
        "grant_type":    "authorization_code",
    }
    resp = requests.post(TOKEN_URL, data=payload)
    resp.raise_for_status()
    tokens = resp.json()

    _store_token_response(tokens)  # ✅ writes to TOKEN_STATE

    return {
        "access_token":  TOKEN_STATE["access_token"][:40] + "…",
        "refresh_token": (TOKEN_STATE["refresh_token"][:40] + "…") if TOKEN_STATE["refresh_token"] else "N/A",
        "expires_in":    tokens.get("expires_in"),
        "message":       "Tokens stored. You can now call any health data tool.",
    }


@mcp.tool()
def refresh_access_token() -> dict:
    """
    Use the stored refresh_token to obtain a new access_token without
    re-running the full OAuth flow. Automatically updates the server CONFIG.
    """
    with _token_lock:
        rt = TOKEN_STATE["refresh_token"]  # ✅ read from TOKEN_STATE
    if not rt:
        raise ValueError("No refresh_token stored. Complete OAuth first.")
    _do_refresh()  # ✅ already writes to TOKEN_STATE correctly
    with _token_lock:
        at = TOKEN_STATE["access_token"]
        exp = TOKEN_STATE["expiry_at"]
    return {
        "access_token": at[:40] + "…",
        "expires_in":   int(exp - time.time()) if exp else None,
        "message":      "access_token refreshed successfully.",
    }


@mcp.tool()
def set_tokens(access_token: str, refresh_token: str = "") -> dict:
    """
    Manually inject tokens (e.g. obtained externally) into the server
    so tool calls work without re-running OAuth.

    Args:
        access_token:  A valid Google OAuth access token.
        refresh_token: (Optional) The associated refresh token.
    """
    with _token_lock:
        TOKEN_STATE["access_token"]  = access_token
        TOKEN_STATE["refresh_token"] = refresh_token
        TOKEN_STATE["expiry_at"]     = None  # unknown expiry
    return {"message": "Tokens set successfully."}


# ──────────────────────────────────────────────
# USER TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def get_user_identity() -> dict:
    """Return the authenticated user's unique Google Health identity."""
    return _get("users/me/identity")


@mcp.tool()
def get_user_profile() -> dict:
    """Return the authenticated user's basic Google profile info."""
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()


# @mcp.tool()
# def get_user_settings() -> dict:
#     """Return the user's settings: locale, units, clock display, etc."""
#     return _get("users/me/settings")


# ──────────────────────────────────────────────
# ACTIVITY & FITNESS TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def get_exercises(start_civil_time: str = "") -> dict:
    """
    List exercise sessions.

    Args:
        start_civil_time: Optional ISO 8601 datetime string to filter sessions
                          on or after this time, e.g. "2026-01-01T00:00:00".
    """
    params = {}
    if start_civil_time:
        params["filter"] = f'exercise.interval.civil_start_time >= "{start_civil_time}"'
    return _list("exercise", params)


@mcp.tool()
def get_single_exercise(data_point_id: str) -> dict:
    """
    Retrieve a single exercise session by its data point ID.

    Args:
        data_point_id: The ID of the exercise data point.
    """
    return _get(f"users/me/dataTypes/exercise/dataPoints/{data_point_id}")


@mcp.tool()
def export_exercise_tcx(data_point_id: str) -> str:
    """
    Export a single exercise session in TCX (Training Center XML) format.

    Args:
        data_point_id: The ID of the exercise data point.
    """
    name = f"users/me/dataTypes/exercise/dataPoints/{data_point_id}"
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept":        "application/xml",
    }
    resp = requests.get(f"{BASE_URL}/{name}:exportExerciseTcx", headers=headers)
    resp.raise_for_status()
    return resp.text


@mcp.tool()
def get_steps() -> dict:
    """List intraday step count data points."""
    return _list("steps")


@mcp.tool()
def get_steps_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily step totals aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("steps", start_date, end_date)


@mcp.tool()
def get_distance() -> dict:
    """List distance data points (values in millimeters)."""
    return _list("distance")


@mcp.tool()
def get_distance_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily distance totals aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("distance", start_date, end_date)


@mcp.tool()
def get_calories() -> dict:
    """List total calorie expenditure data points."""
    return _list("total-calories")


@mcp.tool()
def get_calories_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily calorie totals aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("total-calories", start_date, end_date)


@mcp.tool()
def get_active_minutes() -> dict:
    """List active minutes data points."""
    return _list("active-minutes")


@mcp.tool()
def get_active_zone_minutes() -> dict:
    """List active zone minutes data points."""
    return _list("active-zone-minutes")


@mcp.tool()
def get_active_zone_minutes_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily active zone minutes aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("active-zone-minutes", start_date, end_date)


@mcp.tool()
def get_activity_level() -> dict:
    """List activity level interval data points (sedentary/light/moderate/vigorous)."""
    return _list("activity-level")


@mcp.tool()
def get_sedentary_periods() -> dict:
    """List sedentary period data points."""
    return _list("sedentary-period")


@mcp.tool()
def get_floors_daily_rollup(start_date: str = "", end_date: str = "") -> dict:
    """
    Daily floors-climbed totals. Defaults to the last 7 days if no dates given.

    Args:
        start_date: Start date in YYYY-MM-DD format (optional).
        end_date:   End date in YYYY-MM-DD format (optional).
    """
    today     = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    return _daily_rollup("floors", start_date or week_ago, end_date or today)


@mcp.tool()
def get_altitude() -> dict:
    """List altitude data points."""
    return _list("altitude")


@mcp.tool()
def get_altitude_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily altitude data aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("altitude", start_date, end_date)


@mcp.tool()
def get_daily_vo2_max() -> dict:
    """List daily VO2 max estimates."""
    return _list("daily-vo2-max")


@mcp.tool()
def get_calories_in_heart_rate_zone() -> dict:
    """List calories burned within each heart rate zone."""
    return _list("calories-in-heart-rate-zone")


@mcp.tool()
def get_time_in_heart_rate_zone() -> dict:
    """List time spent in each heart rate zone."""
    return _list("time-in-heart-rate-zone")


# ──────────────────────────────────────────────
# HEALTH METRICS & MEASUREMENTS TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def get_heart_rate() -> dict:
    """List intraday heart rate samples (beats per minute)."""
    return _list("heart-rate")


@mcp.tool()
def get_heart_rate_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily heart rate statistics aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("heart-rate", start_date, end_date)


@mcp.tool()
def get_daily_resting_heart_rate() -> dict:
    """List daily resting heart rate values."""
    return _list("daily-resting-heart-rate")


@mcp.tool()
def get_heart_rate_variability() -> dict:
    """List intraday heart rate variability (HRV) samples."""
    return _list("heart-rate-variability")


@mcp.tool()
def get_daily_hrv() -> dict:
    """List daily heart rate variability (HRV) summaries."""
    return _list("daily-heart-rate-variability")


@mcp.tool()
def get_oxygen_saturation() -> dict:
    """List blood oxygen saturation (SpO2) samples."""
    return _list("oxygen-saturation")


@mcp.tool()
def get_daily_oxygen_saturation() -> dict:
    """List daily oxygen saturation summaries."""
    return _list("daily-oxygen-saturation")


@mcp.tool()
def get_daily_respiratory_rate() -> dict:
    """List daily respiratory rate values (breaths per minute)."""
    return _list("daily-respiratory-rate")


@mcp.tool()
def get_respiratory_rate_sleep_summary() -> dict:
    """List respiratory rate summaries derived from sleep sessions."""
    return _list("respiratory-rate-sleep-summary")


@mcp.tool()
def get_weight() -> dict:
    """List body weight measurements."""
    return _list("weight")

@mcp.tool()
def get_height() -> dict:
    """List body Height measurements."""
    return _list("height")


@mcp.tool()
def get_weight_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily weight statistics aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("weight", start_date, end_date)


@mcp.tool()
def get_single_weight(data_point_id: str) -> dict:
    """
    Retrieve a single weight measurement by its data point ID.

    Args:
        data_point_id: The ID of the weight data point.
    """
    return _get(f"users/me/dataTypes/weight/dataPoints/{data_point_id}")


@mcp.tool()
def get_body_fat() -> dict:
    """List body fat percentage measurements."""
    return _list("body-fat")


@mcp.tool()
def get_body_fat_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily body fat statistics aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("body-fat", start_date, end_date)


@mcp.tool()
def get_single_body_fat(data_point_id: str) -> dict:
    """
    Retrieve a single body fat measurement by its data point ID.

    Args:
        data_point_id: The ID of the body fat data point.
    """
    return _get(f"users/me/dataTypes/body-fat/dataPoints/{data_point_id}")


@mcp.tool()
def get_daily_heart_rate_zones() -> dict:
    """List daily heart rate zone boundary data."""
    return _list("daily-heart-rate-zones")


@mcp.tool()
def get_daily_sleep_temperature_derivations() -> dict:
    """List daily sleep temperature derivation data points."""
    return _list("daily-sleep-temperature-derivations")


# ──────────────────────────────────────────────
# SLEEP TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def get_sleep() -> dict:
    """List sleep session data points."""
    return _list("sleep")


@mcp.tool()
def get_single_sleep(data_point_id: str) -> dict:
    """
    Retrieve a single sleep session by its data point ID.

    Args:
        data_point_id: The ID of the sleep data point.
    """
    return _get(f"users/me/dataTypes/sleep/dataPoints/{data_point_id}")


# ──────────────────────────────────────────────
# NUTRITION TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def get_hydration() -> dict:
    """List hydration log entries (fluid intake in milliliters)."""
    return _list("hydration-log")


@mcp.tool()
def get_hydration_daily_rollup(start_date: str, end_date: str) -> dict:
    """
    Daily hydration totals aggregated per civil day.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup("hydration-log", start_date, end_date)


@mcp.tool()
def get_single_hydration(data_point_id: str) -> dict:
    """
    Retrieve a single hydration log entry by its data point ID.

    Args:
        data_point_id: The ID of the hydration data point.
    """
    return _get(f"users/me/dataTypes/hydration-log/dataPoints/{data_point_id}")


# ──────────────────────────────────────────────
# GENERIC / ADVANCED TOOLS
# ──────────────────────────────────────────────

@mcp.tool()
def list_data_points(data_type: str, filter_expr: str = "") -> dict:
    """
    Generic tool: list data points for ANY supported data type.

    Supported data_type values:
      Activity & Fitness:
        active-minutes, active-zone-minutes, activity-level, altitude,
        calories-in-heart-rate-zone, daily-vo2-max, distance, exercise,
        floors, run-vo2-max, sedentary-period, steps, time-in-heart-rate-zone,
        total-calories, vo2-max
      Health Metrics:
        body-fat, daily-heart-rate-variability, daily-heart-rate-zones,
        daily-oxygen-saturation, daily-respiratory-rate, daily-resting-heart-rate,
        daily-sleep-temperature-derivations, heart-rate, heart-rate-variability,
        oxygen-saturation, respiratory-rate-sleep-summary, weight
      Sleep:
        sleep
      Nutrition:
        hydration-log

    Args:
        data_type:   One of the data type strings listed above.
        filter_expr: Optional AIP-160 filter expression, e.g.
                     'exercise.interval.civil_start_time >= "2026-01-01T00:00:00"'
    """
    params = {"filter": filter_expr} if filter_expr else {}
    return _list(data_type, params)


@mcp.tool()
def daily_rollup(data_type: str, start_date: str, end_date: str) -> dict:
    """
    Generic daily rollup for any supported data type.

    Args:
        data_type:  See list_data_points for valid values.
        start_date: Start date in YYYY-MM-DD format.
        end_date:   End date in YYYY-MM-DD format.
    """
    return _daily_rollup(data_type, start_date, end_date)


@mcp.tool()
def rollup(data_type: str, start_time: str, end_time: str) -> dict:
    """
    Generic rollup over a physical time interval for any supported data type.

    Args:
        data_type:  See list_data_points for valid values.
        start_time: ISO 8601 start timestamp, e.g. "2026-05-01T00:00:00Z".
        end_time:   ISO 8601 end timestamp,   e.g. "2026-05-08T00:00:00Z".
    """
    return _rollup(data_type, start_time, end_time)


@mcp.tool()
def reconcile_data_points(data_type: str, filter_expr: str = "") -> dict:
    """
    Merge data from multiple sources into a single consistent stream.

    Args:
        data_type:   See list_data_points for valid values.
        filter_expr: Optional AIP-160 filter expression.
    """
    params = {"filter": filter_expr} if filter_expr else {}
    return _get(f"users/me/dataTypes/{data_type}/dataPoints:reconcile", params)




from starlette.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

async def auth_url_endpoint(request: Request) -> JSONResponse:
    """GET /auth/url — returns the Google OAuth URL as plain JSON."""
    params = {
        "client_id":     CONFIG["client_id"],
        "redirect_uri":  CONFIG["redirect_uri"],
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    return JSONResponse({"url": url})


async def exchange_endpoint(request: Request) -> JSONResponse:
    """POST /auth/exchange — exchanges a code or redirect URL for tokens."""
    body = await request.json()
    authorization_code = body.get("authorization_code", "")

    if "code=" in authorization_code:
        parsed = urlparse(authorization_code)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            return JSONResponse({"error": "Could not extract code"}, status_code=400)
    else:
        code = authorization_code

    payload = {
        "code":          code,
        "client_id":     CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"],
        "redirect_uri":  CONFIG["redirect_uri"],
        "grant_type":    "authorization_code",
    }
    resp = requests.post(TOKEN_URL, data=payload, timeout=10)
    resp.raise_for_status()
    tokens = resp.json()
    _store_token_response(tokens)

    return JSONResponse({
        "message": "Tokens stored successfully.",
        "expires_in": tokens.get("expires_in"),
    })


# ──────────────────────────────────────────────
# # Entry point
# # ──────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn
    from starlette.routing import Route

    app = mcp.streamable_http_app()
    app.routes.insert(0, Route("/health/token-status", token_status))
    app.routes.insert(1, Route("/auth/url", auth_url_endpoint))
    app.routes.insert(2, Route("/auth/exchange", exchange_endpoint, methods=["POST"]))

    print("Starting on http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
