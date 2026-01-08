import asyncio
import json
import logging
import os
import re

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import asyncio.subprocess
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, async_playwright

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

import config
from bots import google_flights_bot, myidtravel_bot, stafftraveler_bot

load_dotenv()
logger = logging.getLogger("globalpass")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_USER_OAUTH_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
SLACK_ENABLED = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN)

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

OUTPUT_ROOT = Path("outputs")
RUNS: Dict[str, "RunState"] = {}
RUN_SEMAPHORE = asyncio.Semaphore(1)

slack_web_client: Optional[AsyncWebClient] = None
slack_socket_client: Optional[SocketModeClient] = None
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
                    await slack_web_client.reactions_add(
                        channel=channel,
                        timestamp=ts,
                        name="white_check_mark"
                    )

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
                        "travel_partner": []
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

                    # Send initial confirmation
                    await slack_web_client.chat_postMessage(
                        channel=channel,
                        thread_ts=ts,
                        text=f"<@{user}> Scraper started! :rocket:\n"
                             f"Run ID: `{run_id}`\n"
                             f"Route: {input_data['trips'][0]['origin']} → {input_data['trips'][0]['destination']}\n"
                             f"Status: Running..."
                    )

                    # Start the run
                    asyncio.create_task(execute_run(state, limit=30, headed=False))

                except Exception as e:
                    logger.error(f"Error processing Slack command: {e}", exc_info=True)
                    try:
                        await slack_web_client.chat_postMessage(
                            channel=channel,
                            thread_ts=ts,
                            text=f"<@{user}> Error starting scraper: {str(e)} :x:"
                        )
                    except:
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
                            status_emoji = {
                                "pending": "⏳",
                                "running": "🔄",
                                "completed": "✅",
                                "error": "❌"
                            }.get(state.status, "❓")

                            route = "N/A"
                            if state.input_data.get("trips"):
                                trip = state.input_data["trips"][0]
                                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"

                            status_lines.append(
                                f"{status_emoji} `{run_id}` - {state.status.upper()} - {route}"
                            )

                        status_text = "\n".join(status_lines)

                    await slack_web_client.chat_postMessage(
                        channel=channel,
                        thread_ts=ts,
                        text=status_text
                    )

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
        slack_socket_client = SocketModeClient(
            app_token=SLACK_APP_TOKEN,
            web_client=slack_web_client
        )

        # Register event handler
        slack_socket_client.socket_mode_request_listeners.append(process_slack_event)

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
    def __init__(self, run_id: str, output_dir: Path, input_data: Dict[str, Any]):
        self.id = run_id
        self.output_dir = output_dir
        self.input_data = input_data
        self.status = "pending"
        self.error: str | None = None
        self.created_at = datetime.utcnow()
        self.completed_at: datetime | None = None
        self.logs: list[Dict[str, Any]] = []
        self.subscribers: dict[WebSocket, asyncio.Queue] = {}
        self.done = asyncio.Event()
        self.result_files: Dict[str, Path] = {}

        # Slack-specific fields
        self.slack_channel: Optional[str] = None
        self.slack_thread_ts: Optional[str] = None

    def subscribe(self, ws: WebSocket) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers[ws] = queue
        return queue

    def unsubscribe(self, ws: WebSocket) -> None:
        self.subscribers.pop(ws, None)

    async def _broadcast(self, payload: Dict[str, Any], store: bool = False) -> None:
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

                message = (
                    f"*Scraper Completed!*\n"
                    f"Run ID: `{self.id}`\n"
                    f"Route: {route}\n"
                    f"Files generated:\n"
                )

                for file_key, file_path in self.result_files.items():
                    if file_path.exists():
                        message += f"• {file_key}\n"

                message += f"\nDownload results at: `{self.output_dir}`"

            elif self.status == "error":
                message = f"*Scraper Failed*\nRun ID: `{self.id}`\nError: {self.error}"
            else:
                return  # Don't send updates for pending/running status

            await slack_web_client.chat_postMessage(
                channel=self.slack_channel,
                thread_ts=self.slack_thread_ts,
                text=message
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
            channel = os.environ.get("SLACK_CHANNEL_ID")  # Replace with your channel ID
            logger.info(f"Attempting to send Slack notification to channel: {channel}")
            
            # Format input data
            trips = self.input_data.get('trips', [])
            route = "N/A"
            if trips:
                trip = trips[0]
                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"
            
            itinerary = self.input_data.get('itinerary', [])
            date = itinerary[0].get('date', 'N/A') if itinerary else 'N/A'
            travel_class = itinerary[0].get('class', 'N/A') if itinerary else 'N/A'
            
            airline = self.input_data.get('airline', 'All Airlines') or 'All Airlines'
            travel_status = self.input_data.get('travel_status', 'N/A')
            nonstop = "Yes" if self.input_data.get('nonstop_flights', False) else "No"
            
            travellers = self.input_data.get('traveller', [])
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
                f"*Travellers:* {traveller_count}\n\n"
                f"*Status:* Running scrapers..."
            )
            
            response = await slack_web_client.chat_postMessage(
                channel=channel,
                text=message
            )
            
            logger.info(f"Successfully sent Slack notification to {channel}")
            logger.info(f"Message TS: {response.get('ts')}")
            logger.info(f"Full response: {response}")
            # Store the message timestamp for later updates
            self.slack_channel = channel
            self.slack_thread_ts = response['ts']
            
            logger.info(f"Sent initial Slack notification for run {self.id}")
            
        except Exception as e:
            logger.error(f"Failed to send initial Slack notification: {e}", exc_info=True)
            logger.error(f"Error type: {type(e).__name__}")
            if hasattr(e, 'response'):
                logger.error(f"Slack API response: {e.response}")


    async def update_slack_status(self, status_text: str):
        """Update the status line in the original Slack message"""
        if not self.slack_channel or not self.slack_thread_ts or not slack_web_client:
            return
        
        try:
            # Rebuild the original message with updated status
            trips = self.input_data.get('trips', [])
            route = "N/A"
            if trips:
                trip = trips[0]
                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"
            
            itinerary = self.input_data.get('itinerary', [])
            date = itinerary[0].get('date', 'N/A') if itinerary else 'N/A'
            travel_class = itinerary[0].get('class', 'N/A') if itinerary else 'N/A'
            
            airline = self.input_data.get('airline', 'All Airlines') or 'All Airlines'
            travel_status = self.input_data.get('travel_status', 'N/A')
            nonstop = "Yes" if self.input_data.get('nonstop_flights', False) else "No"
            
            travellers = self.input_data.get('traveller', [])
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
                f"*Travellers:* {traveller_count}\n\n"
                f"*Status:* {status_text}"
            )
            
            await slack_web_client.chat_update(
                channel=self.slack_channel,
                ts=self.slack_thread_ts,
                text=message
            )
            
            logger.info(f"Updated Slack status to: {status_text}")
            
        except Exception as e:
            logger.error(f"Failed to update Slack status: {e}", exc_info=True)


    async def send_completion_slack_notification(self, top_flights: list):
        """Send completion notification with top 5 flights as a thread reply"""
        if not self.slack_channel or not self.slack_thread_ts or not slack_web_client:
            logger.warning(f"Cannot send completion notification - channel: {self.slack_channel}, ts: {self.slack_thread_ts}, client: {bool(slack_web_client)}")
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
            trips = self.input_data.get('trips', [])
            route = "N/A"
            if trips:
                trip = trips[0]
                route = f"{trip.get('origin', '?')} → {trip.get('destination', '?')}"
            
            travel_status = (self.input_data.get('travel_status') or '').strip().lower()
            is_bookable = travel_status == "bookable"
            
            if self.status == "completed":
                # Build top flights message
                flights_text = []
                for idx, flight in enumerate(top_flights[:5], 1):
                    flight_info = (
                        f"*{idx}. {flight['Flight']}* ({flight['Airline']})\n"
                        f"   - Aircraft: {flight['Aircraft']}\n"
                        f"   - Departure: {flight['Departure']}\n"
                        f"   - Arrival: {flight['Arrival']}\n"
                        f"   - Duration: {flight['Duration']}\n"
                        f"   - Stops: {flight['Stops']}\n"
                    )
                    
                    if is_bookable and 'Price' in flight:
                        flight_info += f"   - Price: ${flight['Price']}\n"
                    elif 'Chance' in flight:
                        flight_info += f"   - Availability: {flight['Chance']}\n"
                    
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
                channel=self.slack_channel,
                thread_ts=self.slack_thread_ts,
                text=message
            )
            
            logger.info(f"Sent completion reply to thread for run {self.id}")
            
        except Exception as e:
            logger.error(f"Failed to send completion reply: {e}", exc_info=True)
            logger.error(f"Error type: {type(e).__name__}")
            if hasattr(e, 'response'):
                logger.error(f"Slack API response: {e.response}")


