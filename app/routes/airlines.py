from fastapi import APIRouter, HTTPException

from app.db import list_airlines, save_airlines
from app.services.airlines import scrape_airlines_task

router = APIRouter(prefix="/api")


@router.get("/airlines")
async def get_airlines():
    return {"airlines": list_airlines()}


@router.post("/airlines/refresh")
async def refresh_airlines(headed: bool = False):
    try:
        result = await scrape_airlines_task(headless=not headed)
        airlines = result.get("airlines") or []
        if not airlines:
            raise RuntimeError("No airlines found during scrape.")
        save_airlines(airlines)
        return {"status": "ok", "count": len(airlines)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to refresh airlines: {exc}") from exc
