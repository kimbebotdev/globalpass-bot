import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

import config
from bots.myidtravel_bot import read_input

load_dotenv()
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

_notify_callback: Optional[Callable[[str], Awaitable[None]]] = None


def set_notifier(callback: Optional[Callable[[str], Awaitable[None]]]) -> None:
    global _notify_callback
    _notify_callback = callback


async def _notify_message(message: str) -> None:
    if _notify_callback:
        try:
            await _notify_callback(message)
        except Exception:
            pass


LOGIN_URL = "https://stafftraveler.app/login"
STEALTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


async def _first_locator(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            return locator.first
    return None


async def _wait_for_first_locator(page, selectors: Iterable[str], timeout_ms: int = 10000, poll_ms: int = 200):
    start = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
        locator = await _first_locator(page, list(selectors))
        if locator:
            return locator
        await page.wait_for_timeout(poll_ms)
    return None


async def _dismiss_banners(page) -> None:
    buttons = [
        page.get_by_role("button", name=re.compile("accept|agree|got it|okay", re.I)),
        page.get_by_role("button", name=re.compile("close", re.I)),
    ]
    for btn in buttons:
        try:
            if await btn.count():
                await btn.click()
                await page.wait_for_timeout(200)
        except Exception:
            continue


async def _fill_autosuggest_field(page, trigger_selector: str, value: str) -> None:
    if not value:
        return
    trigger = page.locator(trigger_selector).first
    if not await trigger.count():
        return
    try:
        await trigger.scroll_into_view_if_needed()
        await trigger.click(force=True)
    except Exception:
        pass
    input_box = page.locator(config.STAFF_AUTOSUGGEST_INPUT).last
    try:
        await input_box.wait_for(state="visible", timeout=4000)
    except Exception:
        return
    try:
        await input_box.fill("")
        await input_box.type(value, delay=500)
        await input_box.press("Enter")
    except Exception:
        pass
    await page.wait_for_timeout(300)


async def _scrape_results(page) -> list[dict]:
    results = []
    containers = page.locator(config.STAFF_RESULTS_CONTAINER)
    try:
        await containers.first.wait_for(state="visible", timeout=8000)
    except Exception:
        return results

    count = await containers.count()
    for idx in range(count):
        group = containers.nth(idx)
        date_text = ""
        day_text = ""
        header = group.locator(".css-1vdvsal")
        if await header.count():
            parts = [t.strip() for t in await header.locator("p").all_text_contents()]
            if parts:
                date_text = parts[0]
            if len(parts) > 1:
                day_text = parts[1]

        flights = []
        flight_cards = group.locator(".css-1yt60yy")
        card_count = await flight_cards.count()
        for j in range(card_count):
            card = flight_cards.nth(j)
            airline = ""
            airline_nodes = await card.locator(".css-zvlevn").all_text_contents()
            if airline_nodes:
                airline = airline_nodes[0].strip()
            if not airline:
                alt_nodes = await card.locator("img[alt]").all_inner_texts()
                if alt_nodes:
                    airline = alt_nodes[0].strip()

            flight_number = ""
            flight_nodes = await card.locator(".css-1nthn72").all_text_contents()
            if flight_nodes:
                flight_number = flight_nodes[0].strip()

            aircraft = ""
            aircraft_nodes = await card.locator(".css-15x0uos .chakra-text").all_text_contents()
            if aircraft_nodes:
                aircraft = aircraft_nodes[0].strip()

            duration = ""
            duration_nodes = await card.locator(".css-phz870 p").all_text_contents()
            if duration_nodes:
                duration = duration_nodes[-1].strip()

            airports = await card.locator(".css-wib9zn p").all_text_contents()
            origin = airports[0].strip() if len(airports) > 0 else ""
            destination = airports[1].strip() if len(airports) > 1 else ""

            times = await card.locator(".css-1g1rqrm p").all_text_contents()
            time_str = " - ".join([t.strip() for t in times if t.strip()]) if times else ""

            flights.append(
                {
                    "airlines": airline,
                    "aircraft": aircraft,
                    "airline_flight_number": flight_number,
                    "origin": origin,
                    "destination": destination,
                    "time": time_str,
                    "duration": duration,
                }
            )

        results.append(
            {
                "flight_date": date_text,
                "day": day_text,
                "flight_details": flights,
            }
        )

    return results

async def _pick_date_from_calendar(page, field_selector: str, date_str: str) -> None:

    await close_date_selection_ui(page)

    if not date_str:
        return
    try:
        target = datetime.strptime(date_str, "%m/%d/%Y")
    except Exception:
        await _set_value_direct(page, field_selector, date_str)
        return

    field = page.locator(field_selector).first
    if not await field.count():
        return
    try:
        await field.scroll_into_view_if_needed()
        await field.click(force=True)
    except Exception:
        pass

    calendar = page.locator(".react-calendar").first
    try:
        await calendar.wait_for(state="visible", timeout=4000)
    except Exception:
        await _set_value_direct(page, field_selector, date_str)
        return

    def _parse_label(text: str):
        try:
            return datetime.strptime(text.strip(), "%B %Y")
        except Exception:
            return None

    try:
        label_loc = calendar.locator(".react-calendar__navigation__label__labelText--from").first
        label_text = (await label_loc.inner_text()).strip()
        current_month = _parse_label(label_text)
        if current_month:
            delta_months = (target.year - current_month.year) * 12 + (target.month - current_month.month)
            steps = min(abs(delta_months), 18)
            if delta_months != 0:
                next_arrow = calendar.locator(".react-calendar__navigation__next-button").first
                prev_arrow = calendar.locator(".react-calendar__navigation__prev-button").first
                for _ in range(steps):
                    if delta_months > 0 and await next_arrow.count():
                        await next_arrow.click()
                    elif delta_months < 0 and await prev_arrow.count():
                        await prev_arrow.click()
                    await page.wait_for_timeout(120)
    except Exception:
        pass

    exact_label = f"{target.strftime('%B')} {target.day}, {target.year}"
    day_btn = calendar.locator(f'button[aria-label="{exact_label}"]').first
    if not await day_btn.count():
        day_btn = calendar.locator(
            "button.react-calendar__tile",
            has=page.locator("abbr", has_text=str(target.day)),
        ).filter(has_text=str(target.day)).first

    clicked = False
    try:
        if await day_btn.count():
            await day_btn.click()
            clicked = True
    except Exception:
        clicked = False

    if not clicked:
        await _set_value_direct(page, field_selector, date_str)
        return

    await close_date_selection_ui(page)
    await page.wait_for_timeout(250)


async def _set_value_direct(page, selector: str, value: str) -> None:
    await close_date_selection_ui(page)

    if not value:
        return
    
    field = page.locator(selector).first
    if not await field.count():
        return
    try:
        # await field.scroll_into_view_if_needed()
        await field.click()
    except Exception:
        pass

    # Close date selection
    await close_date_selection_ui(page)

    # Clear existing text.
    for combo in ("Meta+A", "Control+A"):
        try:
            await field.press(combo)
            await field.press("Backspace")
            break
        except Exception:
            continue

    try:
        await page.keyboard.type(value, delay=90)
        await field.press("Tab")
    except Exception:
        pass
    await page.wait_for_timeout(200)
    try:
        current = await field.input_value()
        if current.strip() != value.strip():
            await field.evaluate(
                "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); }",
                value,
            )
    except Exception:
        pass
    await page.wait_for_timeout(200)


async def close_date_selection_ui(page):
    close_date_button = page.locator(config.STAFF_DATE_DONE_BUTTON)
    if await close_date_button.count():
        try:
            await close_date_button.click()
            await page.wait_for_timeout(500)
        
        except Exception:
            pass

# End of close_date_selection_ui


async def perform_flight_search(page, input_data: dict) -> None:
    trips = input_data.get("trips") or []
    itinerary = input_data.get("itinerary") or []
    if not trips:
        raise SystemExit("Input must include at least one trip to search flights.")

    await page.wait_for_selector(config.STAFF_FLIGHT_CONTAINER)

    for idx, trip in enumerate(trips):
        containers = page.locator(config.STAFF_FLIGHT_CONTAINER)
        if await containers.count() <= idx:
            add_btn = page.locator(config.STAFF_ADD_FLIGHT_BUTTON).first
            if await add_btn.count():
                await add_btn.click()
                await page.wait_for_timeout(500)
                await page.wait_for_selector(config.STAFF_FLIGHT_CONTAINER)

        origin = trip.get("origin", "")
        dest = trip.get("destination", "")
        leg = itinerary[idx] if idx < len(itinerary) else {}

        await _fill_autosuggest_field(page, config.STAFF_FROM_TEMPLATE.format(index=idx), origin)
        await _fill_autosuggest_field(page, config.STAFF_TO_TEMPLATE.format(index=idx), dest)
        await _pick_date_from_calendar(page, config.STAFF_DATE_TEMPLATE.format(index=idx), leg.get("date", ""))
        await page.wait_for_timeout(400)

        if idx < len(trips) - 1:
            add_btn = page.locator(config.STAFF_ADD_FLIGHT_BUTTON).first
            if await add_btn.count():
                await add_btn.click()
                await page.wait_for_timeout(500)
                await page.wait_for_selector(config.STAFF_FLIGHT_CONTAINER)

    search_btn = page.locator(config.STAFF_SEARCH_BUTTON).first
    if not await search_btn.count():
        raise SystemExit("Could not find Search flights button.")
    await search_btn.click()
    await page.wait_for_timeout(1500)

    results = await _scrape_results(page)
    if config.STAFF_RESULTS_OUTPUT:
        config.STAFF_RESULTS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        config.STAFF_RESULTS_OUTPUT.write_text(json.dumps(results, indent=2))


async def perform_stafftraveller_login(
    headless: bool,
    screenshot: str | None,
    storage_path: str,
    input_data: dict | None = None,
) -> None:
    logger.info("Starting StaffTraveler login headless=%s", headless)
    username = os.getenv("ST_USERNAME")
    password = os.getenv("ST_PASSWORD")
    if not username or not password:
        raise SystemExit("Set ST_USERNAME and ST_PASSWORD in your environment before running.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=STEALTH_UA,
            viewport={"width": 1280, "height": 900},
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        page = await context.new_page()

        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)  # allow client-side scripts to mount
        await _dismiss_banners(page)

        email_field = await _wait_for_first_locator(
            page,
            [
                'input[name="email"]',
                'input[type="email"]',
                'input[autocomplete="email"]',
                'input[placeholder*="email" i]',
                'input[id*="email" i]',
            ],
            timeout_ms=12000,
        )

        if not email_field:
            raise SystemExit("Could not find email address field")
        
        await email_field.click()
        await email_field.fill("")
        await email_field.type(username)

        btn_continue = await _wait_for_first_locator(
            page,
            [
                "#continue",
                'button[type="button"]'
            ],
            timeout_ms=6000,
        )
        if btn_continue:
            await btn_continue.click()
        await page.wait_for_timeout(1200)

        password_field = await _wait_for_first_locator(
            page,
            [
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]',
                'input[placeholder*="password" i]',
                'input[id*="password" i]',
            ],
            timeout_ms=12000,
        )

        if not email_field or not password_field:
            raise SystemExit("Could not find password field")

        await password_field.click()
        await password_field.fill("")
        await password_field.type(password)

        login_button = await _first_locator(
            page,
            [
                '#login-with-password'
            ],
        )
        if login_button:
            await login_button.click()
        else:
            await password_field.press("Enter")

        try:
            await page.wait_for_url(lambda url: "login" not in url, timeout=5000)
        except PlaywrightTimeout:
            pass

        # Need to revisit this URL to remove the login wrapper
        await page.goto("https://stafftraveler.app")

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            # Fall back to a short wait if the page keeps streaming.
            await page.wait_for_timeout(1500)
        await page.wait_for_timeout(1200)

        if "login" in page.url.lower():
            error_text = ""
            possible_errors = page.locator(
                ".error, .alert, [data-testid*='error' i], [role='alert'], [class*='error' i]"
            )
            if await possible_errors.count():
                try:
                    error_text = (await possible_errors.first.inner_text()).strip()
                except Exception:
                    error_text = ""
            raise SystemExit(
                f"Login appears to have failed (still on login page).{f' Error: {error_text}' if error_text else ''}"
            )

        if input_data:
            logger.info("Performing StaffTraveler flight search")
            await perform_flight_search(page, input_data)

        if screenshot:
            await page.screenshot(path=screenshot, full_page=True)

        await context.storage_state(path=storage_path)
        logger.info("StaffTraveler login/search complete; storage saved to %s", storage_path)
        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate StaffTraveler login with Playwright.")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode for debugging.",
    )
    parser.add_argument(
        "--screenshot",
        default="",
        help="Optional path to save a post-login screenshot.",
    )
    parser.add_argument(
        "--storage-state",
        default="stafftraveller_auth_state.json",
        help="Path to write the authenticated storage state.",
    )
    parser.add_argument(
        "--input",
        default="input.json",
        help="Path to input JSON for the flight search.",
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Skip flight search after login.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    screenshot = args.screenshot or None
    input_data = None if args.login_only else read_input(args.input)
    await perform_stafftraveller_login(
        headless=not args.headed,
        screenshot=screenshot,
        storage_path=args.storage_state,
        input_data=input_data,
    )


if __name__ == "__main__":
    asyncio.run(main())
