import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

import config

load_dotenv()

def read_input(path: str) -> Dict[str, Any]:
    input_path = Path(path)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")
    data = json.loads(input_path.read_text())
    # Required fields
    for key in ["origin", "destination", "departure"]:
        if not data.get(key):
            raise SystemExit(f"Missing required field '{key}' in {input_path}")
    return data


async def perform_login(context, headless: bool, screenshot: str | None):
    username = os.getenv("UAL_USERNAME")
    password = os.getenv("UAL_PASSWORD")
    if not username or not password:
        raise SystemExit("Set UAL_USERNAME and UAL_PASSWORD in your environment before running.")

    page = await context.new_page()
    await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
    await page.fill("#username", username)
    await page.fill("#password", password)
    await page.click("input[type=submit][value='Login']")

    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=15000)
    except PlaywrightTimeout:
        pass

    await page.wait_for_load_state("networkidle")
    if screenshot:
        await page.screenshot(path=screenshot, full_page=True)

    return page


async def goto_home(page) -> str:
    for url in config.BASE_URLS:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1200)
            if await page.locator("div.styles_flightScheduleContent__GDxe9").first.is_visible():
                return page.url
            await page.wait_for_timeout(2000)
            if await page.locator("div.styles_flightScheduleContent__GDxe9").first.is_visible():
                return page.url
        except Exception:
            continue
    raise RuntimeError("Could not reach the flight schedule page with the current auth state.")


async def type_and_select_autocomplete(page, selector: str, value: str) -> None:
    field = page.locator(selector).first
    await field.click()
    await field.fill("")
    await field.type(value, delay=50)
    option = page.locator('[role="option"]', has_text=value).first
    try:
        await option.wait_for(timeout=4000)
        await option.click()
    except PlaywrightTimeout:
        await field.press("Enter")


async def select_react_select(page, selector: str, value: str) -> None:
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


async def close_modal_if_present(page) -> None:
    # Try a few common close buttons.
    selectors = [
        "[aria-label='Close']",
        "button:has-text('Close')",
        "button:has-text('close')",
        ".modal [data-testid='close'], .modal .close",
    ]
    for sel in selectors:
        btn = page.locator(sel).first
        if await btn.count():
            try:
                await btn.click()
                return
            except Exception:
                continue


async def submit_form_and_capture(page, output_path: Path) -> None:
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

    submit_btn = page.locator(config.SUBMIT_SELECTOR).first
    await submit_btn.click()

    try:
        response = await asyncio.wait_for(flightschedule_future, timeout=20000)
        try:
            data = await response.json()
        except Exception:
            data = await response.text()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data))
        print(f"Saved flightschedule response to {output_path}")
    except asyncio.TimeoutError:
        print("Timed out waiting for flightschedule response; no JSON saved.")
    except Exception as exc:
        print(f"Error capturing flightschedule response: {exc}")


async def fill_form_from_input(page, input_data: Dict[str, Any]) -> None:
    # home_url = await goto_home(page)
    # print(f"Opened {home_url}")
    print("Successful login")

    # Click "New Flight" first, then close modal if it appears.
    new_flight_btn = page.locator(config.NEW_FLIGHT_SELECTOR).first
    if await new_flight_btn.count():
        await new_flight_btn.click()
        await page.wait_for_timeout(2000)
        await close_modal_if_present(page)

    # schedule_section = page.locator("div.styles_flightScheduleContent__GDxe9").first
    # await schedule_section.scroll_into_view_if_needed()

    await type_and_select_autocomplete(page, config.ORIGIN_SELECTOR, input_data["origin"])
    await type_and_select_autocomplete(page, config.DEST_SELECTOR, input_data["destination"])

    date_input = page.locator(config.DATE_SELECTOR).first
    await date_input.click()
    await date_input.fill("")
    await date_input.type(input_data["departure"])
    await date_input.press("Enter")

    airline = input_data.get("airline", "")
    if airline:
        await select_react_select(page, config.AIRLINE_SELECTOR, airline)

    travel_status = input_data.get("travel_status", "")
    if travel_status:
        await select_react_select(page, config.TRAVEL_STATUS_SELECTOR, travel_status)

    time_val = input_data.get("time", "")
    if time_val:
        time_input = page.locator(config.TIME_SELECTOR).first
        await time_input.click()
        await time_input.fill("")
        await time_input.type(time_val)
        await time_input.press("Enter")

    class_val = input_data.get("class", "")
    if class_val:
        class_input = page.locator(config.CLASS_SELECTOR).first
        await class_input.click()
        await class_input.fill("")
        await class_input.type(class_val)
        await class_input.press("Enter")

    await page.wait_for_timeout(500)

    # Save under a consistent flightschedule filename (independent of flight_type input).
    output_path = config.FLIGHTSCHEDULE_OUTPUT
    await submit_form_and_capture(page, output_path)

    await page.wait_for_timeout(1000)


async def run(headless: bool, screenshot: str | None, input_path: str) -> None:
    input_data = read_input(input_path)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()

        page = await perform_login(context, headless=headless, screenshot=screenshot)
        await context.storage_state(path="auth_state.json")

        await fill_form_from_input(page, input_data)

        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Login and fill flight form using input.json values.")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode.")
    parser.add_argument("--screenshot", default="", help="Optional path to save login screenshot.")
    parser.add_argument("--input", default="input.json", help="Path to input JSON file.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    screenshot = args.screenshot or None
    await run(headless=not args.headed, screenshot=screenshot, input_path=args.input)


if __name__ == "__main__":
    asyncio.run(main())
