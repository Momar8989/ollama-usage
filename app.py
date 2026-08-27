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
WEEK_BLOCK = timedelta(days=7)


def next_reset(now: datetime, block: timedelta) -> datetime:
    """EPOCH + ceil((now - EPOCH) / block) * block"""
    delta = (now - EPOCH).total_seconds()
    if delta < 0:
        return EPOCH
    n = math.ceil(delta / block.total_seconds())
    return EPOCH + n * block


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
    w_reset = next_reset(now, WEEK_BLOCK)

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
            "resets_at_utc": w_reset.isoformat(),
            "resets_in": human_eta((w_reset - now).total_seconds()),
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