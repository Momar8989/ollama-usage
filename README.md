# Ollama Usage Collector

Retrieves, stores, and serves Ollama Cloud quota usage. The data comes from the
undocumented endpoint `GET https://ollama.com/api/usage` (same Bearer key you use
for chat completions), which returns:

- `activity` — 4-week metered cost + per-model request counts
- `limits.session` / `limits.weekly` — usage as a 0–1 fraction + per-model counts

The API does **not** expose reset times. Measured behavior (6 resets logged over
48h, zero drift): **session resets in fixed 5-hour blocks at minute :55**,
anchored to epoch `2026-08-12 11:55 UTC`. **Weekly is a ROLLING 72-hour window**
(per the ollama.com GUI, confirmed 2026-08-27) — not fixed blocks. A rolling
window has no computable reset from the API, so `weekly_resets_in` estimates
when the current usage burst ages out (72h after the last observed decay event
in history); it becomes more accurate as history accumulates.

## Run

```bash
export OLLAMA_API_KEY=...        # or keep it in ~/.hermes/.env
pip install fastapi uvicorn
python3 app.py                   # serves on :5093 (override with PORT)
```

Background poller hits ollama.com every 60s (`POLL_SECONDS`) and keeps a
rolling history in `usage_history.json`.

## Endpoints (LAN only — no auth, don't expose)

| Endpoint | Returns |
|---|---|
| `GET /api/usage` | Full snapshot: session + weekly usage %, per-model counts, reset times, ETA, metered cost, fetch age |
| `GET /api/summary` | Compact object for widgets: `session_pct`, `session_resets_in`, `weekly_pct`, `weekly_resets_in`, reset timestamps |
| `GET /api/history` | Time-series samples `{ts, session, weekly}` |
| `GET /health` | `{"ok": bool, "error": str|null}` |

### Example

```json
{
  "session_pct": 0.6,
  "session_resets_in": "1h 7m",
  "weekly_pct": 24.9,
  "weekly_resets_in": "6d 1h 7m",
  "weekly_resets_at": "2026-09-02T11:55:00+00:00"
}
```

## Consumers

- **Homepage widget** — gethomepage.dev custom API widget pointed at `/api/summary`.
- **Tronbyt** — `apps/ollama-usage/ollama-usage.star` renders session + weekly
  usage with time-to-reset; lives in the tronbyt-apps custom repo.

## Related findings (from the measurement session)

- GLM 5.3 flash burns ~0.030 session quota-pts/call vs DeepSeek v4 flash ~0.015
  for the same ~400-token prompt (~2× per call, ~2.3× per token).
- Tiny requests (<~4 tokens) don't move the meter — it tracks GPU work, not call count.
- The metered-cost ledger may show $0.00 and no model rows even while quota is
  actively consumed; quota fractions are the reliable metric.