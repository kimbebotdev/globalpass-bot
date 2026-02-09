import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import delete as sa_delete
from sqlalchemy.engine import make_url
from sqlmodel import Session, col, create_engine, desc, select

from models import Airline, LookupBotResponse, MyidtravelAccount, Run, StafftravelerAccount, StandbyBotResponse

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
    standby_bots_payload: list[Any] | dict[str, Any] | None,
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
                standby_bots_payload=standby_bots_payload,
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
    lookup_payload: Any | None,
    error: str | None = None,
) -> None:
    try:
        with Session(engine) as session:
            response = LookupBotResponse(
                run_id=run_id,
                status=status,
                google_flights_payload=google_flights_payload,
                stafftraveler_payload=stafftraveler_payload,
                lookup_payload=lookup_payload,
                output_paths=output_paths,
                error=error,
                created_at=datetime.utcnow(),
            )
            session.add(response)
            session.commit()
    except Exception as exc:
        logger.warning("Failed to persist lookup response for %s: %s", run_id, exc)


def get_lookup_response(run_id: str) -> LookupBotResponse | None:
    try:
        with Session(engine) as session:
            statement = select(LookupBotResponse).where(LookupBotResponse.run_id == run_id)
            return session.exec(statement).first()
    except Exception as exc:
        logger.warning("Failed to fetch lookup response for %s: %s", run_id, exc)
        return None


def get_run_input(run_id: str) -> dict[str, Any] | None:
    try:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            return run.input_payload if run else None
    except Exception as exc:
        logger.warning("Failed to fetch run input for %s: %s", run_id, exc)
        return None

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
                    onclause=(col(StafftravelerAccount.employee_name) == col(MyidtravelAccount.employee_name)),
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


def get_latest_standby_response(run_id: str) -> StandbyBotResponse | None:
    try:
        with Session(engine) as session:
            statement = (
                select(StandbyBotResponse)
                .where(StandbyBotResponse.run_id == run_id)
                .order_by(desc(StandbyBotResponse.created_at))
            )
            return session.exec(statement).first()
    except Exception as exc:
        logger.warning("Failed to fetch standby response for %s: %s", run_id, exc)
        return None


def save_airlines(airlines: list[dict[str, Any]]) -> None:
    try:
        with Session(engine) as session:
            session.exec(sa_delete(Airline))  # type: ignore[arg-type]
            for item in airlines:
                code = str(item.get("value") or item.get("code") or "").strip()
                label = str(item.get("label") or code).strip()
                if not code:
                    continue
                session.add(
                    Airline(
                        code=code,
                        label=label,
                        disabled=bool(item.get("disabled", False)),
                        created_at=datetime.utcnow(),
                    )
                )
            session.commit()
    except Exception as exc:
        logger.warning("Failed to save airlines: %s", exc)


def list_airlines() -> list[dict[str, Any]]:
    try:
        with Session(engine) as session:
            statement = select(Airline).order_by(Airline.label)
            rows = session.exec(statement).all()
        return [
            {"value": row.code, "label": row.label, "disabled": row.disabled}
            for row in rows
        ]
    except Exception as exc:
        logger.warning("Failed to list airlines: %s", exc)
        return []


def get_airline_label(code: str) -> str | None:
    if not code:
        return None
    try:
        with Session(engine) as session:
            statement = select(Airline.label).where(Airline.code == code)
            row = session.exec(statement).first()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("Failed to fetch airline label for %s: %s", code, exc)
        return None


def list_stafftraveler_accounts() -> list[dict[str, Any]]:
    try:
        with Session(engine) as session:
            statement = select(StafftravelerAccount.id, StafftravelerAccount.employee_name).order_by(
                StafftravelerAccount.employee_name
            )
            rows = session.exec(statement).all()
        return [{"id": row[0], "employee_name": row[1]} for row in rows]
    except Exception as exc:
        logger.warning("Failed to list stafftraveler accounts: %s", exc)
        return []


def get_stafftraveler_account_by_id(account_id: int) -> StafftravelerAccount | None:
    try:
        with Session(engine) as session:
            return session.get(StafftravelerAccount, account_id)
    except Exception as exc:
        logger.warning("Failed to fetch stafftraveler account %s: %s", account_id, exc)
        return None


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
            statement = select(StafftravelerAccount).where(StafftravelerAccount.employee_name == employee_name)
            return session.exec(statement).first()
    except Exception as exc:
        logger.warning("Failed to fetch stafftraveler account for %s: %s", employee_name, exc)
        return None
