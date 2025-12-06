## Playwright login helper

Automates login to `https://signon.ual.com/oamfed/idp/initiatesso?providerid=DPmyidtravel` with Playwright for Python.

### Setup
- Create a virtual env (use your preferred name, e.g., `env`): `python -m venv env && source env/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Install browser binaries: `python -m playwright install chromium`
- Add credentials (loaded via python-dotenv if present):
  - Either export: `export UAL_USERNAME="your_username"` and `export UAL_PASSWORD="your_password"`
  - Or add them to `.env` (see `.env.example`) and the script will load them automatically.

### Run (combined login + fill)
```
python main.py --headed              # login with .env creds, fill form using input.json, capture flightschedule JSON
python main.py --input custom.json   # use a different input file
python main.py --screenshot login.png # optional login screenshot
```
After a successful run you will have:
- `auth_state.json` containing the authenticated storage state for reuse
- `json/<flight_type>-flightschedule.json` saved from the flightschedule POST response

`input.json` example (required keys: origin, destination, departure; optional: airline, flight_type):
```json
{
  "flight_type": "one-way",
  "origin": "DXB",
  "destination": "SIN",
  "departure": "01/10/2026",
  "airline": "United Airlines"
}
```

If you want login only, run `python login.py` (uses .env creds and writes auth_state.json).

### Scrape airlines (and capture origin lookup traffic)
- Ensure `auth_state.json` exists (run `python main.py` first).
- Run `python scrape_airlines.py` to write `airlines.json` with the dropdown entries.
- Optional flags:
  - `--origin-query LAX` types into the Origin field and saves the observed lookup responses to `origin_lookup_sample.json`. Adjust the query to suit your testing.
  - `--url https://swa.myidtravel.com/myidtravel/` (or another host) if the script can’t auto-detect the correct myIDTravel home URL.
  - `--extra-wait-ms 3000` adds extra hydration time after navigation if the form is slow to render.
  - `--airport-term a` calls the airportPicker endpoint with the given term and saves the response to `airport_picker.json` (auto-uses CSRF from cookies; override with `--csrf <token>` if needed).

Troubleshooting:
- If you get redirected to `signon.ual.com`, refresh `auth_state.json` by re-running `python main.py`.
- If the page shows “not eligible for OA travel”, the current account/session cannot access the OA travel form; use an eligible account and refresh `auth_state.json`.
- Use `--headed` to visually confirm the page renders and identify any blocking modal/MFA screens.

### Fill flight form
`fill_form.py` (legacy helper) uses `auth_state.json` and `.env` values to populate the flight schedule form:
- Required env vars: `INPUT_ORIGIN`, `INPUT_DESTINATION`, `INPUT_DEPARTURE`, `INPUT_AIRLINE` (set `HEADLESS=false` to watch).
- Run: `python fill_form.py` (add `--headed` to watch, or set `HEADLESS=false`).
