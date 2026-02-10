from fastapi import APIRouter

from app.slack import slack_status_data

router = APIRouter(prefix="/api")


@router.get("/slack/status")
async def slack_status():
    return slack_status_data()
