import asyncio
import copy
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import bcrypt
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, async_playwright
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from starlette.middleware.sessions import SessionMiddleware

from models import StafftravelerAccount

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True, interpolate=False)

import config
from bots import google_flights_bot, myidtravel_bot, stafftraveler_bot
from db import (
    create_run_record,
    ensure_data_dir,
    get_account_options,
    get_airline_label,
    get_latest_standby_response,
    get_lookup_response,
    get_myidtravel_account,
    get_stafftraveler_account_by_employee_name,
    get_stafftraveler_account_by_id,
    list_airlines,
    list_stafftraveler_accounts,
    save_airlines,
    save_lookup_response,
    save_standby_response,
    update_run_record,
)
from helpers import scrape_airlines as scrape_airlines_helper

logger = logging.getLogger("globalpass")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_USER_OAUTH_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_ENABLED = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

AIRLINE_OUTPUT = Path("airlines.json")
ORIGIN_LOOKUP_OUTPUT = Path("origin_lookup_sample.json")
AIRPORT_PICKER_OUTPUT = Path("airport_picker.json")
BOT_OUTPUTS = {
    "myidtravel": "myidtravel_flightschedule.json",
    "google_flights": "google_flights_results.json",
    "stafftraveler": "stafftraveller_results.json",
}
REPORT_JSON = "standby_report_multi.json"
REPORT_XLSX = "standby_report_multi.xlsx"

app = FastAPI(title="Globalpass Bot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
if not SECRET_KEY:
    logger.warning("SECRET_KEY is not set; session authentication will not work.")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

BODY_REQUIRED = Body(...)
BODY_DEFAULT: dict[str, Any] = Body(default={})


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user"))


def _verify_password(username: str, password: str) -> bool:
    if not ADMIN_PASSWORD_HASH:
        return False
    if username != ADMIN_USERNAME:
        return False
    try:
        return bcrypt.checkpw(password.encode(), ADMIN_PASSWORD_HASH.encode())
    except Exception:
        return False


@app.on_event("startup")
async def _startup_db() -> None:
    ensure_data_dir()


OUTPUT_ROOT = Path("outputs")
RUNS: dict[str, "RunState"] = {}
RUN_SEMAPHORE = asyncio.Semaphore(1)

slack_web_client: AsyncWebClient | None = None
slack_socket_client: SocketModeClient | None = None
slack_connected: bool = False


async def process_slack_event(client: SocketModeClient, req: SocketModeRequest):
    """Process incoming Slack Socket Mode events"""

    # Acknowledge the request
    response = SocketModeResponse(envelope_id=req.envelope_id)
    await client.send_socket_mode_response(response)

    # Check if it's an event
    if req.type == "events_api":
        event = req.payload.get("event", {})

        # Check if it's a message event (ignore bot messages and subtypes)
        if event.get("type") == "message" and "subtype" not in event and "bot_id" not in event:
            text = event.get("text", "").lower()
            user = event.get("user")
            channel = event.get("channel")
            ts = event.get("ts")

            # Check for "run scraper" command
            if "run scraper" in text:
                logger.info(f"Scraper triggered by user {user} in channel {channel}")

                try:
                    # Add reaction to acknowledge
                    if slack_web_client:
                        await slack_web_client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")

                    # Parse command for parameters (optional)
                    # Format: "run scraper [origin] [destination] [date]"
                    parts = text.split()

                    # Default input data
                    input_data = {
                        "flight_type": "one-way",
                        "nonstop_flights": True,
                        "airline": "",
                        "travel_status": "Bookable",
                        "trips": [{"origin": "DXB", "destination": "SIN"}],
                        "itinerary": [{"date": "01/10/2026", "time": "00:00", "class": "Economy"}],
                        "traveller": [{"name": "Slack User", "salutation": "MR", "checked": True}],
                        "travel_partner": [],
                    }

                    # Try to parse origin, destination, and date from message
                    if len(parts) >= 4:
                        try:
                            input_data["trips"][0]["origin"] = parts[2].upper()
                            input_data["trips"][0]["destination"] = parts[3].upper()
                            if len(parts) >= 5:
                                input_data["itinerary"][0]["date"] = parts[4]
                        except Exception as parse_error:
                            logger.warning(f"Could not parse command parameters: {parse_error}")

                    # Create and start run
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
                        status=state.status,
                        run_type="standard",
                        slack_channel=channel,
                        slack_thread_ts=ts,
                    )

                    # Send initial confirmation
                    if slack_web_client:
                        await slack_web_client.chat_postMessage(
                            channel=channel,
                            thread_ts=ts,
                            text=f"<@{user}> Scraper started! :rocket:\n"
                            f"Run ID: `{run_id}`\n"
                            f"Route: {input_data['trips'][0]['origin']} → {input_data['trips'][0]['destination']}\n"
                            f"Status: Running...",
                        )

                    # Start the run
                    asyncio.create_task(execute_run(state, limit=30, headed=False))

                except Exception as e:
                    logger.error(f"Error processing Slack command: {e}", exc_info=True)

                    if slack_web_client:
                        try:
                            await slack_web_client.chat_postMessage(
                                channel=channel, thread_ts=ts, text=f"<@{user}> Error starting scraper: {str(e)} :x:"
                            )
                        except Exception:
                            pass

            # Check for "scraper status" command
            elif "scraper status" in text:
                logger.info(f"Status check by user {user} in channel {channel}")

                try:
                    # Get recent runs
                    recent_runs = sorted(RUNS.items(), key=lambda x: x[1].created_at, reverse=True)[:5]

                    if not recent_runs:
                        status_text = "No scraper runs found."
                    else:
                        status_lines = ["*Recent Scraper Runs:*\n"]
                        for run_id, state in recent_runs:
                            status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "error": "❌"}.get(
                                state.status, "❓"
                            )

                            route = "N/A"
                            if state.input_data.get("trips"):
                                trip = state.input_data["trips"][0]
                                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"

                            status_lines.append(f"{status_emoji} `{run_id}` - {state.status.upper()} - {route}")

                        status_text = "\n".join(status_lines)

                    if slack_web_client:
                        await slack_web_client.chat_postMessage(channel=channel, thread_ts=ts, text=status_text)

                except Exception as e:
                    logger.error(f"Error checking status: {e}", exc_info=True)


async def start_slack_bot():
    """Initialize and start the Slack bot"""
    global slack_web_client, slack_socket_client, slack_connected

    if not SLACK_ENABLED:
        logger.info("Slack integration disabled (missing tokens)")
        return

    try:
        slack_web_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
        slack_socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=slack_web_client)

        # Register event handler
        slack_socket_client.socket_mode_request_listeners.append(process_slack_event)  # type: ignore

        # Connect in background
        await slack_socket_client.connect()
        slack_connected = True

        logger.info("Slack bot connected and listening for commands!")
        logger.info("Available commands: 'run scraper [origin] [destination] [date]', 'scraper status'")

    except Exception as e:
        logger.error(f"Failed to start Slack bot: {e}", exc_info=True)
        slack_socket_client = None
        slack_connected = False


async def stop_slack_bot():
    """Gracefully stop the Slack bot"""
    global slack_socket_client, slack_connected

    if slack_socket_client:
        try:
            await slack_socket_client.close()
            logger.info("Slack bot disconnected")
        except Exception as e:
            logger.error(f"Error stopping Slack bot: {e}")
        finally:
            slack_connected = False


