from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.state import RUNS

router = APIRouter()


@router.websocket("/ws/{run_id}")
async def websocket_run(ws: WebSocket, run_id: str):
    await ws.accept()
    state = RUNS.get(run_id)
    if not state:
        await ws.send_json({"type": "status", "status": "error", "error": "run not found", "run_id": run_id})
        await ws.close()
        return

    queue = state.subscribe(ws)
    try:
        for log in state.logs:
            await ws.send_json(log)
        await state.push_status()

        while True:
            payload = await queue.get()
            await ws.send_json(payload)
    except WebSocketDisconnect:
        state.unsubscribe(ws)
    except Exception:
        state.unsubscribe(ws)
        await ws.close()
