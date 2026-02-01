import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

import config

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

def read_input(path: str) -> Dict[str, Any]:
    input_path = Path(path)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")
    data = json.loads(input_path.read_text())
    # Required trips
    trips = data.get("trips", [])
    if not trips or not isinstance(trips, list):
        raise SystemExit("Input must include a 'trips' array with at least one object.")
    for idx, trip in enumerate(trips):
        for key in ["origin", "destination"]:
            if not trip.get(key):
                raise SystemExit(f"Missing required field '{key}' in trips[{idx}] in {input_path}")
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
        logger.warning("multiple-legs not fully supported yet; using first itinerary leg only.")
    else:
        raise SystemExit(f"Unsupported flight_type '{flight_type}'. Use one-way, round-trip, or multiple-legs.")
    return data


async def perform_login(
    context,
    headless: bool,
    screenshot: str | None,
    username: str | None = None,
    password: str | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
):
    username = username or os.getenv("UAL_USERNAME")
    password = password or os.getenv("UAL_PASSWORD")

    if not username or not password:
        await _notify_message("MyIDTravel: no Username/Password found.")
        raise SystemExit("Set UAL_USERNAME and UAL_PASSWORD in your environment before running.")

    page = await context.new_page()
    await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
    if progress_cb:
        await progress_cb(15, "loaded")
    await page.fill("#username", username)
    await page.wait_for_timeout(500)
    await page.fill("#password", password)
    await page.click("input[type=submit][value='Login']")

    # Wait for navigation to complete
    await page.wait_for_load_state("networkidle", timeout=15000)

    # Check if we're still on a login page or if error is visible
    current_url = page.url
    error_loc = page.locator("div.login-error").first

    # Check for error message
    is_error_visible = await error_loc.is_visible()

    # Check if URL still contains "login" or if error is present
    if "login" in current_url.lower() or is_error_visible:
        error_text = ""
        if is_error_visible:
            try:
                error_text = (await error_loc.inner_text()).strip()
            except Exception:
                pass

        message = f"MyIDTravel: login failed. {error_text}" if error_text else "MyIDTravel: login failed."
        await _notify_message(message)
        raise SystemExit(message)

    # Login succeeded
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


async def type_and_select_in_container(container_or_field, selector: str, value: str) -> None:
    """
    Type/select into an autocomplete inside a container or directly into a provided field locator.
    """
    field = container_or_field
    # If we were given a container (e.g., Page or Locator that is not the input), try to find the input inside.
    try:
        is_input_like = hasattr(container_or_field, "fill") and hasattr(container_or_field, "press") and not hasattr(container_or_field, "goto")
    except Exception:
        is_input_like = False
    if not is_input_like and hasattr(container_or_field, "locator"):
        try:
            field = container_or_field.locator(selector).first
        except Exception:
            field = container_or_field
    # Fallback by placeholder if nothing found.
    try:
        if not await field.count() and hasattr(container_or_field, "locator"):
            field = container_or_field.locator(f'input[placeholder*="{value}" i]').first
    except Exception:
        pass
    if not await field.count():
        return
    await field.click()
    await field.fill("")
    await field.type(value, delay=50)
    page_obj = getattr(field, "page", None) or getattr(container_or_field, "page", None)
    if not page_obj:
        return
    option = page_obj.locator('[role="option"]', has_text=value).first
    try:
        await option.wait_for(timeout=4000)
        await option.click()
    except PlaywrightTimeout:
        await field.press("Enter")


async def select_react_select(page, selector: str, value: str, placeholder_hint: str | None = None) -> None:
    input_el = page.locator(selector).first
    if not await input_el.count() and placeholder_hint:
        input_el = page.locator(f'input[placeholder*="{placeholder_hint}" i], input[aria-label*="{placeholder_hint}" i]').first
    if not await input_el.count():
        return
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


