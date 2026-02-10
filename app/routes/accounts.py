from fastapi import APIRouter

from app.db import get_account_options, list_stafftraveler_accounts

router = APIRouter(prefix="/api")


@router.get("/accounts")
async def account_options():
    return {"accounts": get_account_options()}


@router.get("/stafftraveler-accounts")
async def stafftraveler_accounts():
    return {"accounts": list_stafftraveler_accounts()}
