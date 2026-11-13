from datetime import timedelta
import threading
import time
import pytest

from app.clock import iso
from app.config import Settings
from app.db import connect
from app import db
from app.sender import send_one, send_batch, recover_on_startup
from app.approval import (
    resolve_unknown,
    add_suppression,
)
from app.x.base import (
    REASON_RECIPIENT_NOT_ACCEPTING,
    REASON_SENDER_NOT_VERIFIED,
    REASON_PRECHECK_NOT_ACCEPTING,
    REASON_SUPPRESSED,
    REASON_INTERRUPTED,
    REASON_TRANSPORT_ERROR,
)
from app.x.mock import MockTransport, SimulatedCrash
from tests.conftest import seed, approve_all


def test_happy_path_sends_once_and_records_ids(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=2)
    approve_all(conn, clock, seeded)

    transport = MockTransport()
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)

    assert res.sent == 2
    assert transport.delivered_count(seeded.user_ids[0]) == 1
    assert transport.delivered_count(seeded.user_ids[1]) == 1

    cur = conn.execute(
        "SELECT state, x_dm_event_id, x_dm_conversation_id, sent_at FROM outreach"
    )
    for row in cur.fetchall():
        assert row["state"] == "sent"
        assert row["x_dm_event_id"] is not None
        assert row["x_dm_conversation_id"] is not None
        assert row["sent_at"] is not None


def test_calling_send_one_twice_sends_once(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport()
    res1 = send_one(
        conn,
        outreach_ids[0],
        transport,
        clock,
        settings.daily_send_cap,
        settings.timezone,
    )
    assert res1 == "sent"

    res2 = send_one(
        conn,
        outreach_ids[0],
        transport,
        clock,
        settings.daily_send_cap,
        settings.timezone,
    )
    assert res2 == "skipped"
    assert transport.delivered_count(seeded.user_ids[0]) == 1


def test_timeout_after_delivery_becomes_unknown_and_is_never_retried(
    conn, clock, settings
):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["timeout_after_delivery"])
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.unknown == 1
    assert transport.delivered_count(seeded.user_ids[0]) == 1

    # Run send_batch again; delivered_count stays 1
    res2 = send_batch(
        conn, seeded.sender_account_id, transport, clock, settings
    )
    assert transport.delivered_count(seeded.user_ids[0]) == 1
    cur = conn.execute(
        "SELECT state FROM outreach WHERE id = ?", (outreach_ids[0],)
    )
    assert cur.fetchone()["state"] == "unknown"


def test_error_before_delivery_becomes_unknown_not_failed(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["error_before_delivery"])
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.unknown == 1
    assert transport.delivered_count(seeded.user_ids[0]) == 0

    cur = conn.execute(
        "SELECT state, state_reason FROM outreach WHERE id = ?",
        (outreach_ids[0],),
    )
    row = cur.fetchone()
    assert row["state"] == "unknown"
    assert row["state_reason"] == REASON_TRANSPORT_ERROR


def test_crash_mid_send_is_recovered_as_unknown_on_startup(
    db_path, conn, clock, settings
):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["crash_after_delivery"])
    with pytest.raises(SimulatedCrash):
        send_batch(conn, seeded.sender_account_id, transport, clock, settings)

    # Outreach row was left in 'sending' before recovery
    cur = conn.execute(
        "SELECT state FROM outreach WHERE id = ?", (outreach_ids[0],)
    )
    assert cur.fetchone()["state"] == "sending"

    # Open a new connection and recover
    new_conn = connect(db_path)
    rec_count = recover_on_startup(new_conn, clock)
    assert rec_count == 1

    cur = new_conn.execute(
        "SELECT state, state_reason FROM outreach WHERE id = ?",
        (outreach_ids[0],),
    )
    row = cur.fetchone()
    assert row["state"] == "unknown"
    assert row["state_reason"] == REASON_INTERRUPTED

    # Next batch sends nothing to that recipient
    send_batch(new_conn, seeded.sender_account_id, transport, clock, settings)
    assert transport.delivered_count(seeded.user_ids[0]) == 1
    new_conn.close()