async def _fill_time_input(handle, value: str) -> bool:
    """
    Fill a time input by clicking it and selecting from the dropdown menu.
    Follows the same pattern as _fill_input but handles time dropdowns.

    Args:
        locator: Playwright locator for the time input element
        value: Time string in format "HH:MM" (e.g., "14:00", "09:00", "00:00")

    Returns:
        bool: True if time was successfully selected, False otherwise
    """
    if not value:
        return False

    if not await handle.count():
        return False

    page_obj = getattr(handle, "page", None)

    if not page_obj:
        logger.warning("No page object found for time input")
        return False

    try:
        # Parse the time value
        parts = value.strip().split(":")
        if len(parts) != 2:
            logger.warning("Invalid time format for time input: %s", value)
            return False

        hour = parts[0].strip().lstrip("0") or "0"
        minute = parts[1].strip().lstrip("0") or "0"

        # Format variations to try matching in dropdown
        formats_to_try = [
            f"{hour.zfill(2)} :{minute.zfill(2)}",
            f"{hour.zfill(2)}:{minute.zfill(2)}",
            f"{hour} :{minute.zfill(2)}",
            f"{hour}:{minute.zfill(2)}",
        ]

        # Click the input to open dropdown
        try:
            await handle.scroll_into_view_if_needed()
            await handle.click()
            await page_obj.wait_for_timeout(400)
        except Exception:
            try:
                await handle.click(force=True)
                await page_obj.wait_for_timeout(400)
            except Exception as e:
                logger.debug("Could not click time input: %s", e)
                return False

        # Wait for dropdown to appear
        try:
            await page_obj.wait_for_selector('div[role="menu"][id*="dropdown-menu"].show', timeout=2000, state="visible")
        except Exception:
            logger.debug("Dropdown menu did not appear for time input")
            # Try fallback: direct input
            return await _fill_time_fallback(handle, value)

        # Find the dropdown menu - it should be visible now
        dropdown = page_obj.locator('div[role="menu"][id*="dropdown-menu"].show').first
        if not await dropdown.count() or not await dropdown.is_visible():
            logger.debug("Dropdown menu not visible for time input")
            return await _fill_time_fallback(handle, value)

        # Find all menu items
        menu_items = dropdown.locator('button[role="menuitem"]')
        items_count = await menu_items.count()

        # Try to find matching time
        selected = False
        for format_str in formats_to_try:
            if selected:
                break

            for i in range(items_count):
                try:
                    item = menu_items.nth(i)
                    item_text = (await item.inner_text()).strip()

                    if item_text == format_str:
                        try:
                            await item.scroll_into_view_if_needed()
                            await page_obj.wait_for_timeout(100)
                            await item.click()
                            await page_obj.wait_for_timeout(300)
                            selected = True
                            break
                        except Exception:
                            try:
                                await item.click(force=True)
                                await page_obj.wait_for_timeout(300)
                                selected = True
                                break
                            except Exception:
                                continue
                except Exception:
                    continue

        if not selected:
            logger.debug("No matching time option found for %s", value)
            return await _fill_time_fallback(handle, value)

        return True

    except Exception as e:
        logger.error("Error in _fill_time_input for value %s: %s", value, e, exc_info=True)
        # Fallback to direct input
        return await _fill_time_fallback(handle, value)


async def _fill_time_fallback(handle, value: str) -> bool:
    """
    Fallback method: directly set the time input value if dropdown fails.
    """
    try:
        await handle.fill("")
        await handle.type(value)
        await handle.press("Enter")
    except Exception:
        pass

    # Force-set via JS if typing didn't work
    try:
        current = await handle.input_value()
        if current.strip() != value.strip():
            await handle.evaluate(
                "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                value,
            )
        return True
    except Exception as e:
        logger.debug("Fallback time fill failed: %s", e)
        return False


async def fill_leg_fields(container, date_val: str, time_val: str, class_val: str) -> None:
    """
    Fill date, time, and class fields within a specific container (for round-trip duplicate groups).
    """
    if date_val:
        date_input = _input_locator(container, config.DATE_SELECTOR.lstrip("#"), placeholder_hint="Date")
        await _fill_input(date_input, date_val)
    if time_val:
        time_input = _input_locator(container, config.TIME_SELECTOR.lstrip("#"), name_val="Time", placeholder_hint="Time")
        await _fill_time_input(time_input, time_val)
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