@contextmanager
def patch_config(attr: str, value: Any):
    previous = getattr(config, attr, None)
    setattr(config, attr, value)
    try:
        yield
    finally:
        setattr(config, attr, previous)


def make_run_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def normalize_google_time(time_str: str) -> Optional[str]:
    """Converts 12h time (Google) to 24h 'HH:MM' for matching."""
    try:
        time_str = time_str.replace('\u202f', ' ').strip()
        return datetime.strptime(time_str, "%I:%M %p").strftime("%H:%M")
    except:
        return None


def to_minutes(duration_str: str) -> int:
    """Converts duration strings like '7h 25m' to total minutes."""
    if not duration_str:
        return 1440
    try:
        clean = duration_str.lower().replace('hr', 'h').replace('min', 'm').replace(' ', '')
        h = int(clean.split('h')[0]) if 'h' in clean else 0
        m_part = clean.split('h')[-1] if 'h' in clean else clean
        m = int(m_part.replace('m', '')) if 'm' in m_part else 0
        return h * 60 + m
    except:
        return 1440


async def run_myidtravel(state: RunState, input_path: Path, headed: bool) -> Dict[str, Any]:
    output_path = state.output_dir / "myidtravel_flightschedule.json"
    await state.log("[myidtravel] starting")
    notify: Optional[Callable[[str], Awaitable[None]]] = None
    if state.slack_channel and slack_web_client:
        async def _notify(msg: str) -> None:
            try:
                await slack_web_client.chat_postMessage(
                    channel=state.slack_channel,
                    thread_ts=state.slack_thread_ts,
                    text=msg,
                )
            except Exception as exc:
                logger.debug("Slack notify failed: %s", exc)
        notify = _notify

    try:
        with patch_config("FLIGHTSCHEDULE_OUTPUT", output_path):
            await myidtravel_bot.run(
                headless=not headed,
                screenshot=None,
                input_path=str(input_path),
                notify=notify,
            )
        if output_path.exists():
            state.result_files["myidtravel"] = output_path
            await state.log(f"[myidtravel] wrote results to {output_path}")
        else:
            await state.log("[myidtravel] finished but output file was not found")
        return {"name": "myidtravel", "status": "ok", "output": str(output_path)}
    except Exception as exc:
        await state.log(f"[myidtravel] error: {exc}")
        return {"name": "myidtravel", "status": "error", "error": str(exc)}


