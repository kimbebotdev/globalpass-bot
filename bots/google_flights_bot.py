import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

import config
from bots.myidtravel_bot import read_input


# Output path for captured Google Flights results.
OUTPUT_PATH = Path("json/google_flights_results.json")
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


def _parse_date(date_str: str) -> Optional[datetime]:
    """
    Try to parse a date string in common formats used by input.json.
    """
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _iso_date(date_str: str) -> str:
    dt = _parse_date(date_str)
    return dt.strftime("%Y-%m-%d") if dt else date_str


def build_legs(input_data: Dict[str, Any]) -> tuple[List[Dict[str, Any]], str]:
    """
    Normalize the input.json format into legs suitable for Google Flights form.
    """
    trips = input_data.get("trips") or []
    itinerary = input_data.get("itinerary") or []
    flight_type = (input_data.get("flight_type") or "one-way").lower()

    if not trips:
        raise SystemExit("Input must include at least one trip.")

    legs: List[Dict[str, Any]] = []

    if flight_type == "one-way":
        leg_data = itinerary[0] if itinerary else {}
        legs.append(
            {
                "origin": trips[0].get("origin", ""),
                "destination": trips[0].get("destination", ""),
                "depart_date": leg_data.get("date", ""),
                "return_date": "",
                "type": "one-way",
            }
        )
    elif flight_type == "round-trip":
        depart_leg = itinerary[0] if itinerary else {}
        return_leg = itinerary[1] if len(itinerary) > 1 else {}
        legs.append(
            {
                "origin": trips[0].get("origin", ""),
                "destination": trips[0].get("destination", ""),
                "depart_date": depart_leg.get("date", ""),
                "return_date": return_leg.get("date", ""),
                "type": "round-trip",
            }
        )
    else:
        # multiple-legs -> each item is one leg in order
        for idx, trip in enumerate(trips):
            leg_data = itinerary[idx] if idx < len(itinerary) else {}
            legs.append(
                {
                    "origin": trip.get("origin", ""),
                    "destination": trip.get("destination", ""),
                    "depart_date": leg_data.get("date", ""),
                    "return_date": "",
                    "type": "one-way",
                }
            )

    return legs, flight_type


async def _handle_cookie_banner(page) -> None:
    buttons = [
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('Accept')",
        "[aria-label='Accept all']",
    ]
    for sel in buttons:
        btn = page.locator(sel).first
        if await btn.count():
            try:
                await btn.click()
                await page.wait_for_timeout(300)
                break
            except Exception:
                continue


async def _apply_nonstop_filter(page) -> None:
    """
    Try to enable the Nonstop filter if the toolbar is available.
    """
    stops_button = page.get_by_role("button", name=re.compile("Stops", re.I))
    if not await stops_button.count():
        return

    try:
        await stops_button.click()
        await page.wait_for_timeout(250)
    except Exception:
        return

    nonstop_option = page.get_by_role("radio", name=re.compile("Nonstop", re.I))
    if await nonstop_option.count():
        try:
            await nonstop_option.click()
        except Exception:
            pass

    done_button = page.get_by_role("button", name=re.compile("Done", re.I))
    if await done_button.count():
        try:
            await done_button.click()
        except Exception:
            pass

    await page.wait_for_timeout(400)


def _extract_price(text: str) -> str:
    match = re.search(r"([€$£]\s?[\d,]+)", text)
    return match.group(1) if match else ""


def _extract_times(text: str) -> tuple[str, str]:
    matches = re.findall(r"\d{1,2}:\d{2}\s?(?:AM|PM)?", text, flags=re.IGNORECASE)
    depart = matches[0] if matches else ""
    arrive = matches[1] if len(matches) > 1 else ""
    return depart, arrive


def _extract_duration(text: str) -> str:
    match = re.search(r"(\d+ ?h(?: ?\d+m)?)", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d+ ?hr(?: ?\d+ ?min)?)", text)
    return match.group(1) if match else ""


def _extract_stops(text: str) -> str:
    lower = text.lower()
    if "nonstop" in lower:
        return "Nonstop"
    match = re.search(r"(\d+)\s+stop", lower)
    if match:
        return f"{match.group(1)} stop"
    match = re.search(r"(\d+)\s+stops", lower)
    if match:
        return f"{match.group(1)} stops"
    return ""


