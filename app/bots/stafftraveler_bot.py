import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from app import config
from app.bots.myidtravel_bot import read_input

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


async def _scrape_results(page, selectable_numbers: set[str] | None = None) -> list[dict]:
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
        cards_to_click: list[int] = []
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
                if selectable_numbers:
                    variants = _flight_number_variants(flight_number)
                    if any(variant in selectable_numbers for variant in variants):
                        cards_to_click.append(j)

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

        for offset, idx_to_click in enumerate(cards_to_click):
            try:
                await flight_cards.nth(max(0, idx_to_click - offset)).click()
                await page.wait_for_timeout(250)
            except Exception:
                continue

    return results


async def close_date_selection_ui(page):
    close_date_button = page.locator(config.STAFF_DATE_DONE_BUTTON)
    if await close_date_button.count():
        try:
            await close_date_button.click()
            await page.wait_for_timeout(500)
        except Exception:
            pass


async def _set_value_direct(page, selector: str, value: str) -> None:
    await close_date_selection_ui(page)

    if not value:
        return

    field = page.locator(selector).first
    if not await field.count():
        return
    try:
        await field.click()
    except Exception:
        pass

    await close_date_selection_ui(page)

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
                """
                    (el, val) => { el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    "el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); }
                """,
                value,
            )
    except Exception:
        pass
    await page.wait_for_timeout(200)


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
    day_btn = (
        calendar.locator(f'button[aria-label="{exact_label}"]')
        .or_(calendar.locator("button.react-calendar__tile", has=page.locator(f'abbr[aria-label="{exact_label}"]')))
        .first
    )

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


async def perform_flight_search(
    page,
    input_data: dict,
    output_path: Path | None = None,
    selectable_numbers: set[str] | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
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
    if progress_cb:
        await progress_cb(50, "submitted")

    results = await _scrape_results(page, selectable_numbers=selectable_numbers)
    if progress_cb:
        await progress_cb(85, "parsed")
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, indent=2))
    return results


async def perform_stafftraveller_login(
    headless: bool,
    screenshot: str | None,
    output_path: Path | None = None,
    input_data: dict | None = None,
    username: str | None = None,
    password: str | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    logger.info("Starting StaffTraveler login headless=%s", headless)
    username = username or os.getenv("ST_USERNAME")
    password = password or os.getenv("ST_PASSWORD")
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


async def perform_stafftraveller_search(
    headless: bool,
    screenshot: str | None,
    input_data: dict | None = None,
    output_path: Path | None = None,
    selectable_numbers: set[str] | None = None,
    username: str | None = None,
    password: str | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
    request_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    logger.info("Starting StaffTraveler search headless=%s", headless)
    username = username or os.getenv("ST_USERNAME")
    password = password or os.getenv("ST_PASSWORD")
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
        await page.wait_for_timeout(800)
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

        await page.goto("https://stafftraveler.app")

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
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

        results: list[dict[str, Any]] = []
        if input_data:
            logger.info("Performing StaffTraveler flight search")
            if progress_cb:
                await progress_cb(35, "form filled")
            results = await perform_flight_search(
                page,
                input_data,
                output_path=output_path,
                selectable_numbers=selectable_numbers,
                progress_cb=progress_cb,
            )
            request_btn = page.locator("button.chakra-button.css-en50w4").first
            if await request_btn.count():
                try:
                    if await request_btn.is_disabled():
                        await _notify_message("StaffTraveler: request button disabled (monthly limit reached).")
                        if request_state is not None:
                            request_state.update({"posted": False, "reason": "disabled"})
                    else:
                        await request_btn.click()
                        await page.wait_for_timeout(10000)
                        if request_state is not None:
                            request_state.update({"posted": True, "reason": None})
                except Exception:
                    if request_state is not None and "posted" not in request_state:
                        request_state.update({"posted": False, "reason": "error"})
                    pass
            elif request_state is not None:
                request_state.update({"posted": False, "reason": "missing"})
            try:
                await page.reload()
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await page.wait_for_timeout(1500)

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


def _normalize_flight_number(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _flight_number_variants(value: str | None) -> set[str]:
    normalized = _normalize_flight_number(value)
    if not normalized:
        return set()
    match = re.match(r"([A-Z]+)(\d+)", normalized)
    if not match:
        return {normalized}
    prefix, number = match.groups()
    trimmed = str(int(number)) if number.isdigit() else number
    return {normalized, f"{prefix}{trimmed}"}


def _map_staff_seats(seats: dict[str, Any]) -> dict[str, str]:
    return {
        "first": seats.get("first", ""),
        "bus": seats.get("bus", ""),
        "eco": seats.get("eco", ""),
        "ecoplus": seats.get("eco_plus", ""),
        "nonrev": seats.get("non_rev", ""),
    }


async def update_selectable_flights(
    headless: bool,
    selectable_payload: list[dict[str, Any]],
    username: str,
    password: str,
    screenshot: str | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    input_data = {"flight_number": ""}
    results = await perform_stafftraveller_login(
        headless=headless,
        screenshot=screenshot,
        output_path=None,
        input_data=input_data,
        username=username,
        password=password,
        progress_cb=progress_cb,
    )

    staff_by_number: dict[str, dict[str, Any]] = {}
    for item in results:
        for variant in _flight_number_variants(item.get("flight_number")):
            staff_by_number[variant] = item

    for routing in selectable_payload:
        if not isinstance(routing, dict):
            continue
        flights = routing.get("flights") or []
        if not isinstance(flights, list):
            continue
        for flight in flights:
            if not isinstance(flight, dict):
                continue
            variants = _flight_number_variants(flight.get("flight_number"))
            if not variants:
                continue
            staff_match = next((staff_by_number.get(v) for v in variants if v in staff_by_number), None)
            if not staff_match:
                continue
            seats = flight.get("seats") or {}
            seats["stafftraveler"] = _map_staff_seats(staff_match.get("seats", {}))
            flight["seats"] = seats

    return selectable_payload


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