async def wait_for_modal_or_travellers(page, timeout_ms: int = 8000) -> None:
    """
    Wait for a traveller modal (or any common modal close button) to appear before continuing.
    Helps avoid racing ahead while the modal is still mounting.
    """
    selectors = [
        config.TRAVELLER_ITEM_SELECTOR,
        "[aria-label='Close']",
        "button:has-text('Close')",
        ".modal [data-testid='close'], .modal .close",
    ]
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=timeout_ms)
            return
        except PlaywrightTimeout:
            continue
        except Exception:
            continue


async def apply_traveller_selection(page, travellers: list[dict]) -> None:
    """Check/uncheck travellers in the modal based on input list and set salutation when provided."""
    if not travellers:
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

        await page.wait_for_timeout(500)

async def add_travel_partners(page, partners: list[dict]) -> None:
    """Add travel partners using the modal form."""
    if not partners:
        return

    add_btn = page.locator(config.ADD_TRAVEL_PARTNER).first
    if not await add_btn.count():
        return

    for idx, partner in enumerate(partners):
        try:
            await add_btn.click()
            await page.wait_for_timeout(300)
        except Exception:
            continue

        containers = page.locator("div.travellerDiv")
        count = await containers.count()
        container = containers.nth(count - 1) if count else page

        # Type: Adult/Child
        p_type = (partner.get("type") or "").strip()
        if p_type:
            type_sel = container.locator("select#type")
            if await type_sel.count():
                try:
                    await type_sel.select_option(label=p_type)
                except Exception:
                    await type_sel.select_option(value=p_type)

        # Salutation (Adult only) or DOB (Child)
        sal = (partner.get("salutations") or partner.get("salutation") or "").strip()
        dob = (partner.get("dob") or "").strip()
        if p_type.lower() == "child":
            if dob:
                dob_input = container.locator("input#date-picker")
                if await dob_input.count():
                    await dob_input.click()
                    await dob_input.fill("")
                    await dob_input.type(dob)
                    await dob_input.press("Enter")
        else:
            if sal:
                sal_sel = container.locator("select#salutations")
                if await sal_sel.count():
                    try:
                        await sal_sel.select_option(label=sal)
                    except Exception:
                        await sal_sel.select_option(value=sal)

        first_name = (partner.get("first_name") or "").strip()
        if first_name:
            name_input = container.locator("input#name")
            if await name_input.count():
                await name_input.fill(first_name)

        last_name = (partner.get("last_name") or "").strip()
        if last_name:
            last_input = container.locator("input#lastName")
            if await last_input.count():
                await last_input.fill(last_name)

        add_partner_btn = container.locator(config.TRAVEL_PARTNER_ADD).first
        if not await add_partner_btn.count():
            add_partner_btn = page.locator(config.TRAVEL_PARTNER_ADD).first
        if await add_partner_btn.count():
            try:
                await add_partner_btn.click()
            except Exception:
                pass

        await page.wait_for_timeout(300)


async def fill_multiple_legs(page, trips: list[dict], itinerary: list[dict]) -> None:
    """
    Fill multiple legs using trips (origin/destination) and itinerary (date/time/class).
    Clicks Add Flight to create additional leg sections if needed.
    """
    if not trips:
        return

    add_btn = page.locator(config.ADD_FLIGHT_BUTTON).first

    airline_containers = page.locator(config.AIRLINE_REASON_CONTAINER)
    leg_containers = page.locator(config.LEG_SELECTOR)
    origin_inputs = page.locator(config.ORIGIN_SELECTOR)
    dest_inputs = page.locator(config.DEST_SELECTOR)

    for idx, trip in enumerate(trips):
        # Ensure containers exist for this leg; if not, click Add Flight and refresh.
        while True:
            air_count = await airline_containers.count()
            leg_count = await leg_containers.count()
            if air_count > idx and leg_count > idx:
                break
            if not await add_btn.count():
                break
            try:
                await add_btn.scroll_into_view_if_needed()
                await add_btn.click()
                await page.wait_for_timeout(600)
            except Exception:
                break
            airline_containers = page.locator(config.AIRLINE_REASON_CONTAINER)
            leg_containers = page.locator(config.LEG_SELECTOR)
            origin_inputs = page.locator(config.ORIGIN_SELECTOR)
            dest_inputs = page.locator(config.DEST_SELECTOR)

        origin = trip.get("origin", "")
        dest = trip.get("destination", "")
        if origin:
            origin_field = origin_inputs.nth(idx) if await origin_inputs.count() > idx else page.locator(config.ORIGIN_SELECTOR).first
            await type_and_select_in_container(origin_field, config.ORIGIN_SELECTOR, origin)
        if dest:
            dest_field = dest_inputs.nth(idx) if await dest_inputs.count() > idx else page.locator(config.DEST_SELECTOR).first
            await type_and_select_in_container(dest_field, config.DEST_SELECTOR, dest)

        leg_data = itinerary[idx] if idx < len(itinerary) else {}
        leg_container = leg_containers.nth(idx) if await leg_containers.count() > idx else page
        await fill_leg_fields(
            leg_container,
            leg_data.get("date", ""),
            leg_data.get("time", ""),
            leg_data.get("class", ""),
        )

