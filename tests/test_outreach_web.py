from datetime import datetime, timezone
import pytest
from starlette.testclient import TestClient

from app.clock import FrozenClock, iso
from app.config import Settings
from app.db import connect, init_db
from app.discovery import run_discovery
from app.drafting import draft_campaign, latest_draft
from app.llm.fake import FakeLLM
from app.main import app
from app.sender import send_one
from app.x.fixtures import FixtureReader
from app.x.mock import MockTransport


@pytest.fixture
def outreach_setup(tmp_path, monkeypatch):
    db_file = tmp_path / "outreach_web.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    conn = connect(str(db_file))
    init_db(conn)

    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    settings = Settings(
        mode="mock",
        db_path=str(db_file),
        fixtures_path="./fixtures/demo_x.json",
    )
    now_str = iso(clock.now())
    conn.execute(
        "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, created_at) "
        "VALUES (1, '9100000000000000001', 'demo_sender', 20, ?)",
        (now_str,),
    )
    conn.execute(
        """
        INSERT INTO campaigns (
            id, sender_account_id, name, segment, brief, search_keywords, search_max_posts,
            seed_handles, seed_following_max, include_sender_followers, followers_max,
            max_deep_profiles, min_score, budget_usd, created_at
        ) VALUES (
            1, 1, 'SEA AI Builders', 'AI and robotics in SEA',
            'We are a small research team developing evaluation benchmarks and open tools for embodied AI agents in Southeast Asia.',
            'robot, LLM, agent', 100, 'demo_seed_investor', 100, 1, 200, 40, 60, 50.0, ?
        )
        """,
        (now_str,),
    )

    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    llm = FakeLLM()
    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    draft_campaign(conn, 1, llm, settings, clock, simulated=True, limit=10)

    # Find two drafted prospects and approve them
    cur = conn.execute(
        "SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 2"
    )
    pids = [r["id"] for r in cur.fetchall()]
    assert len(pids) == 2

    with TestClient(app) as client:
        for pid in pids:
            d = latest_draft(conn, pid)
            client.post(
                f"/prospects/{pid}/approve",
                data={"text": d["text"]},
                follow_redirects=True,
            )

    conn.close()
    return str(db_file)


def test_outreach_page_shows_queue_with_approved_messages(outreach_setup):
    with TestClient(app) as client:
        resp = client.get("/outreach")
        assert resp.status_code == 200
        assert "Outreach" in resp.text
        assert "Queue (2)" in resp.text
        assert "Withdraw on the review page" in resp.text
        assert "Sent today: 0 of 20" in resp.text


def test_send_in_mock_mode_marks_messages_sent(outreach_setup):
    with TestClient(app) as client:
        conn = connect(outreach_setup)
        cur = conn.execute(
            "SELECT u.username FROM outreach o JOIN x_users u ON o.recipient_x_user_id = u.x_user_id"
        )
        usernames = [r["username"] for r in cur.fetchall()]
        conn.close()

        resp = client.post("/outreach/send", follow_redirects=True)
        assert resp.status_code == 200
        assert "Sending started" in resp.text

        conn = connect(outreach_setup)
        cur_o = conn.execute("SELECT state, attempts FROM outreach")
        rows = cur_o.fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["state"] == "sent"
            assert r["attempts"] == 1
        conn.close()

        resp_sent = client.get("/outreach?tab=sent")
        assert resp_sent.status_code == 200
        for u in usernames:
            assert f"@{u}" in resp_sent.text


def test_send_twice_never_sends_a_second_time(outreach_setup):
    with TestClient(app) as client:
        # First send
        client.post("/outreach/send", follow_redirects=True)

        # Second send
        resp = client.post("/outreach/send", follow_redirects=True)
        assert resp.status_code == 200
        assert "Nothing approved to send." in resp.text

        conn = connect(outreach_setup)
        cur = conn.execute("SELECT attempts FROM outreach")
        for r in cur.fetchall():
            assert r["attempts"] == 1
        conn.close()