async def run_google_flights(state: RunState, input_path: Path, limit: int, headed: bool) -> Dict[str, Any]:
    output_path = state.output_dir / "google_flights_results.json"
    await state.log("[google_flights] starting")
    try:
        await google_flights_bot.run(
            headless=not headed,
            input_path=str(input_path),
            output=output_path,
            limit=limit,
            screenshot=None,
        )
        state.result_files["google_flights"] = output_path
        await state.log(f"[google_flights] wrote results to {output_path}")
        return {"name": "google_flights", "status": "ok", "output": str(output_path)}
    except Exception as exc:
        await state.log(f"[google_flights] error: {exc}")
        return {"name": "google_flights", "status": "error", "error": str(exc)}


async def run_stafftraveler(state: RunState, input_path: Path, headed: bool) -> Dict[str, Any]:
    output_path = state.output_dir / "stafftraveller_results.json"
    storage_path = state.output_dir / "stafftraveler_auth_state.json"
    await state.log("[stafftraveler] starting")
    try:
        with patch_config("STAFF_RESULTS_OUTPUT", output_path):
            await stafftraveler_bot.perform_stafftraveller_login(
                headless=not headed,
                screenshot=None,
                storage_path=str(storage_path),
                input_data=myidtravel_bot.read_input(str(input_path)),
            )
        state.result_files["stafftraveler"] = output_path
        await state.log(f"[stafftraveler] wrote results to {output_path}")
        return {"name": "stafftraveler", "status": "ok", "output": str(output_path)}
    except Exception as exc:
        await state.log(f"[stafftraveler] error: {exc}")
        return {"name": "stafftraveler", "status": "error", "error": str(exc)}


