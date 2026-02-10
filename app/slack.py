import asyncio
import logging
import os
import re
from typing import Any, TYPE_CHECKING

from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from app import config
from app.db import create_run_record
from app.state import OUTPUT_ROOT, RUNS
from app.utils import make_run_id
from app.validation import validate_and_normalize_input
if TYPE_CHECKING:
    from app.ws import RunState

logger = logging.getLogger("globalpass")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_USER_OAUTH_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_ENABLED = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN)

slack_web_client: AsyncWebClient | None = None
slack_socket_client: SocketModeClient | None = None
slack_connected: bool = False


def truncate_slack_message(message: str, limit: int = 3900) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."


async def process_slack_event(client: SocketModeClient, req: SocketModeRequest) -> None:
    response = SocketModeResponse(envelope_id=req.envelope_id)
    await client.send_socket_mode_response(response)

    if req.type != "events_api":
        return
    event = req.payload.get("event", {})
    if event.get("type") != "message" or "subtype" in event or "bot_id" in event:
        return

    text = (event.get("text") or "").lower()
    user = event.get("user")
    channel = event.get("channel")
    ts = event.get("ts")
    if not channel or not ts:
        return

    if "run scraper" in text:
        logger.info("Slack command received: %s", text)
        if slack_web_client:
            await slack_web_client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")

        match = re.search(r"run scraper\\s+(\\w{3})\\s+(\\w{3})\\s+(\\d{2}/\\d{2}/\\d{4})", text)
        if not match:
            if slack_web_client:
                await slack_web_client.chat_postMessage(
                    channel=channel,
                    thread_ts=ts,
                    text="Usage: run scraper ORG DST MM/DD/YYYY",
                )
            return

        origin, destination, date = match.groups()
        input_data = {
            "flight_type": "one-way",
            "trips": [{"origin": origin, "destination": destination}],
            "itinerary": [{"date": date, "time": "09:00", "class": "Economy"}],
            "airline": "",
            "travel_status": "Standby",
            "nonstop_flights": False,
            "traveller": [],
            "travel_partner": [],
        }

        input_data, errors = validate_and_normalize_input(input_data)
        if errors:
            await notify_invalid_input(errors, channel=channel)
            return

        run_id = make_run_id()
        run_dir = OUTPUT_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(run_id, run_dir, input_data)
        state.slack_channel = channel
        state.slack_thread_ts = ts
        RUNS[run_id] = state

        create_run_record(
            run_id=run_id,
            input_data=input_data,
            output_dir=run_dir,
            status="running",
            run_type="standard",
            slack_channel=channel,
            slack_thread_ts=ts,
        )

        if slack_web_client:
            await slack_web_client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=f"Run started (`{run_id}`) for {origin} → {destination} on {date}.",
            )

        from app.runners.standard import execute_run

        asyncio.create_task(execute_run(state, limit=30, headed=False))
        return

    if "scraper status" in text:
        status_text = "Scraper status: Running." if slack_connected else "Scraper status: Not running."
        if slack_web_client:
            await slack_web_client.chat_postMessage(channel=channel, thread_ts=ts, text=status_text)


async def start_slack_bot() -> None:
    global slack_web_client, slack_socket_client, slack_connected
    if not SLACK_ENABLED:
        logger.warning("Slack integration disabled")
        return
    try:
        slack_web_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
        slack_socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=slack_web_client)
        slack_socket_client.socket_mode_request_listeners.append(process_slack_event)  # type: ignore
        await slack_socket_client.connect()
        slack_connected = True
        logger.info("Slack bot connected")
    except Exception as exc:
        logger.error("Failed to start Slack bot: %s", exc)
        slack_socket_client = None
        slack_connected = False


async def stop_slack_bot() -> None:
    global slack_socket_client, slack_connected
    if slack_socket_client:
        try:
            await slack_socket_client.close()
        except Exception:
            pass
    slack_socket_client = None
    slack_connected = False


async def notify_invalid_input(errors: list[str], channel: str | None = None) -> None:
    if not slack_web_client or not SLACK_ENABLED:
        return
    message = "Invalid input:\n" + "\n".join(f"• {err}" for err in errors)
    await slack_web_client.chat_postMessage(channel=channel or os.environ.get("SLACK_CHANNEL_ID"), text=message)


async def notify_validation_errors(state: "RunState", errors: list[str]) -> None:
    if not slack_web_client or not SLACK_ENABLED:
        return
    channel = state.slack_channel or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        return
    message = "Validation errors:\n" + "\n".join(f"• {err}" for err in errors)
    await slack_web_client.chat_postMessage(
        channel=channel,
        thread_ts=state.slack_thread_ts,
        text=message,
    )


async def notify_thread_message(state: "RunState", message: str) -> None:
    if not slack_web_client or not SLACK_ENABLED:
        return
    channel = state.slack_channel or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        return
    await slack_web_client.chat_postMessage(
        channel=channel,
        thread_ts=state.slack_thread_ts,
        text=message,
    )


def slack_status_data() -> dict[str, Any]:
    return {
        "enabled": SLACK_ENABLED,
        "connected": slack_connected,
        "commands": ["run scraper [origin] [destination] [date]", "scraper status"],
    }
