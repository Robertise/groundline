import sqlite3
from app import db
from app.state import transition, record_insert, TransitionError


class AlreadyContacted(Exception):
    pass


class Suppressed(Exception):
    pass


def approve_prospect(
    conn: sqlite3.Connection, prospect_id: int, final_text: str, now: str
) -> int:
    is_suppressed = False
    is_already_contacted = False
    outreach_id = None
    x_user_id = None
    sender_account_id = None

    with db.transaction(conn):
        cur = conn.execute(
            "SELECT p.id, p.campaign_id, p.x_user_id, p.state, c.sender_account_id "
            "FROM prospects p "
            "JOIN campaigns c ON p.campaign_id = c.id "
            "WHERE p.id = ?",
            (prospect_id,),
        )
        row = cur.fetchone()
        if not row:
            raise TransitionError(f"Prospect {prospect_id} not found")
        if row["state"] != "drafted":
            raise TransitionError(
                f"Prospect {prospect_id} state is '{row['state']}', expected 'drafted'"
            )

        x_user_id = row["x_user_id"]
        sender_account_id = row["sender_account_id"]

        cur_supp = conn.execute(
            "SELECT 1 FROM suppression WHERE x_user_id = ?", (x_user_id,)
        )
        if cur_supp.fetchone() is not None:
            transition(
                conn,
                "prospect",
                prospect_id,
                "drafted",
                "filtered_out",
                "system",
                now,
                reason="suppressed",
            )
            is_suppressed = True
        else:
            conn.execute("SAVEPOINT sp_approve")
            try:
                cur_ins = conn.execute(
                    "INSERT INTO outreach (sender_account_id, recipient_x_user_id, prospect_id, state, final_text, approved_at, updated_at) "
                    "VALUES (?, ?, ?, 'approved', ?, ?, ?)",
                    (sender_account_id, x_user_id, prospect_id, final_text, now, now),
                )
                outreach_id = cur_ins.lastrowid
                conn.execute("RELEASE SAVEPOINT sp_approve")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK TO SAVEPOINT sp_approve")
                conn.execute("RELEASE SAVEPOINT sp_approve")
                transition(
                    conn,
                    "prospect",
                    prospect_id,
                    "drafted",
                    "filtered_out",
                    "system",
                    now,
                    reason="already_contacted",
                )
                is_already_contacted = True
            else:
                record_insert(conn, "outreach", outreach_id, "approved", "human", now)
                transition(
                    conn, "prospect", prospect_id, "drafted", "queued", "human", now
                )

    if is_suppressed:
        raise Suppressed(f"User {x_user_id} is suppressed")
    if is_already_contacted:
        raise AlreadyContacted(
            f"User {x_user_id} has already been contacted by sender {sender_account_id}"
        )

    return outreach_id


def cancel_approval(conn: sqlite3.Connection, outreach_id: int, now: str) -> None:
    with db.transaction(conn):
        transition(
            conn, "outreach", outreach_id, "approved", "cancelled", "human", now
        )


def reapprove(conn: sqlite3.Connection, outreach_id: int, now: str) -> None:
    with db.transaction(conn):
        transition(
            conn, "outreach", outreach_id, "cancelled", "approved", "human", now
        )


def resolve_unknown(
    conn: sqlite3.Connection, outreach_id: int, was_delivered: bool, now: str
) -> None:
    with db.transaction(conn):
        if was_delivered:
            transition(
                conn,
                "outreach",
                outreach_id,
                "unknown",
                "sent",
                "human",
                now,
                reason="resolved_by_human",
                sent_at=now,
            )
        else:
            transition(
                conn,
                "outreach",
                outreach_id,
                "unknown",
                "approved",
                "human",
                now,
                reason="resolved_by_human",
            )


def retry_failed(conn: sqlite3.Connection, outreach_id: int, now: str) -> None:
    with db.transaction(conn):
        transition(
            conn, "outreach", outreach_id, "failed", "approved", "human", now
        )


def recheck_blocked(conn: sqlite3.Connection, outreach_id: int, now: str) -> None:
    with db.transaction(conn):
        transition(
            conn, "outreach", outreach_id, "blocked", "approved", "human", now
        )


def mark_replied(
    conn: sqlite3.Connection, outreach_id: int, now: str, actor: str = "human"
) -> None:
    with db.transaction(conn):
        transition(conn, "outreach", outreach_id, "sent", "replied", actor, now)


def mark_replied_with_note(conn: sqlite3.Connection, outreach_id: int, note: str, now: str) -> None:
    with db.transaction(conn):
        transition(conn, "outreach", outreach_id, "sent", "replied", "human", now)
        conn.execute(
            "INSERT INTO replies (outreach_id, x_event_id, text, classification, handled, created_at) "
            "VALUES (?, NULL, ?, NULL, 0, ?)",
            (outreach_id, note.strip()[:2000], now),
        )


def add_suppression(
    conn: sqlite3.Connection, x_user_id: str, reason: str, source: str, now: str
) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO suppression (x_user_id, reason, source, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(x_user_id) DO UPDATE SET reason = excluded.reason, source = excluded.source, created_at = excluded.created_at",
            (x_user_id, reason, source, now),
        )
