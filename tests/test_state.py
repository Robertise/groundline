import pytest
from app import db
from app.clock import iso
from app.state import (
    transition,
    TransitionError,
    StaleStateError,
    PROSPECT_TRANSITIONS,
    OUTREACH_TRANSITIONS,
)
from tests.conftest import seed


def test_allowed_transition_updates_state_and_writes_audit(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    with db.transaction(conn):
        transition(
            conn,
            "prospect",
            prospect_id,
            "drafted",
            "queued",
            "human",
            now_str,
            reason="ready_to_send",
        )

    cur = conn.execute(
        "SELECT state, state_reason, updated_at FROM prospects WHERE id = ?",
        (prospect_id,),
    )
    row = cur.fetchone()
    assert row["state"] == "queued"
    assert row["state_reason"] == "ready_to_send"
    assert row["updated_at"] == now_str

    cur_audit = conn.execute(
        "SELECT entity, entity_id, from_state, to_state, actor, reason, created_at "
        "FROM audit_log WHERE entity = 'prospect' AND entity_id = ? ORDER BY id DESC LIMIT 1",
        (prospect_id,),
    )
    audit = cur_audit.fetchone()
    assert audit["from_state"] == "drafted"
    assert audit["to_state"] == "queued"
    assert audit["actor"] == "human"
    assert audit["reason"] == "ready_to_send"
    assert audit["created_at"] == now_str


def test_disallowed_transition_raises_and_changes_nothing(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    with pytest.raises(TransitionError):
        with db.transaction(conn):
            # drafted -> scored is not an allowed transition
            transition(
                conn, "prospect", prospect_id, "drafted", "scored", "system", now_str
            )

    cur = conn.execute("SELECT state FROM prospects WHERE id = ?", (prospect_id,))
    assert cur.fetchone()["state"] == "drafted"


def test_wrong_actor_raises(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    # Create an outreach row in unknown
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO outreach (sender_account_id, recipient_x_user_id, prospect_id, state, final_text, approved_at, updated_at) "
            "VALUES (?, ?, ?, 'unknown', 'text', ?, ?)",
            (seeded.sender_account_id, seeded.user_ids[0], prospect_id, now_str, now_str),
        )
        outreach_id = cur.lastrowid

    # unknown -> approved allows ONLY human, not system
    with pytest.raises(TransitionError):
        with db.transaction(conn):
            transition(
                conn,
                "outreach",
                outreach_id,
                "unknown",
                "approved",
                "system",
                now_str,
            )

    cur = conn.execute("SELECT state FROM outreach WHERE id = ?", (outreach_id,))
    assert cur.fetchone()["state"] == "unknown"


def test_stale_from_state_raises_stale_state_error(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    # Row is in drafted, but from_state claims it is in discovered
    with pytest.raises(StaleStateError):
        with db.transaction(conn):
            transition(
                conn,
                "prospect",
                prospect_id,
                "discovered",
                "scored",
                "system",
                now_str,
            )


def test_sending_to_approved_requires_rate_limited_reason(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO outreach (sender_account_id, recipient_x_user_id, prospect_id, state, final_text, approved_at, updated_at) "
            "VALUES (?, ?, ?, 'sending', 'text', ?, ?)",
            (seeded.sender_account_id, seeded.user_ids[0], prospect_id, now_str, now_str),
        )
        outreach_id = cur.lastrowid

    # sending -> approved with other reason or None must raise TransitionError
    with pytest.raises(TransitionError):
        with db.transaction(conn):
            transition(
                conn,
                "outreach",
                outreach_id,
                "sending",
                "approved",
                "system",
                now_str,
                reason="other_reason",
            )

    # sending -> approved with reason="rate_limited" succeeds
    with db.transaction(conn):
        transition(
            conn,
            "outreach",
            outreach_id,
            "sending",
            "approved",
            "system",
            now_str,
            reason="rate_limited",
        )

    cur = conn.execute("SELECT state FROM outreach WHERE id = ?", (outreach_id,))
    assert cur.fetchone()["state"] == "approved"


def test_no_transition_leads_from_sent_or_replied_to_approved():
    for table_name, transitions in [
        ("prospects", PROSPECT_TRANSITIONS),
        ("outreach", OUTREACH_TRANSITIONS),
    ]:
        for (from_s, to_s) in transitions.keys():
            if from_s in ("sent", "replied"):
                assert to_s != "approved", f"Illegal transition {from_s} -> approved in {table_name}"
            if from_s == "replied":
                pytest.fail(f"Illegal transition out of replied: {from_s} -> {to_s} in {table_name}")


def test_unknown_extra_field_is_rejected(conn, clock):
    seeded = seed(conn, clock, n_recipients=1)
    prospect_id = seeded.prospect_ids[0]
    now_str = iso(clock.now())

    with pytest.raises(TransitionError):
        with db.transaction(conn):
            transition(
                conn,
                "prospect",
                prospect_id,
                "drafted",
                "queued",
                "human",
                now_str,
                invalid_extra="bad_value",
            )
