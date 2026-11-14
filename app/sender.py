from dataclasses import dataclass, field
from datetime import timedelta
import time
import uuid
import zoneinfo
import sqlite3

from app import db
from app.clock import iso
from app.state import transition, StaleStateError
from app.x.base import (
    SendOutcome,
    REASON_INTERRUPTED,
    REASON_PRECHECK_NOT_ACCEPTING,
    REASON_SUPPRESSED,
    REASON_RATE_LIMITED,
    REASON_TRANSPORT_ERROR,
)


@dataclass
class BatchResult:
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    unknown: int = 0
    halted_reason: str | None = (
        None  # 'lock_held', 'daily_cap_reached', 'rate_limited', 'too_many_errors'
    )
    outreach_ids: list[int] = field(
        default_factory=list
    )  # ids attempted, in order


def recover_on_startup(conn: sqlite3.Connection, clock) -> int:
    with db.transaction(conn):
        cur = conn.execute("SELECT id FROM outreach WHERE state = 'sending'")
        ids = [row[0] for row in cur.fetchall()]
        now_str = iso(clock.now())
        for oid in ids:
            transition(
                conn,
                "outreach",
                oid,
                "sending",
                "unknown",
                "system",
                now_str,
                reason=REASON_INTERRUPTED,
            )
        return len(ids)


def send_one(
    conn: sqlite3.Connection,
    outreach_id: int,
    transport,
    clock,
    daily_cap: int,
    tz_name: str,
    report: dict | None = None,
) -> str:
    # 1. Read the row outside a transaction. If it is not approved, return "skipped".
    cur = conn.execute(
        "SELECT id, sender_account_id, recipient_x_user_id, state, final_text FROM outreach WHERE id = ?",
        (outreach_id,),
    )
    row = cur.fetchone()
    if not row or row["state"] != "approved":
        return "skipped"

    sender_account_id = row["sender_account_id"]
    recipient_x_user_id = row["recipient_x_user_id"]
    final_text = row["final_text"]

    # 2. Pre-check, outside any transaction: transport.check_recipient(...)
    reach = transport.check_recipient(sender_account_id, recipient_x_user_id)
    if reach.can_dm is False:
        with db.transaction(conn):
            try:
                transition(
                    conn,
                    "outreach",
                    outreach_id,
                    "approved",
                    "blocked",
                    "system",
                    iso(clock.now()),
                    reason=REASON_PRECHECK_NOT_ACCEPTING,
                )
            except StaleStateError:
                return "skipped"
        return "blocked"
    if reach.can_dm is None:
        return "skipped"

    # 3. In one transaction:
    with db.transaction(conn):
        cur_supp = conn.execute(
            "SELECT 1 FROM suppression WHERE x_user_id = ?",
            (recipient_x_user_id,),
        )
        if cur_supp.fetchone() is not None:
            try:
                transition(
                    conn,
                    "outreach",
                    outreach_id,
                    "approved",
                    "blocked",
                    "system",
                    iso(clock.now()),
                    reason=REASON_SUPPRESSED,
                )
            except StaleStateError:
                return "skipped"
            return "blocked"

        tz = zoneinfo.ZoneInfo(tz_name)
        now_utc = clock.now()
        now_local = now_utc.astimezone(tz)
        start_of_day_local = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_of_today_utc_iso = iso(start_of_day_local)

        cur_cap = conn.execute(
            "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND "
            "((sent_at IS NOT NULL AND sent_at >= ?) OR state IN ('sending','unknown'))",
            (sender_account_id, start_of_today_utc_iso),
        )
        if cur_cap.fetchone()[0] >= daily_cap:
            return "cap_reached"

        attempt_now = iso(clock.now())
        try:
            transition(
                conn,
                "outreach",
                outreach_id,
                "approved",
                "sending",
                "system",
                attempt_now,
                attempt_started_at=attempt_now,
            )
        except StaleStateError:
            return "skipped"

    # 4. Outside any transaction, call transport.send_dm(...)
    try:
        if report is not None:
            report["called_send_dm"] = True
        outcome = transport.send_dm(
            sender_account_id, recipient_x_user_id, final_text
        )
    except Exception as exc:
        outcome = SendOutcome(
            "unknown", reason=REASON_TRANSPORT_ERROR, detail=repr(exc)
        )

    # 5. In one transaction, map the outcome:
    with db.transaction(conn):
        now_str = iso(clock.now())
        if outcome.kind == "sent":
            transition(
                conn,
                "outreach",
                outreach_id,
                "sending",
                "sent",
                "system",
                now_str,
                reason=outcome.reason,
                sent_at=now_str,
                x_dm_event_id=outcome.dm_event_id,
                x_dm_conversation_id=outcome.dm_conversation_id,
            )
        elif outcome.kind == "blocked":
            transition(
                conn,
                "outreach",
                outreach_id,
                "sending",
                "blocked",
                "system",
                now_str,
                reason=outcome.reason,
            )
        elif outcome.kind == "failed":
            transition(
                conn,
                "outreach",
                outreach_id,
                "sending",
                "failed",
                "system",
                now_str,
                reason=outcome.reason,
            )
        elif outcome.kind == "rate_limited":
            transition(
                conn,
                "outreach",
                outreach_id,
                "sending",
                "approved",
                "system",
                now_str,
                reason=REASON_RATE_LIMITED,
            )
        elif outcome.kind == "unknown":
            transition(
                conn,
                "outreach",
                outreach_id,
                "sending",
                "unknown",
                "system",
                now_str,
                reason=outcome.reason,
            )
        else:
            raise ValueError(f"Unknown outcome kind: {outcome.kind}")

    return outcome.kind