async def execute_run(state: RunState, limit: int, headed: bool) -> None:
    async with RUN_SEMAPHORE:
        state.status = "running"
        await state.push_status()
        
        # Send initial Slack notification if run was triggered from web interface
        if not state.slack_channel:  # Only for web interface runs (not Slack-triggered)
            await state.send_initial_slack_notification()
        
        await state.log("Run started; launching three bots concurrently.")
        logger.info("Run %s started (headed=%s, limit=%s)", state.id, headed, limit)

        input_path = state.output_dir / "input.json"
        input_path.write_text(json.dumps(state.input_data, indent=2))

        tasks = [
            run_myidtravel(state, input_path, headed),
            run_google_flights(state, input_path, limit, headed),
            run_stafftraveler(state, input_path, headed),
        ]
        results = await asyncio.gather(*tasks)

        had_error = any(res.get("status") == "error" for res in results)
        
        # Generate flight loads report
        await _run_generate_flight_loads(state)

        state.status = "error" if had_error else "completed"
        state.error = ", ".join(res.get("error", "") for res in results if res.get("error"))
        state.completed_at = datetime.utcnow()
        await state.log("Run finished." if not had_error else "Run finished with errors.")
        logger.info("Run %s completed status=%s", state.id, state.status)
        
        # Get top 5 flights for Slack notification
        top_flights = []
        try:
            json_output = state.output_dir / "standby_report_multi.json"
            if json_output.exists():
                with open(json_output, 'r') as f:
                    report_data = json.load(f)
                    
                # Get the appropriate top 5 list based on travel status
                travel_status = (state.input_data.get('travel_status') or '').strip().lower()
                if travel_status == "bookable":
                    top_flights = report_data.get('Top_5_Bookable', [])
                else:
                    top_flights = report_data.get('Top_5_R2_Standby', [])
        except Exception as e:
            logger.error(f"Failed to load top flights for Slack: {e}")
        
        # Send completion notification with top flights
        await state.send_completion_slack_notification(top_flights)
        
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


async def _goto_home(page: Page, url_override: Optional[str] = None, extra_wait_ms: int = 0) -> str:
    urls = [url_override] if url_override else config.BASE_URLS
    last_error: Exception | None = None
    tried: list[str] = []

    async def _blocking_message() -> Optional[str]:
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


async def _extract_airline_options(page: Page) -> List[Dict[str, Any]]:
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


async def _capture_origin_lookup(page: Page, query: str) -> List[Dict[str, Any]]:
    captured: List[Dict[str, Any]] = []
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


async def _get_csrf_token(context) -> Optional[str]:
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
    csrf_override: Optional[str] = None,
) -> Dict[str, Any]:
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
    sample_origin_query: Optional[str] = None,
    url_override: Optional[str] = None,
    extra_wait_ms: int = 0,
    airport_term: Optional[str] = None,
    csrf_override: Optional[str] = None,
) -> Dict[str, Any]:
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

