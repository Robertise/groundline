from collections.abc import Generator
import json
import sqlite3
from fastapi import Depends

from app.clock import SystemClock, iso
from app.config import Settings, get_settings
from app.crypto import TokenCipher
from app.db import connect
from app.llm.base import LLM
from app.llm.bedrock import BedrockLLM
from app.llm.fake import FakeLLM
from app.x.base import XReader, XAuthError
from app.x.client import XClient
from app.x.fixtures import FixtureReader
from app.x.mock import MockTransport
from app.x.dm import XDMTransport


def get_conn(
    settings: Settings = Depends(get_settings),
) -> Generator[sqlite3.Connection, None, None]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_sender(conn: sqlite3.Connection, settings: Settings) -> sqlite3.Row | None:
    if settings.mode == "mock":
        # Ensure a sender_accounts row for the fixture sender exists
        try:
            with open(settings.fixtures_path, "r", encoding="utf-8") as f:
                fix_data = json.load(f)
                sender = fix_data["sender"]
        except Exception:
            sender = {
                "id": "9100000000000000001",
                "username": "demo_sender",
            }

        cur = conn.execute(
            "SELECT * FROM sender_accounts WHERE x_user_id = ?", (sender["id"],)
        )
        row = cur.fetchone()
        if not row:
            clock = SystemClock()
            now_str = iso(clock.now())
            conn.execute(
                "INSERT INTO sender_accounts (x_user_id, username, daily_send_cap, created_at) VALUES (?, ?, 20, ?)",
                (sender["id"], sender["username"], now_str),
            )
            cur = conn.execute(
                "SELECT * FROM sender_accounts WHERE x_user_id = ?",
                (sender["id"],),
            )
            row = cur.fetchone()
        return row
    else:
        cur = conn.execute(
            "SELECT * FROM sender_accounts ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()


def build_reader(
    conn: sqlite3.Connection,
    settings: Settings,
    clock,
    cost_context: dict | None = None,
) -> XReader:
    if settings.mode == "mock":
        return FixtureReader(
            settings.fixtures_path,
            clock,
            conn=conn,
            cost_context=cost_context,
        )
    else:
        sender_row = current_sender(conn, settings)
        if not sender_row:
            raise XAuthError(401, "no_sender", "No connected X sender account")
        cipher = TokenCipher(settings.secret_key)
        return XClient(
            conn,
            sender_row["id"],
            cipher,
            settings,
            clock,
            cost_context=cost_context,
        )


def build_llm(settings: Settings) -> LLM:
    if settings.mode == "mock":
        return FakeLLM()
    else:
        return BedrockLLM(
            settings.bedrock_model_id,
            settings.aws_region,
            access_key_id=settings.aws_access_key_id,
            secret_access_key=settings.aws_secret_access_key,
            session_token=settings.aws_session_token,
            profile=settings.aws_profile,
        )


def build_transport(conn: sqlite3.Connection, settings: Settings, clock):
    if settings.mode == "mock":
        return MockTransport()
    if settings.mode == "dry_run":
        raise RuntimeError("dry_run mode never sends")
    sender_row = current_sender(conn, settings)
    if not sender_row:
        raise XAuthError(401, "no_sender", "No connected X sender account")
    cipher = TokenCipher(settings.secret_key)
    client = XClient(conn, sender_row["id"], cipher, settings, clock)
    handles = [h.strip().lstrip("@").lower() for h in settings.live_allowed_handles.split(",") if h.strip()]
    allowed_ids = None
    if handles:
        placeholders = ",".join("?" for _ in handles)
        cur = conn.execute(
            f"SELECT x_user_id FROM x_users WHERE lower(username) IN ({placeholders})", handles
        )
        allowed_ids = {r["x_user_id"] for r in cur.fetchall()}
    return XDMTransport(client, allowed_ids)