async def click_traveller_continue(page) -> None:
    """Click the traveller modal continue button if present."""
    continue_button = page.locator(config.TRAVELLER_CONTINUE_BUTTON).first
    if await continue_button.count():
        try:
            await continue_button.scroll_into_view_if_needed()
            await continue_button.click(force=True)
            await page.wait_for_timeout(500)
        except Exception:
            pass


async def submit_form_and_capture(
    page,
    output_path: Path | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> Any | None:
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
    if progress_cb:
        await progress_cb(50, "submitted")

    try:
        response = await asyncio.wait_for(flightschedule_future, timeout=20000)
        try:
            data = await response.json()
        except Exception:
            data = await response.text()
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data))
            logger.info("Saved flightschedule response to %s", output_path)
        if progress_cb:
            await progress_cb(85, "parsed")
        if isinstance(data, dict):
            routings = data.get("routings", [])
            filtered_routings: list[dict[str, Any]] = []
            for routing in routings:
                if not isinstance(routing, dict):
                    continue
                flights = routing.get("flights", [])
                if not isinstance(flights, list):
                    flights = []
                selectable_flights = [
                    flight for flight in flights if isinstance(flight, dict) and flight.get("selectable") is True
                ]
                trimmed = dict(routing)
                trimmed["flights"] = selectable_flights
                filtered_routings.append(trimmed)
            has_flights = any(routing.get("flights") for routing in filtered_routings)
            if not has_flights:
                await _notify_message("MyIDTravel: no selectable flights found for the search.")
            return filtered_routings
        if isinstance(data, list):
            filtered_routings = []
            for routing in data:
                if not isinstance(routing, dict):
                    continue
                flights = routing.get("flights", [])
                if not isinstance(flights, list):
                    flights = []
                selectable_flights = [
                    flight for flight in flights if isinstance(flight, dict) and flight.get("selectable") is True
                ]
                trimmed = dict(routing)
                trimmed["flights"] = selectable_flights
                filtered_routings.append(trimmed)
            return filtered_routings
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for flightschedule response; no JSON saved.")
        await _notify_message("MyIDTravel: Timed out waiting for flight schedule response; no JSON saved.")
    except Exception as exc:
        logger.error("Error capturing flightschedule response: %s", exc, exc_info=True)
        await _notify_message("MyIDTravel: Error capturing flight schedule response.")
    return None


