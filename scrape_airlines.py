import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)

load_dotenv()

BASE_URLS = [
    # Primary United-hosted domain observed in auth_state.json
    "https://myidtravel-united.ual.com/myidtravel/",
    # Common host variants seen in production
    "https://www.myidtravel.com/myidtravel/",
    "https://swa.myidtravel.com/myidtravel/",
    "https://myidtravel.com/myidtravel/",
]

AIRLINE_OUTPUT = Path("airlines.json")
ORIGIN_LOOKUP_OUTPUT = Path("origin_lookup_sample.json")
AIRPORT_PICKER_OUTPUT = Path("airport_picker.json")


async def _page_has_form(page: Page) -> bool:
    """Detect whether the search form has rendered."""
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


async def goto_home(page: Page, url_override: Optional[str] = None, extra_wait_ms: int = 0) -> str:
    """
    Navigate to a reachable myIDTravel home URL.
    extra_wait_ms lets you extend hydration time if the page is slow to render.
    """
    async def _blocking_message() -> Optional[str]:
        # Check for a visible banner/message indicating lack of eligibility or access.
        text_nodes = await page.locator("text=eligible for OA travel").all_text_contents()
        if text_nodes:
            return "User is not eligible for OA travel on this account/session."
        return None

    last_error: Optional[Exception] = None
    urls = [url_override] if url_override else BASE_URLS
    tried: list[str] = []
    for url in urls:
        tried.append(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2000 + extra_wait_ms)  # allow client hydration
            current_url = page.url
            if "signon" in current_url:
                raise RuntimeError("Redirected to signon.ual.com; auth_state.json may be expired.")
            blocking = await _blocking_message()
            if blocking:
                raise RuntimeError(blocking)
            if await _page_has_form(page):
                return current_url
            # Some flows need a longer wait for JS-injected form
            await page.wait_for_timeout(3000 + extra_wait_ms)
            blocking = await _blocking_message()
            if blocking:
                raise RuntimeError(blocking)
            if await _page_has_form(page):
                return current_url
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
    raise RuntimeError(
        f"Failed to load myIDTravel home page from {tried}. Last error: {last_error}"
    )


async def extract_airline_options(page: Page) -> List[Dict[str, Any]]:
    """
    Return airline dropdown entries as [{value, label, disabled, selected}].

    Handles both native <select> and React-select style comboboxes (e.g. input#input-airline).
    """
    # React-select style combobox (input#input-airline or similar).
    airline_input = page.locator(
        "#input-airline, input[aria-autocomplete='list'][role='combobox']"
    )
    # If the input isn't found immediately, click the displayed value to focus it.
    if not await airline_input.count():
        value_handle = page.locator("text=All Airlines").first
        if await value_handle.is_visible():
            await value_handle.click()
        # Also try clicking the indicator/chevron container to open the menu.
        indicator = page.locator('[aria-haspopup="true"], .css-1xc3v61-indicatorContainer').first
        if await indicator.is_visible():
            await indicator.click()

    if await airline_input.count():
        await airline_input.first.click()
        await airline_input.first.press("ArrowDown")
        await page.wait_for_timeout(250)

        # Scroll the virtualized list within the menu container until no new options appear.
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

                // Sweep downwards in fixed steps.
                for (let pos = 0; pos <= totalHeight + step; pos += step) {
                    scrollable.scrollTop = pos;
                    scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                    await sleep(60);
                    capture();
                }
                // Sweep upwards to catch any missed renders.
                for (let pos = totalHeight; pos >= 0; pos -= step) {
                    scrollable.scrollTop = pos;
                    scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                    await sleep(60);
                    capture();
                }
                // Final pass at bottom.
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


def _input_selector(label_keyword: str) -> str:
    """CSS selector to find an input by placeholder containing keyword (case-insensitive)."""
    return f'input[placeholder*="{label_keyword}" i]'