async def _run_generate_flight_loads(state: RunState) -> None:
    """
    Generate standby_report_multi.* using available data with fallback handling.
    Works even if one or more JSON sources are missing.
    Uses 'chance' as proxy for load since exact seat counts unavailable.
    """
    await state.log("[report] Starting flight loads report generation...")

    try:
        # Load sources with fallback to empty data
        myid_data = {}
        google_data = []
        staff_data = []

        myid_path = state.result_files.get("myidtravel")
        google_path = state.result_files.get("google_flights")
        staff_path = state.result_files.get("stafftraveler")

        # Load MyIDTravel (most critical source)
        if myid_path and myid_path.exists():
            try:
                with open(myid_path, 'r') as f:
                    myid_data = json.load(f)
                await state.log("[report] Loaded MyIDTravel data")
            except Exception as e:
                await state.log(f"[report] Failed to load MyIDTravel: {e}")
        else:
            await state.log("[report] MyIDTravel data not available")

        # Load Google Flights
        if google_path and google_path.exists():
            try:
                with open(google_path, 'r') as f:
                    google_data = json.load(f)
                await state.log("[report] Loaded Google Flights data")
            except Exception as e:
                await state.log(f"[report] Failed to load Google Flights: {e}")
        else:
            await state.log("[report] Google Flights data not available")

        # Load StaffTraveler
        if staff_path and staff_path.exists():
            try:
                with open(staff_path, 'r') as f:
                    staff_data = json.load(f)
                await state.log("[report] Loaded StaffTraveler data")
            except Exception as e:
                await state.log(f"[report] Failed to load StaffTraveler: {e}")
        else:
            await state.log("[report] StaffTraveler data not available")

        # If no data at all, cannot proceed
        if not myid_data and not staff_data and not google_data:
            await state.log("[report] No data available from any source")
            return

        # Build flight registry
        registry = {}

        # 1. Process MyIDTravel (primary source for availability)
        for routing in myid_data.get('routings', []):
            for f in routing.get('flights', []):
                seg = f['segments'][0]
                fn = seg['flightNumber']

                registry[fn] = {
                    'Flight': fn,
                    'Airline': seg['operatingAirline']['name'],
                    'Aircraft': seg.get('aircraft', 'N/A'),
                    'Departure': seg['departureTime'],
                    'Arrival': seg['arrivalTime'],
                    'Duration': to_minutes(f.get('duration', '0h 0m')),
                    'Duration_Str': f.get('duration', 'N/A'),
                    'Stops': seg.get('stopQuantity', 0),
                    'Selectable': f.get('selectable', False),
                    'Chance': f.get('chance', 'Unknown'),
                    'Tarif': f.get('tarif', 'Unknown'),
                    'Price': None,  # Will be set from Google
                    'Sources': {'MyIDTravel'}
                }

        # 2. Process StaffTraveler
        for entry in staff_data:
            for f in entry.get('flight_details', []):
                fn = f['airline_flight_number']

                if fn not in registry:
                    # Create new entry if not in MyIDTravel
                    registry[fn] = {
                        'Flight': fn,
                        'Airline': f['airlines'],
                        'Aircraft': f.get('aircraft', 'N/A'),
                        'Departure': 'N/A',
                        'Arrival': 'N/A',
                        'Duration': to_minutes(f.get('duration', '0h 0m')),
                        'Duration_Str': f.get('duration', 'N/A'),
                        'Stops': 0,
                        'Selectable': False,  # Unknown without MyIDTravel
                        'Chance': 'Unknown',
                        'Tarif': 'Unknown',
                        'Price': None,
                        'Sources': set()
                    }

                registry[fn]['Sources'].add('Stafftraveler')

                # Update timing info if missing
                if registry[fn]['Departure'] == 'N/A':
                    time_parts = f.get('time', '').split(' - - - ')
                    if len(time_parts) == 2:
                        registry[fn]['Departure'] = time_parts[0].strip()
                        registry[fn]['Arrival'] = time_parts[1].strip()

        # 3. Process Google Flights (prices only if bookable)
        travel_status_lower = (state.input_data.get("travel_status") or "").strip().lower()
        if travel_status_lower == "bookable":
            for entry in google_data:
                flights_data = entry.get('flights', {})
                all_google = flights_data.get('top_flights', []) + flights_data.get('other_flights', [])
                if not all_google:
                    all_google = flights_data.get('all', [])
                for g_f in all_google:
                    fn_match = re.search(r'\b([A-Z]{2,3}\d{1,4})\b', g_f.get('summary', ''))
                    fn = fn_match.group(1) if fn_match else None
                    price = None
                    price_str = g_f.get('price') or g_f.get('summary') or ""
                    price_match = re.search(r'(\d[\d,]*)', price_str.replace(' ', ''))
                    if price_match:
                        try:
                            price = int(price_match.group(1).replace(',', ''))
                        except Exception:
                            price = None
                    if fn and fn in registry:
                        if price is not None:
                            registry[fn]['Price'] = price
                        registry[fn]['Sources'].add('Google Flights')
                    else:
                        g_airline = g_f.get('airline', '')
                        g_duration = to_minutes(g_f.get('duration', '0h 0m'))
                        for reg_fn, data in registry.items():
                            if (data['Airline'] == g_airline and abs(data['Duration'] - g_duration) <= 5):
                                if price is not None:
                                    data['Price'] = price
                                data['Sources'].add('Google Flights')
                                break

        await state.log(f"[report] Built registry with {len(registry)} flights")

        valid_durations = [f['Duration'] for f in registry.values() if f['Duration'] > 0]
        min_dur = min(valid_durations) if valid_durations else 420  # 7 hours default

        def calculate_standby_load_score(f):
            """Calculate load score based on available data."""
            chance_map = {
                'HIGH': 1000,
                'MEDIUM': 600,
                'MID': 600,
                'LOW': 200,
                'Unknown': 300,
            }
            load_score = chance_map.get(f['Chance'], 300)
            nonstop_bonus = 300 if f['Stops'] == 0 else 0
            duration_bonus = (min_dur / f['Duration']) * 150 if f['Duration'] > 0 else 0
            tarif_map = {'MID': 100, 'HIGH': 50, 'LOW': 150, 'Unknown': 0}
            tarif_bonus = tarif_map.get(f['Tarif'], 0)
            return load_score + nonstop_bonus + duration_bonus + tarif_bonus

        # Standby ranking (use all registry flights, neutral score when unknown)
        standby_flights = []
        low_load_alerts = []
        for f in registry.values():
            score = calculate_standby_load_score(f)
            standby_flights.append({
                'Flight': f['Flight'],
                'Airline': f['Airline'],
                'Aircraft': f['Aircraft'],
                'Departure': f['Departure'],
                'Arrival': f['Arrival'],
                'Duration': f['Duration_Str'],
                'Stops': f['Stops'],
                'Chance': f['Chance'],
                'Source': ', '.join(sorted(list(f['Sources']))),
                'Score': round(score, 2)
            })
            if f['Chance'] == 'LOW':
                low_load_alerts.append(f"⚠️ {f['Flight']} ({f['Airline']}) - LOW availability")

        standby_flights.sort(key=lambda x: x['Score'], reverse=True)
        if not standby_flights:
            await state.log("[ALERT]️ No standby flights found!")
        
        # NOTE: Hidden for now
        # if low_load_alerts:
        #     for alert in low_load_alerts:
        #         await state.log(f"[ALERT] {alert}")

        top_5_standby = [
            {
                'Rank': i + 1,
                'Flight': item['Flight'],
                'Airline': item['Airline'],
                'Aircraft': item['Aircraft'],
                'Departure': item['Departure'],
                'Arrival': item['Arrival'],
                'Duration': item['Duration'],
                'Stops': item['Stops'],
                'Chance': item['Chance'],
                'Source': item['Source']
            }
            for i, item in enumerate(standby_flights[:5])
        ]

        # Bookable flights (only when travel_status is bookable)
        bookable_flights = []
        if travel_status_lower == "bookable":
            for fn, f in registry.items():
                if f['Price'] is None or f['Price'] <= 0:
                    continue
                score = 0
                score += max(0, 1000 - (f['Price'] / max(f['Price'], 1)) * 1000)
                preferred = ['A380', '77W', '789', '781', '359']
                if any(p in f['Aircraft'] for p in preferred):
                    score += 200
                if f['Stops'] == 0:
                    score += 200
                if f['Duration'] > 0:
                    score += (min_dur / f['Duration']) * 300

                bookable_flights.append({
                    'Flight': fn,
                    'Airline': f['Airline'],
                    'Aircraft': f['Aircraft'],
                    'Departure': f['Departure'],
                    'Arrival': f['Arrival'],
                    'Duration': f['Duration_Str'],
                    'Stops': f['Stops'],
                    'Price': f['Price'],
                    'Source': ', '.join(sorted(list(f['Sources']))),
                    'Score': round(score, 2)
                })
            bookable_flights.sort(key=lambda x: x['Score'], reverse=True)

        top_5_bookable = [
            {
                'Rank': i + 1,
                'Flight': item['Flight'],
                'Airline': item['Airline'],
                'Aircraft': item['Aircraft'],
                'Departure': item['Departure'],
                'Arrival': item['Arrival'],
                'Duration': item['Duration'],
                'Stops': item['Stops'],
                'Price': item['Price'],
                'Source': item['Source']
            }
            for i, item in enumerate(bookable_flights[:5])
        ]

        # Prepare data for all source sheets (filtered by mode)
        myid_all_flights = []
        for routing in myid_data.get('routings', []):
            for f in routing.get('flights', []):
                seg = f['segments'][0]
                myid_all_flights.append({
                    'Flight': seg['flightNumber'],
                    'Airline': seg['operatingAirline']['name'],
                    'Aircraft': seg.get('aircraft', 'N/A'),
                    'Departure': seg['departureTime'],
                    'Arrival': seg['arrivalTime'],
                    'Duration': f.get('duration', 'N/A'),
                    'Stops': seg.get('stopQuantity', 0),
                    'Chance': f.get('chance', 'N/A'),
                    'Tarif': f.get('tarif', 'N/A'),
                    'Selectable': 'YES' if f.get('selectable', False) else 'NO'
                })

        staff_all_flights = []
        for entry in staff_data:
            for f in entry.get('flight_details', []):
                staff_all_flights.append({
                    'Flight': f['airline_flight_number'],
                    'Airline': f['airlines'],
                    'Aircraft': f.get('aircraft', 'N/A'),
                    'Time': f.get('time', 'N/A'),
                    'Duration': f.get('duration', 'N/A')
                })

        google_all_flights = []
        for entry in google_data:
            flights_data = entry.get('flights', {})
            all_gf = flights_data.get('top_flights', []) + flights_data.get('other_flights', [])
            if not all_gf:
                all_gf = flights_data.get('all', [])
            for g_f in all_gf:
                google_all_flights.append({
                    'Airline': g_f.get('airline', 'N/A'),
                    'Departure': g_f.get('depart_time', 'N/A'),
                    'Arrival': g_f.get('arrival_time', 'N/A'),
                    'Duration': g_f.get('duration', 'N/A'),
                    'Stops': g_f.get('stops', 'N/A'),
                    'Price': g_f.get('price', 'N/A'),
                    'Emissions': g_f.get('emissions', 'N/A')
                })

        # Prepare input summary
        input_data = state.input_data
        input_summary = []

        # Basic parameters
        input_summary.append({'Parameter': 'Flight Type', 'Value': input_data.get('flight_type', 'N/A')})
        input_summary.append({'Parameter': 'Nonstop Only', 'Value': 'Yes' if input_data.get('nonstop_flights', False) else 'No'})
        input_summary.append({'Parameter': 'Airline', 'Value': input_data.get('airline', 'All Airlines') or 'All Airlines'})
        input_summary.append({'Parameter': 'Travel Status', 'Value': input_data.get('travel_status', 'N/A')})
        input_summary.append({'Parameter': '', 'Value': ''})

        # Trip information
        trips = input_data.get('trips', [])
        for idx, trip in enumerate(trips, 1):
            if len(trips) == 1:
                input_summary.append({'Parameter': 'Origin', 'Value': trip.get('origin', 'N/A')})
                input_summary.append({'Parameter': 'Destination', 'Value': trip.get('destination', 'N/A')})
            else:
                input_summary.append({'Parameter': f'Trip {idx} - Origin', 'Value': trip.get('origin', 'N/A')})
                input_summary.append({'Parameter': f'Trip {idx} - Destination', 'Value': trip.get('destination', 'N/A')})
        input_summary.append({'Parameter': '', 'Value': ''})

        # Itinerary
        itinerary = input_data.get('itinerary', [])
        for idx, itin in enumerate(itinerary, 1):
            if len(itinerary) == 1:
                input_summary.append({'Parameter': 'Date', 'Value': itin.get('date', 'N/A')})
                input_summary.append({'Parameter': 'Time', 'Value': itin.get('time', 'N/A')})
                input_summary.append({'Parameter': 'Class', 'Value': itin.get('class', 'N/A')})
            else:
                input_summary.append({'Parameter': f'Leg {idx} - Date', 'Value': itin.get('date', 'N/A')})
                input_summary.append({'Parameter': f'Leg {idx} - Time', 'Value': itin.get('time', 'N/A')})
                input_summary.append({'Parameter': f'Leg {idx} - Class', 'Value': itin.get('class', 'N/A')})
        input_summary.append({'Parameter': '', 'Value': ''})

        # Travellers
        travellers = input_data.get('traveller', [])
        input_summary.append({'Parameter': 'Number of Travellers', 'Value': len(travellers)})
        for idx, traveller in enumerate(travellers, 1):
            name = f"{traveller.get('salutation', '')} {traveller.get('name', 'N/A')}".strip()
            input_summary.append({'Parameter': f'Traveller {idx}', 'Value': name})
        input_summary.append({'Parameter': '', 'Value': ''})

        # Results summary
        input_summary.append({'Parameter': '--- Results Summary ---', 'Value': ''})
        input_summary.append({'Parameter': 'Total Flights Found', 'Value': len(registry)})
        input_summary.append({'Parameter': 'Selectable Flights', 'Value': len([f for f in registry.values() if f['Selectable']])})
        input_summary.append({'Parameter': 'MyIDTravel Flights', 'Value': len(myid_all_flights)})
        input_summary.append({'Parameter': 'Stafftraveler Flights', 'Value': len(staff_all_flights)})
        input_summary.append({'Parameter': 'Google Flights Results', 'Value': len(google_all_flights)})

        # Build results
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

        # Save JSON
        json_output = state.output_dir / "standby_report_multi.json"
        with open(json_output, 'w') as f:
            json.dump(results, f, indent=4)

        state.result_files["standby_report_multi.json"] = json_output
        await state.log(f"[report] Generated {json_output}")

        # Generate Excel
        try:
            import pandas as pd
            excel_output = state.output_dir / "standby_report_multi.xlsx"

            with pd.ExcelWriter(excel_output, engine='openpyxl') as writer:
                pd.DataFrame(input_summary).to_excel(writer, sheet_name='Input', index=False)

                if travel_status_lower == "bookable" and top_5_bookable:
                    pd.DataFrame(top_5_bookable).to_excel(writer, sheet_name='Bookable', index=False)

                if travel_status_lower != "bookable" and top_5_standby:
                    pd.DataFrame(top_5_standby).to_excel(writer, sheet_name='R2 Standby', index=False)

                if myid_all_flights:
                    pd.DataFrame(myid_all_flights).to_excel(writer, sheet_name='MyIDTravel', index=False)

                if staff_all_flights:
                    pd.DataFrame(staff_all_flights).to_excel(writer, sheet_name='Stafftraveler', index=False)

                if google_all_flights:
                    pd.DataFrame(google_all_flights).to_excel(writer, sheet_name='Google Flights', index=False)

            state.result_files["standby_report_multi.xlsx"] = excel_output
            await state.log(f"[report] Generated {excel_output}")
        except ImportError:
            await state.log("[report] Pandas not available, skipping Excel")
        except Exception as exc:
            await state.log(f"[report] Excel generation failed: {exc}")

        # Summary logs
        await state.log(f"[report] Report complete:")
        await state.log(f"  - Total flights: {len(registry)}")
        await state.log(f"  - Top 5 Standby (Plan A-E): {len(top_5_standby)}")
        await state.log(f"  - Top 5 Bookable: {len(top_5_bookable)}")
        if low_load_alerts:
            await state.log(f"  -️  {len(low_load_alerts)} low availability alerts")

    except Exception as exc:
        await state.log(f"[report] Error: {exc}")
        import traceback
        await state.log(f"[report] {traceback.format_exc()}")

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