class RunState:
    def __init__(self, run_id: str, output_dir: Path, input_data: dict[str, Any]):
        self.id = run_id
        self.output_dir = output_dir
        self.input_data = input_data
        self.status = "pending"
        self.error: str | None = None
        self.created_at = datetime.utcnow()
        self.completed_at: datetime | None = None
        self.logs: list[dict[str, Any]] = []
        self.subscribers: dict[WebSocket, asyncio.Queue] = {}
        self.done = asyncio.Event()
        self.result_files: dict[str, Path] = {}
        self.myidtravel_credentials: dict[str, str] | None = None
        self.stafftraveler_credentials: dict[str, str] | None = None
        self.employee_name: str | None = None

        # Slack-specific fields
        self.slack_channel: str | None = None
        self.slack_thread_ts: str | None = None

    def subscribe(self, ws: WebSocket) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers[ws] = queue
        return queue

    def unsubscribe(self, ws: WebSocket) -> None:
        self.subscribers.pop(ws, None)

    async def _broadcast(self, payload: dict[str, Any], store: bool = False) -> None:
        if store:
            self.logs.append(payload)
        stale: list[WebSocket] = []
        for ws, queue in self.subscribers.items():
            try:
                await queue.put(payload)
            except RuntimeError:
                stale.append(ws)
        for ws in stale:
            self.unsubscribe(ws)

    async def progress(self, bot: str, percent: int, status: str | None = None) -> None:
        payload = {
            "type": "progress",
            "bot": bot,
            "percent": max(0, min(100, percent)),
        }
        if status:
            payload["status"] = status
        await self._broadcast(payload, store=False)

    async def log(self, message: str) -> None:
        payload = {
            "type": "log",
            "ts": datetime.utcnow().isoformat(),
            "message": message,
        }
        await self._broadcast(payload, store=True)

    async def push_status(self) -> None:
        """Push status update to WebSocket subscribers and Slack"""
        payload = {
            "type": "status",
            "status": self.status,
            "error": self.error,
            "run_id": self.id,
        }
        if self.completed_at:
            payload["completed_at"] = self.completed_at.isoformat()
        await self._broadcast(payload, store=False)

        # Send Slack notification if this run was triggered from Slack
        if self.slack_channel and self.slack_thread_ts and slack_web_client:
            logger.info(
                "Sending Slack status update for run %s (status=%s) to channel %s thread %s",
                self.id,
                self.status,
                self.slack_channel,
                self.slack_thread_ts,
            )
            await self._send_slack_status_update()
        else:
            logger.debug(
                "Skipping Slack status update for run %s (channel=%s thread=%s client=%s)",
                self.id,
                self.slack_channel,
                self.slack_thread_ts,
                bool(slack_web_client),
            )

    async def _send_slack_status_update(self):
        """Send status update to Slack thread"""
        try:
            if self.status == "completed":
                # Build file links
                route = "N/A"
                if self.input_data.get("trips"):
                    trip = self.input_data["trips"][0]
                    route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"

                message = f"*Scraper Completed!*\nRun ID: `{self.id}`\nRoute: {route}\nFiles generated:\n"

                for file_key, file_path in self.result_files.items():
                    if file_path.exists():
                        message += f"• {file_key}\n"

                report_url = f"{config.BASE_URL}/api/runs/{self.id}/download-report-xlsx"
                message += f"\nDownload Excel: <{report_url}|{self.id}.xlsx>"

            elif self.status == "error":
                message = f"*Scraper Failed*\nRun ID: `{self.id}`\nError: {self.error}"
            else:
                return  # Don't send updates for pending/running status

            if slack_web_client and self.slack_channel:
                await slack_web_client.chat_postMessage(
                    channel=self.slack_channel, thread_ts=self.slack_thread_ts, text=message
                )
            logger.info("Successfully sent Slack notification for run %s", self.id)

        except Exception as e:
            logger.error("Error sending Slack notification for run %s: %s", self.id, e, exc_info=True)

    async def send_initial_slack_notification(self):
        """Send initial notification when a run starts via web interface"""
        if not slack_web_client or not SLACK_ENABLED:
            logger.warning("Slack client not available or disabled")
            return

        try:
            # Get default channel from environment or use a specific channel
            channel = os.environ.get("SLACK_CHANNEL_ID", "")  # Replace with your channel ID
            logger.info(f"Attempting to send Slack notification to channel: {channel}")

            # Format input data
            trips = self.input_data.get("trips", [])
            route = "N/A"
            if trips:
                trip = trips[0]
                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"

            itinerary = self.input_data.get("itinerary", [])
            date = itinerary[0].get("date", "N/A") if itinerary else "N/A"
            travel_class = itinerary[0].get("class", "N/A") if itinerary else "N/A"

            airline = self.input_data.get("airline", "All Airlines") or "All Airlines"
            travel_status = self.input_data.get("travel_status", "N/A")
            nonstop = "Yes" if self.input_data.get("nonstop_flights", False) else "No"

            travellers = self.input_data.get("traveller", [])
            traveller_count = len(travellers)

            message = (
                f"*New Flight Search Started*\n\n"
                f"*Run ID:* `{self.id}`\n"
                f"*Route:* {route}\n"
                f"*Date:* {date}\n"
                f"*Class:* {travel_class}\n"
                f"*Airline:* {airline}\n"
                f"*Travel Status:* {travel_status}\n"
                f"*Nonstop Only:* {nonstop}\n"
                f"*Travelers:* {traveller_count}\n\n"
                f"*Status:* Running scrapers..."
            )

            response = await slack_web_client.chat_postMessage(channel=channel, text=message)

            logger.info(f"Successfully sent Slack notification to {channel}")
            logger.info(f"Message TS: {response.get('ts')}")
            logger.info(f"Full response: {response}")
            # Store the message timestamp for later updates
            self.slack_channel = channel
            self.slack_thread_ts = response["ts"]

            logger.info(f"Sent initial Slack notification for run {self.id}")

        except SlackApiError as e:
            logger.error(f"Slack API response: {e.response}")

        except Exception as e:
            logger.error(f"Failed to send initial Slack notification: {e}", exc_info=True)
            logger.error(f"Error type: {type(e).__name__}")

    async def update_slack_status(self, status_text: str):
        """Update the status line in the original Slack message"""
        if not self.slack_channel or not self.slack_thread_ts or not slack_web_client:
            return

        try:
            # Rebuild the original message with updated status
            trips = self.input_data.get("trips", [])
            route = "N/A"
            if trips:
                trip = trips[0]
                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"

            itinerary = self.input_data.get("itinerary", [])
            date = itinerary[0].get("date", "N/A") if itinerary else "N/A"
            travel_class = itinerary[0].get("class", "N/A") if itinerary else "N/A"

            airline = self.input_data.get("airline", "All Airlines") or "All Airlines"
            travel_status = self.input_data.get("travel_status", "N/A")
            nonstop = "Yes" if self.input_data.get("nonstop_flights", False) else "No"

            travellers = self.input_data.get("traveller", [])
            traveller_count = len(travellers)

            message = (
                f"*New Flight Search Started*\n\n"
                f"*Run ID:* `{self.id}`\n"
                f"*Route:* {route}\n"
                f"*Date:* {date}\n"
                f"*Class:* {travel_class}\n"
                f"*Airline:* {airline}\n"
                f"*Travel Status:* {travel_status}\n"
                f"*Nonstop Only:* {nonstop}\n"
                f"*Travelers:* {traveller_count}\n\n"
                f"*Status:* {status_text}"
            )

            await slack_web_client.chat_update(
                channel=self.slack_channel, ts=self.slack_thread_ts, text=_truncate_slack_message(message)
            )

            logger.info(f"Updated Slack status to: {status_text}")

        except Exception as e:
            logger.error(f"Failed to update Slack status: {e}", exc_info=True)

    async def send_completion_slack_notification(self, top_flights: list):
        """Send completion notification with top 5 flights as a thread reply"""
        if not self.slack_channel or not self.slack_thread_ts or not slack_web_client:
            logger.warning(
                "Cannot send completion notification - channel: %s, ts: %s, client: %s",
                self.slack_channel,
                self.slack_thread_ts,
                bool(slack_web_client),
            )
            return

        try:
            logger.info(f"Sending completion notification to {self.slack_channel}")
            logger.info(f"Replying to thread with TS: {self.slack_thread_ts}")

            # First, update the main message status
            if self.status == "completed":
                await self.update_slack_status("Completed")
            elif self.status == "error":
                await self.update_slack_status(f"Failed - {self.error or 'Unknown error'}")

            # Then send the detailed results in thread
            # Format route
            trips = self.input_data.get("trips", [])
            route = "N/A"
            if trips:
                trip = trips[0]
                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"

            travel_status = (self.input_data.get("travel_status") or "").strip().lower()
            is_bookable = travel_status == "bookable"

            if self.status == "completed":
                # Build top flights message
                flights_text = []
                for idx, flight in enumerate(top_flights[:5], 1):
                    flight_number = flight.get("Flight") or flight.get("flight_number") or "N/A"
                    airline = flight.get("Airline") or flight.get("airline") or "N/A"
                    flight_info = (
                        f"*{idx}. {flight_number}* ({airline})\n"
                        f"   - Aircraft: {flight.get('Aircraft') or flight.get('aircraft') or 'N/A'}\n"
                        f"   - Departure: {flight.get('Departure') or flight.get('departure_time') or 'N/A'}\n"
                        f"   - Arrival: {flight.get('Arrival') or flight.get('arrival_time') or 'N/A'}\n"
                        f"   - Duration: {flight.get('Duration') or flight.get('duration') or 'N/A'}\n"
                        f"   - Stops: {flight.get('Stops') or flight.get('stops') or 'N/A'}\n"
                    )

                    if is_bookable and ("Price" in flight or "price" in flight):
                        price_val = flight.get("Price") or flight.get("price")
                        flight_info += f"   - Price: ${price_val}\n"
                    elif "Chance" in flight or "load_status" in flight:
                        chance_val = flight.get("Chance") or flight.get("load_status")
                        flight_info += f"   - Availability: {chance_val}\n"

                    flights_text.append(flight_info)

                flights_section = "\n".join(flights_text) if flights_text else "_No flights found_"

                category = "Bookable Flights" if is_bookable else "R2 Standby Flights"

                message = (
                    f"*Flight Search Complete*\n\n"
                    f"*Run ID:* `{self.id}`\n"
                    f"*Route:* {route}\n\n"
                    f"*Top 5 {category}:*\n\n"
                    f"{flights_section}\n"
                    f"Full report available in run outputs"
                )
            else:
                # Error case
                message = (
                    f"*Flight Search Failed*\n\n"
                    f"*Run ID:* `{self.id}`\n"
                    f"*Route:* {route}\n"
                    f"*Error:* {self.error or 'Unknown error'}"
                )

            # Reply in the thread instead of updating
            await slack_web_client.chat_postMessage(
                channel=self.slack_channel, thread_ts=self.slack_thread_ts, text=message
            )

            logger.info(f"Sent completion reply to thread for run {self.id}")

        except SlackApiError as e:
            logger.error(f"Slack API response: {e.response}")

        except Exception as e:
            logger.error(f"Failed to send completion reply: {e}", exc_info=True)
            logger.error(f"Error type: {type(e).__name__}")


