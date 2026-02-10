import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.db import create_run_record, get_lookup_response, get_stafftraveler_account_by_id
from app.runners.lookup import execute_find_flight
from app.state import OUTPUT_ROOT, RUNS
from app.utils import make_run_id
from app.ws import RunState

router = APIRouter(prefix="/api")


@router.post("/find-flight")
async def start_find_flight(payload: dict[str, Any] = Body(...)):
    input_data = payload.get("input") or {}
    headed = bool(payload.get("headed"))

    account_id = input_data.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")
    try:
        account_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="account_id is invalid") from exc

    staff_account = get_stafftraveler_account_by_id(account_id)
    if not staff_account:
        raise HTTPException(status_code=404, detail="StaffTraveler account not found")

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
        run_type="lookup",
        slack_channel=None,
        slack_thread_ts=None,
    )

    auto_request = bool(input_data.get("auto_request_stafftraveler"))
    asyncio.create_task(execute_find_flight(state, headed=headed, staff_account=staff_account, auto_request=auto_request))
    return {"run_id": run_id, "status": "started"}


@router.get("/find-flight/{run_id}")
async def get_find_flight(run_id: str):
    response = get_lookup_response(run_id)
    if not response:
        raise HTTPException(status_code=404, detail="Lookup run not found")
    return {
        "run_id": run_id,
        "status": response.status,
        "legs_results": response.lookup_payload or [],
        "error": response.error,
    }
