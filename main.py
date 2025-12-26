import asyncio
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, async_playwright

import consolidator
import config
from bots import google_flights_bot, myidtravel_bot, stafftraveler_bot

load_dotenv()

AIRLINE_OUTPUT = Path("airlines.json")
ORIGIN_LOOKUP_OUTPUT = Path("origin_lookup_sample.json")
AIRPORT_PICKER_OUTPUT = Path("airport_picker.json")

app = FastAPI(title="Travel Automation Hub", version="0.1.0")
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
    output_path = state.output_dir / "stafftraveler_results.json"
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

        input_path = state.output_dir / "input.json"
        input_path.write_text(json.dumps(state.input_data, indent=2))

        tasks = [
            run_myidtravel(state, input_path, headed),
            run_google_flights(state, input_path, limit, headed),
            run_stafftraveler(state, input_path, headed),
        ]
        results = await asyncio.gather(*tasks)

        had_error = any(res.get("status") == "error" for res in results)
        consolidated = consolidator.consolidate(state.id, state.output_dir)
        consolidated_path = state.output_dir / "consolidated.json"
        excel_path = state.output_dir / "consolidated.xlsx"

        consolidator.write_json(consolidated, consolidated_path)
        try:
            consolidator.write_excel(consolidated, excel_path)
        except Exception as exc:
            await state.log(f"[consolidator] excel export failed: {exc}")

        state.result_files["consolidated_json"] = consolidated_path
        state.result_files["consolidated_excel"] = excel_path

        state.status = "error" if had_error else "completed"
        state.error = ", ".join(res.get("error", "") for res in results if res.get("error"))
        state.completed_at = datetime.utcnow()
        await state.log("Run finished." if not had_error else "Run finished with errors.")
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

    asyncio.create_task(execute_run(state, limit=limit, headed=headed))
    return {"run_id": run_id, "status": state.status, "output_dir": str(run_dir)}


@app.get("/api/runs/{run_id}")
async def run_details(run_id: str):
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found.")
    consolidated = consolidator.consolidate(run_id, state.output_dir)
    return {
        "run_id": run_id,
        "status": state.status,
        "error": state.error,
        "created_at": state.created_at.isoformat(),
        "completed_at": state.completed_at.isoformat() if state.completed_at else None,
        "output_dir": str(state.output_dir),
        "consolidated": consolidated,
        "files": {k: str(v) for k, v in state.result_files.items() if v.exists()},
    }


@app.get("/api/runs/{run_id}/download/{kind}")
async def download(run_id: str, kind: str):
    run_dir = OUTPUT_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found.")

    if kind == "json":
        path = run_dir / "consolidated.json"
        if not path.exists():
            consolidator.write_json(consolidator.consolidate(run_id, run_dir), path)
        return FileResponse(path, filename=f"{run_id}.json")

    if kind == "excel":
        path = run_dir / "consolidated.xlsx"
        if not path.exists():
            consolidator.write_excel(consolidator.consolidate(run_id, run_dir), path)
        return FileResponse(path, filename=f"{run_id}.xlsx")

    if kind in consolidator.BOT_OUTPUTS:
        bot_path = run_dir / consolidator.BOT_OUTPUTS[kind]
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
    return HTMLResponse("<h1>Travel Automation Hub</h1><p>UI not found.</p>")


@app.get("/airlines.json")
async def airlines():
    path = Path("airlines.json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="airlines.json not found")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
