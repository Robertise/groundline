import pytest
from app import db
from app.clock import iso
from app.approval import (
    approve_prospect,
    cancel_approval,
    reapprove,
    add_suppression,
    AlreadyContacted,
    Suppressed,
)
from tests.conftest import seed


def test_approve_creates_outreach_and_queues_prospect(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    outreach_id = approve_prospect(conn, prospect_id, "Personalized outreach", now_str)
    assert outreach_id > 0

    cur_p = conn.execute("SELECT state FROM prospects WHERE id = ?", (prospect_id,))
    assert cur_p.fetchone()["state"] == "queued"

    cur_o = conn.execute("SELECT * FROM outreach WHERE id = ?", (outreach_id,))
    row_o = cur_o.fetchone()
    assert row_o["state"] == "approved"
    assert row_o["final_text"] == "Personalized outreach"
    assert row_o["recipient_x_user_id"] == seeded.user_ids[0]


def test_same_person_in_second_campaign_is_already_contacted(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id_1 = seeded.prospect_ids[0]
    user_id = seeded.user_ids[0]
    now_str = iso(clock.now())

    # Approve prospect 1 in campaign 1
    approve_prospect(conn, prospect_id_1, "Message 1", now_str)

    # Create a second campaign for the same sender account
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO campaigns (sender_account_id, name, created_at) VALUES (?, 'Campaign 2', ?)",
            (seeded.sender_account_id, now_str),
        )
        campaign_2_id = cur.lastrowid
        cur_p2 = conn.execute(
            "INSERT INTO prospects (campaign_id, x_user_id, state, created_at, updated_at) VALUES (?, ?, 'drafted', ?, ?)",
            (campaign_2_id, user_id, now_str, now_str),
        )
        prospect_id_2 = cur_p2.lastrowid

    # Trying to approve prospect 2 with same sender & recipient must raise AlreadyContacted
    with pytest.raises(AlreadyContacted):
        approve_prospect(conn, prospect_id_2, "Message 2", now_str)

    cur_p2_state = conn.execute(
        "SELECT state, state_reason FROM prospects WHERE id = ?", (prospect_id_2,)
    )
    p2_row = cur_p2_state.fetchone()
    assert p2_row["state"] == "filtered_out"
    assert p2_row["state_reason"] == "already_contacted"


def test_same_user_id_with_new_handle_is_already_contacted(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id_1 = seeded.prospect_ids[0]
    user_id = seeded.user_ids[0]
    now_str = iso(clock.now())

    approve_prospect(conn, prospect_id_1, "Message 1", now_str)

    # Recipient updates their username on X
    with db.transaction(conn):
        conn.execute(
            "UPDATE x_users SET username = 'renamed_user_handle' WHERE x_user_id = ?",
            (user_id,),
        )
        # Create campaign 2 and prospect 2
        cur = conn.execute(
            "INSERT INTO campaigns (sender_account_id, name, created_at) VALUES (?, 'Campaign 2', ?)",
            (seeded.sender_account_id, now_str),
        )
        campaign_2_id = cur.lastrowid
        cur_p2 = conn.execute(
            "INSERT INTO prospects (campaign_id, x_user_id, state, created_at, updated_at) VALUES (?, ?, 'drafted', ?, ?)",
            (campaign_2_id, user_id, now_str, now_str),
        )
        prospect_id_2 = cur_p2.lastrowid

    with pytest.raises(AlreadyContacted):
        approve_prospect(conn, prospect_id_2, "Message 2", now_str)

    cur_p2_state = conn.execute(
        "SELECT state, state_reason FROM prospects WHERE id = ?", (prospect_id_2,)
    )
    p2_row = cur_p2_state.fetchone()
    assert p2_row["state"] == "filtered_out"
    assert p2_row["state_reason"] == "already_contacted"


def test_suppressed_person_cannot_be_approved(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    user_id = seeded.user_ids[0]
    now_str = iso(clock.now())

    add_suppression(conn, user_id, reason="opted_out", source="opt_out", now=now_str)

    with pytest.raises(Suppressed):
        approve_prospect(conn, prospect_id, "Message to suppressed", now_str)

    cur_p = conn.execute(
        "SELECT state, state_reason FROM prospects WHERE id = ?", (prospect_id,)
    )
    p_row = cur_p.fetchone()
    assert p_row["state"] == "filtered_out"
    assert p_row["state_reason"] == "suppressed"


def test_cancel_and_reapprove_reuse_the_same_outreach_row(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    outreach_id = approve_prospect(conn, prospect_id, "Message", now_str)

    cancel_approval(conn, outreach_id, now_str)
    cur = conn.execute("SELECT state FROM outreach WHERE id = ?", (outreach_id,))
    assert cur.fetchone()["state"] == "cancelled"

    reapprove(conn, outreach_id, now_str)
    cur = conn.execute("SELECT state FROM outreach WHERE id = ?", (outreach_id,))
    assert cur.fetchone()["state"] == "approved"
