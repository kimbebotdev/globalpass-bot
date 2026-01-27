## Globalpass Bot (FastAPI + Playwright)

Runs three bots (myIDTravel, Google Flights, StaffTraveler) concurrently, streams logs over WebSocket, and serves a single-page UI for building inputs, triggering runs, and downloading consolidated outputs.

### Setup
- `python -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements.txt`
- `python -m playwright install chromium`
- Set credentials in your shell or `.env`:
  - `UAL_USERNAME`, `UAL_PASSWORD` (myIDTravel)
  - `ST_USERNAME`, `ST_PASSWORD` (StaffTraveler)
- Optional Slack and Gemini:
  - `SLACK_BOT_USER_OAUTH_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`
  - `GEMINI_API_KEY`, `GEMINI_MODEL` (default: `gemini-1.5-flash`)

### Run the server
```
uvicorn main:app --reload
```
Open `http://localhost:8000` to use the UI (served from `index.html` + `/static`).

### Usage
See `USAGE.md` for a full walkthrough of the form fields, JSON input, Slack notifications, and the end‑to‑end run flow.

### API
- `POST /api/run` with `{input, headed?}` launches all bots; responses include `run_id`.
- `WS /ws/{run_id}` streams live logs/status.
- `GET /api/runs/{run_id}` returns run details and file paths.
- `GET /api/runs/{run_id}/download/{json|excel|myidtravel|google_flights|stafftraveler}` downloads outputs.
- `GET /api/runs/{run_id}/download-report-xlsx` downloads the Excel report as `{run_id}.xlsx`.
- `POST /api/scrape-airlines` refreshes `airlines.json` using existing `auth_state.json` (options: `headed`, `origin_query`, `url`, `extra_wait_ms`, `airport_term`, `csrf`).
- `GET /airlines.json` serves the cached airline list for the UI dropdown.

### Outputs
Each run writes to `outputs/YYYYMMDD_HHMMSS/`:
- `myidtravel_flightschedule.json`, `google_flights_results.json`, `stafftraveler_results.json`
- `standby_report_multi.json`, `standby_report_multi.xlsx`
- `input.json` copy plus any per-bot state (e.g., `stafftraveler_auth_state.json`)
- `gemini_response.txt` and `gemini_response.json` when Gemini is enabled

### Legacy helpers (optional)
- `login.py` / `fill_form.py` for standalone myIDTravel flows.
- `scrape_airlines.py` still runnable directly, but the scraper is also exposed via `POST /api/scrape-airlines`.
