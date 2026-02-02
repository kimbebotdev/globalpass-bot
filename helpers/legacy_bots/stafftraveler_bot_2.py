import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

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

_notify_callback: Callable[[str], Awaitable[None]] | None = None


def set_notifier(callback: Callable[[str], Awaitable[None]] | None) -> None:
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
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
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


async def _expand_all_flight_cards(page) -> None:
    sections = page.locator("div.css-1xjwpnn")
    count = await sections.count()
    for idx in range(count):
        section = sections.nth(idx)
        button = section.locator("button.chakra-button.css-srlxmk").first
        if await button.count():
            try:
                await button.click()
                await page.wait_for_timeout(300)
            except Exception:
                pass


async def _scrape_all_flights(page, flight_number: str | None = None) -> list[dict]:
    results = []
    target_number = (flight_number or "").replace(" ", "").upper()
    groups = page.locator("div.css-ceo8c9")
    group_count = await groups.count()
    for i in range(group_count):
        group = groups.nth(i)
        cards = group.locator(":scope > div.css-0")
        card_count = await cards.count()
        for j in range(card_count):
            card = cards.nth(j)

            airline_task = card.locator("img[alt]").first.get_attribute("alt")
            flight_number_task = card.locator("p.chakra-text.css-1m9eb7l").first.inner_text()
            date_task = card.locator("p.chakra-text.css-1tzeee1").first.inner_text()
            day_task = card.locator("p.chakra-text.css-zjgxih").first.inner_text()
            origin_task = card.locator("p.chakra-text.css-2plwd4").first.inner_text()
            destination_task = card.locator(
                "div.chakra-stack.css-2wo2bk > div.css-0:nth-of-type(2) p"
            ).first.inner_text()
            details_task = card.locator("div.chakra-stack.css-emtrgo p.chakra-text.css-epvm6").all_inner_texts()
            times_task = card.locator("div.chakra-stack.css-1y1yqzu p.chakra-text.css-epvm6").all_inner_texts()

            (
                airline_raw,
                flight_number_raw,
                date_raw,
                day_raw,
                origin_raw,
                destination_raw,
                details_raw,
                times_raw,
            ) = await asyncio.gather(
                airline_task,
                flight_number_task,
                date_task,
                day_task,
                origin_task,
                destination_task,
                details_task,
                times_task,
                return_exceptions=True,
            )

            def safe_strip(val) -> str:
                """Returns a stripped string if valid, otherwise an empty string."""
                return val.strip() if isinstance(val, str) else ""

            # Apply to your fields
            airline = safe_strip(airline_raw)
            flight_number = safe_strip(flight_number_raw)
            date_text = safe_strip(date_raw)
            day_text = safe_strip(day_raw)
            origin = safe_strip(origin_raw)
            destination = safe_strip(destination_raw)

            aircraft = ""
            duration = ""
            details = details_raw if isinstance(details_raw, list) else []
            details = [t.strip() for t in details if isinstance(t, str)]
            if details:
                aircraft = details[0]
            if len(details) > 1:
                duration = details[1]

            depart_time = ""
            arrive_time = ""
            times = times_raw if isinstance(times_raw, list) else []
            times = [t.strip() for t in times if isinstance(t, str)]
            if times:
                depart_time = times[0]
            if len(times) > 1:
                arrive_time = times[1]

            seats = {
                "first": "",
                "bus": "",
                "eco": "",
                "eco_plus": "",
                "non_rev": "",
            }
            try:
                seat_data = await card.locator("div.css-1j8r2w0 div.chakra-stat__group.css-1mpfoc5 > div").evaluate_all(
                    """blocks => blocks.map(block => {
                        const valueNode = block.querySelector(
                          "dd.chakra-stat__number.css-ia7pv7, dd.chakra-stat__number.css-pwhod9, dd.chakra-stat__number.css-1axeus7"
                        );
                        const labelNode = block.querySelector(
                          "dd.chakra-stat__help-text.css-dw3d13, dd.chakra-stat__help-text.css-1dyk2dh"
                        );
                        return {
                          value: valueNode ? valueNode.textContent.trim() : "",
                          label: labelNode ? labelNode.textContent.trim() : "",
                        };
                      })"""  # noqa: E501
                )
            except Exception:
                seat_data = []

            for seat in seat_data:
                label_norm = seat.get("label", "").replace(" ", "").upper()
                value_text = seat.get("value", "")
                if label_norm in {"FIRST"}:
                    seats["first"] = value_text
                elif label_norm in {"BUS"}:
                    seats["bus"] = value_text
                elif label_norm in {"ECO"}:
                    seats["eco"] = value_text
                elif label_norm in {"ECO+", "ECOPLUS"}:
                    seats["eco_plus"] = value_text
                elif label_norm in {"NON-REV", "NONREV"}:
                    seats["non_rev"] = value_text

            flight_record = {
                "airline": airline,
                "flight_number": flight_number,
                "date": date_text,
                "day": day_text,
                "origin": origin,
                "destination": destination,
                "aircraft": aircraft,
                "duration": duration,
                "departure_time": depart_time,
                "arrival_time": arrive_time,
                "seats": seats,
            }
            if target_number:
                scraped_number = flight_number.replace(" ", "").upper()
                if scraped_number == target_number:
                    return [flight_record]
            else:
                results.append(flight_record)
    return results


async def perform_stafftraveller_login(
    headless: bool,
    screenshot: str | None,
    output_path: Path | None = None,
    input_data: dict | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    logger.info("Starting StaffTraveler login headless=%s", headless)
    username = os.getenv("ST_USERNAME")
    password = os.getenv("ST_PASSWORD")
    if not username or not password:
        raise SystemExit("Set ST_USERNAME and ST_PASSWORD in your environment before running.")

    async with async_playwright() as p:
        if progress_cb:
            await progress_cb(5, "launching")
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
        if progress_cb:
            await progress_cb(15, "loaded")
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
            ["#continue", 'button[type="button"]'],
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
            ["#login-with-password"],
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

        await _expand_all_flight_cards(page)
        if progress_cb:
            await progress_cb(70, "results loaded")
        raw_number = (input_data or {}).get("flight_number")
        target_number = raw_number.upper() if isinstance(raw_number, str) else raw_number
        results = await _scrape_all_flights(page, flight_number=target_number)
        if progress_cb:
            await progress_cb(85, "parsed")
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(results, indent=2))
            logger.info("StaffTraveler results written to %s", output_path)

        if screenshot:
            try:
                screenshot_path = Path(screenshot)
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot_path), full_page=True)
                if progress_cb:
                    await progress_cb(95, "screenshot")
            except Exception:
                pass

        await browser.close()
        if progress_cb:
            await progress_cb(100, "done")
        return results
    return []


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
        "--input",
        default="",
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

    input_data = read_input(args.input or "input.json") if args.input else {}
    await perform_stafftraveller_login(headless=not args.headed, screenshot=screenshot, input_data=input_data)


if __name__ == "__main__":
    asyncio.run(main())