async def _scrape_section(listitems, limit: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not listitems:
        return results
    count = min(await listitems.count(), limit)
    for idx in range(count):
        card = listitems.nth(idx)
        aria_summary = (await card.get_attribute("aria-label")) or ""
        try:
            text_summary = (await card.inner_text()).strip()
        except Exception:
            text_summary = aria_summary

        airline = ""
        airline_loc = card.locator(".sSHqwe, .Ir0Voe")
        if await airline_loc.count():
            try:
                airline = (await airline_loc.first.inner_text()).strip()
            except Exception:
                airline = ""

        if not airline and aria_summary:
            airline = aria_summary.split(",")[0].replace("Select", "").strip()

        price = _extract_price(aria_summary or text_summary)
        duration = _extract_duration(aria_summary or text_summary)
        depart_time, arrive_time = _extract_times(aria_summary or text_summary)
        stops = _extract_stops(aria_summary or text_summary)

        results.append(
            {
                "airline": airline,
                "price": price,
                "duration": duration,
                "depart_time": depart_time,
                "arrival_time": arrive_time,
                "stops": stops,
                "summary": aria_summary or text_summary,
            }
        )
    return results


async def _scrape_section(items, limit: int) -> List[Dict[str, Any]]:
    """
    Scrape flight information from a list of flight card items.
    """
    results: List[Dict[str, Any]] = []
    count = min(await items.count(), limit)

    for idx in range(count):
        card = items.nth(idx)
        try:
            flight_data = await _extract_flight_data(card)
            if flight_data:
                results.append(flight_data)
        except Exception as e:
            logger.error("Error scraping card %s: %s", idx, e, exc_info=True)
            continue

    return results


async def _extract_flight_data(card) -> Dict[str, Any]:
    """
    Extract flight details from a single flight card element.
    Based on the structure in Sample LI.html
    """
    flight_data = {
        "airline": "",
        "price": "",
        "duration": "",
        "depart_time": "",
        "arrival_time": "",
        "stops": "",
        "origin": "",
        "destination": "",
        "emissions": "",
        "summary": ""
    }

    try:
        # Get aria-label from the main clickable div for summary
        main_link = card.locator('div[role="link"]').first
        if await main_link.count():
            aria_label = await main_link.get_attribute("aria-label")
            if aria_label:
                flight_data["summary"] = aria_label

        # Extract airline name
        # Look for airline name in the Ir0Voe div > sSHqwe span
        airline_loc = card.locator('.Ir0Voe .sSHqwe').first
        if await airline_loc.count():
            try:
                flight_data["airline"] = (await airline_loc.inner_text()).strip()
            except Exception:
                pass

        # If airline not found, try alternate location
        if not flight_data["airline"]:
            airline_alt = card.locator('.h1fkLb span').first
            if await airline_alt.count():
                try:
                    flight_data["airline"] = (await airline_alt.inner_text()).strip()
                except Exception:
                    pass

        # Extract departure time
        depart_time_loc = card.locator('.wtdjmc').first
        if await depart_time_loc.count():
            try:
                flight_data["depart_time"] = (await depart_time_loc.get_attribute("aria-label") or "").replace("Departure time: ", "").replace(".", "").strip()
            except Exception:
                try:
                    flight_data["depart_time"] = (await depart_time_loc.inner_text()).strip()
                except Exception:
                    pass

        # Extract arrival time
        arrival_time_loc = card.locator('.XWcVob').first
        if await arrival_time_loc.count():
            try:
                flight_data["arrival_time"] = (await arrival_time_loc.get_attribute("aria-label") or "").replace("Arrival time: ", "").replace(".", "").strip()
            except Exception:
                try:
                    flight_data["arrival_time"] = (await arrival_time_loc.inner_text()).strip()
                except Exception:
                    pass

        # Extract origin airport code
        origin_loc = card.locator('.G2WY5c').first
        if await origin_loc.count():
            try:
                flight_data["origin"] = (await origin_loc.inner_text()).strip()
            except Exception:
                pass

        # Extract destination airport code
        dest_loc = card.locator('.c8rWCd').first
        if await dest_loc.count():
            try:
                flight_data["destination"] = (await dest_loc.inner_text()).strip()
            except Exception:
                pass

        # Extract duration and stops information
        # Duration is in aria-label format: "Total duration 13 hr 20 min"
        duration_loc = card.locator('.gvkrdb').first
        if await duration_loc.count():
            try:
                duration_aria = await duration_loc.get_attribute("aria-label")
                if duration_aria:
                    flight_data["duration"] = duration_aria.replace("Total duration ", "").replace(".", "").strip()
                else:
                    flight_data["duration"] = (await duration_loc.inner_text()).strip()
            except Exception:
                pass

        # Extract stops information
        stops_loc = card.locator('.EfT7Ae span').first
        if await stops_loc.count():
            try:
                stops_aria = await stops_loc.get_attribute("aria-label")
                if stops_aria:
                    flight_data["stops"] = stops_aria.replace(" flight.", "").strip()
                else:
                    flight_data["stops"] = (await stops_loc.inner_text()).strip()
            except Exception:
                pass

        # If stops not found, try alternate location
        if not flight_data["stops"]:
            stops_alt = card.locator('.VG3hNb').first
            if await stops_alt.count():
                try:
                    flight_data["stops"] = (await stops_alt.inner_text()).strip()
                except Exception:
                    pass

        # Extract price
        # Price is in the YMlIz FpEdX jLMuyc span with aria-label
        price_loc = card.locator('.YMlIz.FpEdX.jLMuyc span[aria-label]').first
        if await price_loc.count():
            try:
                price_aria = await price_loc.get_attribute("aria-label")
                if price_aria:
                    flight_data["price"] = price_aria.strip()
                else:
                    flight_data["price"] = (await price_loc.inner_text()).strip()
            except Exception:
                pass

        # Extract emissions data
        emissions_loc = card.locator('.AdWm1c.lc3qH').first
        if await emissions_loc.count():
            try:
                flight_data["emissions"] = (await emissions_loc.inner_text()).strip()
            except Exception:
                pass

    except Exception as e:
        logger.error("Error extracting flight data: %s", e, exc_info=True)

    return flight_data


async def _scrape_results(page, limit: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """
    Scrape results grouped into top_flights and other_flights using the tabpanel
    inside the main results container. Falls back to a flat list under 'all' if
    sections are not found.
    """
    top_flights: List[Dict[str, Any]] = []
    other_flights: List[Dict[str, Any]] = []

    # Find visible tabpanel inside the main results container.
    main_panels = page.locator("div.FXkZv[role='main'] div.eQ35Ce div[role='tabpanel']")
    panel = None
    try:
        count = await main_panels.count()
        for i in range(count):
            candidate = main_panels.nth(i)
            try:
                if await candidate.is_visible():
                    panel = candidate
                    break
            except Exception:
                continue
    except Exception:
        panel = None

    if panel:
        # Identify child sections; two of them should contain the lists.
        sections = panel.locator("> div")
        sec_count = await sections.count()
        found_top = None
        found_other = None

        for i in range(sec_count):
            sec = sections.nth(i)
            heading = sec.get_by_role("heading").first
            label = ""
            try:
                if await heading.count():
                    label = (await heading.inner_text()).strip()
            except Exception:
                label = ""

            # Look for list items - try ul > li structure first
            items = sec.locator("ul li.pIav2d")
            if not await items.count():
                items = sec.locator("li.pIav2d")
            if not await items.count():
                items = sec.locator("ul li[role='listitem']")
            if not await items.count():
                items = sec.locator("li[role='listitem']")

            if re.search(r"top flight", label, re.I):
                found_top = items
            elif re.search(r"other flight", label, re.I):
                found_other = items
            elif found_top is None and await items.count():
                found_top = items
            elif found_other is None and await items.count():
                found_other = items

        if found_top:
            logger.info("Found %s top flights", await found_top.count())
            top_flights = await _scrape_section(found_top, limit)
        if found_other:
            logger.info("Found %s other flights", await found_other.count())
            other_flights = await _scrape_section(found_other, limit)

    all_flights: List[Dict[str, Any]] = []
    if not top_flights and not other_flights:
        logger.info("No sections found, trying flat list scrape")
        selectors = [
            "div.FXkZv[role='main'] li.pIav2d",
            "li.pIav2d",
            "div.FXkZv[role='main'] li[role='listitem']",
            "[aria-label*='Results list'] div[role='listitem']",
            "ul[role='listbox'] li[role='listitem']",
            "li[role='listitem']",
            "[aria-label*='Flight result']",
        ]
        cards = None
        for sel in selectors:
            loc = page.locator(sel)
            if await loc.count():
                logger.info("Found %s flights using selector: %s", await loc.count(), sel)
                cards = loc
                break
        if cards:
            all_flights = await _scrape_section(cards, limit)

    return {"top_flights": top_flights, "other_flights": other_flights, "all": all_flights}

def _age_from_dob(dob_str: str) -> Optional[int]:
    if not dob_str:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            born = datetime.strptime(dob_str, fmt).date()
            today = date.today()
            years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            return years
        except Exception:
            continue
    return None


def _compute_passenger_clicks(input_data: Dict[str, Any]) -> Dict[str, int]:
    traveller = input_data.get("traveller") or []
    partners = input_data.get("travel_partner") or []

    adults = max(len(traveller), 1)  # Google defaults to 1 adult
    children = 0
    infant_seat = 0
    infant_lap = 0

    for partner in partners:
        own_seat = partner.get("own_seat", True)
        p_type = (partner.get("type") or "").lower()
        if p_type == "adult":
            if own_seat:
                adults += 1
            continue

        if p_type == "child":
            age = _age_from_dob(partner.get("dob", ""))
            if age is None:
                # Assume child if unknown age and has seat
                if own_seat:
                    children += 1
                continue

            if age < 2:
                if own_seat:
                    infant_seat += 1
                else:
                    infant_lap += 1
            elif 2 <= age <= 11:
                if own_seat:
                    children += 1
            else:
                if own_seat:
                    adults += 1

    # Convert totals to "add" counts (Google already has 1 adult selected)
    add_adults = max(adults - 1, 0)
    return {
        "adult": add_adults,
        "child": children,
        "infant_seat": infant_seat,
        "infant_lap": infant_lap,
    }


async def _wait_for_results(page, timeout_ms: int = 15000) -> None:
    """
    Wait for visible flight listitems to appear in the main results container.
    Helps avoid scraping before the DOM renders (e.g., when running without breakpoints).
    """
    start = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
        # Prefer the main container; fall back to any listitem.
        main_items = page.locator("div.FXkZv[role='main'] li[role='listitem']")
        try:
            count = await main_items.count()
            if count > 0:
                return
        except Exception:
            pass
        generic_items = page.locator("li[role='listitem']")
        try:
            if await generic_items.count():
                return
        except Exception:
            pass
        await page.wait_for_timeout(300)


async def _switch_trip_type(page, desired: str) -> None:
    form = page.locator(config.GF_FORM_CONTAINER).first
    toggle = form.locator(config.GF_TRIP_TYPE_TOGGLE).first
    if not await toggle.count():
        toggle = page.get_by_role("button", name=re.compile("Round trip|One way|Multi-city", re.I))
    if not await toggle.count():
        return
    try:
        await toggle.click()
        await page.wait_for_timeout(200)
    except Exception:
        return

    option_list = form.locator(config.GF_TRIP_TYPE_OPTIONS)
    label_map = {
        "round-trip": "Round trip",
        "one-way": "One way",
        "multiple-legs": "Multi-city",
        "multi-city": "Multi-city",
    }
    label = label_map.get(desired.lower(), desired)
    if await option_list.count():
        for idx in range(await option_list.count()):
            opt = option_list.nth(idx)
            try:
                text = (await opt.inner_text()).strip()
            except Exception:
                text = ""
            if text.lower().startswith(label.lower().split()[0]):
                try:
                    await opt.click()
                except Exception:
                    pass
                break
    else:
        option = page.get_by_role("option", name=re.compile(label, re.I))
        if not await option.count():
            option = page.get_by_role("menuitem", name=re.compile(label, re.I))
        if await option.count():
            try:
                await option.click()
            except Exception:
                pass
    await page.wait_for_timeout(400)


async def _ensure_leg_rows(page, count: int) -> None:
    form = page.locator(config.GF_FORM_CONTAINER).first
    add_btn = form.locator(config.GF_ADD_FLIGHT_BUTTON).first
    if not await add_btn.count():
        add_btn = page.get_by_role("button", name=re.compile("Add flight", re.I))
    for _ in range(6):  # cap to avoid runaway loop
        fields_container = page.locator(config.GF_FIELDS_CONTAINER).first
        if not await fields_container.count():
            fields_container = page
        from_fields = fields_container.locator(config.GF_FROM_INPUT)
        if await from_fields.count() >= count:
            return
        if await add_btn.count():
            try:
                await add_btn.click()
                await page.wait_for_timeout(300)
            except Exception:
                break
        else:
            break


async def _fill_leg_row(page, idx: int, origin: str, destination: str, date_str: str) -> None:
    """Fill a specific leg row in multi-city flight form."""
    # Wait for any "from" input to exist in the page
    try:
        await page.wait_for_selector(config.GF_FROM_INPUT, timeout=8000)
    except Exception:
        pass

    logger.info(f"Attempting to fill leg {idx}: {origin} -> {destination} on {date_str}")

    # Try multiple strategies to locate leg rows
    rows = None

    # Strategy 1: Use configured leg selector across likely containers.
    candidate_containers = [
        page.locator(config.GF_FIELDS_CONTAINER).first,
        page.locator(config.GF_FORM_CONTAINER).first,
        page,
    ]

    for idx_container, container in enumerate(candidate_containers, start=1):
        try:
            if container and await container.count():
                candidate_rows = container.locator(config.GF_LEG_ROW)
            else:
                candidate_rows = page.locator(config.GF_LEG_ROW)
            row_count = await candidate_rows.count()
            logger.info(f"Strategy 1.{idx_container} (GF_LEG_ROW via container {idx_container}): found {row_count} rows")
            if row_count > 0:
                rows = candidate_rows
                break
        except Exception as e:
            logger.debug(f"Strategy 1.{idx_container} error: {e}")
            continue

    if rows and await rows.count() > 0:
        row_count = await rows.count()
        logger.info(f"Found {row_count} rows, filling row {idx}")

        if idx >= row_count:
            logger.warning(f"Row index {idx} exceeds available rows ({row_count})")
            return

        # Get the specific row for this index
        row = rows.nth(idx)

        # Get fields within THIS specific row only
        from_fields = row.locator(config.GF_FROM_INPUT)
        to_fields = row.locator(config.GF_TO_INPUT)
        date_fields = row.locator(config.GF_DEPART_INPUT)

        # Verify we have the fields in this row
        from_count = await from_fields.count()
        to_count = await to_fields.count()
        date_count = await date_fields.count()

        logger.info(f"Row {idx}: from={from_count}, to={to_count}, date={date_count} fields")

        if from_count == 0 or to_count == 0 or date_count == 0:
            logger.error(f"Missing fields in row {idx}")
            return

        # Use first field in this row
        origin_field = from_fields.first
        dest_field = to_fields.first
        date_field = date_fields.first

        # Fill the fields
        await _fill_simple_field(origin_field, origin)
        await _fill_simple_field(dest_field, destination)

        # Date picker
        if date_field and await date_field.count():
            try:
                await date_field.click()
                await date_field.fill("")
                if date_str:
                    await date_field.type(_iso_date(date_str))
                    await date_field.press("Enter")
                    done_btn = page.get_by_role("button", name=re.compile("Done", re.I)).first
                    if await done_btn.count():
                        try:
                            await done_btn.click()
                        except Exception:
                            pass
            except Exception:
                try:
                    await date_field.evaluate(
                        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                        _iso_date(date_str),
                    )
                except Exception:
                    pass

        await page.wait_for_timeout(500)
        logger.info(f"Completed filling row {idx}")
    else:
        logger.error(f"Could not locate rows for leg {idx}")


async def _fill_simple_field(locator, value: str) -> None:
    if not value or not locator:
        return
    target = locator.first if hasattr(locator, "first") else locator
    page_obj = getattr(target, "page", None)

    if not page_obj:
        logger.warning("No page object found for field")
        return

    try:
        await target.wait_for(state="visible", timeout=5000)
    except Exception:
        pass

    try:
        if hasattr(target, "count") and not await target.count():
            return
    except Exception:
        return

    try:
        await target.scroll_into_view_if_needed()

        # Click the field to open the overlay
        try:
            await target.click(timeout=3000)
            await page_obj.wait_for_timeout(800)
        except Exception as e:
            logger.debug("Normal click failed, trying force click: %s", e)
            try:
                await target.click(force=True, timeout=3000)
                await page_obj.wait_for_timeout(800)
            except Exception as e2:
                logger.debug("Force click also failed: %s", e2)
                return

        # Find the active input (the one that's focused)
        overlay_input = None
        try:
            # The focused element is usually the correct input
            overlay_input = page_obj.locator(':focus').first
            if await overlay_input.count() > 0 and await overlay_input.is_visible():
                logger.debug("Found focused input")
            else:
                overlay_input = None
        except Exception:
            pass

        # Fallback: find visible combobox
        if not overlay_input:
            try:
                all_combos = page_obj.locator('input[role="combobox"]:visible')
                if await all_combos.count() > 0:
                    overlay_input = all_combos.first
                    logger.debug("Found visible combobox")
            except Exception:
                pass

        if not overlay_input:
            logger.debug("Could not find overlay input, using original target")
            overlay_input = target

        # Clear and type the value
        try:
            await overlay_input.wait_for(state="visible", timeout=2000)

            # Clear using triple-click
            try:
                await overlay_input.click(click_count=3)
                await page_obj.wait_for_timeout(100)
            except Exception:
                pass

            # Type the value
            logger.debug("Typing value: %s", value)
            await page_obj.keyboard.type(value, delay=80)
            await page_obj.wait_for_timeout(1500)  # Longer wait for autocomplete

        except Exception as e:
            logger.debug("Error during input: %s", e)
            return

        # Now find the options - they appear in a listbox, not necessarily in a dialog
        code = value.strip().upper()

        try:
            # Look for the listbox that appears after typing
            # Based on your HTML sample, it's a <ul role="listbox">
            listbox_selector = 'ul[role="listbox"]'

            try:
                await page_obj.wait_for_selector(listbox_selector, timeout=3000, state="visible")
                await page_obj.wait_for_timeout(300)
                logger.debug("Found listbox")
            except Exception as e:
                logger.debug("Listbox not found: %s", e)
                # Try alternate wait
                await page_obj.wait_for_timeout(1000)

            # Find options within any visible listbox
            option = None

            # Strategy 1: Find by exact data-code for airports (data-type="1")
            try:
                option_selector = f'li[role="option"][data-code="{code}"][data-type="1"]'
                option = page_obj.locator(option_selector).first
                if await option.count() > 0:
                    is_visible = await option.is_visible()
                    logger.debug("Found option by data-code, visible: %s", is_visible)
                    if not is_visible:
                        option = None
            except Exception as e:
                logger.debug("Error finding by data-code: %s", e)
                option = None

            # Strategy 2: Search through all airport options (data-type="1")
            if not option:
                try:
                    airport_options = page_obj.locator('li[role="option"][data-type="1"]')
                    count = await airport_options.count()
                    logger.debug("Searching through %s airport options", count)

                    for i in range(min(count, 20)):
                        opt = airport_options.nth(i)
                        try:
                            # Check if visible
                            if not await opt.is_visible():
                                continue

                            opt_code = await opt.get_attribute("data-code")
                            opt_label = await opt.get_attribute("aria-label")

                            logger.debug("Option %s: code=%s label=%s", i, opt_code, opt_label)

                            if opt_code == code:
                                option = opt
                                logger.debug("Match by code")
                                break
                            elif opt_label and code in opt_label:
                                option = opt
                                logger.debug("Match by label")
                                break
                        except Exception as e:
                            logger.debug("Error checking option %s: %s", i, e)
                            continue
                except Exception as e:
                    logger.debug("Error searching airport options: %s", e)

            # Strategy 3: Any visible option containing the code
            if not option:
                try:
                    logger.debug("Trying text-based search for: %s", code)
                    all_options = page_obj.locator(f'li[role="option"]:visible')
                    count = await all_options.count()
                    logger.debug("Found %s visible options total", count)

                    for i in range(min(count, 10)):
                        opt = all_options.nth(i)
                        try:
                            text = await opt.inner_text()
                            if code in text:
                                option = opt
                                logger.debug("Found option by text match")
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug("Error in text search: %s", e)

            # Click the matched option
            if option and await option.count() > 0:
                try:
                    await option.scroll_into_view_if_needed()
                    await page_obj.wait_for_timeout(200)

                    try:
                        await option.click(timeout=2000)
                        logger.debug("Clicked option")
                    except Exception:
                        await option.click(force=True)
                        logger.debug("Force-clicked option")

                    await page_obj.wait_for_timeout(600)
                    return
                except Exception as e:
                    logger.debug("Error clicking option: %s", e)
            else:
                logger.debug("No matching option found for %s", code)

        except Exception as e:
            logger.debug("Error with option selection: %s", e, exc_info=True)

        # Fallback: Press Enter
        try:
            logger.debug("Pressing Enter as fallback")
            await page_obj.keyboard.press("Enter")
            await page_obj.wait_for_timeout(600)
        except Exception as e:
            logger.debug("Could not press Enter: %s", e)

    except Exception as e:
        logger.error("Error filling field with value '%s': %s", value, e, exc_info=True)

async def _fill_basic_form(
    page,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str,
) -> None:
    try:
        await page.wait_for_selector(config.GF_FROM_INPUT, timeout=8000)
    except Exception:
        pass

    logger.info("Filling basic form %s -> %s", origin, destination)

    fields_container = page.locator(config.GF_FIELDS_CONTAINER).first
    if not await fields_container.count():
        fields_container = page
    rows = fields_container.locator(config.GF_LEG_ROW)
    row = rows.first if await rows.count() else fields_container

    from_field = row.locator(config.GF_FROM_INPUT).first if await row.locator(config.GF_FROM_INPUT).count() else page.locator(config.GF_FROM_INPUT).first

    to_field = row.locator(config.GF_TO_INPUT).first if await row.locator(config.GF_TO_INPUT).count() else page.locator(config.GF_TO_INPUT).first

    await _fill_simple_field(from_field, origin)
    logger.info("Filled origin: %s", origin)

    await _fill_simple_field(to_field, destination)
    logger.info("Filled destination: %s", destination)

    date_fields = row.locator(config.GF_DEPART_INPUT)
    depart_field = date_fields.first if await date_fields.count() else page.locator(config.GF_DEPART_INPUT).first
    if depart_field and await depart_field.count():
        try:
            await depart_field.click()
            await depart_field.fill("")
            if depart_date:
                await depart_field.type(_iso_date(depart_date))
                await depart_field.press("Enter")
                done_btn = page.get_by_role("button", name=re.compile("Done", re.I)).first
                if await done_btn.count():
                    try:
                        await done_btn.click()
                    except Exception:
                        pass
        except Exception:
            try:
                await depart_field.evaluate(
                    "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                    _iso_date(depart_date),
                )
            except Exception:
                pass

    if return_date:
        # Prefer explicit return field, fall back to second date input.
        ret_candidates = row.locator(config.GF_RETURN_INPUT)
        ret_count = await ret_candidates.count()
        if ret_count == 0:
            ret_candidates = fields_container.locator(config.GF_RETURN_INPUT)
            ret_count = await ret_candidates.count()
        if ret_count == 0:
            ret_candidates = page.locator(config.GF_RETURN_INPUT)
            ret_count = await ret_candidates.count()

        ret_field = ret_candidates.first if ret_count else None
        if not ret_field or not await ret_field.count():
            date_fields = fields_container.locator(config.GF_DEPART_INPUT)
            ret_field = date_fields.nth(1)

        if ret_field and await ret_field.count():
            try:
                await ret_field.click()
                await ret_field.fill("")
                await ret_field.type(_iso_date(return_date))
                await ret_field.press("Enter")
                done_btn = page.get_by_role("button", name=re.compile("Done", re.I)).first
                if await done_btn.count():
                    try:
                        await done_btn.click()
                    except Exception:
                        pass
            except Exception:
                try:
                    await ret_field.evaluate(
                        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                        _iso_date(return_date),
                    )
                except Exception:
                    pass
    await page.wait_for_timeout(400)


async def scrape_basic_form(
    page,
    leg: Dict[str, Any],
    flight_type: str,
    nonstop_only: bool,
    limit: int,
) -> Dict[str, Any] | None:
    try:
        await page.goto("https://www.google.com/travel/flights")
    except Exception:
        return None

    await _handle_cookie_banner(page)
    desired = "Round trip" if flight_type == "round-trip" else "One way"
    await _switch_trip_type(page, desired)

    await _fill_basic_form(
        page,
        leg.get("origin", ""),
        leg.get("destination", ""),
        leg.get("depart_date", ""),
        leg.get("return_date", ""),
    )

    search_btn = page.get_by_role("button", name=re.compile("Search", re.I))
    if await search_btn.count():
        try:
            await search_btn.click()
        except Exception:
            pass

    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass

    if nonstop_only:
        await _apply_nonstop_filter(page)

    try:
        await _wait_for_results(page, timeout_ms=15000)
    except Exception:
        try:
            await page.wait_for_selector("[role='listitem']", timeout=8000)
        except PlaywrightTimeout:
            pass

    flights = await _scrape_results(page, limit=limit)
    if not any(flights.values()):
        return None

    return {
        "origin": leg.get("origin", ""),
        "destination": leg.get("destination", ""),
        "depart_date": leg.get("depart_date", ""),
        "return_date": leg.get("return_date", ""),
        "type": flight_type,
        "query_url": page.url,
        "flights": flights,
    }


async def _select_passengers(page, input_data: Dict[str, Any]) -> None:
    counts = _compute_passenger_clicks(input_data)
    form = page.locator(config.GF_FORM_CONTAINER).first
    if not await form.count():
        form = page
    pax_toggle = form.locator(config.GF_PASSENGER_TOGGLE).first
    if not await pax_toggle.count():
        pax_toggle = page.get_by_role("button", name=re.compile("passenger|traveler|adult", re.I))
    if not await pax_toggle.count():
        return
    try:
        await pax_toggle.click()
        await page.wait_for_timeout(200)
    except Exception:
        return

    def _button(selector: str):
        return page.locator(selector).first

    mapping = {
        "adult": config.GF_PAX_ADD_ADULT,
        "child": config.GF_PAX_ADD_CHILD,
        "infant_seat": config.GF_PAX_ADD_INFANT_SEAT,
        "infant_lap": config.GF_PAX_ADD_INFANT_LAP,
    }

    for key, selector in mapping.items():
        clicks = counts.get(key, 0)
        btn = _button(selector)
        for _ in range(clicks):
            if not await btn.count():
                break
            try:
                await btn.click()
                await page.wait_for_timeout(120)
            except Exception:
                break

    # Close the dialog if a close/done exists; otherwise press Escape.
    done = page.get_by_role("button", name=re.compile("Done|Close|Save", re.I))
    if await done.count():
        try:
            await done.click()
        except Exception:
            pass
    else:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    await page.wait_for_timeout(200)


async def _select_seat_class(page, seat_class: str) -> None:
    if not seat_class:
        return
    form = page.locator(config.GF_FORM_CONTAINER).first
    if not await form.count():
        form = page
    class_toggle = form.locator(config.GF_CLASS_TOGGLE).first
    if not await class_toggle.count():
        class_toggle = page.get_by_role("button", name=re.compile("Economy|Business|First|Class", re.I))
    if not await class_toggle.count():
        return

    try:
        await class_toggle.click()
        await page.wait_for_timeout(200)
    except Exception:
        return

    options = form.locator(config.GF_CLASS_OPTIONS)
    label = seat_class.strip()
    if await options.count():
        for idx in range(await options.count()):
            opt = options.nth(idx)
            try:
                text = (await opt.inner_text()).strip()
            except Exception:
                text = ""
            if text.lower().startswith(label.lower().split()[0]):
                try:
                    await opt.click()
                except Exception:
                    pass
                break
    else:
        opt = page.get_by_role("option", name=re.compile(label, re.I))
        if await opt.count():
            try:
                await opt.click()
            except Exception:
                pass
    await page.wait_for_timeout(300)


async def run(headless: bool, input_path: str, output: Path, limit: int, screenshot: str | None) -> None:
    logger.info("Starting Google Flights run headless=%s input=%s limit=%s", headless, input_path, limit)
    input_data = read_input(input_path)
    legs, flight_type = build_legs(input_data)
    logger.info("Prepared %s leg(s) for flight_type=%s", len(legs), flight_type)
    nonstop_only = bool(input_data.get("nonstop_flights"))

    results: List[Dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        # Open flights home
        await page.goto("https://www.google.com/travel/flights")
        await _handle_cookie_banner(page)

        # Set trip type, passengers, seat class
        await _switch_trip_type(page, flight_type)
        await _select_passengers(page, input_data)
        seat_class = ""
        if input_data.get("itinerary"):
            seat_class = input_data["itinerary"][0].get("class", "")
        await _select_seat_class(page, seat_class)
        logger.info("Configured trip type=%s passengers seat_class=%s", flight_type, seat_class or "default")

        if flight_type == "multiple-legs" and len(legs) > 1:
            logger.info("Filling %s multi-city legs", len(legs))
            await _ensure_leg_rows(page, len(legs))
            for idx, leg in enumerate(legs):
                await _fill_leg_row(
                    page,
                    idx,
                    leg.get("origin", ""),
                    leg.get("destination", ""),
                    leg.get("depart_date", ""),
                )
        else:
            leg = legs[0]
            logger.info("Filling basic form for %s -> %s on %s", leg.get("origin"), leg.get("destination"), leg.get("depart_date"))
            await _fill_basic_form(
                page,
                leg.get("origin", ""),
                leg.get("destination", ""),
                leg.get("depart_date", ""),
                leg.get("return_date", ""),
            )

        search_btn = page.get_by_role("button", name=re.compile("Search", re.I)).first
        if await search_btn.count():
            try:
                await search_btn.click()
            except Exception:
                pass

        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeout:
            pass

        if nonstop_only:
            await _apply_nonstop_filter(page)
            logger.info("Applied nonstop filter")

        try:
            await _wait_for_results(page, timeout_ms=15000)
        except Exception:
            try:
                await page.wait_for_selector("[role='listitem']", timeout=8000)
            except PlaywrightTimeout:
                pass

        flights = await _scrape_results(page, limit=limit)
        logger.info(
            "Scraped flights: top=%s other=%s all=%s",
            len(flights.get("top_flights", [])),
            len(flights.get("other_flights", [])),
            len(flights.get("all", [])),
        )
        if not any(flights.values()):
            flights = {"top_flights": [], "other_flights": [], "all": []}
        results.append(
            {
                "type": "multi-city" if flight_type == "multiple-legs" and len(legs) > 1 else flight_type,
                "legs": legs,
                "query_url": page.url,
                "flights": flights,
            }
        )

        if screenshot:
            try:
                await page.screenshot(path=screenshot, full_page=True)
            except Exception:
                pass

        await browser.close()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    logger.info("Wrote %s leg result(s) to %s", len(results), output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Google Flights using input.json values.")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode.")
    parser.add_argument("--input", default="input.json", help="Path to the input JSON file.")
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help="Path to write scraped Google Flights results (JSON).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximum number of flight cards to capture per leg.",
    )
    parser.add_argument("--screenshot", default="", help="Optional path to save a final screenshot.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    screenshot = args.screenshot or None
    await run(
        headless=not args.headed,
        input_path=args.input,
        output=Path(args.output),
        limit=args.limit,
        screenshot=screenshot,
    )


if __name__ == "__main__":
    asyncio.run(main())