async def capture_origin_lookup(page: Page, query: str) -> List[Dict[str, Any]]:
    """
    Type into the origin field and capture JSON responses for lookup suggestions.
    Writes the first captured payloads to ORIGIN_LOOKUP_OUTPUT.
    """
    captured: List[Dict[str, Any]] = []
    keywords = ("airport", "origin", "destination", "lookup", "suggest")

    async def handle_response(response) -> None:
        try:
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            url_lower = response.url.lower()
            if not any(k in url_lower for k in keywords):
                return
            body: Any
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
            # Swallow errors so network logging never blocks the main flow.
            return

    page.on("response", lambda resp: asyncio.create_task(handle_response(resp)))

    origin_input = page.locator(_input_selector("Origin")).first
    await origin_input.click()
    await origin_input.fill("")
    await origin_input.type(query, delay=60)
    await page.wait_for_timeout(2500)

    if captured:
        ORIGIN_LOOKUP_OUTPUT.write_text(json.dumps(captured, indent=2))
    return captured


async def _get_csrf_token(context) -> Optional[str]:
    """Attempt to discover a CSRF token from cookies or storage."""
    try:
        cookies = await context.cookies()
        for c in cookies:
            if c.get("name", "").lower() in {"csrf", "xsrf-token", "x-csrf-token"}:
                return c.get("value")
    except Exception:
        pass
    return None


async def fetch_airport_picker(
    page: Page,
    context,
    term: str,
    url_base: str,
    page_num: int = 1,
    limit: int = 25,
    csrf_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call the airportPicker endpoint directly and save the result.
    Uses the current authenticated context; requires a valid CSRF token.
    """
    csrf_token = csrf_override or await _get_csrf_token(context)
    if not csrf_token:
        raise RuntimeError("Could not find CSRF token. Provide one via --csrf or refresh auth.")

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


async def run(
    headless: bool,
    sample_origin_query: Optional[str],
    url_override: Optional[str],
    extra_wait_ms: int,
    airport_term: Optional[str],
    csrf_override: Optional[str],
) -> None:
    storage_file = Path("auth_state.json")
    if not storage_file.exists():
        raise SystemExit("auth_state.json not found. Run main.py first to create it.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(storage_file))
        page = await context.new_page()

        home_url = await goto_home(page, url_override=url_override, extra_wait_ms=extra_wait_ms)
        print(f"Opened {home_url}")

        airlines = await extract_airline_options(page)
        AIRLINE_OUTPUT.write_text(json.dumps(airlines, indent=2))
        print(f"Wrote {len(airlines)} airlines to {AIRLINE_OUTPUT}")

        if airport_term:
            data = await fetch_airport_picker(
                page=page,
                context=context,
                term=airport_term,
                url_base=home_url,
                csrf_override=csrf_override,
            )
            print(
                f"Fetched airport picker results for term '{airport_term}' "
                f"and wrote to {AIRPORT_PICKER_OUTPUT}"
            )

        if sample_origin_query:
            results = await capture_origin_lookup(page, sample_origin_query)
            if results:
                print(
                    f"Captured {len(results)} lookup response(s) for query '{sample_origin_query}' "
                    f"to {ORIGIN_LOOKUP_OUTPUT}"
                )
            else:
                print("No lookup responses captured; selectors/keywords may need adjustment.")

        await context.close()
        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape airline dropdown options and sample origin lookup traffic."
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window.",
    )
    parser.add_argument(
        "--origin-query",
        default=None,
        help="If provided, type this into the Origin field and capture lookup responses.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Override the myIDTravel home URL if automatic discovery fails.",
    )
    parser.add_argument(
        "--extra-wait-ms",
        type=int,
        default=0,
        help="Add extra wait time (ms) after navigation to allow slow hydration.",
    )
    parser.add_argument(
        "--airport-term",
        default=None,
        help="If provided, call the airportPicker endpoint with this search term and save the response.",
    )
    parser.add_argument(
        "--csrf",
        default=None,
        help="Override CSRF token for airportPicker (otherwise discovered from cookies).",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    await run(
        headless=not args.headed,
        sample_origin_query=args.origin_query,
        url_override=args.url,
        extra_wait_ms=args.extra_wait_ms,
        airport_term=args.airport_term,
        csrf_override=args.csrf,
    )


if __name__ == "__main__":
    asyncio.run(main())
