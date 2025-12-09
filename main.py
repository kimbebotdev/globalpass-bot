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
    for key in ["origin", "destination"]:
        if not data.get(key):
            raise SystemExit(f"Missing required field '{key}' in {input_path}")
    # Itinerary validation
    flight_type = data.get("flight_type", "one-way").lower()
    itinerary = data.get("itinerary", [])
    if flight_type == "one-way":
        if not itinerary or len(itinerary) < 1:
            raise SystemExit("itinerary must contain at least one entry for one-way trips.")
    elif flight_type == "round-trip":
        if not itinerary or len(itinerary) < 2:
            raise SystemExit("itinerary must contain two entries (departure, return) for round-trip.")
    elif flight_type == "multiple-legs":
        # Not implemented yet; allow but warn
        print("Warning: multiple-legs not fully supported yet; using first itinerary leg only.")
    else:
        raise SystemExit(f"Unsupported flight_type '{flight_type}'. Use one-way, round-trip, or multiple-legs.")
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

async def trigger_nonstop_flights(page, selector: str, value: str) -> None:
    container = page.locator(selector).first
    switch = container.locator(".switch-handle").first
    is_active = switch.get_attribute("active")

    if is_active:
        await switch.click()

async def select_flight_type(page, flight_type: str) -> None:
    """Click the flight type tab based on input (one-way, round-trip, multiple-legs)."""
    mapping = {
        "one-way": "One Way",
        "round-trip": "Round Trip",
        "multiple-legs": "Multiple Legs",
    }
    label = mapping.get(flight_type.lower())
    if not label:
        return
    tab = page.locator(f"{config.FLIGHT_TYPE} li", has_text=label).first
    if await tab.count():
        await tab.click()


async def fill_text_input(page, selector: str, value: str, placeholder_hint: str | None = None) -> bool:
    """
    Fill a simple text input; returns True if something was filled.
    Optionally uses a placeholder hint if the primary selector is missing.
    """
    field = page.locator(selector).first
    if not await field.count() and placeholder_hint:
        field = page.locator(f'input[placeholder*="{placeholder_hint}" i]').first
    if await field.count():
        await field.click()
        await field.fill("")
        await field.type(value)
        await field.press("Enter")
        return True
    return False


def _input_locator(container, field_id: str, name_val: str | None = None, placeholder_hint: str | None = None):
    """
    Build a locator that tries id, then name, then placeholder within a container.
    """
    parts = [f"input#{field_id}"]
    if name_val:
        parts.append(f"input[name='{name_val}']")
    if placeholder_hint:
        parts.append(f'input[placeholder*="{placeholder_hint}" i]')
    selector = ", ".join(parts)
    return container.locator(selector)


async def _fill_input(locator, value: str) -> bool:
    """
    Try to fill a given locator, falling back to JS set if typing fails.
    """
    if not await locator.count():
        return False
    handle = locator.first
    try:
        await handle.scroll_into_view_if_needed()
        await handle.click(force=True)
    except Exception:
        pass
    try:
        await handle.fill("")
        await handle.type(value)
        await handle.press("Enter")
    except Exception:
        pass
    # If the value didn't stick, force-set via JS.
    try:
        current = await handle.input_value()
        if current.strip() != value.strip():
            await handle.evaluate(
                "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                value,
            )
    except Exception:
        pass
    return True


async def fill_leg_fields(container, date_val: str, time_val: str, class_val: str) -> None:
    """
    Fill date, time, and class fields within a specific container (for round-trip duplicate groups).
    """
    if date_val:
        date_input = _input_locator(container, config.DATE_SELECTOR.lstrip("#"), placeholder_hint="Date")
        await _fill_input(date_input, date_val)
    if time_val:
        time_input = _input_locator(container, config.TIME_SELECTOR.lstrip("#"), name_val="Time", placeholder_hint="Time")
        await _fill_input(time_input, time_val)
    if class_val:
        class_input = _input_locator(container, config.CLASS_SELECTOR.lstrip("#"), name_val="Class", placeholder_hint="Class")
        await _fill_input(class_input, class_val)


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


