import random
from app.clock import iso, FrozenClock
from app.config import Settings
from app.db import connect, init_db
from app.sender import send_batch, send_one, recover_on_startup
from app.approval import resolve_unknown, retry_failed
from app.x.mock import MockTransport, SimulatedCrash, FAULT_KINDS
from tests.conftest import seed, approve_all
from datetime import datetime, timezone


def test_no_recipient_receives_two_messages_across_200_seeded_runs(tmp_path):
    settings = Settings(
        min_seconds_between_sends=0.0,
        daily_send_cap=20,
        max_consecutive_errors=3,
        send_lock_seconds=600,
    )

    for seed_num in range(200):
        rng = random.Random(seed_num)
        db_file = tmp_path / f"run{seed_num}.db"
        conn = connect(str(db_file))
        init_db(conn)

        dt = datetime(2026, 11, 13, 3, 0, 0, tzinfo=timezone.utc)
        clock = FrozenClock(dt)

        # 1. Fresh database, 5 recipients, all approved
        seeded = seed(conn, clock, n_recipients=5)
        approve_all(conn, clock, seeded)

        # 2. MockTransport with 12 random faults
        transport = MockTransport(
            faults=[rng.choice(FAULT_KINDS) for _ in range(12)]
        )

        # 3. Up to 4 rounds
        for _ in range(4):
            try:
                send_batch(
                    conn, seeded.sender_account_id, transport, clock, settings
                )
            except SimulatedCrash:
                conn.close()
                conn = connect(str(db_file))
                recover_on_startup(conn, clock)

            if rng.random() < 0.3:
                cur = conn.execute(
                    "SELECT id FROM outreach WHERE state = 'approved'"
                )
                approved = cur.fetchall()
                if approved:
                    chosen_id = rng.choice(approved)["id"]
                    for _ in range(2):
                        try:
                            send_one(
                                conn,
                                chosen_id,
                                transport,
                                clock,
                                settings.daily_send_cap,
                                settings.timezone,
                            )
                        except SimulatedCrash:
                            conn.close()
                            conn = connect(str(db_file))
                            recover_on_startup(conn, clock)

            # After each round, act like a careful human
            now_str = iso(clock.now())
            cur = conn.execute(
                "SELECT id, recipient_x_user_id FROM outreach WHERE state = 'unknown'"
            )
            unknowns = cur.fetchall()
            for r in unknowns:
                was_delivered = (
                    transport.delivered_count(r["recipient_x_user_id"]) > 0
                )
                resolve_unknown(conn, r["id"], was_delivered, now_str)

            cur = conn.execute("SELECT id FROM outreach WHERE state = 'failed'")
            faileds = cur.fetchall()
            for r in faileds:
                retry_failed(conn, r["id"], now_str)

            # 4. Invariant after every round: for every recipient, transport.delivered_count(r) <= 1
            for uid in seeded.user_ids:
                count = transport.delivered_count(uid)
                assert (
                    count <= 1
                ), f"Seed {seed_num}: recipient {uid} received {count} > 1 messages"

        # 5. Invariant at the end: every recipient with a delivery has an outreach row in sent
        for uid in seeded.user_ids:
            count = transport.delivered_count(uid)
            if count > 0:
                cur = conn.execute(
                    "SELECT state FROM outreach WHERE sender_account_id = ? AND recipient_x_user_id = ?",
                    (seeded.sender_account_id, uid),
                )
                row = cur.fetchone()
                assert (
                    row is not None and row["state"] == "sent"
                ), f"Seed {seed_num}: recipient {uid} was delivered but state is {row['state'] if row else 'None'}"

        conn.close()
