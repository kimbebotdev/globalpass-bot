import asyncio
import copy
import logging
import re
from datetime import datetime
from typing import Any

from app.db import save_lookup_response, update_run_record
from app.ws import RunState
from bots import google_flights_bot, stafftraveler_bot

logger = logging.getLogger("globalpass")


def _normalize_flight_number(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _lookup_seat_class(value: str | None) -> str:
    seat = (value or "").strip().lower()
    if seat == "business":
        return "Business"
    if seat == "economy":
        return "Economy"
    return ""


def _staff_has_flight(staff_payload: list[dict[str, Any]], flight_number: str) -> bool:
    if not flight_number:
        return False
    variants = stafftraveler_bot._flight_number_variants(flight_number)
    for entry in staff_payload or []:
        if not isinstance(entry, dict):
            continue
        number = entry.get("flight_number") or entry.get("flightNumber") or ""
        if _normalize_flight_number(number) in variants:
            return True
    return False


def _merge_google_lookup_payloads(
    economy_payload: list[dict[str, Any]],
    business_payload: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        "economy": copy.deepcopy(economy_payload),
        "business": copy.deepcopy(business_payload),
    }


def _extract_lookup_google_flight(google_payload: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not google_payload:
        return None
    for entry in google_payload:
        if not isinstance(entry, dict):
            continue
        flights = entry.get("flights") or {}
        if isinstance(flights, dict):
            top = flights.get("top_flights") or []
            other = flights.get("other_flights") or []
            if top:
                return top[0]
            if other:
                return other[0]
    return None


def _strip_google_fields(flight: dict[str, Any] | None) -> dict[str, Any] | None:
    if not flight:
        return None
    cleaned = dict(flight)
    cleaned.pop("price", None)
    cleaned.pop("emissions", None)
    cleaned.pop("summary", None)
    return cleaned


async def execute_find_flight(
    state: RunState,
    headed: bool,
    staff_account,
    auto_request: bool,
) -> None:
    state.status = "running"
    await state.push_status()

    try:
        input_data = state.input_data
        flight_numbers = input_data.get("flight_numbers") or []
        trips = input_data.get("trips") or []
        itinerary = input_data.get("itinerary") or []
        seat_choice = ""
        if itinerary and isinstance(itinerary[0], dict):
            seat_choice = itinerary[0].get("class", "")

        legs_results: list[dict[str, Any]] = []
        google_raw: list[dict[str, Any]] = []
        staff_raw: list[dict[str, Any]] = []
        errors: list[str] = []
        for idx, flight_number in enumerate(flight_numbers or [""]):
            trip = trips[idx] if idx < len(trips) else (trips[0] if trips else {})
            itin = itinerary[idx] if idx < len(itinerary) else (itinerary[0] if itinerary else {})
            leg_input = dict(input_data)
            leg_input["trips"] = [trip]
            leg_input["itinerary"] = [copy.deepcopy(itin)]
            leg_input["flight_number"] = _normalize_flight_number(flight_number)

            seat_class = _lookup_seat_class(seat_choice)
            if seat_class:
                leg_input["itinerary"][0]["class"] = seat_class

            google_payload: list[dict[str, Any]] = []
            staff_payload: list[dict[str, Any]] = []
            request_state: dict[str, Any] = {"attempted": False, "posted": None, "reason": None}

            async def _run_google():
                nonlocal google_payload
                if seat_choice == "both":
                    econ_input = copy.deepcopy(leg_input)
                    econ_input["itinerary"][0]["class"] = "Economy"
                    bus_input = copy.deepcopy(leg_input)
                    bus_input["itinerary"][0]["class"] = "Business"
                    econ_payload = await google_flights_bot.run(
                        headless=not headed,
                        input_path=None,
                        output=None,
                        limit=30,
                        screenshot=str(state.output_dir / f"google_flights_final_{idx+1}.png"),
                        input_data=econ_input,
                        progress_cb=lambda percent, status: state.progress("google_flights", percent, status),
                    )
                    bus_payload = await google_flights_bot.run(
                        headless=not headed,
                        input_path=None,
                        output=None,
                        limit=30,
                        screenshot=None,
                        input_data=bus_input,
                        progress_cb=lambda percent, status: state.progress("google_flights", percent, status),
                    )
                    google_payload = _merge_google_lookup_payloads(econ_payload, bus_payload)
                else:
                    google_payload = await google_flights_bot.run(
                        headless=not headed,
                        input_path=None,
                        output=None,
                        limit=30,
                        screenshot=str(state.output_dir / f"google_flights_final_{idx+1}.png"),
                        input_data=leg_input,
                        progress_cb=lambda percent, status: state.progress("google_flights", percent, status),
                    )

            async def _run_staff():
                nonlocal staff_payload
                staff_payload = await stafftraveler_bot.perform_stafftraveller_login(
                    headless=not headed,
                    screenshot=str(state.output_dir / f"stafftraveler_final_{idx+1}.png"),
                    input_data=leg_input,
                    output_path=None,
                    username=staff_account.username,
                    password=staff_account.password,
                    progress_cb=lambda percent, status: state.progress("stafftraveler", percent, status),
                )

            try:
                await asyncio.gather(_run_google(), _run_staff())
            except Exception as exc:
                logger.exception("Lookup leg %s failed", idx + 1)
                errors.append(str(exc))

            if auto_request and not _staff_has_flight(staff_payload, flight_number):
                request_state["attempted"] = True
                request_meta: dict[str, Any] = {}
                try:
                    selectable_numbers = stafftraveler_bot._flight_number_variants(flight_number)
                    await stafftraveler_bot.perform_stafftraveller_search(
                        headless=not headed,
                        screenshot=None,
                        input_data=leg_input,
                        output_path=None,
                        selectable_numbers=selectable_numbers,
                        username=staff_account.username,
                        password=staff_account.password,
                        progress_cb=lambda percent, status: state.progress("stafftraveler", percent, status),
                        request_state=request_meta,
                    )
                except Exception as exc:
                    logger.exception("StaffTraveler auto-request failed for leg %s", idx + 1)
                    errors.append(str(exc))
                request_state.update(request_meta)

            legs_results.append(
                {
                    "index": idx,
                    "flight_number": flight_number,
                    "google_flights": (
                        {
                            "economy": _strip_google_fields(
                                _extract_lookup_google_flight(google_payload.get("economy") or [])
                            ),
                            "business": _strip_google_fields(
                                _extract_lookup_google_flight(google_payload.get("business") or [])
                            ),
                        }
                        if isinstance(google_payload, dict)
                        else {
                            "economy": _strip_google_fields(_extract_lookup_google_flight(google_payload))
                            if _lookup_seat_class(seat_choice) == "Economy"
                            else None,
                            "business": _strip_google_fields(_extract_lookup_google_flight(google_payload))
                            if _lookup_seat_class(seat_choice) == "Business"
                            else None,
                        }
                    ),
                    "stafftraveler": staff_payload,
                    "stafftraveler_request": request_state,
                }
            )
            google_raw.append({"index": idx, "flight_number": flight_number, "results": google_payload})
            staff_raw.append({"index": idx, "flight_number": flight_number, "results": staff_payload})

        status = "error" if errors else "completed"
        if errors:
            await state.log(f"[lookup] Errors: {' | '.join(errors)}")
        save_lookup_response(
            run_id=state.id,
            status=status,
            output_paths={},
            google_flights_payload=google_raw,
            stafftraveler_payload=staff_raw,
            lookup_payload=legs_results,
            error=", ".join(errors) if errors else None,
        )
        update_run_record(
            run_id=state.id,
            status=status,
            error=", ".join(errors) if errors else None,
            completed_at=datetime.utcnow(),
        )
        state.status = status
        state.error = ", ".join(errors) if errors else None
        state.completed_at = datetime.utcnow()
        await state.push_status()
    except Exception as exc:
        logger.exception("Lookup run failed")
        state.status = "error"
        state.error = str(exc)
        state.completed_at = datetime.utcnow()
        await state.log(f"[lookup] Fatal error: {exc}")
        update_run_record(
            run_id=state.id,
            status="error",
            error=str(exc),
            completed_at=state.completed_at,
        )
        await state.push_status()
