import asyncio
from io import BytesIO
from typing import Any

import pandas as pd
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from app.db import create_run_record, get_latest_standby_response, get_lookup_response
from app.runners.standard import execute_run
from app.state import OUTPUT_ROOT, RUNS
from app.utils import make_run_id
from app.ws import RunState

router = APIRouter(prefix="/api")


def _excel_response(filename: str, sheets: dict[str, list[dict[str, Any]]]) -> StreamingResponse:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            safe_name = name[:31] or "Sheet1"
            df = pd.DataFrame(rows or [])
            df.to_excel(writer, sheet_name=safe_name, index=False)
    output.seek(0)
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


def _flatten_standby_payload(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for routing in payload or []:
        flights = routing.get("flights") if isinstance(routing, dict) else []
        for flight in flights or []:
            if not isinstance(flight, dict):
                continue
            seats = flight.get("seats") or {}
            myid = seats.get("myidtravel") or {}
            google = seats.get("google_flights") or {}
            staff = seats.get("stafftraveler") or {}
            rows.append(
                {
                    "Airline": flight.get("airline_name"),
                    "Flight Number": flight.get("flight_number"),
                    "From": flight.get("departure"),
                    "To": flight.get("arrival"),
                    "Departure Time": flight.get("departure_time"),
                    "Arrival Time": flight.get("arrival_time"),
                    "Duration": flight.get("duration"),
                    "MyIDTravel Economy": myid.get("economy"),
                    "MyIDTravel Business": myid.get("business"),
                    "MyIDTravel First": myid.get("first"),
                    "Google Flights Economy": google.get("economy"),
                    "Google Flights Business": google.get("business"),
                    "Google Flights First": google.get("first"),
                    "StaffTraveler Business": staff.get("bus") or staff.get("business"),
                    "StaffTraveler Economy": staff.get("eco"),
                    "StaffTraveler Economy+": staff.get("ecoplus"),
                    "StaffTraveler Non-Rev": staff.get("nonrev"),
                    "StaffTraveler First": staff.get("first"),
                }
            )
    return rows


def _flatten_lookup_payload(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leg in payload or []:
        if not isinstance(leg, dict):
            continue
        google = leg.get("google_flights") or {}
        staff_list = leg.get("stafftraveler") or []
        staff = staff_list[0] if staff_list else {}
        staff_seats = staff.get("seats") or {}

        if isinstance(google, dict) and ("economy" in google or "business" in google):
            google_econ = google.get("economy") or {}
            google_bus = google.get("business") or {}
        else:
            google_econ = google if isinstance(google, dict) else {}
            google_bus = {}

        base = google_econ or google_bus or {}
        request_state = leg.get("stafftraveler_request") or {}
        rows.append(
            {
                "Leg": (leg.get("index") or 0) + 1,
                "Flight Number": leg.get("flight_number"),
                "Airline": base.get("airline") or staff.get("airline"),
                "From": base.get("origin") or staff.get("origin"),
                "To": base.get("destination") or staff.get("destination"),
                "Departure Time": base.get("depart_time") or staff.get("departure_time"),
                "Arrival Time": base.get("arrival_time") or staff.get("arrival_time"),
                "Duration": base.get("duration") or staff.get("duration"),
                "Google Economy Seats": google_econ.get("seats_available"),
                "Google Business Seats": google_bus.get("seats_available"),
                "StaffTraveler Business": staff_seats.get("bus"),
                "StaffTraveler Economy": staff_seats.get("eco"),
                "StaffTraveler Economy+": staff_seats.get("eco_plus") or staff_seats.get("ecoplus"),
                "StaffTraveler Non-Rev": staff_seats.get("non_rev") or staff_seats.get("nonrev"),
                "Request Attempted": request_state.get("attempted"),
                "Request Posted": request_state.get("posted"),
                "Request Reason": request_state.get("reason"),
            }
        )
    return rows


@router.post("/run")
async def start_run(payload: dict[str, Any] = Body(...)):
    input_data = payload.get("input") or {}
    headed = bool(payload.get("headed"))

    run_id = make_run_id()
    output_dir = OUTPUT_ROOT / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_id, output_dir, input_data)
    RUNS[run_id] = state

    create_run_record(
        run_id=run_id,
        input_data=input_data,
        output_dir=output_dir,
        status="running",
        run_type="standard",
        slack_channel=None,
        slack_thread_ts=None,
    )

    asyncio.create_task(execute_run(state, limit=30, headed=headed))
    return {"run_id": run_id, "status": "started"}


@router.get("/runs/{run_id}")
async def run_status(run_id: str):
    standby = get_latest_standby_response(run_id)
    lookup = get_lookup_response(run_id)
    files: dict[str, Any] = {}
    status = "unknown"
    if standby:
        status = standby.status
        files = standby.output_paths or {}
    elif lookup:
        status = lookup.status
        files = lookup.output_paths or {}
    return {"run_id": run_id, "status": status, "files": files}


@router.get("/runs/{run_id}/download/{kind}")
async def download_run(run_id: str, kind: str):
    if kind != "excel":
        raise HTTPException(status_code=404, detail="Unknown download kind")

    lookup = get_lookup_response(run_id)
    if lookup and lookup.lookup_payload:
        rows = _flatten_lookup_payload(lookup.lookup_payload)
        if not rows:
            raise HTTPException(status_code=404, detail="Lookup data is empty")
        return _excel_response(f"{run_id}.xlsx", {"Seat Availability": rows})

    standby = get_latest_standby_response(run_id)
    if not standby:
        raise HTTPException(status_code=404, detail="Run not found")

    sheets: dict[str, list[dict[str, Any]]] = {}
    if standby.standby_bots_payload:
        sheets["Flights"] = _flatten_standby_payload(standby.standby_bots_payload)
    if standby.gemini_payload and isinstance(standby.gemini_payload, list):
        sheets["Top 5"] = standby.gemini_payload
    if not sheets:
        raise HTTPException(status_code=404, detail="No report data available")
    return _excel_response(f"{run_id}.xlsx", sheets)


@router.get("/runs/{run_id}/download-report-xlsx")
async def download_run_report(run_id: str):
    return await download_run(run_id, "excel")
