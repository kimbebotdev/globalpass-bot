from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Run(SQLModel, table=True):
    __tablename__: str = "runs"
    id: str = Field(primary_key=True, index=True)
    status: str = Field(nullable=False)
    error: str | None = Field(default=None)
    input_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_dir: str | None = Field(default=None)
    slack_channel: str | None = Field(default=None)
    slack_thread_ts: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    completed_at: datetime | None = Field(default=None)


class BotResponse(SQLModel, table=True):
    __tablename__: str = "bot_responses"
    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", index=True, nullable=False)
    bot_name: str = Field(nullable=False)
    status: str = Field(nullable=False)
    output_path: str | None = Field(default=None)
    payload: Any | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class MyidtravelAccount(SQLModel, table=True):
    __tablename__: str = "myidtravel_accounts"
    id: int | None = Field(default=None, primary_key=True)
    employee_name: str = Field(nullable=False)
    username: str = Field(nullable=False, index=True)
    password: str = Field(nullable=False)
    gender: str | None = Field(default=None)
    airport: str | None = Field(default=None)
    position: str | None = Field(default=None)
    travellers: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class StafftravelerAccount(SQLModel, table=True):
    __tablename__: str = "stafftraveler_accounts"
    id: int | None = Field(default=None, primary_key=True)
    employee_name: str = Field(nullable=False)
    username: str = Field(nullable=False, index=True)
    email: str | None = Field(default=None)
    password: str = Field(nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
