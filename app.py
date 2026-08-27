"""
Ollama Cloud Usage Collector — retrieves and stores Ollama Cloud quota usage,
serves it to other apps (homepage widget, Tronbyt display, anything else).

Data source: the undocumented endpoint  GET https://ollama.com/api/usage
authenticated with the same Bearer key used for chat completions.

Session quota resets in fixed 5-hour blocks (measured: resets land at minute
:55, anchored to 2026-08-12 11:55 UTC). Weekly appears to be a fixed 7-day
window anchored to the same epoch. Both are provisional — the API does not
expose reset times, so we compute them.

Endpoints served (LAN, no auth — do not expose to the internet):
  GET /api/usage        full snapshot (live fetch of ollama + computed resets)
  GET /api/summary      small object for widgets: session/week %, resets, ETA
  GET /health           liveness

Env:
  OLLAMA_API_KEY   (required) Bearer key for ollama.com
  PORT             (default 5093)
  POLL_SECONDS     background poll interval (default 60)
"""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import urllib.request

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

OLLAMA_USAGE_URL = "https://ollama.com/api/usage"
API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()
if not API_KEY:
    # fall back to Hermes' env file
    for line in open(os.path.expanduser("~/.hermes/.env")):
        if line.startswith("OLLAMA_API_KEY="):
            API_KEY = line.split("=", 1)[1].strip()
            break
if not API_KEY:
    raise SystemExit("OLLAMA_API_KEY not set and not found in ~/.hermes/.env")

PORT = int(os.environ.get("PORT", "5093"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))

# --- Reset math (provisional, from measurement: see README) ---------------
EPOCH = datetime(2026, 8, 12, 11, 55, tzinfo=timezone.utc)
SESSION_BLOCK = timedelta(hours=5)
# Weekly is a ROLLING 72h window per ollama.com's GUI (verified by Jordan 2026-08-27) —
# NOT fixed 7-day blocks as first assumed. A rolling window has no computable reset time
# from the API (no per-request timestamps), so weekly_resets_* are the time until the
# most recent usage BURST ages out of the window, updated as history accumulates.
WEEK_WINDOW = timedelta(hours=72)


def next_reset(now: datetime, block: timedelta) -> datetime:
    """EPOCH + ceil((now - EPOCH) / block) * block"""
    delta = (now - EPOCH).total_seconds()
    if delta < 0:
        return EPOCH
    n = math.ceil(delta / block.total_seconds())
    return EPOCH + n * block


def rolling_week_reset(now: datetime, current_usage: float):
    """Estimate when the rolling 72h window fully rolls over for the current usage.

    The API gives no per-request timestamps, so we approximate: find the oldest
    history sample whose weekly usage is within 1pt of the current value (the
    start of the current usage plateau = when this burst began accruing), then
    add 72h. If the window's usage has decayed since (a drop), the oldest
    still-counted activity is after that drop. Returns None if no history.
    """
    hist = _state.get("history") or []
    samples = [(s["ts"], s["weekly"]) for s in hist if s.get("weekly") is not None]
    if not samples:
        return None
    now_ts = now.timestamp()
    # keep only samples within the last 72h (everything older has left the window)
    samples = [(ts, w) for ts, w in samples if now_ts - ts < WEEK_WINDOW.total_seconds()]
    if not samples:
        return None
    # find last decay: a sample lower than the previous one
    last_drop_ts = None
    prev = None
    for ts, w in samples:
        if prev is not None and w < prev - 0.001:
            last_drop_ts = ts
        prev = w
    # activity counted in the current window began after the last decay event
    # (or the oldest sample we have). Anchor: earliest sample after last drop
    # that is >= current usage minus a small decay allowance.
    anchor_ts = None
    for ts, w in samples:
        if last_drop_ts is not None and ts <= last_drop_ts:
            continue
        if anchor_ts is None:
            anchor_ts = ts
    if anchor_ts is None:
        anchor_ts = samples[0][0]
    return now + timedelta(seconds=(anchor_ts + WEEK_WINDOW.total_seconds()) - now_ts)


def human_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# --- Storage ----------------------------------------------------------------
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_history.json")
_state = {
    "raw": None,          # last successful fetch from ollama.com
    "fetched_at": None,   # unix ts
    "error": None,
    "history": [],        # [{ts, session, weekly}] sampled each poll
}


