import asyncio
import json
import logging
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio.subprocess
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, async_playwright

import config
from bots import google_flights_bot, myidtravel_bot, stafftraveler_bot

load_dotenv()
logger = logging.getLogger("globalpass")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

AIRLINE_OUTPUT = Path("airlines.json")
ORIGIN_LOOKUP_OUTPUT = Path("origin_lookup_sample.json")
AIRPORT_PICKER_OUTPUT = Path("airport_picker.json")
BOT_OUTPUTS = {
    "myidtravel": "myidtravel_flightschedule.json",
    "google_flights": "google_flights_results.json",
    "stafftraveler": "stafftraveller_results.json",
}
REPORT_JSON = "standby_report_multi.json"
REPORT_XLSX = "standby_report_multi.xlsx"

app = FastAPI(title="Globalpass Bot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_ROOT = Path("outputs")
RUNS: Dict[str, "RunState"] = {}
RUN_SEMAPHORE = asyncio.Semaphore(1)


class RunState:
    def __init__(self, run_id: str, output_dir: Path, input_data: Dict[str, Any]):
        self.id = run_id
        self.output_dir = output_dir
        self.input_data = input_data
        self.status = "pending"
        self.error: str | None = None
        self.created_at = datetime.utcnow()
        self.completed_at: datetime | None = None
        self.logs: list[Dict[str, Any]] = []
        self.subscribers: dict[WebSocket, asyncio.Queue] = {}
        self.done = asyncio.Event()
        self.result_files: Dict[str, Path] = {}

    def subscribe(self, ws: WebSocket) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers[ws] = queue
        return queue

    def unsubscribe(self, ws: WebSocket) -> None:
        self.subscribers.pop(ws, None)

    async def _broadcast(self, payload: Dict[str, Any], store: bool = False) -> None:
        if store:
            self.logs.append(payload)
        stale: list[WebSocket] = []
        for ws, queue in self.subscribers.items():
            try:
                await queue.put(payload)
            except RuntimeError:
                stale.append(ws)
        for ws in stale:
            self.unsubscribe(ws)

    async def log(self, message: str) -> None:
        payload = {
            "type": "log",
            "ts": datetime.utcnow().isoformat(),
            "message": message,
        }
        await self._broadcast(payload, store=True)

    async def push_status(self) -> None:
        payload = {
            "type": "status",
            "status": self.status,
            "error": self.error,
            "run_id": self.id,
        }
        if self.completed_at:
            payload["completed_at"] = self.completed_at.isoformat()
        await self._broadcast(payload, store=False)


@contextmanager
def patch_config(attr: str, value: Any):
    previous = getattr(config, attr, None)
    setattr(config, attr, value)
    try:
        yield
    finally:
        setattr(config, attr, previous)


def make_run_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def normalize_google_time(time_str: str) -> Optional[str]:
    """Converts 12h time (Google) to 24h 'HH:MM' for matching."""
    try:
        time_str = time_str.replace('\u202f', ' ').strip()
        return datetime.strptime(time_str, "%I:%M %p").strftime("%H:%M")
    except:
        return None
    

def to_minutes(duration_str: str) -> int:
    """Converts duration strings like '7h 25m' to total minutes."""
    if not duration_str:
        return 1440
    try:
        clean = duration_str.lower().replace('hr', 'h').replace('min', 'm').replace(' ', '')
        h = int(clean.split('h')[0]) if 'h' in clean else 0
        m_part = clean.split('h')[-1] if 'h' in clean else clean
        m = int(m_part.replace('m', '')) if 'm' in m_part else 0
        return h * 60 + m
    except:
        return 1440


async def run_myidtravel(state: RunState, input_path: Path, headed: bool) -> Dict[str, Any]:
    output_path = state.output_dir / "myidtravel_flightschedule.json"
    await state.log("[myidtravel] starting")
    try:
        with patch_config("FLIGHTSCHEDULE_OUTPUT", output_path):
            await myidtravel_bot.run(
                headless=not headed,
                screenshot=None,
                input_path=str(input_path),
            )
        if output_path.exists():
            state.result_files["myidtravel"] = output_path
            await state.log(f"[myidtravel] wrote results to {output_path}")
        else:
            await state.log("[myidtravel] finished but output file was not found")
        return {"name": "myidtravel", "status": "ok", "output": str(output_path)}
    except Exception as exc:
        await state.log(f"[myidtravel] error: {exc}")
        return {"name": "myidtravel", "status": "error", "error": str(exc)}


async def run_google_flights(state: RunState, input_path: Path, limit: int, headed: bool) -> Dict[str, Any]:
    output_path = state.output_dir / "google_flights_results.json"
    await state.log("[google_flights] starting")
    try:
        await google_flights_bot.run(
            headless=not headed,
            input_path=str(input_path),
            output=output_path,
            limit=limit,
            screenshot=None,
        )
        state.result_files["google_flights"] = output_path
        await state.log(f"[google_flights] wrote results to {output_path}")
        return {"name": "google_flights", "status": "ok", "output": str(output_path)}
    except Exception as exc:
        await state.log(f"[google_flights] error: {exc}")
        return {"name": "google_flights", "status": "error", "error": str(exc)}


async def run_stafftraveler(state: RunState, input_path: Path, headed: bool) -> Dict[str, Any]:
    output_path = state.output_dir / "stafftraveller_results.json"
    storage_path = state.output_dir / "stafftraveler_auth_state.json"
    await state.log("[stafftraveler] starting")
    try:
        with patch_config("STAFF_RESULTS_OUTPUT", output_path):
            await stafftraveler_bot.perform_stafftraveller_login(
                headless=not headed,
                screenshot=None,
                storage_path=str(storage_path),
                input_data=myidtravel_bot.read_input(str(input_path)),
            )
        state.result_files["stafftraveler"] = output_path
        await state.log(f"[stafftraveler] wrote results to {output_path}")
        return {"name": "stafftraveler", "status": "ok", "output": str(output_path)}
    except Exception as exc:
        await state.log(f"[stafftraveler] error: {exc}")
        return {"name": "stafftraveler", "status": "error", "error": str(exc)}


async def execute_run(state: RunState, limit: int, headed: bool) -> None:
    async with RUN_SEMAPHORE:
        state.status = "running"
        await state.push_status()
        await state.log("Run started; launching three bots concurrently.")
        logger.info("Run %s started (headed=%s, limit=%s)", state.id, headed, limit)

        input_path = state.output_dir / "input.json"
        input_path.write_text(json.dumps(state.input_data, indent=2))

        tasks = [
            run_myidtravel(state, input_path, headed),
            run_google_flights(state, input_path, limit, headed),
            run_stafftraveler(state, input_path, headed),
        ]
        results = await asyncio.gather(*tasks)

        had_error = any(res.get("status") == "error" for res in results)
        # await _mirror_legacy_outputs(state)
        # Generate flight loads report directly from mirrored legacy outputs
        await _run_generate_flight_loads(state)

        state.status = "error" if had_error else "completed"
        state.error = ", ".join(res.get("error", "") for res in results if res.get("error"))
        state.completed_at = datetime.utcnow()
        await state.log("Run finished." if not had_error else "Run finished with errors.")
        logger.info("Run %s completed status=%s", state.id, state.status)
        await state.push_status()
        state.done.set()


async def _page_has_form(page: Page) -> bool:
    selectors = [
        "text=Find Flights",
        'input[placeholder*="Origin" i]',
        'input[placeholder*="Destination" i]',
        "select",
    ]
    for sel in selectors:
        try:
            handle = page.locator(sel).first
            if await handle.is_visible():
                return True
        except Exception:
            continue
    return False


async def _goto_home(page: Page, url_override: Optional[str] = None, extra_wait_ms: int = 0) -> str:
    urls = [url_override] if url_override else config.BASE_URLS
    last_error: Exception | None = None
    tried: list[str] = []

    async def _blocking_message() -> Optional[str]:
        text_nodes = await page.locator("text=eligible for OA travel").all_text_contents()
        if text_nodes:
            return "User is not eligible for OA travel on this account/session."
        return None

    for url in urls:
        tried.append(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2000 + extra_wait_ms)
            current_url = page.url
            if "signon" in current_url:
                raise RuntimeError("Redirected to signon.ual.com; auth_state.json may be expired.")
            blocking = await _blocking_message()
            if blocking:
                raise RuntimeError(blocking)
            if await _page_has_form(page):
                return current_url
            await page.wait_for_timeout(3000 + extra_wait_ms)
            blocking = await _blocking_message()
            if blocking:
                raise RuntimeError(blocking)
            if await _page_has_form(page):
                return current_url
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Failed to load myIDTravel home page from {tried}. Last error: {last_error}")


async def _extract_airline_options(page: Page) -> List[Dict[str, Any]]:
    airline_input = page.locator("#input-airline, input[aria-autocomplete='list'][role='combobox']")
    if not await airline_input.count():
        value_handle = page.locator("text=All Airlines").first
        if await value_handle.is_visible():
            await value_handle.click()
        indicator = page.locator('[aria-haspopup="true"], .css-1xc3v61-indicatorContainer').first
        if await indicator.is_visible():
            await indicator.click()

    if await airline_input.count():
        await airline_input.first.click()
        await airline_input.first.press("ArrowDown")
        await page.wait_for_timeout(250)

        options = await page.evaluate(
            """
            async () => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const menu = document.querySelector('[role="listbox"]') || document.querySelector('.css-5736gi-menu');
                if (!menu) return [];
                const scrollable = menu.querySelector('[style*="overflow: auto"]') || menu;
                const seen = new Map();

                const capture = () => {
                    const opts = menu.querySelectorAll('[role="option"]');
                    opts.forEach(opt => {
                        const raw = (opt.textContent || '').trim();
                        const codeEl = opt.querySelector('#airline-code-container');
                        const code = codeEl ? (codeEl.textContent || '').trim() : null;
                        const label = code ? raw.replace(code, '').trim() : raw;
                        const value = opt.getAttribute('data-value') || opt.getAttribute('value') || code || label;
                        const disabled = opt.getAttribute('aria-disabled') === 'true';
                        const selected = opt.getAttribute('aria-selected') === 'true';
                        const key = code || value || label;
                        seen.set(key, { value: code || value || label, label, disabled, selected });
                    });
                };

                const step = Math.max(40, Math.floor(scrollable.clientHeight * 0.6));
                const totalHeight = scrollable.scrollHeight;

                for (let pos = 0; pos <= totalHeight + step; pos += step) {
                    scrollable.scrollTop = pos;
                    scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                    await sleep(60);
                    capture();
                }
                for (let pos = totalHeight; pos >= 0; pos -= step) {
                    scrollable.scrollTop = pos;
                    scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                    await sleep(60);
                    capture();
                }
                scrollable.scrollTop = scrollable.scrollHeight;
                scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                await sleep(120);
                capture();

                return Array.from(seen.values());
            }
            """
        )
        if options:
            return options

    raise RuntimeError("Airline dropdown not found. Is the page layout different?")


async def _capture_origin_lookup(page: Page, query: str) -> List[Dict[str, Any]]:
    captured: List[Dict[str, Any]] = []
    keywords = ("airport", "origin", "destination", "lookup", "suggest")

    async def handle_response(response) -> None:
        try:
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            url_lower = response.url.lower()
            if not any(k in url_lower for k in keywords):
                return
            try:
                body = await response.json()
            except Exception:
                body = await response.text()
            captured.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "headers": dict(response.headers),
                    "body": body,
                }
            )
        except Exception:
            return

    page.on("response", lambda resp: asyncio.create_task(handle_response(resp)))

    origin_input = page.locator('input[placeholder*="Origin" i]').first
    await origin_input.click()
    await origin_input.fill("")
    await origin_input.type(query, delay=60)
    await page.wait_for_timeout(2500)

    if captured:
        ORIGIN_LOOKUP_OUTPUT.write_text(json.dumps(captured, indent=2))
    return captured