async def fill_form_from_input(
    page,
    input_data: Dict[str, Any],
    output_path: Path | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> Any | None:
    logger.info("Successful login; filling form")

    try:
        await page.wait_for_selector(config.TRAVEL_STATUS_SELECTOR, timeout=15000)
    except Exception:
        try:
            await page.wait_for_selector(
                "input[placeholder*='Travel' i], input[aria-label*='Travel' i]",
                timeout=15000,
            )
        except Exception:
            pass

    # Click "New Flight" first, then close modal if it appears.
    new_flight_btn = page.locator(config.NEW_FLIGHT_SELECTOR).first
    if await new_flight_btn.count():
        await new_flight_btn.click()
        await wait_for_modal_or_travellers(page, timeout_ms=8000)

    travellers = input_data.get("traveller", [])
    await apply_traveller_selection(page, travellers)
    travel_partners = input_data.get("travel_partner", [])
    await add_travel_partners(page, travel_partners)
    await click_traveller_continue(page)
    await close_modal_if_present(page)

    # Select flight type tab
    flight_type = input_data.get("flight_type", "one-way").lower()
    await select_flight_type(page, flight_type)

    only_nonstop_flights = input_data.get("nonstop_flights", "")
    if only_nonstop_flights:
        await trigger_nonstop_flights(page, config.NONSTOP_FLIGHTS_CONTAINER, only_nonstop_flights)

    airline = input_data.get("airline", "")
    if airline:
        await select_react_select(page, config.AIRLINE_SELECTOR, airline, placeholder_hint="Airline")

    travel_status = input_data.get("travel_status", "")
    if travel_status:
        try:
            selector_count = await page.locator(config.TRAVEL_STATUS_SELECTOR).count()
            fallback_count = await page.locator(
                "input[placeholder*='Travel' i], input[aria-label*='Travel' i]"
            ).count()
            logger.info(
                "Travel status locator counts: selector=%s fallback=%s",
                selector_count,
                fallback_count,
            )
            await page.screenshot(path="debug_travel_status.png", full_page=True)
        except Exception as exc:
            logger.info("Travel status debug failed: %s", exc)
        await select_react_select(page, config.TRAVEL_STATUS_SELECTOR, travel_status, placeholder_hint="Travel status")

    trips = input_data.get("trips", [])
    trip0 = trips[0] if trips else {}

    itinerary = input_data.get("itinerary", [])
    if flight_type == "multiple-legs":
        await fill_multiple_legs(page, trips, itinerary)
    else:
        departure_leg = itinerary[0] if itinerary else {}
        return_leg = itinerary[1] if flight_type == "round-trip" and len(itinerary) > 1 else None

        await type_and_select_autocomplete(page, config.ORIGIN_SELECTOR, trip0.get("origin", ""))
        await type_and_select_autocomplete(page, config.DEST_SELECTOR, trip0.get("destination", ""))

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
    if progress_cb:
        await progress_cb(35, "form filled")

    # Save under a consistent flightschedule filename (independent of flight_type input).
    return await submit_form_and_capture(page, output_path, progress_cb=progress_cb)

    await page.wait_for_timeout(1000)


async def run(
    headless: bool,
    screenshot: str | None,
    input_path: str | None,
    final_screenshot: str | None = None,
    input_data: dict[str, Any] | None = None,
    output_path: Path | None = None,
    username: str | None = None,
    password: str | None = None,
    progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
) -> Any | None:
    resolved_input = input_data or read_input(input_path or "input.json")

    async with async_playwright() as p:
        if progress_cb:
            await progress_cb(5, "launching")
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()

        page = await perform_login(
            context,
            headless=headless,
            screenshot=screenshot,
            username=username,
            password=password,
            progress_cb=progress_cb,
        )
        data = await fill_form_from_input(page, resolved_input, output_path=output_path, progress_cb=progress_cb)

        if final_screenshot:
            try:
                screenshot_path = Path(final_screenshot)
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot_path), full_page=True)
                if progress_cb:
                    await progress_cb(95, "screenshot")
            except Exception:
                pass

        await browser.close()
        if progress_cb:
            await progress_cb(100, "done")
        return data
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Login and fill flight form using input.json values.")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode.")
    parser.add_argument("--screenshot", default="", help="Optional path to save login screenshot.")
    parser.add_argument("--input", default="", help="Path to input JSON file.")
    parser.add_argument("--output", default="", help="Optional path to save flight schedule JSON.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    screenshot = args.screenshot or None
    input_path = args.input or None
    output_path = Path(args.output) if args.output else None
    await run(
        headless=not args.headed,
        screenshot=screenshot,
        input_path=input_path,
        output_path=output_path,
    )


if __name__ == "__main__":
    asyncio.run(main())
