import urllib.parse
import sqlite3
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clock import SystemClock, iso
from app.config import Settings, get_settings, missing_live_settings
from app.crypto import TokenCipher
from app.deps import get_conn
from app.x.oauth import start, finish, OAuthError

router = APIRouter()


@router.get("/connect")
def connect_x(
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    if settings.mode == "mock":
        return HTMLResponse(
            content="<h1>Connect X is disabled in Mock mode</h1>",
            status_code=400,
        )

    missing = missing_live_settings(settings)
    if missing:
        return HTMLResponse(
            content=f"<h1>Cannot connect X: Missing settings: {', '.join(missing)}</h1>",
            status_code=400,
        )

    clock = SystemClock()
    auth_url = start(conn, settings, iso(clock.now()))
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback")
def oauth_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    if error:
        return RedirectResponse(
            url=f"/campaigns?error={urllib.parse.quote(error)}", status_code=302
        )

    if not code or not state:
        return RedirectResponse(
            url="/campaigns?error=Missing+code+or+state", status_code=302
        )

    try:
        cipher = TokenCipher(settings.secret_key)
        clock = SystemClock()
        sender_id = finish(conn, settings, cipher, clock, code, state)
        cur = conn.execute(
            "SELECT username FROM sender_accounts WHERE id = ?", (sender_id,)
        )
        row = cur.fetchone()
        username = row["username"] if row else "unknown"
        return RedirectResponse(
            url=f"/campaigns?msg=Connected+as+@{username}", status_code=302
        )
    except OAuthError as e:
        return RedirectResponse(
            url=f"/campaigns?error={urllib.parse.quote(str(e))}",
            status_code=302,
        )