@app.get("/api/slack/status")
async def slack_status():
    """Check if Slack integration is enabled and connected"""
    return {
        "enabled": SLACK_ENABLED,
        "connected": slack_connected,
        "commands": [
            "run scraper [origin] [destination] [date]",
            "scraper status"
        ]
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
async def start_run(payload: Dict[str, Any] = Body(...)):
    input_data = payload.get("input") if isinstance(payload, dict) else None
    if not input_data:
        input_data = payload

    if not isinstance(input_data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    limit = payload.get("limit") if isinstance(payload, dict) else None
    limit = int(limit) if isinstance(limit, int) or (isinstance(limit, str) and limit.isdigit()) else 30
    headed = bool(payload.get("headed")) if isinstance(payload, dict) else False

    run_id = make_run_id()
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_id, run_dir, input_data)
    RUNS[run_id] = state

    logger.info("Queued run %s", run_id)
    asyncio.create_task(execute_run(state, limit=limit, headed=headed))
    return {"run_id": run_id, "status": state.status, "output_dir": str(run_dir)}


@app.get("/api/runs/{run_id}")
async def run_details(run_id: str):
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found.")
    report_path = state.output_dir / REPORT_JSON
    report_data = {}
    if report_path.exists():
        try:
            report_data = json.loads(report_path.read_text())
        except Exception:
            report_data = {}
    return {
        "run_id": run_id,
        "status": state.status,
        "error": state.error,
        "created_at": state.created_at.isoformat(),
        "completed_at": state.completed_at.isoformat() if state.completed_at else None,
        "output_dir": str(state.output_dir),
        "report": report_data,
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
        path = run_dir / REPORT_XLSX
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report Excel not found.")
        return FileResponse(path, filename=path.name)

    if kind in BOT_OUTPUTS:
        bot_path = run_dir / BOT_OUTPUTS[kind]
        if bot_path.exists():
            return FileResponse(bot_path, filename=bot_path.name)
        raise HTTPException(status_code=404, detail=f"No output found for {kind}")

    raise HTTPException(status_code=400, detail="Unsupported download format.")


@app.post("/api/scrape-airlines")
async def scrape_airlines_api(payload: Dict[str, Any] = Body(default={})):
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
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = Path("index.html")
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse("<h1>Globalpass Bot</h1><p>UI not found.</p>")


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