async def _get_csrf_token(context) -> Optional[str]:
    try:
        cookies = await context.cookies()
        for c in cookies:
            if c.get("name", "").lower() in {"csrf", "xsrf-token", "x-csrf-token"}:
                return c.get("value")
    except Exception:
        pass
    return None


async def _fetch_airport_picker(
    page: Page,
    context,
    term: str,
    url_base: str,
    page_num: int = 1,
    limit: int = 25,
    csrf_override: Optional[str] = None,
) -> Dict[str, Any]:
    csrf_token = csrf_override or await _get_csrf_token(context)
    if not csrf_token:
        raise RuntimeError("Could not find CSRF token. Provide one via request body or refresh auth.")

    endpoint = url_base.rstrip("/") + "/json/general/airportPicker"
    payload = {
        "term": term,
        "page": page_num,
        "start": 0 if page_num <= 1 else (page_num - 1) * limit,
        "limit": limit,
        "csrf": csrf_token,
    }

    resp = await page.request.post(endpoint, data=payload)
    if not resp.ok:
        raise RuntimeError(f"airportPicker request failed {resp.status}: {await resp.text()}")
    data = await resp.json()
    AIRPORT_PICKER_OUTPUT.write_text(json.dumps(data, indent=2))
    return data


async def scrape_airlines_task(
    headless: bool = True,
    sample_origin_query: Optional[str] = None,
    url_override: Optional[str] = None,
    extra_wait_ms: int = 0,
    airport_term: Optional[str] = None,
    csrf_override: Optional[str] = None,
) -> Dict[str, Any]:
    storage_file = Path("auth_state.json")
    if not storage_file.exists():
        raise RuntimeError("auth_state.json not found. Run the myIDTravel bot first to create it.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(storage_file))
        page = await context.new_page()

        home_url = await _goto_home(page, url_override=url_override, extra_wait_ms=extra_wait_ms)

        airlines = await _extract_airline_options(page)
        AIRLINE_OUTPUT.write_text(json.dumps(airlines, indent=2))

        airport_picker_payload = None
        if airport_term:
            airport_picker_payload = await _fetch_airport_picker(
                page=page,
                context=context,
                term=airport_term,
                url_base=home_url,
                csrf_override=csrf_override,
            )

        origin_lookup_payload = None
        if sample_origin_query:
            origin_lookup_payload = await _capture_origin_lookup(page, sample_origin_query)

        await context.close()
        await browser.close()

    return {
        "home_url": home_url,
        "airlines_count": len(airlines),
        "airlines_path": str(AIRLINE_OUTPUT),
        "airport_picker_path": str(AIRPORT_PICKER_OUTPUT) if airport_picker_payload else None,
        "origin_lookup_path": str(ORIGIN_LOOKUP_OUTPUT) if origin_lookup_payload else None,
    }

async def _run_generate_flight_loads(state: RunState) -> None:
    """
    Generate standby_report_multi.* using the three bot outputs.
    This is the migrated logic from generate-flight-loads.py.
    """
    await state.log("[report] Starting flight loads report generation...")
    
    try:
        # Load all sources directly from run directory
        myid_path = state.result_files.get("myidtravel")
        google_path = state.result_files.get("google_flights")
        staff_path = state.result_files.get("stafftraveler")
        
        if not all([myid_path, google_path, staff_path]):
            await state.log("[report] Missing required input files, skipping report generation")
            return
        
        # Load JSON data
        with open(myid_path, 'r') as f:
            myid_data = json.load(f)
        with open(google_path, 'r') as f:
            google_data = json.load(f)
        with open(staff_path, 'r') as f:
            staff_data = json.load(f)
        
        await state.log("[report] Loaded all input files successfully")
        
        # Pre-process external sources for matching
        staff_map = {
            d['airline_flight_number']: d.get('aircraft', 'N/A')
            for entry in staff_data
            for d in entry.get('flight_details', [])
        }
        
        google_map = {}
        for entry in google_data:
            for ftype in ['top_flights', 'other_flights']:
                for g_f in entry.get('flights', {}).get(ftype, []):
                    norm_time = normalize_google_time(g_f.get('depart_time', ''))
                    if norm_time:
                        google_map[(g_f['airline'], norm_time)] = True
        
        chance_weights = {"HIGH": 100, "MID": 50, "LOW": 10}
        eligible_flights = []
        
        # Filter and score selectable flights
        for routing in myid_data.get('routings', []):
            for flight in routing.get('flights', []):
                if flight.get('selectable') is not True:
                    continue
                
                seg = flight['segments'][0]
                f_num = seg['flightNumber']
                airline = seg['operatingAirline']['name']
                dep_time = seg['departureTime']
                
                # Source detection
                in_staff = f_num in staff_map
                in_google = (airline, dep_time) in google_map
                sources = ["MyIDTravel.com"]
                if in_google:
                    sources.append("Google Flights")
                if in_staff:
                    sources.append("Stafftraveler")
                
                # Scoring logic
                dur_min = to_minutes(flight.get('duration', '0h 0m'))
                score = chance_weights.get(flight.get('chance', 'LOW'), 0)
                score += 20 if len(flight.get('segments', [])) == 1 else 0  # Nonstop bonus
                score += max(0, (720 - dur_min) / 10)  # Duration bonus
                
                eligible_flights.append({
                    "Flight": f_num,
                    "Airline": airline,
                    "Aircraft": staff_map.get(f_num, "N/A"),
                    "Departure": dep_time,
                    "Arrival": seg['arrivalTime'],
                    "Duration": flight.get('duration'),
                    "Chance": flight.get('chance'),
                    "Source": ", ".join(sources),
                    "Score": round(score, 2),
                    "In_Staff": in_staff,
                    "In_Google": in_google
                })
        
        await state.log(f"[report] Processed {len(eligible_flights)} eligible flights")
        
        # Ranking helper function
        def get_top_5(subset):
            ranked = sorted(subset, key=lambda x: x['Score'], reverse=True)
            final = []
            for i, item in enumerate(ranked[:5], 1):
                clean = {"Rank": i}
                clean.update({k: v for k, v in item.items() if not k.startswith("In_")})
                final.append(clean)
            return final
        
        # Generate output lists
        results = {
            "Top_5_Overall": get_top_5(eligible_flights),
            "Top_5_MyIDTravel": get_top_5(eligible_flights),
            "Top_5_Stafftraveler": get_top_5([f for f in eligible_flights if f['In_Staff']]),
            "Top_5_Google_Flights": get_top_5([f for f in eligible_flights if f['In_Google']])
        }
        
        # Save to run directory
        json_output = state.output_dir / "standby_report_multi.json"
        excel_output = state.output_dir / "standby_report_multi.xlsx"
        
        with open(json_output, 'w') as f:
            json.dump(results, f, indent=4)
        
        state.result_files["standby_report_multi.json"] = json_output
        await state.log(f"[report] Generated {json_output}")
        
        # Generate Excel output
        try:
            import pandas as pd
            with pd.ExcelWriter(excel_output) as writer:
                pd.DataFrame(results["Top_5_Overall"]).to_excel(writer, sheet_name='Top 5 Overall', index=False)
                pd.DataFrame(results["Top_5_MyIDTravel"]).to_excel(writer, sheet_name='MyIDTravel', index=False)
                pd.DataFrame(results["Top_5_Stafftraveler"]).to_excel(writer, sheet_name='Stafftraveler', index=False)
                pd.DataFrame(results["Top_5_Google_Flights"]).to_excel(writer, sheet_name='Google Flights', index=False)
            
            state.result_files["standby_report_multi.xlsx"] = excel_output
            await state.log(f"[report] Generated {excel_output}")
        except ImportError:
            await state.log("[report] pandas not available, skipping Excel generation")
        except Exception as exc:
            await state.log(f"[report] Excel generation failed: {exc}")
        
        await state.log("[report] Flight loads report generation completed successfully")
        
    except FileNotFoundError as exc:
        await state.log(f"[report] Required JSON files not found: {exc}")
    except json.JSONDecodeError as exc:
        await state.log(f"[report] JSON parsing error: {exc}")
    except Exception as exc:
        await state.log(f"[report] Error generating report: {exc}")


@app.websocket("/ws/{run_id}")
async def logs_ws(websocket: WebSocket, run_id: str):
    state = RUNS.get(run_id)
    if not state:
        await websocket.close(code=4000)
        return

    await websocket.accept()
    queue = state.subscribe(websocket)

    # Send existing logs to new subscribers.
    for log in state.logs:
        await websocket.send_json(log)
    await websocket.send_json({"type": "status", "status": state.status, "run_id": state.id})

    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
            if payload.get("type") == "status" and payload.get("status") in {"completed", "error"}:
                break
    except WebSocketDisconnect:
        state.unsubscribe(websocket)


@app.post("/api/run")
async def start_run(payload: Dict[str, Any] = Body(...)):
    input_data = payload.get("input") if isinstance(payload, dict) else None
    if not input_data:
        input_data = payload

    if not isinstance(input_data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    limit = payload.get("limit") if isinstance(payload, dict) else None
    limit = int(limit) if isinstance(limit, int) or (isinstance(limit, str) and limit.isdigit()) else 30
    headed = bool(payload.get("headed")) if isinstance(payload, dict) else False

    run_id = make_run_id()
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_id, run_dir, input_data)
    RUNS[run_id] = state

    logger.info("Queued run %s", run_id)
    asyncio.create_task(execute_run(state, limit=limit, headed=headed))
    return {"run_id": run_id, "status": state.status, "output_dir": str(run_dir)}


@app.get("/api/runs/{run_id}")
async def run_details(run_id: str):
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found.")
    report_path = state.output_dir / REPORT_JSON
    report_data = {}
    if report_path.exists():
        try:
            report_data = json.loads(report_path.read_text())
        except Exception:
            report_data = {}
    return {
        "run_id": run_id,
        "status": state.status,
        "error": state.error,
        "created_at": state.created_at.isoformat(),
        "completed_at": state.completed_at.isoformat() if state.completed_at else None,
        "output_dir": str(state.output_dir),
        "report": report_data,
        "files": {k: str(v) for k, v in state.result_files.items() if v.exists()},
    }


@app.get("/api/runs/{run_id}/download/{kind}")
async def download(run_id: str, kind: str):
    run_dir = OUTPUT_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found.")

    if kind == "json":
        path = run_dir / REPORT_JSON
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report JSON not found.")
        return FileResponse(path, filename=path.name)

    if kind == "excel":
        path = run_dir / REPORT_XLSX
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report Excel not found.")
        return FileResponse(path, filename=path.name)

    if kind in BOT_OUTPUTS:
        bot_path = run_dir / BOT_OUTPUTS[kind]
        if bot_path.exists():
            return FileResponse(bot_path, filename=bot_path.name)
        raise HTTPException(status_code=404, detail=f"No output found for {kind}")

    raise HTTPException(status_code=400, detail="Unsupported download format.")


@app.post("/api/scrape-airlines")
async def scrape_airlines_api(payload: Dict[str, Any] = Body(default={})):
    """
    Run the airline dropdown scraper using existing auth_state.json.
    """
    try:
        result = await scrape_airlines_task(
            headless=not bool(payload.get("headed")),
            sample_origin_query=payload.get("origin_query"),
            url_override=payload.get("url"),
            extra_wait_ms=int(payload.get("extra_wait_ms") or 0),
            airport_term=payload.get("airport_term"),
            csrf_override=payload.get("csrf"),
        )
        return {"status": "ok", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = Path("index.html")
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse("<h1>Globalpass Bot</h1><p>UI not found.</p>")


@app.get("/airlines.json")
async def airlines():
    path = Path("airlines.json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="airlines.json not found")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=config.API_HOST, port=config.API_PORT, reload=True)