def test_resolve_unknown_not_delivered_allows_one_retry(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["error_before_delivery", "ok"])
    send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert transport.delivered_count(seeded.user_ids[0]) == 0

    # Resolve with was_delivered=False
    resolve_unknown(
        conn, outreach_ids[0], was_delivered=False, now=iso(clock.now())
    )

    # Batch again, exactly one delivery
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.sent == 1
    assert transport.delivered_count(seeded.user_ids[0]) == 1


def test_rate_limit_halts_batch_and_keeps_rest_approved(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=3)
    approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["ok", "rate_limited", "ok"])
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.sent == 1
    assert res.halted_reason == "rate_limited"

    cur = conn.execute("SELECT state FROM outreach ORDER BY id ASC")
    states = [r["state"] for r in cur.fetchall()]
    assert states == ["sent", "approved", "approved"]


def test_recipient_not_accepting_becomes_blocked_with_reason(
    conn, clock, settings
):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["not_accepting"])
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.blocked == 1

    cur = conn.execute(
        "SELECT state, state_reason FROM outreach WHERE id = ?",
        (outreach_ids[0],),
    )
    row = cur.fetchone()
    assert row["state"] == "blocked"
    assert row["state_reason"] == REASON_RECIPIENT_NOT_ACCEPTING


