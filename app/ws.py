import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from slack_sdk.errors import SlackApiError

from app import config
from app import slack

logger = logging.getLogger("globalpass")


class RunState:
    def __init__(self, run_id: str, output_dir, input_data: dict[str, Any]):
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
        self.result_files: dict[str, Any] = {}
        self.myidtravel_credentials: dict[str, str] | None = None
        self.stafftraveler_credentials: dict[str, str] | None = None
        self.employee_name: str | None = None
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
        payload = {
            "type": "status",
            "status": self.status,
            "error": self.error,
            "run_id": self.id,
        }
        if self.completed_at:
            payload["completed_at"] = self.completed_at.isoformat()
        await self._broadcast(payload, store=False)

        if self.slack_channel and self.slack_thread_ts and slack.slack_web_client:
            await self._send_slack_status_update()

    async def _send_slack_status_update(self) -> None:
        try:
            if self.status == "completed":
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
                return

            await slack.slack_web_client.chat_postMessage(
                channel=self.slack_channel,
                thread_ts=self.slack_thread_ts,
                text=message,
            )
        except Exception as exc:
            logger.error("Error sending Slack status update for run %s: %s", self.id, exc, exc_info=True)

    async def send_initial_slack_notification(self) -> None:
        if not slack.slack_web_client or not slack.SLACK_ENABLED:
            logger.warning("Slack client not available or disabled")
            return
        try:
            channel = os.environ.get("SLACK_CHANNEL_ID")
            if not channel:
                return
            message = f"New scraper run started (Run ID: `{self.id}`)"
            response = await slack.slack_web_client.chat_postMessage(channel=channel, text=message)
            self.slack_channel = channel
            self.slack_thread_ts = response["ts"]
        except SlackApiError as exc:
            logger.error("Slack API error: %s", exc)

    async def update_slack_status(self, status_text: str) -> None:
        if not self.slack_channel or not self.slack_thread_ts or not slack.slack_web_client:
            return
        try:
            await slack.slack_web_client.chat_update(
                channel=self.slack_channel,
                ts=self.slack_thread_ts,
                text=slack.truncate_slack_message(status_text),
            )
        except SlackApiError as exc:
            logger.error("Slack update failed: %s", exc)
