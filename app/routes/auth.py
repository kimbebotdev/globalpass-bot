from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import is_authenticated, verify_password

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    index_path = Path("index.html")
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse("<h1>Globalpass Bot</h1><p>UI not found.</p>")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(
        """
        <html>
            <head>
                <title>Login</title>
                <style>
                    body {
                        font-family: Arial, sans-serif;
                        background: #f5f5f5;
                    }
                    .card {
                        max-width: 360px;
                        margin: 10vh auto;
                        background: #fff;
                        padding: 24px;
                        border-radius: 8px;
                        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
                    }
                    label {
                        display: block;
                        margin-bottom: 6px;
                        font-weight: 600;
                    }
                    input {
                        width: 100%;
                        padding: 10px;
                        margin-bottom: 12px;
                        border: 1px solid #ddd;
                        border-radius: 6px;
                    }
                    button {
                        width: 100%;
                        padding: 10px;
                        background: #111827;
                        color: #fff;
                        border: none;
                        border-radius: 6px;
                        font-weight: 600;
                        cursor: pointer;
                    }
                </style>
            </head>
            <body>
                <div class="card">
                    <h2>Admin Login</h2>
                    <form method="post" action="/login">
                        <label for="username">Username</label>
                        <input id="username" name="username" type="text" required />
                        <label for="password">Password</label>
                        <input id="password" name="password" type="password" required />
                        <button type="submit">Sign in</button>
                    </form>
                </div>
            </body>
        </html>
        """
    )


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "").strip()
    if verify_password(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse("<h3>Invalid credentials</h3><a href='/login'>Try again</a>", status_code=401)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@router.get("/airlines.json")
async def airlines():
    path = Path("airlines.json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="airlines.json not found")
    return FileResponse(path)