async def apply_traveller_selection(page, travellers: list[dict]) -> None:
    """Check/uncheck travellers in the modal based on input list and set salutation when provided."""
    if not travellers:
        await close_modal_if_present(page)
        return

    # Wait briefly for modal to render.
    await page.wait_for_timeout(500)
    items = page.locator(config.TRAVELLER_ITEM_SELECTOR)
    count = await items.count()
    if count == 0:
        await close_modal_if_present(page)
        return

    # Build a lookup of desired checks by lowercased name.
    desired = {}
    for trav in travellers:
        name = (trav.get("name") or "").strip()
        if not name:
            continue
        desired[name.lower()] = {
            "checked": bool(trav.get("checked", False)),
            "salutation": (trav.get("salutation") or "").strip().upper(),
        }

    for idx in range(count):
        item = items.nth(idx)
        name_text = (await item.locator(config.TRAVELLER_NAME_SELECTOR).inner_text()).strip()
        name_key = name_text.lower()

        if name_key not in desired:
            continue

        desired_state = desired[name_key]
        should_check = desired_state["checked"]
        checkbox = item.locator(config.TRAVELLER_CHECKBOX_SELECTOR).first

        if not await checkbox.count():
            continue

        try:
            checked = await checkbox.is_checked()
        except Exception:
            checked = False

        if should_check and not checked:
            await checkbox.check(force=True)
        elif not should_check and checked:
            await checkbox.uncheck(force=True)

        # Apply salutation if provided (MR/MS) using the dropdown sibling to this traveller item.
        salutation = desired_state.get("salutation")
        if salutation in {"MR", "MS"}:
            parent = item.locator("xpath=..")
            dropdowns = parent.locator(config.TRAVELLER_SALUTATION_TOGGLE)
            dropdown = dropdowns.nth(idx) if await dropdowns.count() > idx else dropdowns.first
            if not await dropdown.count():
                dropdown = page.locator(config.TRAVELLER_SALUTATION_TOGGLE).nth(idx) if await page.locator(config.TRAVELLER_SALUTATION_TOGGLE).count() > idx else page.locator(config.TRAVELLER_SALUTATION_TOGGLE).first
            if await dropdown.count():
                try:
                    await dropdown.click()
                    await page.wait_for_timeout(150)
                    # Scope menu to the same parent block when possible.
                    menu = parent.locator(".styles_LabelWithDropdown_DropdownMenu__UXOnK.dropdown-menu.show").first
                    if not await menu.count():
                        menus = page.locator(".styles_LabelWithDropdown_DropdownMenu__UXOnK.dropdown-menu.show")
                        menu = menus.last
                    option = menu.locator("button.dropdown-item", has_text=salutation).first
                    if await option.count():
                        await option.click()
                except Exception:
                    # Ignore salutation failures and continue.
                    pass

    # Add travel partner

    continue_button = page.locator(config.TRAVELLER_CONTINUE_BUTTON)
    if continue_button.count():
        continue_button.click()
        await page.wait_for_timeout(500)

    await close_modal_if_present(page)


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
    print("Successful login")

    # Click "New Flight" first, then close modal if it appears.
    new_flight_btn = page.locator(config.NEW_FLIGHT_SELECTOR).first
    if await new_flight_btn.count():
        await new_flight_btn.click()
        await page.wait_for_timeout(3000)

    travellers = input_data.get("traveller", [])
    await apply_traveller_selection(page, travellers)

    # Select flight type tab
    flight_type = input_data.get("flight_type", "one-way").lower()
    await select_flight_type(page, flight_type)

    only_nonstop_flights = input_data.get("nonstop_flights", "")
    if only_nonstop_flights:
        await trigger_nonstop_flights(page, config.NONSTOP_FLIGHTS_CONTAINER, only_nonstop_flights)

    airline = input_data.get("airline", "")
    if airline:
        await select_react_select(page, config.AIRLINE_SELECTOR, airline)

    travel_status = input_data.get("travel_status", "")
    if travel_status:
        await select_react_select(page, config.TRAVEL_STATUS_SELECTOR, travel_status)

    itinerary = input_data.get("itinerary", [])
    departure_leg = itinerary[0] if itinerary else {}
    return_leg = itinerary[1] if flight_type == "round-trip" and len(itinerary) > 1 else None

    await type_and_select_autocomplete(page, config.ORIGIN_SELECTOR, input_data["origin"])
    await type_and_select_autocomplete(page, config.DEST_SELECTOR, input_data["destination"])

    # Containers for date/time/class groups (one per leg for round-trip).
    leg_containers = page.locator(config.LEG_SELECTOR)
    # Departure leg
    if departure_leg:
        container = leg_containers.nth(0) if await leg_containers.count() else page
        await fill_leg_fields(
            container,
            departure_leg.get("date", ""),
            departure_leg.get("time", ""),
            departure_leg.get("class", ""),
        )

    # Return leg (round-trip) uses second container if present, otherwise falls back to page.
    if return_leg:
        container = leg_containers.nth(1) if await leg_containers.count() > 1 else page
        await fill_leg_fields(
            container,
            return_leg.get("date", ""),
            return_leg.get("time", ""),
            return_leg.get("class", ""),
        )

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
