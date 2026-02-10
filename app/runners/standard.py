import asyncio
import copy
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from app import config
from app.db import (
    get_myidtravel_account,
    get_stafftraveler_account_by_employee_name,
    save_standby_response,
    update_run_record,
)
from app.slack import notify_thread_message, notify_validation_errors, slack_web_client
from app.state import RUN_SEMAPHORE
from app.utils import build_route_string, extract_json_from_text
from app.validation import validate_and_normalize_input
from app.ws import RunState
from bots import google_flights_bot, myidtravel_bot, stafftraveler_bot

logger = logging.getLogger("globalpass")


def _call_gemini(prompt: str) -> str:
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else str(exc)
        raise RuntimeError(f"Gemini HTTP error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini request failed: {exc}") from exc
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def _generate_flight_loads_gemini(
    input_data: dict[str, Any],
    myid_data: dict[str, Any],
    staff_data: list,
    google_data: list,
) -> tuple[list[dict[str, Any]] | None, str]:
    route = build_route_string(input_data)
    logger.info("Gemini: model=%s route=%s", config.GEMINI_MODEL, route)
    prompt = (
        "Context: I am a software engineer and frequent user of staff travel benefits. "
        "I am providing you with multiple JSON files containing flight search results: "
        "one from a commercial aggregator (Google Flights), one from a staff booking portal "
        "(myIDTravel), and one from a load-sharing app (StaffTraveller).\n\n"
        "Task: Analyze the provided JSON files to identify the top 5 flight options for the route "
        f"{route}.\n\n"
        "Requirements:\n"
        "1. Prioritize Load Data: Use the chance or travelStatus fields from the staff travel files "
        'to determine availability. Note that in staff travel contexts, "LOW chance" typically '
        "indicates a high load (full flight).\n"
        "2. Cross-Reference: Use the commercial data to verify flight times or equipment if the staff "
        "travel data is incomplete.\n"
        "3. Output Format: Provide the final result in a clean JSON format with the following keys: "
        "flight_number, airline, origin, destination, departure_time, arrival_time, date, load_status, "
        "and source_file.\n"
        "4. Tone: Be concise and direct. Do not include introductory fluff.\n\n"
        "myIDTravel JSON:\n"
        f"{json.dumps(myid_data)}\n\n"
        "StaffTraveller JSON:\n"
        f"{json.dumps(staff_data)}\n\n"
        "Google Flights JSON:\n"
        f"{json.dumps(google_data)}\n"
    )
    logger.info("Gemini: sending request (prompt chars=%s)", len(prompt))
    text = await asyncio.to_thread(_call_gemini, prompt)
    logger.info("Gemini: received response (chars=%s)", len(text))
    parsed = extract_json_from_text(text)
    if isinstance(parsed, list):
        return parsed, text
    return None, text


async def _generate_top5_from_standby_payload(
    input_data: dict[str, Any],
    standby_payload: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str]:
    route = build_route_string(input_data)
    logger.info("Gemini: model=%s route=%s (standby payload)", config.GEMINI_MODEL, route)
    prompt = (
        "Context: I am a software engineer and frequent user of staff travel benefits. "
        "I am providing you with a JSON payload that contains selectable flights from myIDTravel "
        "augmented with seat availability from Google Flights and StaffTraveler.\n\n"
        f"Task: Analyze the payload to identify the top 5 flight options for the route {route}.\n\n"
        "Requirements:\n"
        "1. Use seat availability across sources to rank the top 5 flights. "
        "If multiple sources disagree, prefer StaffTraveler for staff loads and Google Flights for public seats.\n"
        "2. Output format: Return a JSON array of 5 objects with keys: "
        "flight_number, airline_name, origin, destination, departure_time, arrival_time, "
        "date, load_summary, and source_notes.\n"
        "3. Be concise and return only JSON.\n\n"
        "Standby Bot Payload JSON:\n"
        f"{json.dumps(standby_payload)}\n"
    )
    logger.info("Gemini: sending request (prompt chars=%s)", len(prompt))
    text = await asyncio.to_thread(_call_gemini, prompt)
    logger.info("Gemini: received response (chars=%s)", len(text))
    parsed = extract_json_from_text(text)
    if isinstance(parsed, list):
        return parsed, text
    return None, text