def make_run_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _truncate_slack_message(message: str, limit: int = 3900) -> str:
    if len(message) <= limit:
        return message
    return message[:limit].rstrip() + "…"


def _build_route_string(input_data: dict[str, Any]) -> str:
    trips = input_data.get("trips", [])
    if not trips:
        return "N/A"
    if len(trips) == 1:
        trip = trips[0]
        return f"{trip.get('origin', '?')} -> {trip.get('destination', '?')}"
    return " | ".join(f"{trip.get('origin', '?')} -> {trip.get('destination', '?')}" for trip in trips)


def _extract_json_from_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return None


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
    route = _build_route_string(input_data)
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
    parsed = _extract_json_from_text(text)
    if isinstance(parsed, list):
        return parsed, text
    return None, text


async def _generate_top5_from_standby_payload(
    input_data: dict[str, Any],
    standby_payload: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str]:
    route = _build_route_string(input_data)
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
    parsed = _extract_json_from_text(text)
    if isinstance(parsed, list):
        return parsed, text
    return None, text


def _is_valid_date_mmddyyyy(value: str) -> bool:
    try:
        datetime.strptime(value, "%m/%d/%Y")
        return True
    except Exception:
        return False


def _validate_and_normalize_input(input_data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    normalized = dict(input_data or {})

    flight_type = (normalized.get("flight_type") or "").strip()
    travel_status = (normalized.get("travel_status") or "").strip()
    if not flight_type:
        errors.append("flight_type is required.")
    if not travel_status:
        errors.append("travel_status is required.")

    normalized["airline"] = normalized.get("airline") or ""
    if "nonstop_flights" not in normalized or normalized.get("nonstop_flights") in ("", None):
        normalized["nonstop_flights"] = False
    if "auto_request_stafftraveler" not in normalized or normalized.get("auto_request_stafftraveler") in (
        "",
        None,
    ):
        normalized["auto_request_stafftraveler"] = False

    trips = normalized.get("trips")
    if not isinstance(trips, list) or not trips:
        errors.append("trips must be a non-empty array.")
        trips = []
    for idx, trip in enumerate(trips):
        if not isinstance(trip, dict):
            errors.append(f"trips[{idx}] must be an object.")
            continue
        if not (trip.get("origin") or "").strip():
            errors.append(f"trips[{idx}].origin is required.")
        if not (trip.get("destination") or "").strip():
            errors.append(f"trips[{idx}].destination is required.")

    itinerary = normalized.get("itinerary")
    if not isinstance(itinerary, list) or not itinerary:
        errors.append("itinerary must be a non-empty array.")
        itinerary = []
    for idx, leg in enumerate(itinerary):
        if not isinstance(leg, dict):
            errors.append(f"itinerary[{idx}] must be an object.")
            continue
        date_val = (leg.get("date") or "").strip()
        time_val = (leg.get("time") or "").strip()
        class_val = (leg.get("class") or "").strip()
        if not date_val:
            errors.append(f"itinerary[{idx}].date is required.")
        elif not _is_valid_date_mmddyyyy(date_val):
            errors.append(f"itinerary[{idx}].date must be MM/DD/YYYY.")
        if not time_val:
            errors.append(f"itinerary[{idx}].time is required.")
        if not class_val:
            errors.append(f"itinerary[{idx}].class is required.")

    if flight_type == "one-way":
        if len(trips) < 1:
            errors.append("one-way requires at least 1 trip.")
        if len(itinerary) < 1:
            errors.append("one-way requires at least 1 itinerary entry.")
    elif flight_type == "round-trip":
        if len(trips) < 2:
            errors.append("round-trip requires 2 trips.")
        if len(itinerary) < 2:
            errors.append("round-trip requires 2 itinerary entries.")
    elif flight_type == "multiple-legs":
        if len(trips) < 1:
            errors.append("multiple-legs requires at least 1 trip.")
        if len(itinerary) < 1:
            errors.append("multiple-legs requires at least 1 itinerary entry.")

    travellers = normalized.get("traveller", [])
    if travellers and not isinstance(travellers, list):
        errors.append("traveller must be an array.")
        travellers = []
    for idx, traveller in enumerate(travellers or []):
        if not isinstance(traveller, dict):
            errors.append(f"traveller[{idx}] must be an object.")
            continue
        name_val = (traveller.get("name") or "").strip()
        salutation_val = (traveller.get("salutation") or "").strip().upper()
        checked_val = traveller.get("checked")
        if not name_val:
            errors.append(f"traveller[{idx}].name is required.")
        if salutation_val not in {"MR", "MS"}:
            errors.append(f"traveller[{idx}].salutation must be MR or MS.")
        if not isinstance(checked_val, bool):
            errors.append(f"traveller[{idx}].checked must be a boolean.")
        traveller["salutation"] = salutation_val

    partners = normalized.get("travel_partner", [])
    if partners and not isinstance(partners, list):
        errors.append("travel_partner must be an array.")
        partners = []
    for idx, partner in enumerate(partners or []):
        if not isinstance(partner, dict):
            errors.append(f"travel_partner[{idx}] must be an object.")
            continue
        p_type = (partner.get("type") or "").strip().lower()
        if p_type not in {"adult", "child"}:
            errors.append(f"travel_partner[{idx}].type must be Adult or Child.")
        first_name = (partner.get("first_name") or "").strip()
        last_name = (partner.get("last_name") or "").strip()
        if not first_name:
            errors.append(f"travel_partner[{idx}].first_name is required.")
        if not last_name:
            errors.append(f"travel_partner[{idx}].last_name is required.")
        if "own_seat" not in partner or partner.get("own_seat") is None:
            partner["own_seat"] = True
        if p_type == "adult":
            salutation_val = (partner.get("salutation") or "").strip().upper()
            if salutation_val not in {"MR", "MS"}:
                errors.append(f"travel_partner[{idx}].salutation must be MR or MS.")
            partner["salutation"] = salutation_val
        if p_type == "child":
            dob_val = (partner.get("dob") or "").strip()
            if not dob_val:
                errors.append(f"travel_partner[{idx}].dob is required for Child.")
            elif not _is_valid_date_mmddyyyy(dob_val):
                errors.append(f"travel_partner[{idx}].dob must be MM/DD/YYYY.")

    return normalized, errors


async def _notify_invalid_input(errors: list[str]) -> None:
    if not slack_web_client or not SLACK_ENABLED:
        return
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        return
    message = "*Run blocked: invalid input*\n" + "\n".join(f"• {err}" for err in errors)
    try:
        await slack_web_client.chat_postMessage(channel=channel, text=message)
    except Exception as exc:
        logger.error("Failed to send invalid input notification: %s", exc, exc_info=True)


async def _notify_validation_errors(state: "RunState", errors: list[str]) -> None:
    if not slack_web_client or not SLACK_ENABLED:
        return
    message = "*Run blocked: invalid input*\n" + "\n".join(f"• {err}" for err in errors)
    channel = state.slack_channel or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        return
    try:
        await slack_web_client.chat_postMessage(
            channel=channel,
            thread_ts=state.slack_thread_ts,
            text=message,
        )
    except Exception as exc:
        logger.error("Failed to send validation errors: %s", exc, exc_info=True)


async def _notify_thread_message(state: "RunState", message: str) -> None:
    if not slack_web_client or not SLACK_ENABLED:
        return
    channel = state.slack_channel or os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        return
    try:
        await slack_web_client.chat_postMessage(
            channel=channel,
            thread_ts=state.slack_thread_ts,
            text=message,
        )
    except Exception as exc:
        logger.error("Failed to send thread message: %s", exc, exc_info=True)


def normalize_google_time(time_str: str) -> str | None:
    """Converts 12h time (Google) to 24h 'HH:MM' for matching."""
    try:
        time_str = time_str.replace("\u202f", " ").strip()
        return datetime.strptime(time_str, "%I:%M %p").strftime("%H:%M")
    except Exception:
        return None


def to_minutes(duration_str: str) -> int:
    """Converts duration strings like '7h 25m' to total minutes."""
    if not duration_str:
        return 1440
    try:
        clean = duration_str.lower().replace("hr", "h").replace("min", "m").replace(" ", "")
        h = int(clean.split("h")[0]) if "h" in clean else 0
        m_part = clean.split("h")[-1] if "h" in clean else clean
        m = int(m_part.replace("m", "")) if "m" in m_part else 0
        return h * 60 + m
    except Exception:
        return 1440


async def run_myidtravel(state: RunState, headed: bool) -> dict[str, Any]:
    await state.log("[myidtravel] starting")
    notify: Callable[[str], Awaitable[None]] | None = None
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
    notify: Callable[[str], Awaitable[None]] | None = None
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
    notify: Callable[[str], Awaitable[None]] | None = None

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

        # Send initial Slack notification if run was triggered from web interface
        if not state.slack_channel:  # Only for web interface runs (not Slack-triggered)
            await state.send_initial_slack_notification()

        normalized_input, errors = _validate_and_normalize_input(state.input_data)
        if errors:
            await state.log("Run aborted: invalid input.")
            await _notify_validation_errors(state, errors)
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
            await _notify_thread_message(state, message)
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
            await _notify_thread_message(state, message)
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
            await _notify_thread_message(state, message)
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
            await _notify_thread_message(state, message)
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
            await _notify_thread_message(state, message)
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
                    gemini_payload = _extract_json_from_text(gemini_raw)
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


async def _page_has_form(page: Page) -> bool:
    selectors = [
        "text=Find Flights",
        'input[placeholder*="Origin" i]',
        'input[placeholder*="Destination" i]',
        "select",
    ]
    for sel in selectors:
        try:
            handle = page.locator(sel).first
            if await handle.is_visible():
                return True
        except Exception:
            continue
    return False


async def _goto_home(page: Page, url_override: str | None = None, extra_wait_ms: int = 0) -> str:
    urls = [url_override] if url_override else config.BASE_URLS
    last_error: Exception | None = None
    tried: list[str] = []

    async def _blocking_message() -> str | None:
        text_nodes = await page.locator("text=eligible for OA travel").all_text_contents()
        if text_nodes:
            return "User is not eligible for OA travel on this account/session."
        return None

    for url in urls:
        tried.append(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2000 + extra_wait_ms)
            current_url = page.url
            if "signon" in current_url:
                raise RuntimeError("Redirected to signon.ual.com; auth_state.json may be expired.")
            blocking = await _blocking_message()
            if blocking:
                raise RuntimeError(blocking)
            if await _page_has_form(page):
                return current_url
            await page.wait_for_timeout(3000 + extra_wait_ms)
            blocking = await _blocking_message()
            if blocking:
                raise RuntimeError(blocking)
            if await _page_has_form(page):
                return current_url
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Failed to load myIDTravel home page from {tried}. Last error: {last_error}")


async def _extract_airline_options(page: Page) -> list[dict[str, Any]]:
    airline_input = page.locator("#input-airline, input[aria-autocomplete='list'][role='combobox']")
    if not await airline_input.count():
        value_handle = page.locator("text=All Airlines").first
        if await value_handle.is_visible():
            await value_handle.click()
        indicator = page.locator('[aria-haspopup="true"], .css-1xc3v61-indicatorContainer').first
        if await indicator.is_visible():
            await indicator.click()

    if await airline_input.count():
        await airline_input.first.click()
        await airline_input.first.press("ArrowDown")
        await page.wait_for_timeout(250)

        options = await page.evaluate(
            """
            async () => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const menu = document.querySelector('[role="listbox"]') || document.querySelector('.css-5736gi-menu');
                if (!menu) return [];
                const scrollable = menu.querySelector('[style*="overflow: auto"]') || menu;
                const seen = new Map();

                const capture = () => {
                    const opts = menu.querySelectorAll('[role="option"]');
                    opts.forEach(opt => {
                        const raw = (opt.textContent || '').trim();
                        const codeEl = opt.querySelector('#airline-code-container');
                        const code = codeEl ? (codeEl.textContent || '').trim() : null;
                        const label = code ? raw.replace(code, '').trim() : raw;
                        const value = opt.getAttribute('data-value') || opt.getAttribute('value') || code || label;
                        const disabled = opt.getAttribute('aria-disabled') === 'true';
                        const selected = opt.getAttribute('aria-selected') === 'true';
                        const key = code || value || label;
                        seen.set(key, { value: code || value || label, label, disabled, selected });
                    });
                };

                const step = Math.max(40, Math.floor(scrollable.clientHeight * 0.6));
                const totalHeight = scrollable.scrollHeight;

                for (let pos = 0; pos <= totalHeight + step; pos += step) {
                    scrollable.scrollTop = pos;
                    scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                    await sleep(60);
                    capture();
                }
                for (let pos = totalHeight; pos >= 0; pos -= step) {
                    scrollable.scrollTop = pos;
                    scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                    await sleep(60);
                    capture();
                }
                scrollable.scrollTop = scrollable.scrollHeight;
                scrollable.dispatchEvent(new Event('scroll', { bubbles: true }));
                await sleep(120);
                capture();

                return Array.from(seen.values());
            }
            """
        )
        if options:
            return options

    raise RuntimeError("Airline dropdown not found. Is the page layout different?")


async def _capture_origin_lookup(page: Page, query: str) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    keywords = ("airport", "origin", "destination", "lookup", "suggest")

    async def handle_response(response) -> None:
        try:
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            url_lower = response.url.lower()
            if not any(k in url_lower for k in keywords):
                return
            try:
                body = await response.json()
            except Exception:
                body = await response.text()
            captured.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "headers": dict(response.headers),
                    "body": body,
                }
            )
        except Exception:
            return

    page.on("response", lambda resp: asyncio.create_task(handle_response(resp)))

    origin_input = page.locator('input[placeholder*="Origin" i]').first
    await origin_input.click()
    await origin_input.fill("")
    await origin_input.type(query, delay=60)
    await page.wait_for_timeout(2500)

    if captured:
        ORIGIN_LOOKUP_OUTPUT.write_text(json.dumps(captured, indent=2))
    return captured