def test_send_is_refused_in_dry_run_mode(outreach_setup, monkeypatch):
    monkeypatch.setenv("GROUNDLINE_MODE", "dry_run")
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("app.routes_outreach.run_send_batch", spy)
    with TestClient(app) as client:
        resp = client.post("/outreach/send", follow_redirects=False)
        assert resp.status_code == 303
        assert "error=Dry-run" in resp.headers["location"]
        assert calls == []

        conn = connect(outreach_setup)
        cur = conn.execute("SELECT state FROM outreach")
        for r in cur.fetchall():
            assert r["state"] == "approved"
        conn.close()

        page = client.get("/outreach?tab=queue")
        assert page.status_code == 200
        assert "Dry-run mode never sends. Set GROUNDLINE_MODE=live to send." in page.text
        assert "disabled" in page.text


def test_mark_replied_stores_note_and_shows_in_replies_tab(outreach_setup):
    with TestClient(app) as client:
        client.post("/outreach/send", follow_redirects=True)

        conn = connect(outreach_setup)
        cur = conn.execute("SELECT id FROM outreach WHERE state = 'sent' LIMIT 1")
        oid = cur.fetchone()["id"]
        conn.close()

        resp = client.post(
            f"/outreach/{oid}/replied",
            data={"note": "Interested in talking more"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Interested in talking more" in resp.text

        conn = connect(outreach_setup)
        cur_r = conn.execute("SELECT text FROM replies WHERE outreach_id = ?", (oid,))
        reply = cur_r.fetchone()
        assert reply is not None
        assert reply["text"] == "Interested in talking more"
        conn.close()


def test_unknown_row_can_be_resolved_as_delivered(outreach_setup):
    conn = connect(outreach_setup)
    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    cur = conn.execute("SELECT id FROM outreach WHERE state = 'approved' LIMIT 1")
    oid = cur.fetchone()["id"]
    transport = MockTransport(faults=["timeout_after_delivery"])
    send_one(conn, oid, transport, clock, 20, "Asia/Ho_Chi_Minh")

    cur_check = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_check.fetchone()["state"] == "unknown"
    conn.close()

    with TestClient(app) as client:
        resp = client.post(
            f"/outreach/{oid}/resolve",
            data={"delivered": "yes"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    conn = connect(outreach_setup)
    cur_after = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_after.fetchone()["state"] == "sent"
    conn.close()


def test_unknown_row_can_be_resolved_as_not_delivered_and_returns_to_queue(outreach_setup):
    conn = connect(outreach_setup)
    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    cur = conn.execute("SELECT id FROM outreach WHERE state = 'approved' LIMIT 1")
    oid = cur.fetchone()["id"]
    transport = MockTransport(faults=["timeout_after_delivery"])
    send_one(conn, oid, transport, clock, 20, "Asia/Ho_Chi_Minh")
    conn.close()

    with TestClient(app) as client:
        resp = client.post(
            f"/outreach/{oid}/resolve",
            data={"delivered": "no"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    conn = connect(outreach_setup)
    cur_after = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_after.fetchone()["state"] == "approved"
    conn.close()


def test_failed_row_can_be_retried(outreach_setup):
    conn = connect(outreach_setup)
    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    cur = conn.execute("SELECT id FROM outreach WHERE state = 'approved' LIMIT 1")
    oid = cur.fetchone()["id"]
    transport = MockTransport(faults=["http_400"])
    send_one(conn, oid, transport, clock, 20, "Asia/Ho_Chi_Minh")

    cur_check = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_check.fetchone()["state"] == "failed"
    conn.close()

    with TestClient(app) as client:
        resp = client.post(
            f"/outreach/{oid}/retry",
            follow_redirects=True,
        )
        assert resp.status_code == 200

    conn = connect(outreach_setup)
    cur_after = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_after.fetchone()["state"] == "approved"
    conn.close()


def test_blocked_row_shows_reason_and_next_step_and_can_be_rechecked(outreach_setup):
    conn = connect(outreach_setup)
    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    cur = conn.execute("SELECT id FROM outreach WHERE state = 'approved' LIMIT 1")
    oid = cur.fetchone()["id"]
    transport = MockTransport(faults=["not_verified"])
    send_one(conn, oid, transport, clock, 20, "Asia/Ho_Chi_Minh")

    cur_check = conn.execute("SELECT state, state_reason FROM outreach WHERE id = ?", (oid,))
    row = cur_check.fetchone()
    assert row["state"] == "blocked"
    assert row["state_reason"] == "sender_not_verified"
    conn.close()

    with TestClient(app) as client:
        page = client.get("/outreach?tab=attention")
        assert page.status_code == 200
        assert "only accepts message requests from verified accounts" in page.text

        resp = client.post(
            f"/outreach/{oid}/recheck",
            follow_redirects=True,
        )
        assert resp.status_code == 200

    conn = connect(outreach_setup)
    cur_after = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_after.fetchone()["state"] == "approved"
    conn.close()


def test_resolve_with_invalid_value_changes_nothing(outreach_setup):
    conn = connect(outreach_setup)
    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    cur = conn.execute("SELECT id FROM outreach WHERE state = 'approved' LIMIT 1")
    oid = cur.fetchone()["id"]
    transport = MockTransport(faults=["timeout_after_delivery"])
    send_one(conn, oid, transport, clock, 20, "Asia/Ho_Chi_Minh")
    conn.close()

    with TestClient(app) as client:
        resp = client.post(
            f"/outreach/{oid}/resolve",
            data={"delivered": "invalid_choice"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    conn = connect(outreach_setup)
    cur_after = conn.execute("SELECT state FROM outreach WHERE id = ?", (oid,))
    assert cur_after.fetchone()["state"] == "unknown"
    conn.close()


def test_action_on_row_in_wrong_state_redirects_with_error(outreach_setup):
    with TestClient(app) as client:
        client.post("/outreach/send", follow_redirects=True)

        conn = connect(outreach_setup)
        cur = conn.execute("SELECT id FROM outreach WHERE state = 'sent' LIMIT 1")
        oid = cur.fetchone()["id"]
        conn.close()

        resp = client.post(
            f"/outreach/{oid}/retry",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "This message changed state. Refresh and try again." in resp.text


def test_outreach_page_escapes_message_text(outreach_setup):
    conn = connect(outreach_setup)
    cur = conn.execute("SELECT id FROM outreach WHERE state = 'approved' LIMIT 1")
    oid = cur.fetchone()["id"]
    evil_text = "<script>alert(1)</script>"
    conn.execute("UPDATE outreach SET final_text = ? WHERE id = ?", (evil_text, oid))
    conn.commit()
    conn.close()

    with TestClient(app) as client:
        resp = client.get("/outreach?tab=queue")
        assert resp.status_code == 200
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


def test_nav_links_to_outreach(outreach_setup):
    with TestClient(app) as client:
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert '<a href="/outreach">Outreach</a>' in resp.text


def test_limits_page_lists_what_the_tool_will_not_do(outreach_setup):
    with TestClient(app) as client:
        resp = client.get("/limits")
        assert resp.status_code == 200
        assert "never messages the same person twice" in resp.text
        assert "cannot read replies" in resp.text


def test_nav_links_to_limits(outreach_setup):
    with TestClient(app) as client:
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert '<a href="/limits">Limits</a>' in resp.text


def test_queue_explains_message_held_by_live_allowlist(outreach_setup, monkeypatch):
    monkeypatch.setenv("GROUNDLINE_MODE", "live")
    monkeypatch.setenv("GROUNDLINE_LIVE_ALLOWED_HANDLES", "someone_else")
    monkeypatch.setenv("GROUNDLINE_X_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("GROUNDLINE_X_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("GROUNDLINE_SECRET_KEY", "bW9ja19zZWNyZXRfa2V5X2Zvcl90ZXN0aW5nXzMyYnl0ZXM=")

    with TestClient(app) as client:
        resp = client.get("/outreach?tab=queue")
        assert resp.status_code == 200
        assert "Test allowlist is on" in resp.text
        assert "Held: not on the test allowlist" in resp.text


def test_no_allowlist_banner_in_mock_mode(outreach_setup, monkeypatch):
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")
    monkeypatch.setenv("GROUNDLINE_LIVE_ALLOWED_HANDLES", "someone_else")
    monkeypatch.setenv("GROUNDLINE_X_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("GROUNDLINE_X_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("GROUNDLINE_SECRET_KEY", "bW9ja19zZWNyZXRfa2V5X2Zvcl90ZXN0aW5nXzMyYnl0ZXM=")

    with TestClient(app) as client:
        resp = client.get("/outreach?tab=queue")
        assert resp.status_code == 200
        assert "Test allowlist is on" not in resp.text
        assert "Held: not on the test allowlist" not in resp.text


