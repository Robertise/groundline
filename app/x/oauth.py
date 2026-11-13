import base64
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import sqlite3
import urllib.parse
import httpx

from app import db
from app.clock import iso
from app.crypto import TokenCipher

API = "https://api.x.com/2"
SCOPES = "tweet.read users.read follows.read dm.read dm.write offline.access"


class OAuthError(Exception):
    pass


def start(conn: sqlite3.Connection, settings, now: str) -> str:
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)

    # Compute S256 code challenge
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )

    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    cutoff = iso(now_dt - timedelta(minutes=10))

    with db.transaction(conn):
        conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (cutoff,))
        conn.execute(
            "INSERT INTO oauth_states (state, code_verifier, created_at) VALUES (?, ?, ?)",
            (state, code_verifier, now),
        )

    params = {
        "response_type": "code",
        "client_id": settings.x_client_id,
        "redirect_uri": settings.x_redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"https://x.com/i/oauth2/authorize?{urllib.parse.urlencode(params)}"


def finish(
    conn: sqlite3.Connection,
    settings,
    cipher: TokenCipher,
    clock,
    code: str,
    state: str,
    http: httpx.Client | None = None,
) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "SELECT code_verifier, created_at FROM oauth_states WHERE state = ?",
            (state,),
        )
        row = cur.fetchone()
        if not row:
            raise OAuthError("Unknown or invalid OAuth state")

        conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))

    created_dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    now_dt = clock.now()
    if (now_dt - created_dt) > timedelta(minutes=10):
        raise OAuthError("OAuth state expired")

    client = http or httpx.Client(timeout=20.0)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.x_redirect_uri,
        "code_verifier": row["code_verifier"],
        "client_id": settings.x_client_id,
    }
    auth = (settings.x_client_id, settings.x_client_secret)

    resp = client.post(f"{API}/oauth2/token", data=data, auth=auth)
    if resp.status_code != 200:
        raise OAuthError(f"Token exchange failed: {resp.text}")

    token_json = resp.json()
    granted_scopes = set(token_json.get("scope", "").split())
    if "dm.write" not in granted_scopes:
        raise OAuthError("App permissions must be Read and write and Direct message")

    user_resp = client.get(
        f"{API}/users/me",
        headers={"Authorization": f"Bearer {token_json['access_token']}"},
    )
    if user_resp.status_code != 200:
        raise OAuthError(f"Failed to fetch user: {user_resp.text}")

    u_data = user_resp.json().get("data", {})
    x_user_id = str(u_data.get("id"))
    username = u_data.get("username", "")

    now_str = iso(clock.now())
    token_dict = {
        "access_token": token_json["access_token"],
        "refresh_token": token_json.get("refresh_token", ""),
        "expires_in": token_json.get("expires_in", 7200),
        "scope": token_json.get("scope", ""),
        "obtained_at": now_str,
    }
    blob = cipher.encrypt(token_dict)

    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO sender_accounts (x_user_id, username, token_ciphertext, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(x_user_id) DO UPDATE SET
                username = excluded.username,
                token_ciphertext = excluded.token_ciphertext
            """,
            (x_user_id, username, blob, now_str),
        )
        cur = conn.execute(
            "SELECT id FROM sender_accounts WHERE x_user_id = ?", (x_user_id,)
        )
        sender_account_id = cur.fetchone()["id"]

    return sender_account_id
