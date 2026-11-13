from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
import pytest

from app.clock import FrozenClock, iso, Clock
from app.config import Settings
from app.db import connect, init_db
from app import db
from app.state import record_insert
from app.approval import approve_prospect


@dataclass
class SeededData:
    sender_account_id: int
    campaign_id: int
    user_ids: list[str]
    prospect_ids: list[int]


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "groundline_test.db"
    return str(p)


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def clock():
    dt = datetime(2026, 11, 13, 3, 0, 0, tzinfo=timezone.utc)
    return FrozenClock(dt)


@pytest.fixture
def settings():
    return Settings(
        min_seconds_between_sends=0.0,
        daily_send_cap=20,
        max_consecutive_errors=3,
        send_lock_seconds=600,
    )


@pytest.fixture
def seed_helper():
    return seed


@pytest.fixture
def approve_all_helper():
    return approve_all


def seed(
    conn: sqlite3.Connection, clock: Clock, n_recipients: int = 5
) -> SeededData:
    now_str = iso(clock.now())
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO sender_accounts (x_user_id, username, daily_send_cap, created_at) VALUES (?, ?, ?, ?)",
            ("sender_x_1", "sender_user", 20, now_str),
        )
        sender_id = cur.lastrowid

        cur = conn.execute(
            "INSERT INTO campaigns (sender_account_id, name, created_at) VALUES (?, ?, ?)",
            (sender_id, "Campaign 1", now_str),
        )
        campaign_id = cur.lastrowid

        user_ids = []
        prospect_ids = []
        for i in range(1, n_recipients + 1):
            uid = f"user_{i}"
            user_ids.append(uid)
            conn.execute(
                "INSERT INTO x_users (x_user_id, username, name, receives_your_dm, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (uid, f"handle_{i}", f"Name {i}", 1, now_str),
            )
            cur_p = conn.execute(
                "INSERT INTO prospects (campaign_id, x_user_id, state, created_at, updated_at) VALUES (?, ?, 'drafted', ?, ?)",
                (campaign_id, uid, now_str, now_str),
            )
            pid = cur_p.lastrowid
            prospect_ids.append(pid)
            record_insert(conn, "prospect", pid, "drafted", "system", now_str)

    return SeededData(
        sender_account_id=sender_id,
        campaign_id=campaign_id,
        user_ids=user_ids,
        prospect_ids=prospect_ids,
    )


def approve_all(
    conn: sqlite3.Connection, clock: Clock, seeded: SeededData
) -> list[int]:
    outreach_ids = []
    now_str = iso(clock.now())
    for pid in seeded.prospect_ids:
        oid = approve_prospect(conn, pid, f"Hello to prospect {pid}", now_str)
        outreach_ids.append(oid)
    return outreach_ids