async def run_myidtravel(state: RunState, headed: bool) -> dict[str, Any]:
    await state.log("[myidtravel] starting")
    notify = None
    if state.slack_channel and slack_web_client:

        async def _notify(msg: str) -> None:
            try:
                if slack_web_client and state.slack_channel:
                    await slack_web_client.chat_postMessage(
                        channel=state.slack_channel,
                        thread_ts=state.slack_thread_ts,
                        text=msg,
                    )
            except Exception as exc:
                logger.debug("Slack notify failed: %s", exc)

        notify = _notify

    try:
        if not state.myidtravel_credentials:
            await state.log("[myidtravel] error: missing account credentials.")
            return {
                "name": "myidtravel",
                "status": "error",
                "error": "missing myidtravel credentials",
                "payload": None,
            }
        myidtravel_bot.set_notifier(notify)
        try:
            await state.progress("myidtravel", 40, "running")
            payload = await myidtravel_bot.run(
                headless=not headed,
                screenshot=None,
                input_path=None,
                final_screenshot=str(state.output_dir / "myidtravel_final.png"),
                input_data=state.input_data,
                output_path=None,
                username=state.myidtravel_credentials.get("username"),
                password=state.myidtravel_credentials.get("password"),
                progress_cb=lambda percent, status: state.progress("myidtravel", percent, status),
            )
        finally:
            myidtravel_bot.set_notifier(None)
        if payload is None:
            await state.log("[myidtravel] finished but no data was captured")
        return {
            "name": "myidtravel",
            "status": "ok" if payload is not None else "error",
            "payload": payload,
            "error": None if payload is not None else "no data captured",
        }
    except Exception as exc:
        await state.log(f"[myidtravel] error: {exc}")
        return {"name": "myidtravel", "status": "error", "error": str(exc), "payload": None}


async def run_google_flights(state: RunState, limit: int, headed: bool) -> dict[str, Any]:
    await state.log("[google_flights] starting")
    notify = None
    if state.slack_channel and slack_web_client:

        async def _notify(msg: str) -> None:
            try:
                if slack_web_client and state.slack_channel:
                    await slack_web_client.chat_postMessage(
                        channel=state.slack_channel,
                        thread_ts=state.slack_thread_ts,
                        text=msg,
                    )
            except Exception as exc:
                logger.debug("Slack notify failed: %s", exc)

        notify = _notify

    try:
        google_flights_bot.set_notifier(notify)
        try:
            await state.progress("google_flights", 40, "running")
            payload = await google_flights_bot.run(
                headless=not headed,
                input_path=None,
                output=None,
                limit=limit,
                screenshot=str(state.output_dir / "google_flights_final.png"),
                input_data=state.input_data,
                progress_cb=lambda percent, status: state.progress("google_flights", percent, status),
            )
        finally:
            google_flights_bot.set_notifier(None)
        if payload is None:
            await state.log("[google_flights] finished but no data was captured")
        return {
            "name": "google_flights",
            "status": "ok" if payload is not None else "error",
            "payload": payload,
            "error": None if payload is not None else "no data captured",
        }
    except Exception as exc:
        await state.log(f"[google_flights] error: {exc}")
        return {"name": "google_flights", "status": "error", "error": str(exc), "payload": None}


