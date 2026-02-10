import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.db import ensure_data_dir
from app.routes import accounts, airlines, auth, lookup, runs, ws
from app.routes import slack as slack_routes
from app.slack import SLACK_ENABLED, start_slack_bot, stop_slack_bot

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True, interpolate=False)

logger = logging.getLogger("globalpass")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

SECRET_KEY = os.environ.get("SECRET_KEY", "")

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


@app.on_event("startup")
async def startup_event() -> None:
    ensure_data_dir()
    if SLACK_ENABLED:
        await start_slack_bot()
    logger.info("FastAPI application started")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await stop_slack_bot()
    logger.info("FastAPI application stopped")


app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(airlines.router)
app.include_router(slack_routes.router)
app.include_router(ws.router)
app.include_router(runs.router)
app.include_router(lookup.router)

app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.API_HOST, port=config.API_PORT, reload=True)
