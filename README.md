## Travel Automation Hub (FastAPI + Playwright)

Runs three bots (myIDTravel, Google Flights, StaffTraveler) concurrently, streams logs over WebSocket, and serves a single-page UI for building `input.json`, triggering runs, and downloading consolidated outputs.

### Setup
- `python -m venv env && source env/bin/activate`
- `pip install -r requirements.txt`
- `python -m playwright install chromium`
- Set credentials in your shell or `.env`:
  - `UAL_USERNAME`, `UAL_PASSWORD` (myIDTravel)
  - `ST_USERNAME`, `ST_PASSWORD` (StaffTraveler)

### Run the server
```
uvicorn main:app --reload
```
Open `http://localhost:8000` to use the UI (served from `index.html` + `/static`).

### Input format
Use `input-template.json` as a guide; `input.json` is the live default. Key fields:
- `flight_type`: `one-way` | `round-trip` | `multiple-legs`
- `trips`: array of `{origin, destination}` per leg
- `itinerary`: array aligned to trips `{date, time, class}`
- `airline`, `travel_status`, `nonstop_flights`, `traveller`, `travel_partner`

The UI supports dynamic legs for multi-city; you can also paste raw JSON.

### API
- `POST /api/run` with `{input, headed?}` launches all bots; responses include `run_id`.
- `WS /ws/{run_id}` streams live logs/status.
- `GET /api/runs/{run_id}` returns run details and file paths.
- `GET /api/runs/{run_id}/download/{json|excel|myidtravel|google_flights|stafftraveler}` downloads outputs.
- `POST /api/scrape-airlines` refreshes `airlines.json` using existing `auth_state.json` (options: `headed`, `origin_query`, `url`, `extra_wait_ms`, `airport_term`, `csrf`).
- `GET /airlines.json` serves the cached airline list for the UI dropdown.

### Outputs
Each run writes to `outputs/YYYYMMDD_HHMMSS/`:
- `myidtravel_flightschedule.json`, `google_flights_results.json`, `stafftraveler_results.json`
- `consolidated.json`, `consolidated.xlsx`
- `input.json` copy plus any per-bot state (e.g., `stafftraveler_auth_state.json`)

### Legacy helpers (optional)
- `login.py` / `fill_form.py` for standalone myIDTravel flows.
- `scrape_airlines.py` still runnable directly, but the scraper is also exposed via `POST /api/scrape-airlines`.