def save_history():
    try:
        # keep last 30 days of samples (2 samples/10 min => ~4320/day, trim hard)
        _state["history"] = _state["history"][-2016:]
        with open(DATA_FILE, "w") as f:
            json.dump({"history": _state["history"]}, f)
    except Exception as e:
        print(f"[ollama-usage] history save failed: {e}")


def load_history():
    try:
        with open(DATA_FILE) as f:
            _state["history"] = json.load(f).get("history", [])
    except Exception:
        pass


def fetch_ollama():
    req = urllib.request.Request(
        OLLAMA_USAGE_URL, headers={"Authorization": f"Bearer {API_KEY}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def snapshot() -> dict:
    """Live view: cached raw + computed resets + eta."""
    now = datetime.now(timezone.utc)
    raw = _state["raw"]
    if raw is None:
        return {"error": _state["error"] or "no data yet", "fetched_at": None}

    sess = raw.get("limits", {}).get("session", {})
    week = raw.get("limits", {}).get("weekly", {})

    s_reset = next_reset(now, SESSION_BLOCK)

    # Rolling 72h window: estimate the reset as 72h after the oldest activity
    # still counted. We can't see per-request timestamps, so approximate with
    # history: the window "full reset" is when the current cumulative usage
    # would drop — i.e. 72h after the earliest sample at the current level.
    w_reset = rolling_week_reset(now, week.get("usage", 0.0))

    s_usage = sess.get("usage", 0.0)
    w_usage = week.get("usage", 0.0)

    return {
        "session": {
            "usage_pct": round(s_usage * 100, 1),
            "requests": sum(m.get("request_count", 0) for m in sess.get("models", [])),
            "models": sess.get("models", []),
            "resets_at_utc": s_reset.isoformat(),
            "resets_in": human_eta((s_reset - now).total_seconds()),
        },
        "weekly": {
            "usage_pct": round(w_usage * 100, 1),
            "requests": sum(m.get("request_count", 0) for m in week.get("models", [])),
            "models": week.get("models", []),
            "window_hours": 72,
            "rolling": True,
            "resets_at_utc": w_reset.isoformat() if w_reset else None,
            "resets_in": human_eta((w_reset - now).total_seconds()) if w_reset else "72h rolling",
        },
        "metered_cost_4wk": raw.get("activity", {}).get("cost"),
        "fetched_at_utc": (
            datetime.fromtimestamp(_state["fetched_at"], tz=timezone.utc).isoformat()
            if _state["fetched_at"]
            else None
        ),
        "fetched_at_age_s": (
            int(time.time() - _state["fetched_at"]) if _state["fetched_at"] else None
        ),
        "ollama_error": _state["error"],
    }


# --- Background poller ------------------------------------------------------
def poll_loop():
    load_history()
    while True:
        try:
            raw = fetch_ollama()
            _state["raw"] = raw
            _state["fetched_at"] = time.time()
            _state["error"] = None
            s = raw.get("limits", {}).get("session", {}).get("usage")
            w = raw.get("limits", {}).get("weekly", {}).get("usage")
            _state["history"].append(
                {"ts": int(time.time()), "session": s, "weekly": w}
            )
            save_history()
        except Exception as e:
            _state["error"] = str(e)
            print(f"[ollama-usage] fetch failed: {e}")
        time.sleep(POLL_SECONDS)


# --- App --------------------------------------------------------------------
app = FastAPI(title="Ollama Usage Collector", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _start():
    import threading

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()


@app.get("/api/usage")
def full():
    return JSONResponse(snapshot())


@app.get("/api/summary")
def summary():
    snap = snapshot()
    if "error" in snap and snap.get("session") is None:
        return JSONResponse({"error": snap["error"]}, status_code=503)
    return JSONResponse(
        {
            "session_pct": snap["session"]["usage_pct"],
            "session_resets_in": snap["session"]["resets_in"],
            "session_resets_at": snap["session"]["resets_at_utc"],
            "weekly_pct": snap["weekly"]["usage_pct"],
            "weekly_resets_in": snap["weekly"]["resets_in"],
            "weekly_resets_at": snap["weekly"]["resets_at_utc"],
            "fetched_age_s": snap.get("fetched_at_age_s"),
        }
    )


@app.get("/api/history")
def history():
    return JSONResponse({"history": _state["history"]})


@app.get("/health")
def health():
    return {"ok": _state["raw"] is not None, "error": _state["error"]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")