import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine, select

from models import LookupBotResponse, MyidtravelAccount, Run, StafftravelerAccount, StandbyBotResponse

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
    run_type: str = "standard",
    slack_channel: str | None = None,
    slack_thread_ts: str | None = None,
) -> None:
    try:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if run:
                run.status = status
                run.run_type = run_type
                run.input_payload = input_data
                run.output_dir = str(output_dir)
                run.slack_channel = slack_channel
                run.slack_thread_ts = slack_thread_ts
            else:
                run = Run(
                    id=run_id,
                    status=status,
                    run_type=run_type,
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


def save_standby_response(
    run_id: str,
    status: str,
    output_paths: dict[str, Any],
    myidtravel_payload: Any | None,
    google_flights_payload: Any | None,
    stafftraveler_payload: Any | None,
    gemini_payload: Any | None,
    error: str | None = None,
) -> None:
    try:
        with Session(engine) as session:
            response = StandbyBotResponse(
                run_id=run_id,
                status=status,
                myidtravel_payload=myidtravel_payload,
                google_flights_payload=google_flights_payload,
                stafftraveler_payload=stafftraveler_payload,
                gemini_payload=gemini_payload,
                output_paths=output_paths,
                error=error,
                created_at=datetime.utcnow(),
            )
            session.add(response)
            session.commit()
    except Exception as exc:
        logger.warning("Failed to persist standby response for %s: %s", run_id, exc)


def save_lookup_response(
    run_id: str,
    status: str,
    output_paths: dict[str, Any],
    google_flights_payload: Any | None,
    stafftraveler_payload: Any | None,
    error: str | None = None,
) -> None:
    try:
        with Session(engine) as session:
            response = LookupBotResponse(
                run_id=run_id,
                status=status,
                google_flights_payload=google_flights_payload,
                stafftraveler_payload=stafftraveler_payload,
                output_paths=output_paths,
                error=error,
                created_at=datetime.utcnow(),
            )
            session.add(response)
            session.commit()
    except Exception as exc:
        logger.warning("Failed to persist lookup response for %s: %s", run_id, exc)


def get_account_options() -> list[dict[str, Any]]:
    try:
        with Session(engine) as session:
            statement = (
                select(
                    MyidtravelAccount.id,
                    MyidtravelAccount.employee_name,
                    MyidtravelAccount.travellers,
                )
                .join(
                    StafftravelerAccount,
                    StafftravelerAccount.employee_name == MyidtravelAccount.employee_name,
                )
                .order_by(MyidtravelAccount.employee_name)
            )
            rows = session.exec(statement).all()
        return [
            {
                "id": row[0],
                "employee_name": row[1],
                "travellers": row[2] or [],
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("Failed to fetch account options: %s", exc)
        return []


def get_myidtravel_account(account_id: int) -> MyidtravelAccount | None:
    try:
        with Session(engine) as session:
            return session.get(MyidtravelAccount, account_id)
    except Exception as exc:
        logger.warning("Failed to fetch myidtravel account %s: %s", account_id, exc)
        return None


def get_stafftraveler_account_by_employee_name(employee_name: str) -> StafftravelerAccount | None:
    try:
        with Session(engine) as session:
            statement = select(StafftravelerAccount).where(
                StafftravelerAccount.employee_name == employee_name
            )
            return session.exec(statement).first()
    except Exception as exc:
        logger.warning("Failed to fetch stafftraveler account for %s: %s", employee_name, exc)
        return None