def test_sender_not_verified_becomes_blocked_with_reason(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(faults=["not_verified"])
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.blocked == 1

    cur = conn.execute(
        "SELECT state, state_reason FROM outreach WHERE id = ?",
        (outreach_ids[0],),
    )
    row = cur.fetchone()
    assert row["state"] == "blocked"
    assert row["state_reason"] == REASON_SENDER_NOT_VERIFIED


def test_precheck_false_blocks_without_calling_send_dm(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(reachability={seeded.user_ids[0]: False})
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.blocked == 1
    assert transport.send_calls == 0

    cur = conn.execute(
        "SELECT state, state_reason FROM outreach WHERE id = ?",
        (outreach_ids[0],),
    )
    row = cur.fetchone()
    assert row["state"] == "blocked"
    assert row["state_reason"] == REASON_PRECHECK_NOT_ACCEPTING


def test_precheck_unknown_skips_and_keeps_approved(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    transport = MockTransport(reachability={seeded.user_ids[0]: None})
    send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert transport.send_calls == 0

    cur = conn.execute(
        "SELECT state FROM outreach WHERE id = ?", (outreach_ids[0],)
    )
    assert cur.fetchone()["state"] == "approved"


def test_suppression_added_after_approval_blocks_send(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    add_suppression(
        conn, seeded.user_ids[0], reason="opt_out", source="opt_out", now=iso(clock.now())
    )

    transport = MockTransport()
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.blocked == 1
    assert transport.send_calls == 0

    cur = conn.execute(
        "SELECT state, state_reason FROM outreach WHERE id = ?",
        (outreach_ids[0],),
    )
    row = cur.fetchone()
    assert row["state"] == "blocked"
    assert row["state_reason"] == REASON_SUPPRESSED


def test_daily_cap_counts_sent_sending_and_unknown(conn, clock):
    seeded = seed(conn, clock, n_recipients=5)
    approve_all(conn, clock, seeded)

    cap_settings = Settings(
        min_seconds_between_sends=0.0,
        daily_send_cap=2,
        max_consecutive_errors=3,
        send_lock_seconds=600,
    )
    transport = MockTransport()
    res = send_batch(conn, seeded.sender_account_id, transport, clock, cap_settings)
    assert res.sent == 2
    assert res.halted_reason == "daily_cap_reached"


def test_daily_cap_resets_on_next_local_day(conn, clock):
    seeded = seed(conn, clock, n_recipients=2)
    approve_all(conn, clock, seeded)

    cap1_settings = Settings(
        min_seconds_between_sends=0.0,
        daily_send_cap=1,
        max_consecutive_errors=3,
        send_lock_seconds=600,
    )
    transport = MockTransport()
    res1 = send_batch(
        conn, seeded.sender_account_id, transport, clock, cap1_settings
    )
    assert res1.sent == 1
    assert res1.halted_reason == "daily_cap_reached"

    # Advance clock past local midnight in Asia/Ho_Chi_Minh (UTC+7)
    # Current clock is 03:00 UTC = 10:00 local. Advance 15 hours -> 01:00 next day local.
    clock.advance(15 * 3600)

    res2 = send_batch(
        conn, seeded.sender_account_id, transport, clock, cap1_settings
    )
    assert res2.sent == 1
    assert transport.delivered_count(seeded.user_ids[1]) == 1


def test_three_consecutive_errors_halt_batch(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=5)
    approve_all(conn, clock, seeded)

    transport = MockTransport(
        faults=["http_400", "http_400", "http_400", "ok", "ok"]
    )
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.failed == 3
    assert res.halted_reason == "too_many_errors"

    cur = conn.execute("SELECT state FROM outreach ORDER BY id ASC")
    states = [r["state"] for r in cur.fetchall()]
    assert states == ["failed", "failed", "failed", "approved", "approved"]


def test_held_lock_halts_batch_without_sending(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    outreach_ids = approve_all(conn, clock, seeded)

    # Insert a non-expired lock row by hand
    future_time = iso(clock.now() + timedelta(seconds=1000))
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO send_locks (sender_account_id, owner, expires_at) VALUES (?, 'other_worker', ?)",
            (seeded.sender_account_id, future_time),
        )

    transport = MockTransport()
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.halted_reason == "lock_held"
    assert transport.send_calls == 0

    cur = conn.execute(
        "SELECT state FROM outreach WHERE id = ?", (outreach_ids[0],)
    )
    assert cur.fetchone()["state"] == "approved"


def test_expired_lock_is_taken_over(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=1)
    approve_all(conn, clock, seeded)

    # Insert an expired lock row by hand
    past_time = iso(clock.now() - timedelta(seconds=1000))
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO send_locks (sender_account_id, owner, expires_at) VALUES (?, 'dead_worker', ?)",
            (seeded.sender_account_id, past_time),
        )

    transport = MockTransport()
    res = send_batch(conn, seeded.sender_account_id, transport, clock, settings)
    assert res.sent == 1
    assert transport.delivered_count(seeded.user_ids[0]) == 1


def test_concurrent_batches_never_duplicate(db_path, clock, settings):
    # Two threads, separate connections to the same file, a threading.Barrier, one shared MockTransport
    c = connect(db_path)
    from app.db import init_db
    init_db(c)
    seeded = seed(c, clock, n_recipients=5)
    approve_all(c, clock, seeded)
    c.close()

    transport = MockTransport()
    barrier = threading.Barrier(2)

    def worker():
        thread_conn = connect(db_path)
        barrier.wait()
        for _ in range(20):
            cur = thread_conn.execute(
                "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND state = 'approved'",
                (seeded.sender_account_id,),
            )
            if cur.fetchone()[0] == 0:
                break
            send_batch(
                thread_conn,
                seeded.sender_account_id,
                transport,
                clock,
                settings,
            )
            time.sleep(0.005)
        thread_conn.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Assert every delivered_count <= 1 and the total equals the number of recipients
    total_delivered = 0
    for uid in seeded.user_ids:
        count = transport.delivered_count(uid)
        assert count <= 1, f"Recipient {uid} was delivered {count} times"
        total_delivered += count
    assert total_delivered == len(seeded.user_ids)


def test_every_state_change_has_an_audit_row(conn, clock, settings):
    seeded = seed(conn, clock, n_recipients=4)
    approve_all(conn, clock, seeded)

    transport = MockTransport(
        faults=["ok", "not_accepting", "http_400", "timeout_after_delivery"]
    )
    send_batch(conn, seeded.sender_account_id, transport, clock, settings)

    cur = conn.execute("SELECT id, state FROM outreach")
    rows = cur.fetchall()
    assert len(rows) == 4
    for r in rows:
        cur_a = conn.execute(
            "SELECT to_state FROM audit_log WHERE entity = 'outreach' AND entity_id = ? ORDER BY id DESC LIMIT 1",
            (r["id"],),
        )
        audit_row = cur_a.fetchone()
        assert audit_row is not None
        assert audit_row["to_state"] == r["state"]