async def _get_csrf_token(context) -> str | None:
    try:
        cookies = await context.cookies()
        for c in cookies:
            if c.get("name", "").lower() in {"csrf", "xsrf-token", "x-csrf-token"}:
                return c.get("value")
    except Exception:
        pass
    return None


async def _fetch_airport_picker(
    page: Page,
    context,
    term: str,
    url_base: str,
    page_num: int = 1,
    limit: int = 25,
    csrf_override: str | None = None,
) -> dict[str, Any]:
    csrf_token = csrf_override or await _get_csrf_token(context)
    if not csrf_token:
        raise RuntimeError("Could not find CSRF token. Provide one via request body or refresh auth.")

    endpoint = url_base.rstrip("/") + "/json/general/airportPicker"
    payload = {
        "term": term,
        "page": page_num,
        "start": 0 if page_num <= 1 else (page_num - 1) * limit,
        "limit": limit,
        "csrf": csrf_token,
    }

    resp = await page.request.post(endpoint, data=payload)
    if not resp.ok:
        raise RuntimeError(f"airportPicker request failed {resp.status}: {await resp.text()}")
    data = await resp.json()
    AIRPORT_PICKER_OUTPUT.write_text(json.dumps(data, indent=2))
    return data


async def scrape_airlines_task(
    headless: bool = True,
    sample_origin_query: str | None = None,
    url_override: str | None = None,
    extra_wait_ms: int = 0,
    airport_term: str | None = None,
    csrf_override: str | None = None,
) -> dict[str, Any]:
    storage_file = Path("auth_state.json")
    if not storage_file.exists():
        raise RuntimeError("auth_state.json not found. Run the myIDTravel bot first to create it.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(storage_file))
        page = await context.new_page()

        home_url = await _goto_home(page, url_override=url_override, extra_wait_ms=extra_wait_ms)

        airlines = await _extract_airline_options(page)
        AIRLINE_OUTPUT.write_text(json.dumps(airlines, indent=2))

        airport_picker_payload = None
        if airport_term:
            airport_picker_payload = await _fetch_airport_picker(
                page=page,
                context=context,
                term=airport_term,
                url_base=home_url,
                csrf_override=csrf_override,
            )

        origin_lookup_payload = None
        if sample_origin_query:
            origin_lookup_payload = await _capture_origin_lookup(page, sample_origin_query)

        await context.close()
        await browser.close()

    return {
        "home_url": home_url,
        "airlines_count": len(airlines),
        "airlines_path": str(AIRLINE_OUTPUT),
        "airport_picker_path": str(AIRPORT_PICKER_OUTPUT) if airport_picker_payload else None,
        "origin_lookup_path": str(ORIGIN_LOOKUP_OUTPUT) if origin_lookup_payload else None,
    }


async def _run_generate_flight_loads(
    state: RunState,
    myid_payload: list[dict[str, Any]] | dict[str, Any] | None,
    staff_payload: list[dict[str, Any]] | None,
    google_payload: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """
    Generate standby_report_multi.* using available data with fallback handling.
    Works even if one or more JSON sources are missing.
    Uses 'chance' as proxy for load since exact seat counts unavailable.
    """
    await state.log("[report] Starting flight loads report generation...")

    gemini_payload: dict[str, Any] | None = None
    try:
        # Load sources with fallback to empty data
        if isinstance(myid_payload, list):
            myid_data = {"routings": myid_payload}
        else:
            myid_data = myid_payload or {}
        google_data = google_payload or []
        staff_data = staff_payload or []
        gemini_top_flights: list[dict[str, Any]] | None = None

        if myid_data:
            await state.log("[report] Loaded MyIDTravel data")
        else:
            await state.log("[report] MyIDTravel data not available")

        if google_data:
            await state.log("[report] Loaded Google Flights data")
        else:
            await state.log("[report] Google Flights data not available")

        if staff_data:
            await state.log("[report] Loaded StaffTraveler data")
        else:
            await state.log("[report] StaffTraveler data not available")

        if config.FINAL_OUTPUT_FORMAT == "gemini":
            try:
                await state.log("[report] Generating top flights with Gemini")
                gemini_top_flights, gemini_raw = await _generate_flight_loads_gemini(
                    state.input_data,
                    myid_data,
                    staff_data,
                    google_data,
                )
                if gemini_raw:
                    gemini_payload = _extract_json_from_text(gemini_raw)
                if not gemini_top_flights:
                    await state.log("[report] Gemini returned no usable top flights; falling back to default")
            except Exception as exc:
                await state.log(f"[report] Gemini generation failed: {exc}")
                gemini_top_flights = None

        # If no data at all, cannot proceed
        if not myid_data and not staff_data and not google_data:
            await state.log("[report] No data available from any source")
            return {"gemini_payload": gemini_payload}

        # Build flight registry
        registry = {}

        # 1. Process MyIDTravel (primary source for availability)
        myid_flights = [flight for routing in myid_data.get("routings", []) for flight in routing.get("flights", [])]
        if not myid_flights:
            await _notify_thread_message(
                state,
                "Flight load not available: MyIDTravel returned no flights for this search.",
            )

        for routing in myid_data.get("routings", []):
            for f in routing.get("flights", []):
                seg = f["segments"][0]
                fn = seg["flightNumber"]

                registry[fn] = {
                    "Flight": fn,
                    "Airline": seg["operatingAirline"]["name"],
                    "Aircraft": seg.get("aircraft", "N/A"),
                    "Departure": seg["departureTime"],
                    "Arrival": seg["arrivalTime"],
                    "Duration": to_minutes(f.get("duration", "0h 0m")),
                    "Duration_Str": f.get("duration", "N/A"),
                    "Stops": seg.get("stopQuantity", 0),
                    "Selectable": f.get("selectable", False),
                    "Chance": f.get("chance", "Unknown"),
                    "Tarif": f.get("tarif", "Unknown"),
                    "Price": None,  # Will be set from Google
                    "Sources": {"MyIDTravel"},
                }

        # 2. Process StaffTraveler
        staff_flights = [flight for entry in staff_data for flight in entry.get("flight_details", [])]
        if not staff_flights:
            await _notify_thread_message(
                state,
                "Flight load not available: StaffTraveler returned no flights for this search.",
            )

        for entry in staff_data:
            for f in entry.get("flight_details", []):
                fn = f["airline_flight_number"]

                if fn not in registry:
                    # Create new entry if not in MyIDTravel
                    registry[fn] = {
                        "Flight": fn,
                        "Airline": f["airlines"],
                        "Aircraft": f.get("aircraft", "N/A"),
                        "Departure": "N/A",
                        "Arrival": "N/A",
                        "Duration": to_minutes(f.get("duration", "0h 0m")),
                        "Duration_Str": f.get("duration", "N/A"),
                        "Stops": 0,
                        "Selectable": False,  # Unknown without MyIDTravel
                        "Chance": "Unknown",
                        "Tarif": "Unknown",
                        "Price": None,
                        "Sources": set(),
                    }

                registry[fn]["Sources"].add("Stafftraveler")

                # Update timing info if missing
                if registry[fn]["Departure"] == "N/A":
                    time_parts = f.get("time", "").split(" - - - ")
                    if len(time_parts) == 2:
                        registry[fn]["Departure"] = time_parts[0].strip()
                        registry[fn]["Arrival"] = time_parts[1].strip()

        # 3. Process Google Flights (prices only if bookable)
        google_flights = []
        for entry in google_data:
            flights_data = entry.get("flights", {})
            google_flights.extend(flights_data.get("top_flights", []))
            google_flights.extend(flights_data.get("other_flights", []))
            google_flights.extend(flights_data.get("all", []))
        if not google_flights:
            await _notify_thread_message(
                state,
                "Flight load not available: Google Flights returned no flights for this search.",
            )

        travel_status_lower = (state.input_data.get("travel_status") or "").strip().lower()
        if travel_status_lower == "bookable":
            for entry in google_data:
                flights_data = entry.get("flights", {})
                all_google = flights_data.get("top_flights", []) + flights_data.get("other_flights", [])
                if not all_google:
                    all_google = flights_data.get("all", [])
                for g_f in all_google:
                    fn_match = re.search(r"\b([A-Z]{2,3}\d{1,4})\b", g_f.get("summary", ""))
                    fn = fn_match.group(1) if fn_match else None
                    price = None
                    price_str = g_f.get("price") or g_f.get("summary") or ""
                    price_match = re.search(r"(\d[\d,]*)", price_str.replace(" ", ""))
                    if price_match:
                        try:
                            price = int(price_match.group(1).replace(",", ""))
                        except Exception:
                            price = None
                    if fn and fn in registry:
                        if price is not None:
                            registry[fn]["Price"] = price
                        registry[fn]["Sources"].add("Google Flights")
                    else:
                        g_airline = g_f.get("airline", "")
                        g_duration = to_minutes(g_f.get("duration", "0h 0m"))
                        for _reg_fn, data in registry.items():
                            if data["Airline"] == g_airline and abs(data["Duration"] - g_duration) <= 5:
                                if price is not None:
                                    data["Price"] = price
                                data["Sources"].add("Google Flights")
                                break

        await state.log(f"[report] Built registry with {len(registry)} flights")

        valid_durations = [f["Duration"] for f in registry.values() if f["Duration"] > 0]
        min_dur = min(valid_durations) if valid_durations else 420  # 7 hours default

        def calculate_standby_load_score(f):
            """Calculate load score based on available data."""
            chance_map = {
                "HIGH": 1000,
                "MEDIUM": 600,
                "MID": 600,
                "LOW": 200,
                "Unknown": 300,
            }
            load_score = chance_map.get(f["Chance"], 300)
            nonstop_bonus = 300 if f["Stops"] == 0 else 0
            duration_bonus = (min_dur / f["Duration"]) * 150 if f["Duration"] > 0 else 0
            tarif_map = {"MID": 100, "HIGH": 50, "LOW": 150, "Unknown": 0}
            tarif_bonus = tarif_map.get(f["Tarif"], 0)
            return load_score + nonstop_bonus + duration_bonus + tarif_bonus

        # Standby ranking (use all registry flights, neutral score when unknown)
        standby_flights = []
        low_load_alerts = []
        for f in registry.values():
            score = calculate_standby_load_score(f)
            standby_flights.append(
                {
                    "Flight": f["Flight"],
                    "Airline": f["Airline"],
                    "Aircraft": f["Aircraft"],
                    "Departure": f["Departure"],
                    "Arrival": f["Arrival"],
                    "Duration": f["Duration_Str"],
                    "Stops": f["Stops"],
                    "Chance": f["Chance"],
                    "Source": ", ".join(sorted(list(f["Sources"]))),
                    "Score": round(score, 2),
                }
            )
            if f["Chance"] == "LOW":
                low_load_alerts.append(f"⚠️ {f['Flight']} ({f['Airline']}) - LOW availability")

        standby_flights.sort(key=lambda x: x["Score"], reverse=True)
        if not standby_flights:
            await state.log("[ALERT]️ No standby flights found!")
            await _notify_thread_message(
                state,
                "No standby flights found.",
            )

        top_5_standby = [
            {
                "Rank": i + 1,
                "Flight": item["Flight"],
                "Airline": item["Airline"],
                "Aircraft": item["Aircraft"],
                "Departure": item["Departure"],
                "Arrival": item["Arrival"],
                "Duration": item["Duration"],
                "Stops": item["Stops"],
                "Chance": item["Chance"],
                "Source": item["Source"],
            }
            for i, item in enumerate(standby_flights[:5])
        ]

        # Bookable flights (only when travel_status is bookable)
        bookable_flights = []
        if travel_status_lower == "bookable":
            for fn, f in registry.items():
                if f["Price"] is None or f["Price"] <= 0:
                    continue
                score = 0
                score += max(0, 1000 - (f["Price"] / max(f["Price"], 1)) * 1000)
                preferred = ["A380", "77W", "789", "781", "359"]
                if any(p in f["Aircraft"] for p in preferred):
                    score += 200
                if f["Stops"] == 0:
                    score += 200
                if f["Duration"] > 0:
                    score += (min_dur / f["Duration"]) * 300

                bookable_flights.append(
                    {
                        "Flight": fn,
                        "Airline": f["Airline"],
                        "Aircraft": f["Aircraft"],
                        "Departure": f["Departure"],
                        "Arrival": f["Arrival"],
                        "Duration": f["Duration_Str"],
                        "Stops": f["Stops"],
                        "Price": f["Price"],
                        "Source": ", ".join(sorted(list(f["Sources"]))),
                        "Score": round(score, 2),
                    }
                )
            bookable_flights.sort(key=lambda x: x["Score"], reverse=True)

        top_5_bookable = [
            {
                "Rank": i + 1,
                "Flight": item["Flight"],
                "Airline": item["Airline"],
                "Aircraft": item["Aircraft"],
                "Departure": item["Departure"],
                "Arrival": item["Arrival"],
                "Duration": item["Duration"],
                "Stops": item["Stops"],
                "Price": item["Price"],
                "Source": item["Source"],
            }
            for i, item in enumerate(bookable_flights[:5])
        ]

        if gemini_top_flights:
            if travel_status_lower == "bookable":
                top_5_bookable = gemini_top_flights
            else:
                top_5_standby = gemini_top_flights

        # Prepare data for all source sheets (filtered by mode)
        myid_all_flights = []
        for routing in myid_data.get("routings", []):
            for f in routing.get("flights", []):
                seg = f["segments"][0]
                myid_all_flights.append(
                    {
                        "Flight": seg["flightNumber"],
                        "Airline": seg["operatingAirline"]["name"],
                        "Aircraft": seg.get("aircraft", "N/A"),
                        "Departure": seg["departureTime"],
                        "Arrival": seg["arrivalTime"],
                        "Duration": f.get("duration", "N/A"),
                        "Stops": seg.get("stopQuantity", 0),
                        "Chance": f.get("chance", "N/A"),
                        "Tarif": f.get("tarif", "N/A"),
                        "Selectable": "YES" if f.get("selectable", False) else "NO",
                    }
                )

        staff_all_flights = []
        for entry in staff_data:
            for f in entry.get("flight_details", []):
                staff_all_flights.append(
                    {
                        "Flight": f["airline_flight_number"],
                        "Airline": f["airlines"],
                        "Aircraft": f.get("aircraft", "N/A"),
                        "Time": f.get("time", "N/A"),
                        "Duration": f.get("duration", "N/A"),
                    }
                )

        google_all_flights = []
        for entry in google_data:
            flights_data = entry.get("flights", {})
            all_gf = flights_data.get("top_flights", []) + flights_data.get("other_flights", [])
            if not all_gf:
                all_gf = flights_data.get("all", [])
            for g_f in all_gf:
                google_all_flights.append(
                    {
                        "Airline": g_f.get("airline", "N/A"),
                        "Departure": g_f.get("depart_time", "N/A"),
                        "Arrival": g_f.get("arrival_time", "N/A"),
                        "Duration": g_f.get("duration", "N/A"),
                        "Stops": g_f.get("stops", "N/A"),
                        "Price": g_f.get("price", "N/A"),
                        "Emissions": g_f.get("emissions", "N/A"),
                    }
                )

        # Prepare input summary
        input_data = state.input_data
        input_summary = []

        # Basic parameters
        input_summary.append({"Parameter": "Flight Type", "Value": input_data.get("flight_type", "N/A")})
        input_summary.append(
            {"Parameter": "Nonstop Only", "Value": "Yes" if input_data.get("nonstop_flights", False) else "No"}
        )
        input_summary.append(
            {"Parameter": "Airline", "Value": input_data.get("airline", "All Airlines") or "All Airlines"}
        )
        input_summary.append({"Parameter": "Travel Status", "Value": input_data.get("travel_status", "N/A")})
        input_summary.append({"Parameter": "", "Value": ""})

        # Trip information
        trips = input_data.get("trips", [])
        for idx, trip in enumerate(trips, 1):
            if len(trips) == 1:
                input_summary.append({"Parameter": "Origin", "Value": trip.get("origin", "N/A")})
                input_summary.append({"Parameter": "Destination", "Value": trip.get("destination", "N/A")})
            else:
                input_summary.append({"Parameter": f"Trip {idx} - Origin", "Value": trip.get("origin", "N/A")})
                input_summary.append(
                    {"Parameter": f"Trip {idx} - Destination", "Value": trip.get("destination", "N/A")}
                )
        input_summary.append({"Parameter": "", "Value": ""})

        # Itinerary
        itinerary = input_data.get("itinerary", [])
        for idx, itin in enumerate(itinerary, 1):
            if len(itinerary) == 1:
                input_summary.append({"Parameter": "Date", "Value": itin.get("date", "N/A")})
                input_summary.append({"Parameter": "Time", "Value": itin.get("time", "N/A")})
                input_summary.append({"Parameter": "Class", "Value": itin.get("class", "N/A")})
            else:
                input_summary.append({"Parameter": f"Leg {idx} - Date", "Value": itin.get("date", "N/A")})
                input_summary.append({"Parameter": f"Leg {idx} - Time", "Value": itin.get("time", "N/A")})
                input_summary.append({"Parameter": f"Leg {idx} - Class", "Value": itin.get("class", "N/A")})
        input_summary.append({"Parameter": "", "Value": ""})

        # Travelers
        travellers = input_data.get("traveller", [])
        input_summary.append({"Parameter": "Number of Travellers", "Value": len(travellers)})
        for idx, traveller in enumerate(travellers, 1):
            name = f"{traveller.get('salutation', '')} {traveller.get('name', 'N/A')}".strip()
            input_summary.append({"Parameter": f"Traveller {idx}", "Value": name})
        input_summary.append({"Parameter": "", "Value": ""})

        # Results summary
        input_summary.append({"Parameter": "--- Results Summary ---", "Value": ""})
        input_summary.append({"Parameter": "Total Flights Found", "Value": len(registry)})
        input_summary.append(
            {"Parameter": "Selectable Flights", "Value": len([f for f in registry.values() if f["Selectable"]])}
        )
        input_summary.append({"Parameter": "MyIDTravel Flights", "Value": len(myid_all_flights)})
        input_summary.append({"Parameter": "Stafftraveler Flights", "Value": len(staff_all_flights)})
        input_summary.append({"Parameter": "Google Flights Results", "Value": len(google_all_flights)})

        # Build results for Excel
        results = {
            "Input_Summary": input_summary,
            "MyIDTravel_All": myid_all_flights,
            "Stafftraveler_All": staff_all_flights,
            "Google_Flights_All": google_all_flights,
        }
        if travel_status_lower == "bookable":
            results["Top_5_Bookable"] = top_5_bookable
        else:
            results["Top_5_R2_Standby"] = top_5_standby

        # Generate Excel
        try:
            import pandas as pd

            excel_output = state.output_dir / "standby_report_multi.xlsx"

            with pd.ExcelWriter(excel_output, engine="openpyxl") as writer:
                pd.DataFrame(input_summary).to_excel(writer, sheet_name="Input", index=False)

                if travel_status_lower == "bookable" and top_5_bookable:
                    pd.DataFrame(top_5_bookable).to_excel(writer, sheet_name="Bookable", index=False)

                if travel_status_lower != "bookable" and top_5_standby:
                    pd.DataFrame(top_5_standby).to_excel(writer, sheet_name="R2 Standby", index=False)

                if myid_all_flights:
                    pd.DataFrame(myid_all_flights).to_excel(writer, sheet_name="MyIDTravel", index=False)

                if staff_all_flights:
                    pd.DataFrame(staff_all_flights).to_excel(writer, sheet_name="Stafftraveler", index=False)

                if google_all_flights:
                    pd.DataFrame(google_all_flights).to_excel(writer, sheet_name="Google Flights", index=False)

            state.result_files["standby_report_multi.xlsx"] = excel_output
            await state.log(f"[report] Generated {excel_output}")
        except ImportError:
            await state.log("[report] Pandas not available, skipping Excel")
        except Exception as exc:
            await state.log(f"[report] Excel generation failed: {exc}")

        # Summary logs
        await state.log("[report] Report complete:")
        await state.log(f"  - Total flights: {len(registry)}")
        await state.log(f"  - Top 5 Standby (Plan A-E): {len(top_5_standby)}")
        await state.log(f"  - Top 5 Bookable: {len(top_5_bookable)}")
        if low_load_alerts:
            await state.log(f"  -️  {len(low_load_alerts)} low availability alerts")
        return {
            "gemini_payload": gemini_payload,
            "top_5_standby": top_5_standby,
            "top_5_bookable": top_5_bookable,
        }

    except Exception as exc:
        await state.log(f"[report] Error: {exc}")
        import traceback

        await state.log(f"[report] {traceback.format_exc()}")
        return {"gemini_payload": gemini_payload}


@app.on_event("startup")
async def startup_event():
    """Start Slack bot on application startup"""
    if SLACK_ENABLED:
        asyncio.create_task(start_slack_bot())
    logger.info("FastAPI application started")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop Slack bot on application shutdown"""
    await stop_slack_bot()
    logger.info("FastAPI application stopped")


@app.get("/api/accounts")
async def account_options():
    return {"accounts": get_account_options()}


@app.get("/api/airlines")
async def get_airlines():
    return {"airlines": list_airlines()}


@app.get("/api/stafftraveler-accounts")
async def stafftraveler_accounts():
    return {"accounts": list_stafftraveler_accounts()}


@app.post("/api/airlines/refresh")
async def refresh_airlines():
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await myidtravel_bot.perform_login(
                context=context,
                headless=True,
                screenshot=None,
            )
            await scrape_airlines_helper.goto_home(page)
            airlines = await scrape_airlines_helper.extract_airline_options(page)
            await context.close()
            await browser.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to refresh airlines: {exc}") from exc

    save_airlines(airlines)
    return {"airlines": airlines, "count": len(airlines)}


@app.get("/api/slack/status")
async def slack_status():
    """Check if Slack integration is enabled and connected"""
    return {
        "enabled": SLACK_ENABLED,
        "connected": slack_connected,
        "commands": ["run scraper [origin] [destination] [date]", "scraper status"],
    }


@app.websocket("/ws/{run_id}")
async def logs_ws(websocket: WebSocket, run_id: str):
    state = RUNS.get(run_id)
    if not state:
        await websocket.close(code=4000)
        return

    await websocket.accept()
    queue = state.subscribe(websocket)

    # Send existing logs to new subscribers.
    for log in state.logs:
        await websocket.send_json(log)
    await websocket.send_json({"type": "status", "status": state.status, "run_id": state.id})

    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
            if payload.get("type") == "status" and payload.get("status") in {"completed", "error"}:
                break
    except WebSocketDisconnect:
        state.unsubscribe(websocket)


@app.post("/api/run")
async def start_run(payload: dict[str, Any] = BODY_REQUIRED):
    input_data = payload.get("input") if isinstance(payload, dict) else None
    if not input_data:
        input_data = payload

    if not isinstance(input_data, dict):
        await _notify_invalid_input(["Request body must be a JSON object."])
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    limit = payload.get("limit") if isinstance(payload, dict) else None
    limit = int(limit) if isinstance(limit, int) or (isinstance(limit, str) and limit.isdigit()) else 30
    headed = bool(payload.get("headed")) if isinstance(payload, dict) else False
    input_data, errors = _validate_and_normalize_input(input_data)
    if errors:
        await _notify_invalid_input(errors)
        raise HTTPException(status_code=400, detail={"errors": errors})

    run_id = make_run_id()
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_id, run_dir, input_data)
    RUNS[run_id] = state
    create_run_record(
        run_id=run_id,
        input_data=input_data,
        output_dir=run_dir,
        status=state.status,
        run_type="standard",
    )

    logger.info("Queued run %s", run_id)
    asyncio.create_task(execute_run(state, limit=limit, headed=headed))
    return {"run_id": run_id, "status": state.status, "output_dir": str(run_dir)}


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
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    if economy_payload:
        merged = copy.deepcopy(economy_payload)
    else:
        merged = copy.deepcopy(business_payload)

    def _index(payload: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for section in payload or []:
            if not isinstance(section, dict):
                continue
            flights = section.get("flights") or {}
            for bucket in ("top_flights", "other_flights"):
                for flight in flights.get(bucket, []) if isinstance(flights, dict) else []:
                    number = _normalize_flight_number(flight.get("flight_number"))
                    if number:
                        index[number] = flight
        return index

    def _set_google_class_seat(flight: dict[str, Any], class_key: str, seat_value: str | None) -> None:
        if not seat_value:
            return
        seats = flight.get("seats") or {}
        google_seats = seats.get("google_flights") or {}
        if not google_seats.get(class_key):
            google_seats[class_key] = seat_value
        seats["google_flights"] = google_seats
        flight["seats"] = seats

    business_index = _index(business_payload)
    for section in merged:
        flights = section.get("flights") or {}
        for bucket in ("top_flights", "other_flights"):
            for flight in flights.get(bucket, []) if isinstance(flights, dict) else []:
                number = _normalize_flight_number(flight.get("flight_number"))
                if not number:
                    continue
                _set_google_class_seat(flight, "economy", flight.get("seats_available"))
                business_flight = business_index.get(number)
                if not business_flight:
                    continue
                _set_google_class_seat(flight, "business", business_flight.get("seats_available"))
    return merged


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


def _set_google_class_seat(flight: dict[str, Any], class_key: str, seat_value: str | None) -> None:
    if not seat_value:
        return
    seats = flight.get("seats") or {}
    google_seats = seats.get("google_flights") or {}
    if not google_seats.get(class_key):
        google_seats[class_key] = seat_value
    seats["google_flights"] = google_seats
    flight["seats"] = seats


async def execute_find_flight(
    state: RunState,
    headed: bool,
    staff_account: StafftravelerAccount,
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

            async def _run_google(idx, leg_input):
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
                        screenshot=str(state.output_dir / f"google_flights_final_{idx + 1}.png"),
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
                        screenshot=str(state.output_dir / f"google_flights_final_{idx + 1}.png"),
                        input_data=leg_input,
                        progress_cb=lambda percent, status: state.progress("google_flights", percent, status),
                    )

            async def _run_staff(idx, leg_input):
                nonlocal staff_payload
                staff_payload = await stafftraveler_bot.perform_stafftraveller_login(
                    headless=not headed,
                    screenshot=str(state.output_dir / f"stafftraveler_final_{idx + 1}.png"),
                    input_data=leg_input,
                    output_path=None,
                    username=staff_account.username,
                    password=staff_account.password,
                    progress_cb=lambda percent, status: state.progress("stafftraveler", percent, status),
                )

            try:
                await asyncio.gather(_run_google(idx, leg_input), _run_staff(idx, leg_input))
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

            google_flight = _extract_lookup_google_flight(google_payload)
            if google_flight and seat_choice in {"economy", "business"}:
                _set_google_class_seat(google_flight, seat_choice, google_flight.get("seats_available"))

            legs_results.append(
                {
                    "index": idx,
                    "flight_number": flight_number,
                    "google_flights": google_flight,
                    "stafftraveler": staff_payload,
                    "stafftraveler_request": request_state,
                }
            )
            google_raw.append(
                {
                    "index": idx,
                    "flight_number": flight_number,
                    "results": google_payload,
                }
            )
            staff_raw.append(
                {
                    "index": idx,
                    "flight_number": flight_number,
                    "results": staff_payload,
                }
            )

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


@app.post("/api/find-flight")
async def find_flight(payload: dict[str, Any] = BODY_REQUIRED):
    input_data = payload.get("input") if isinstance(payload, dict) else None
    if not input_data:
        input_data = payload

    if not isinstance(input_data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    account_id = input_data.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required.")
    try:
        account_id_int = int(account_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="account_id is invalid.") from None
    staff_account = get_stafftraveler_account_by_id(account_id_int)
    if not staff_account:
        raise HTTPException(status_code=404, detail="StaffTraveler account not found.")

    airline_code = str(input_data.get("airline") or "").strip()
    if airline_code:
        label = get_airline_label(airline_code)
        if label:
            input_data["airline_name"] = label

    flight_numbers = input_data.get("flight_numbers")
    if not isinstance(flight_numbers, list) or not flight_numbers:
        raise HTTPException(status_code=400, detail="flight_numbers is required.")
    trips = input_data.get("trips") or []
    itinerary = input_data.get("itinerary") or []
    for idx, number in enumerate(flight_numbers):
        trip = trips[idx] if idx < len(trips) else {}
        itin = itinerary[idx] if idx < len(itinerary) else {}
        if not str(number or "").strip():
            raise HTTPException(status_code=400, detail=f"Leg {idx + 1}: Flight number is required.")
        if not str(trip.get("origin") or "").strip():
            raise HTTPException(status_code=400, detail=f"Leg {idx + 1}: Origin is required.")
        if not str(trip.get("destination") or "").strip():
            raise HTTPException(status_code=400, detail=f"Leg {idx + 1}: Destination is required.")
        if not str(itin.get("date") or "").strip():
            raise HTTPException(status_code=400, detail=f"Leg {idx + 1}: Date is required.")

    run_id = make_run_id()
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    create_run_record(
        run_id=run_id,
        input_data=input_data,
        output_dir=run_dir,
        status="running",
        run_type="lookup",
    )

    state = RunState(run_id, run_dir, input_data)
    RUNS[run_id] = state

    headed = bool(payload.get("headed")) if isinstance(payload, dict) else False
    auto_request = bool(input_data.get("auto_request_stafftraveler"))
    asyncio.create_task(execute_find_flight(state, headed, staff_account, auto_request))

    return {
        "run_id": run_id,
        "status": "running",
        "output_dir": str(run_dir),
    }


@app.get("/api/find-flight/{run_id}")
async def find_flight_results(run_id: str):
    response = get_lookup_response(run_id)
    if not response:
        raise HTTPException(status_code=404, detail="Lookup response not found.")
    legs_results = response.lookup_payload if isinstance(response.lookup_payload, list) else []
    return {
        "run_id": run_id,
        "status": response.status,
        "legs_results": legs_results,
        "error": response.error,
    }


@app.get("/api/runs/{run_id}")
async def run_details(run_id: str):
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {
        "run_id": run_id,
        "status": state.status,
        "error": state.error,
        "created_at": state.created_at.isoformat(),
        "completed_at": state.completed_at.isoformat() if state.completed_at else None,
        "output_dir": str(state.output_dir),
        "report": {},
        "files": {k: str(v) for k, v in state.result_files.items() if v.exists()},
    }


@app.get("/api/runs/{run_id}/download/{kind}")
async def download(run_id: str, kind: str):
    run_dir = OUTPUT_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found.")

    if kind == "json":
        path = run_dir / REPORT_JSON
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report JSON not found.")
        return FileResponse(path, filename=path.name)

    if kind == "excel":
        standby = get_latest_standby_response(run_id)
        if not standby or not standby.standby_bots_payload:
            lookup = get_lookup_response(run_id)
            if not lookup:
                raise HTTPException(status_code=404, detail="Report data not found.")
            try:
                import pandas as pd
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Pandas not available: {exc}") from exc

            legs_results = lookup.lookup_payload if isinstance(lookup.lookup_payload, list) else []
            rows: list[dict[str, Any]] = []
            for leg in legs_results:
                google_flight = leg.get("google_flights") or None
                staff_flight = (leg.get("stafftraveler") or [None])[0]
                request_state = leg.get("stafftraveler_request") or {}

                rows.append(
                    {
                        "flight_number": leg.get("flight_number") or "",
                        "origin": (google_flight or staff_flight or {}).get("origin", ""),
                        "destination": (google_flight or staff_flight or {}).get("destination", ""),
                        "departure_time": (google_flight or {}).get("depart_time") or "",
                        "arrival_time": (google_flight or {}).get("arrival_time") or "",
                        "airline": (google_flight or staff_flight or {}).get("airline", ""),
                        "gf_seats": (google_flight or {}).get("seats_available") or "",
                        "gf_stops": (google_flight or {}).get("stops") or "",
                        "gf_emissions": (google_flight or {}).get("emissions") or "",
                        "st_bus": (staff_flight or {}).get("seats", {}).get("bus", ""),
                        "st_eco": (staff_flight or {}).get("seats", {}).get("eco", ""),
                        "st_nonrev": (staff_flight or {}).get("seats", {}).get("non_rev", ""),
                        "st_request_attempted": request_state.get("attempted"),
                        "st_request_posted": request_state.get("posted"),
                        "st_request_reason": request_state.get("reason"),
                    }
                )

            output = BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                pd.DataFrame(rows).to_excel(writer, sheet_name="Lookup", index=False)
            output.seek(0)

            headers = {"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'}
            return StreamingResponse(
                output,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers=headers,
            )

        try:
            import pandas as pd
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Pandas not available: {exc}") from exc

        rows: list[dict[str, Any]] = []
        for routing in standby.standby_bots_payload:
            if not isinstance(routing, dict):
                continue
            routing_info = routing.get("routingInfo") or {}
            flights = routing.get("flights") or []
            if not isinstance(flights, list):
                continue
            for flight in flights:
                if not isinstance(flight, dict):
                    continue
                seats = flight.get("seats") or {}
                rows.append(
                    {
                        "route": f"{flight.get('departure', '')} -> {flight.get('arrival', '')}",
                        "date": routing_info.get("departureDate") or routing_info.get("date") or "",
                        "airline_name": flight.get("airline_name") or "",
                        "airline_code": flight.get("airline_code") or "",
                        "flight_number": flight.get("flight_number") or "",
                        "aircraft": flight.get("aircraft") or "",
                        "departure": flight.get("departure") or "",
                        "departure_time": flight.get("departure_time") or "",
                        "arrival": flight.get("arrival") or "",
                        "arrival_time": flight.get("arrival_time") or "",
                        "duration": flight.get("duration") or "",
                        "gf_section": flight.get("google_flights_section") or "",
                        "myid_economy": seats.get("myidtravel", {}).get("economy", ""),
                        "myid_business": seats.get("myidtravel", {}).get("business", ""),
                        "myid_first": seats.get("myidtravel", {}).get("first", ""),
                        "gf_economy": seats.get("google_flights", {}).get("economy", ""),
                        "gf_business": seats.get("google_flights", {}).get("business", ""),
                        "gf_first": seats.get("google_flights", {}).get("first", ""),
                        "st_first": seats.get("stafftraveler", {}).get("first", ""),
                        "st_bus": seats.get("stafftraveler", {}).get("bus", ""),
                        "st_eco": seats.get("stafftraveler", {}).get("eco", ""),
                        "st_ecoplus": seats.get("stafftraveler", {}).get("ecoplus", ""),
                        "st_nonrev": seats.get("stafftraveler", {}).get("nonrev", ""),
                    }
                )

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame(rows).to_excel(writer, sheet_name="Flights", index=False)
            if isinstance(standby.gemini_payload, list):
                pd.DataFrame(standby.gemini_payload).to_excel(writer, sheet_name="Top_5", index=False)
        output.seek(0)

        headers = {"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'}
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers,
        )

    if kind in BOT_OUTPUTS:
        bot_path = run_dir / BOT_OUTPUTS[kind]
        if bot_path.exists():
            return FileResponse(bot_path, filename=bot_path.name)
        raise HTTPException(status_code=404, detail=f"No output found for {kind}")

    raise HTTPException(status_code=400, detail="Unsupported download format.")


@app.get("/api/runs/{run_id}/download-report-xlsx")
async def download_report_xlsx(run_id: str):
    return await download(run_id, "excel")


@app.post("/api/scrape-airlines")
async def scrape_airlines_api(payload: dict[str, Any] = BODY_DEFAULT):
    """
    Run the airline dropdown scraper using existing auth_state.json.
    """
    try:
        result = await scrape_airlines_task(
            headless=not bool(payload.get("headed")),
            sample_origin_query=payload.get("origin_query"),
            url_override=payload.get("url"),
            extra_wait_ms=int(payload.get("extra_wait_ms") or 0),
            airport_term=payload.get("airport_term"),
            csrf_override=payload.get("csrf"),
        )
        return {"status": "ok", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    index_path = Path("index.html")
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse("<h1>Globalpass Bot</h1><p>UI not found.</p>")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(
        """
        <html>
            <head>
                <title>Login</title>
                <style>
                    body {
                        font-family: Arial, sans-serif;
                        background: #f5f5f5;
                    }
                    .card {
                        max-width: 360px;
                        margin: 10vh auto;
                        background: #fff;
                        padding: 24px;
                        border-radius: 8px;
                        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
                    }
                    label {
                        display: block;
                        margin-bottom: 6px;
                        font-weight: 600;
                    }
                    input {
                        width: 100%;
                        padding: 10px;
                        margin-bottom: 12px;
                        border: 1px solid #ddd;
                        border-radius: 6px;
                    }
                    button {
                        width: 100%;
                        padding: 10px;
                        background: #111827;
                        color: #fff;
                        border: none;
                        border-radius: 6px;
                        font-weight: 600;
                        cursor: pointer;
                    }
                </style>
            </head>
            <body>
                <div class="card">
                    <h2>Admin Login</h2>
                    <form method="post" action="/login">
                        <label for="username">Username</label>
                        <input id="username" name="username" type="text" required />
                        <label for="password">Password</label>
                        <input id="password" name="password" type="password" required />
                        <button type="submit">Sign in</button>
                    </form>
                </div>
            </body>
        </html>
        """
    )


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "").strip()
    if _verify_password(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse("<h3>Invalid credentials</h3><a href='/login'>Try again</a>", status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/airlines.json")
async def airlines():
    path = Path("airlines.json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="airlines.json not found")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=config.API_HOST, port=config.API_PORT, reload=True)
