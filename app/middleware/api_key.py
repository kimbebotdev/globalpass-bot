from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import GLOBALPASS_API_KEY


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith("/api/") or path == "/api" or path.startswith("/ws/") or path == "/ws":
            if not GLOBALPASS_API_KEY:
                return JSONResponse({"message": "API key not configured on server"}, status_code=500)

            api_key = request.headers.get("X-API-Key")
            if not api_key or api_key != GLOBALPASS_API_KEY:
                return JSONResponse({"message": "Unauthorized"}, status_code=401)

        return await call_next(request)
