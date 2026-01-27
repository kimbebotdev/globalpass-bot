import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine

from models import BotResponse, Run

load_dotenv()

logger = logging.getLogger("globalpass.db")

DEFAULT_DB_PATH = Path("data") / "globalpass.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

connect_args: dict[str, Any] = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)


def ensure_data_dir() -> None:
    try:
        url = make_url(DATABASE_URL)
        if url.drivername.startswith("sqlite") and url.database:
            Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Failed to ensure sqlite directory: %s", exc)




def create_run_record(
    run_id: str,
    input_data: dict[str, Any],
    output_dir: Path,
    status: str = "pending",
    slack_channel: str | None = None,
    slack_thread_ts: str | None = None,
) -> None:
    try:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if run:
                run.status = status
                run.input_payload = input_data
                run.output_dir = str(output_dir)
                run.slack_channel = slack_channel
                run.slack_thread_ts = slack_thread_ts
            else:
                run = Run(
                    id=run_id,
                    status=status,
                    input_payload=input_data,
                    output_dir=str(output_dir),
                    slack_channel=slack_channel,
                    slack_thread_ts=slack_thread_ts,
                    created_at=datetime.utcnow(),
                )
                session.add(run)
            session.commit()
    except Exception as exc:
        logger.warning("Failed to persist run %s: %s", run_id, exc)


def update_run_record(run_id: str, status: str, error: str | None, completed_at: datetime | None) -> None:
    try:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if not run:
                return
            run.status = status
            run.error = error
            run.completed_at = completed_at
            session.commit()
    except Exception as exc:
        logger.warning("Failed to update run %s: %s", run_id, exc)


def save_bot_response(
    run_id: str,
    bot_name: str,
    status: str,
    output_path: Path | None,
    payload: Any | None,
    error: str | None = None,
) -> None:
    try:
        with Session(engine) as session:
            response = BotResponse(
                run_id=run_id,
                bot_name=bot_name,
                status=status,
                output_path=str(output_path) if output_path else None,
                payload=payload,
                error=error,
                created_at=datetime.utcnow(),
            )
            session.add(response)
            session.commit()
    except Exception as exc:
        logger.warning("Failed to persist bot response for %s/%s: %s", run_id, bot_name, exc)
