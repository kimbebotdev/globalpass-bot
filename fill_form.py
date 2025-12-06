import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

BASE_URLS = [
    "https://myidtravel-united.ual.com/myidtravel/",
    "https://www.myidtravel.com/myidtravel/",
    "https://swa.myidtravel.com/myidtravel/",
    "https://myidtravel.com/myidtravel/",
]

# IDs provided by the user/context
ORIGIN_SELECTOR = "#Origin"
DEST_SELECTOR = "#Destination"
DATE_SELECTOR = "#date-picker"
AIRLINE_SELECTOR = "#input-airline"
SUBMIT_SELECTOR = "#find-flights"
FLIGHTSCHEDULE_OUTPUT = Path("json/one-way-flightschedule.json")

async def goto_home(page) -> str:
    """Navigate to a reachable myIDTravel home URL using stored auth."""
    for url in BASE_URLS:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1200)
            if await page.locator("div.styles_flightScheduleContent__GDxe9").first.is_visible():
                return page.url
            # Give the app a bit longer to hydrate.
            await page.wait_for_timeout(2000)
            if await page.locator("div.styles_flightScheduleContent__GDxe9").first.is_visible():
                return page.url
        except Exception:
            continue
    raise RuntimeError("Could not reach the flight schedule page with the current auth state.")


async def type_and_select_autocomplete(page, selector: str, value: str) -> None:
    """Type into a lookup input and choose the matching suggestion."""
    field = page.locator(selector).first
    await field.click()
    await field.fill("")
    await field.type(value, delay=50)
    # Wait for suggestions and pick the first matching entry.
    option = page.locator('[role="option"]', has_text=value).first
    try:
        await option.wait_for(timeout=4000)
        await option.click()
    except PlaywrightTimeout:
        # Fallback: press Enter to accept the top suggestion.
        await field.press("Enter")


async def select_react_select(page, selector: str, value: str) -> None:
    """Handle React-select style dropdown (e.g., airline input)."""
    input_el = page.locator(selector).first
    await input_el.click()
    await input_el.fill("")
    await input_el.type(value, delay=50)
    option = page.locator('[role="option"]', has_text=value).first
    try:
        await option.wait_for(timeout=4000)
        await option.click()
    except PlaywrightTimeout:
        await input_el.press("Enter")


async def fill_form(
    headless: bool,
    origin: str,
    destination: str,
    departure: str,
    airline: str,
) -> None:
    storage_file = Path("auth_state.json")
    if not storage_file.exists():
        raise SystemExit("auth_state.json not found. Run main.py first to create it.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(storage_file))
        page = await context.new_page()

        home_url = await goto_home(page)
        print(f"Opened {home_url}")

        schedule_section = page.locator("div.styles_flightScheduleContent__GDxe9").first
        await schedule_section.scroll_into_view_if_needed()

        await type_and_select_autocomplete(page, ORIGIN_SELECTOR, origin)
        await type_and_select_autocomplete(page, DEST_SELECTOR, destination)

        date_input = page.locator(DATE_SELECTOR).first
        submit_btn = page.locator(SUBMIT_SELECTOR).first

        await date_input.click()
        await date_input.fill("")
        await date_input.type(departure)
        await date_input.press("Enter")

        # If airline selection is needed, uncomment:
        # await select_react_select(page, AIRLINE_SELECTOR, airline)

        # Keep the page open briefly (useful in headed mode).
        await page.wait_for_timeout(500)

        # Prepare listener for the flightschedule POST before clicking submit.
        loop = asyncio.get_event_loop()
        flightschedule_future: asyncio.Future = loop.create_future()

        def handle_response(resp):
            try:
                if resp.request.method.lower() == "post" and "flightschedule" in resp.url.lower():
                    if not flightschedule_future.done():
                        flightschedule_future.set_result(resp)
            except Exception:
                return

        page.on("response", handle_response)

        await submit_btn.click()

        # Capture the flightschedule POST response and persist JSON.
        try:
            response = await asyncio.wait_for(flightschedule_future, timeout=20000)
            try:
                data = await response.json()
            except Exception:
                data = await response.text()
            FLIGHTSCHEDULE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            FLIGHTSCHEDULE_OUTPUT.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data))
            print(f"Saved flightschedule response to {FLIGHTSCHEDULE_OUTPUT}")
        except asyncio.TimeoutError:
            print("Timed out waiting for flightschedule response; no JSON saved.")
        except Exception as exc:
            print(f"Error capturing flightschedule response: {exc}")

        await page.wait_for_timeout(1000)
        await context.storage_state(path="auth_state.json")
        await browser.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fill the flight form using .env values.")
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window.")
    args = parser.parse_args()

    origin = os.getenv("INPUT_ORIGIN", "")
    destination = os.getenv("INPUT_DESTINATION", "")
    departure = os.getenv("INPUT_DEPARTURE", "")
    airline = os.getenv("INPUT_AIRLINE", "")
    headless = not args.headed if "HEADLESS" not in os.environ else os.getenv("HEADLESS", "true").lower() != "false"

    missing = [name for name, val in [
        ("INPUT_ORIGIN", origin),
        ("INPUT_DESTINATION", destination),
        ("INPUT_DEPARTURE", departure),
        ("INPUT_AIRLINE", airline),
    ] if not val]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

    await fill_form(
        headless=headless,
        origin=origin,
        destination=destination,
        departure=departure,
        airline=airline,
    )


if __name__ == "__main__":
    asyncio.run(main())