async def run_stafftraveler(state: RunState, headed: bool) -> dict[str, Any]:
    await state.log("[stafftraveler] starting")
    notify = None

    if state.slack_channel and slack_web_client:

        async def _notify(msg: str) -> None:
            try:
                if slack_web_client and state.slack_channel:
                    await slack_web_client.chat_postMessage(
                        channel=state.slack_channel,
                        thread_ts=state.slack_thread_ts,
                        text=msg,
                    )
            except Exception as exc:
                logger.debug("Slack notify failed: %s", exc)

        notify = _notify

    try:
        if not state.stafftraveler_credentials:
            await state.log("[stafftraveler] skipped: no account credentials available.")
            return {
                "name": "stafftraveler",
                "status": "skipped",
                "error": "missing stafftraveler credentials",
                "payload": None,
            }
        stafftraveler_bot.set_notifier(notify)
        try:
            await state.progress("stafftraveler", 40, "running")
            payload = await stafftraveler_bot.perform_stafftraveller_login(
                headless=not headed,
                screenshot=str(state.output_dir / "stafftraveler_final.png"),
                input_data=state.input_data,
                output_path=None,
                username=state.stafftraveler_credentials.get("username"),
                password=state.stafftraveler_credentials.get("password"),
                progress_cb=lambda percent, status: state.progress("stafftraveler", percent, status),
            )
        finally:
            stafftraveler_bot.set_notifier(None)

        if payload is None:
            await state.log("[stafftraveler] finished but no data was captured")
        return {
            "name": "stafftraveler",
            "status": "ok" if payload is not None else "error",
            "payload": payload,
            "error": None if payload is not None else "no data captured",
        }
    except Exception as exc:
        await state.log(f"[stafftraveler] error: {exc}")
        return {"name": "stafftraveler", "status": "error", "error": str(exc), "payload": None}