def send_batch(
    conn: sqlite3.Connection,
    sender_account_id: int,
    transport,
    clock,
    settings,
    sleep=time.sleep,
    lock_owner: str | None = None,
) -> BatchResult:
    owner = lock_owner or uuid.uuid4().hex
    now_dt = clock.now()
    now_iso = iso(now_dt)
    expires_at = iso(now_dt + timedelta(seconds=settings.send_lock_seconds))

    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO send_locks (sender_account_id, owner, expires_at) VALUES (?, ?, ?)
            ON CONFLICT(sender_account_id) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at
            WHERE send_locks.expires_at < ?
            """,
            (sender_account_id, owner, expires_at, now_iso),
        )

    cur = conn.execute(
        "SELECT owner FROM send_locks WHERE sender_account_id = ?",
        (sender_account_id,),
    )
    lock_row = cur.fetchone()
    if not lock_row or lock_row[0] != owner:
        return BatchResult(halted_reason="lock_held")

    try:
        cur_acc = conn.execute(
            "SELECT daily_send_cap FROM sender_accounts WHERE id = ?",
            (sender_account_id,),
        )
        acc_row = cur_acc.fetchone()
        daily_cap = (
            min(acc_row[0], settings.daily_send_cap)
            if acc_row and acc_row[0] is not None
            else settings.daily_send_cap
        )

        cur_rows = conn.execute(
            "SELECT id FROM outreach WHERE sender_account_id = ? AND state = 'approved' ORDER BY approved_at ASC, id ASC",
            (sender_account_id,),
        )
        approved_ids = [r[0] for r in cur_rows.fetchall()]

        batch_result = BatchResult()
        consecutive_errors = 0

        for i, oid in enumerate(approved_ids):
            batch_result.outreach_ids.append(oid)
            report: dict = {}
            result = send_one(
                conn, oid, transport, clock, daily_cap, settings.timezone, report=report
            )
            called_send_dm = bool(report.get("called_send_dm"))

            if result == "sent":
                batch_result.sent += 1
                consecutive_errors = 0
            elif result == "blocked":
                batch_result.blocked += 1
                consecutive_errors = 0
            elif result == "failed":
                batch_result.failed += 1
                consecutive_errors += 1
            elif result == "unknown":
                batch_result.unknown += 1
                consecutive_errors += 1
            elif result == "cap_reached":
                batch_result.halted_reason = "daily_cap_reached"
                break
            elif result == "rate_limited":
                batch_result.halted_reason = "rate_limited"
                break

            if consecutive_errors >= settings.max_consecutive_errors:
                batch_result.halted_reason = "too_many_errors"
                break

            if called_send_dm and i < len(approved_ids) - 1:
                sleep(settings.min_seconds_between_sends)

            new_expires = iso(
                clock.now() + timedelta(seconds=settings.send_lock_seconds)
            )
            with db.transaction(conn):
                conn.execute(
                    "UPDATE send_locks SET expires_at = ? WHERE sender_account_id = ? AND owner = ?",
                    (new_expires, sender_account_id, owner),
                )

        return batch_result
    finally:
        try:
            with db.transaction(conn):
                conn.execute(
                    "DELETE FROM send_locks WHERE sender_account_id = ? AND owner = ?",
                    (sender_account_id, owner),
                )
        except Exception:
            pass