async def execute_run(state: RunState, limit: int, headed: bool) -> None:
    async with RUN_SEMAPHORE:
        state.status = "running"
        update_run_record(run_id=state.id, status=state.status, error=None, completed_at=None)
        await state.push_status()

        if not state.slack_channel:
            await state.send_initial_slack_notification()

        normalized_input, errors = validate_and_normalize_input(state.input_data)
        if errors:
            await state.log("Run aborted: invalid input.")
            await notify_validation_errors(state, errors)
            state.status = "error"
            state.error = "invalid input"
            state.completed_at = datetime.utcnow()
            update_run_record(run_id=state.id, status=state.status, error=state.error, completed_at=state.completed_at)
            await state.push_status()
            state.done.set()
            return
        state.input_data = normalized_input

        raw_account_id = state.input_data.get("account_id")
        if not raw_account_id:
            message = "Run blocked: account_id is required to load MyIDTravel credentials."
            await notify_thread_message(state, message)
            await state.log(message)
            state.status = "error"
            state.error = "missing account_id"
            state.completed_at = datetime.utcnow()
            update_run_record(run_id=state.id, status=state.status, error=state.error, completed_at=state.completed_at)
            await state.push_status()
            state.done.set()
            return

        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError):
            message = f"Run blocked: account_id '{raw_account_id}' is invalid."
            await notify_thread_message(state, message)
            await state.log(message)
            state.status = "error"
            state.error = "invalid account_id"
            state.completed_at = datetime.utcnow()
            update_run_record(run_id=state.id, status=state.status, error=state.error, completed_at=state.completed_at)
            await state.push_status()
            state.done.set()
            return

        myid_account = get_myidtravel_account(account_id)
        if not myid_account or not myid_account.username or not myid_account.password:
            message = f"MyIDTravel credentials missing for account_id={account_id}. Run stopped."
            await notify_thread_message(state, message)
            await state.log(message)
            state.status = "error"
            state.error = "missing myidtravel credentials"
            state.completed_at = datetime.utcnow()
            update_run_record(run_id=state.id, status=state.status, error=state.error, completed_at=state.completed_at)
            await state.push_status()
            state.done.set()
            return

        state.employee_name = myid_account.employee_name
        state.myidtravel_credentials = {
            "username": myid_account.username,
            "password": myid_account.password,
        }

        staff_account = get_stafftraveler_account_by_employee_name(myid_account.employee_name)
        if not staff_account or not staff_account.username or not staff_account.password:
            message = (
                f"StaffTraveler account not found for employee '{myid_account.employee_name}'. "
                "Skipping StaffTraveler for this run."
            )
            await notify_thread_message(state, message)
            await state.log(message)
            state.stafftraveler_credentials = None
        else:
            state.stafftraveler_credentials = {
                "username": staff_account.username,
                "password": staff_account.password,
            }

        await state.log("Run started; launching MyIDTravel.")
        logger.info("Run %s started (headed=%s, limit=%s)", state.id, headed, limit)

        myid_result = await run_myidtravel(state, headed)
        myid_payload = myid_result.get("payload") if isinstance(myid_result, dict) else None
        if myid_result.get("status") == "error":
            state.status = "error"
            state.error = myid_result.get("error") or "myidtravel error"
            state.completed_at = datetime.utcnow()
            save_standby_response(
                run_id=state.id,
                status="error",
                output_paths={
                    "myidtravel_screenshot": str(state.output_dir / "myidtravel_final.png"),
                },
                myidtravel_payload=myid_payload,
                google_flights_payload=None,
                stafftraveler_payload=None,
                gemini_payload=None,
                standby_bots_payload=None,
                error=state.error,
            )
            update_run_record(run_id=state.id, status=state.status, error=state.error, completed_at=state.completed_at)
            await state.log("Run finished with errors.")
            logger.info("Run %s completed status=%s", state.id, state.status)
            await state.push_status()
            state.done.set()
            return

        selectable_flights = [
            flight
            for routing in (myid_payload or [])
            if isinstance(routing, dict)
            for flight in (routing.get("flights") or [])
            if isinstance(flight, dict)
        ]
        if not selectable_flights:
            message = "MyIDTravel: no selectable flights found for this search."
            await notify_thread_message(state, message)
            await state.log(message)
            state.status = "error"
            state.error = "no selectable flights"
            state.completed_at = datetime.utcnow()
            save_standby_response(
                run_id=state.id,
                status="error",
                output_paths={
                    "myidtravel_screenshot": str(state.output_dir / "myidtravel_final.png"),
                },
                myidtravel_payload=myid_payload,
                google_flights_payload=None,
                stafftraveler_payload=None,
                gemini_payload=None,
                standby_bots_payload=None,
                error=state.error,
            )
            update_run_record(run_id=state.id, status=state.status, error=state.error, completed_at=state.completed_at)
            await state.push_status()
            state.done.set()
            return

        input_class = ""
        itinerary = state.input_data.get("itinerary", [])
        if isinstance(itinerary, list) and itinerary:
            input_class = (itinerary[0].get("class") or "").strip().lower()
        class_key = "economy"
        if "business" in input_class:
            class_key = "business"
        elif "first" in input_class:
            class_key = "first"
        elif "premium" in input_class:
            class_key = "economy"

        def _chance_to_seats(chance: str | None) -> str:
            chance_val = (chance or "").strip().upper()
            if chance_val == "HIGH":
                return "9+"
            if chance_val == "MID":
                return "4-8"
            if chance_val == "LOW":
                return "0-3"
            return ""

        standby_bots_payload = []
        for routing in myid_payload or []:
            if not isinstance(routing, dict):
                continue
            routing_info = routing.get("routingInfo") or {}
            flights = routing.get("flights") or []
            if not isinstance(flights, list):
                flights = []
            payload_flights = []
            for flight in flights:
                if not isinstance(flight, dict):
                    continue
                chance_value = flight.get("chance")
                segments = flight.get("segments") or []
                first_segment = segments[0] if isinstance(segments, list) and segments else {}
                segment_chance = first_segment.get("chance") if isinstance(first_segment, dict) else None
                seat_value = _chance_to_seats(chance_value or segment_chance)
                myid_seats = {"economy": "", "business": "", "first": ""}
                if seat_value:
                    myid_seats[class_key] = seat_value
                airline = {}
                if isinstance(first_segment, dict):
                    airline = (
                        first_segment.get("operatingAirline")
                        or first_segment.get("marketingAirline")
                        or first_segment.get("ticketingAirline")
                        or {}
                    )
                from_airport = first_segment.get("from") if isinstance(first_segment, dict) else {}
                to_airport = first_segment.get("to") if isinstance(first_segment, dict) else {}
                departure_code = (from_airport or {}).get("code") if isinstance(from_airport, dict) else ""
                arrival_code = (to_airport or {}).get("code") if isinstance(to_airport, dict) else ""
                payload_flights.append(
                    {
                        "airline_name": airline.get("name") or "",
                        "airline_code": airline.get("code") or "",
                        "flight_number": first_segment.get("flightNumber")
                        or flight.get("flightNumber")
                        or flight.get("flight_number")
                        or "",
                        "aircraft": first_segment.get("aircraft") or flight.get("aircraft") or "",
                        "departure": departure_code or first_segment.get("departure") or "",
                        "departure_time": first_segment.get("departureTime") or "",
                        "arrival": arrival_code or first_segment.get("arrival") or "",
                        "arrival_time": first_segment.get("arrivalTime") or "",
                        "duration": first_segment.get("segmentDuration") or flight.get("duration") or "",
                        "seats": {
                            "myidtravel": myid_seats,
                            "google_flights": {"economy": "", "business": "", "first": ""},
                            "stafftraveler": {"first": "", "eco": "", "ecoplus": "", "nonrev": "", "bus": ""},
                        },
                    }
                )
            if payload_flights:
                standby_bots_payload.append(
                    {
                        "routingInfo": routing_info,
                        "flights": payload_flights,
                    }
                )

        async def _run_google(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
            await state.log("[google_flights] starting")
            return await google_flights_bot.update_selectable_flights(
                headless=not headed,
                input_data=state.input_data,
                selectable_payload=payload,
                limit=30,
                screenshot=str(state.output_dir / "google_flights_final.png"),
                progress_cb=lambda percent, status: state.progress("google_flights", percent, status),
            )

        async def _run_staff(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
            await state.log("[stafftraveler] starting")

            if not state.stafftraveler_credentials:
                raise ValueError("Stafftraveler credentials are required but were not found in state.")

            return await stafftraveler_bot.update_selectable_flights(
                headless=not headed,
                selectable_payload=payload,
                username=state.stafftraveler_credentials["username"],
                password=state.stafftraveler_credentials["password"],
                screenshot=str(state.output_dir / "stafftraveler_final.png"),
                progress_cb=lambda percent, status: state.progress("stafftraveler", percent, status),
            )

        def _normalize_flight_number(value: str | None) -> str:
            return re.sub(r"\s+", "", value or "").upper()

        base_payload = copy.deepcopy(standby_bots_payload)
        stafftraveler_payload = None
        if state.input_data.get("auto_request_stafftraveler") and state.stafftraveler_credentials:
            await state.log("[stafftraveler] auto-request search starting")

            def _variants(value: str) -> set[str]:
                normalized = re.sub(r"\s+", "", value or "").upper()
                if not normalized:
                    return set()
                match = re.match(r"([A-Z]+)(\d+)", normalized)
                if not match:
                    return {normalized}
                prefix, number = match.groups()
                trimmed = str(int(number)) if number.isdigit() else number
                return {normalized, f"{prefix}{trimmed}"}

            selectable_numbers: set[str] = set()
            for routing in base_payload:
                if not isinstance(routing, dict):
                    continue
                for flight in routing.get("flights", []):
                    if not isinstance(flight, dict):
                        continue
                    number = flight.get("flight_number") or ""
                    selectable_numbers.update(_variants(number))
            stafftraveler_payload = await stafftraveler_bot.perform_stafftraveller_search(
                headless=not headed,
                screenshot=str(state.output_dir / "stafftraveler_request.png"),
                input_data=state.input_data,
                output_path=None,
                selectable_numbers=selectable_numbers,
                username=state.stafftraveler_credentials["username"],
                password=state.stafftraveler_credentials["password"],
                progress_cb=lambda percent, status: state.progress("stafftraveler", percent, status),
            )

        tasks = [asyncio.create_task(_run_google(copy.deepcopy(base_payload)))]
        if state.stafftraveler_credentials:
            tasks.append(asyncio.create_task(_run_staff(copy.deepcopy(base_payload))))

        results = await asyncio.gather(*tasks)
        google_payload = results[0] if results else base_payload
        staff_payload = results[1] if len(results) > 1 else None

        updated_payload = base_payload
        staff_index: dict[str, dict[str, Any]] = {}
        if staff_payload:
            for routing in staff_payload:
                for flight in routing.get("flights", []) if isinstance(routing, dict) else []:
                    if not isinstance(flight, dict):
                        continue
                    number = _normalize_flight_number(flight.get("flight_number"))
                    if number:
                        staff_index[number] = flight

        google_index: dict[str, dict[str, Any]] = {}
        for routing in google_payload:
            for flight in routing.get("flights", []) if isinstance(routing, dict) else []:
                if not isinstance(flight, dict):
                    continue
                number = _normalize_flight_number(flight.get("flight_number"))
                if number:
                    google_index[number] = flight

        for routing in updated_payload:
            if not isinstance(routing, dict):
                continue
            flights = routing.get("flights", [])
            if not isinstance(flights, list):
                continue
            for flight in flights:
                if not isinstance(flight, dict):
                    continue
                number = _normalize_flight_number(flight.get("flight_number"))
                if not number:
                    continue
                google_flight = google_index.get(number)
                if google_flight:
                    flight["seats"] = google_flight.get("seats", flight.get("seats", {}))
                    if google_flight.get("google_flights_section"):
                        flight["google_flights_section"] = google_flight.get("google_flights_section")
                staff_flight = staff_index.get(number)
                if staff_flight:
                    seats = flight.get("seats", {})
                    staff_seats = staff_flight.get("seats", {}).get("stafftraveler")
                    if staff_seats:
                        seats["stafftraveler"] = staff_seats
                        flight["seats"] = seats

        gemini_payload = None
        if config.FINAL_OUTPUT_FORMAT == "gemini":
            try:
                await state.log("[gemini] Generating top 5 from standby payload")
                gemini_top, gemini_raw = await _generate_top5_from_standby_payload(
                    state.input_data,
                    updated_payload,
                )
                if gemini_raw:
                    gemini_payload = extract_json_from_text(gemini_raw)
                if not gemini_top:
                    await state.log("[gemini] No usable top 5 from Gemini")
            except Exception as exc:
                await state.log(f"[gemini] Failed to generate top 5: {exc}")

        save_standby_response(
            run_id=state.id,
            status="completed",
            output_paths={
                "myidtravel_screenshot": str(state.output_dir / "myidtravel_final.png"),
                "google_flights_screenshot": str(state.output_dir / "google_flights_final.png"),
                "stafftraveler_screenshot": str(state.output_dir / "stafftraveler_final.png"),
                "stafftraveler_request_screenshot": str(state.output_dir / "stafftraveler_request.png"),
            },
            myidtravel_payload=myid_payload,
            google_flights_payload=None,
            stafftraveler_payload=stafftraveler_payload,
            gemini_payload=gemini_payload,
            standby_bots_payload=updated_payload,
            error=None,
        )

        state.status = "completed"
        state.error = None
        state.completed_at = datetime.utcnow()
        update_run_record(run_id=state.id, status=state.status, error=None, completed_at=state.completed_at)
        await state.log("Run finished.")
        logger.info("Run %s completed status=%s", state.id, state.status)

        await state.push_status()
        state.done.set()
