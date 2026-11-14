from datetime import datetime, timezone
import json
import pytest
from starlette.testclient import TestClient

from app.clock import FrozenClock, iso
from app.config import Settings
from app.db import connect, init_db
from app.discovery import run_discovery
from app.drafting import draft_campaign, draft_prospect, latest_draft
from app.llm.base import LLMResult
from app.llm.fake import FakeLLM
from app.main import app
from app.x.fixtures import FixtureReader


@pytest.fixture
def review_setup(tmp_path, monkeypatch):
    db_file = tmp_path / "review_web.db"
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
    conn.close()

    return str(db_file)


def test_review_page_lists_drafts_with_evidence_claims_and_flags(review_setup):
    with TestClient(app) as client:
        resp = client.get("/campaigns/1/review")
        assert resp.status_code == 200
        assert "Review Drafts — SEA AI Builders" in resp.text
        assert "Drafts (" in resp.text
        assert "Approved (" in resp.text
        assert "No Evidence (" in resp.text
        assert "Claims in this draft:" in resp.text
        assert "Verified Evidence" in resp.text
        assert "Save edit" in resp.text
        assert "Approve" in resp.text


def test_approve_requires_saved_text(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        pid = cur.fetchone()["id"]
        conn.close()

        # Submit different text than latest draft without saving first
        resp = client.post(
            f"/prospects/{pid}/approve",
            data={"text": "Unsaved edited text that is different from draft."},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Save your edit before approving" in resp.text


def test_approve_creates_outreach_and_shows_in_approved_tab(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        pid = cur.fetchone()["id"]
        d = latest_draft(conn, pid)
        conn.close()

        resp = client.post(
            f"/prospects/{pid}/approve",
            data={"text": d["text"]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Draft approved" in resp.text

        # Check approved tab shows outreach
        app_resp = client.get("/campaigns/1/review?tab=approved")
        assert app_resp.status_code == 200
        assert "Waiting to send" in app_resp.text
        assert d["text"] in app_resp.text
        assert "Withdraw" in app_resp.text


def test_approve_twice_creates_one_outreach(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        pid = cur.fetchone()["id"]
        d = latest_draft(conn, pid)
        conn.close()

        # First approval
        resp1 = client.post(
            f"/prospects/{pid}/approve",
            data={"text": d["text"]},
            follow_redirects=True,
        )
        assert resp1.status_code == 200
        assert "Draft approved" in resp1.text

        # Second approval fails because state is now queued, not drafted
        resp2 = client.post(
            f"/prospects/{pid}/approve",
            data={"text": d["text"]},
            follow_redirects=True,
        )
        assert resp2.status_code == 200
        assert "This draft was already handled" in resp2.text

        conn = connect(review_setup)
        cnt = conn.execute("SELECT count(*) FROM outreach WHERE prospect_id = ?", (pid,)).fetchone()[0]
        conn.close()
        assert cnt == 1


def test_blocked_draft_cannot_be_approved_until_edited(review_setup):
    conn = connect(review_setup)
    cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
    pid = cur.fetchone()["id"]
    # Mark draft with a block flag
    conn.execute(
        "UPDATE drafts SET validator_flags = ? WHERE prospect_id = ?",
        (json.dumps([{"level": "block", "code": "banned_phrase", "detail": "big fan"}]), pid),
    )
    conn.commit()
    d = latest_draft(conn, pid)
    conn.close()

    with TestClient(app) as client:
        # Check review page displays Blocked message and disables approve
        page = client.get("/campaigns/1/review")
        assert "Blocked:" in page.text

        # Direct POST to approve is refused
        resp = client.post(
            f"/prospects/{pid}/approve",
            data={"text": d["text"]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Edit the draft to resolve the blocked issues" in resp.text

        # Human saves edit
        new_text = "I read what you shared regarding robotics perception models. We build open source developer tools. Would you be open to chatting? No worries if not."
        edit_resp = client.post(
            f"/prospects/{pid}/edit",
            data={"text": new_text},
            follow_redirects=True,
        )
        assert edit_resp.status_code == 200
        assert "Draft edit saved" in edit_resp.text

        # Now approve succeeds because human edited it
        app_resp = client.post(
            f"/prospects/{pid}/approve",
            data={"text": new_text},
            follow_redirects=True,
        )
        assert app_resp.status_code == 200
        assert "Draft approved" in app_resp.text


def test_reject_moves_prospect_to_rejected(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        pid = cur.fetchone()["id"]
        conn.close()

        resp = client.post(
            f"/prospects/{pid}/reject",
            data={"reason": "Not a fit for our current campaign"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Prospect rejected" in resp.text

        conn = connect(review_setup)
        row = conn.execute("SELECT state, state_reason FROM prospects WHERE id = ?", (pid,)).fetchone()
        conn.close()
        assert row["state"] == "rejected"
        assert row["state_reason"] == "Not a fit for our current campaign"


def test_do_not_contact_adds_suppression_and_rejects(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id, x_user_id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        row = cur.fetchone()
        pid = row["id"]
        uid = row["x_user_id"]
        conn.close()

        resp = client.post(
            f"/prospects/{pid}/do-not-contact",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Added to do-not-contact list" in resp.text

        conn = connect(review_setup)
        p_row = conn.execute("SELECT state, state_reason FROM prospects WHERE id = ?", (pid,)).fetchone()
        supp_row = conn.execute("SELECT reason, source FROM suppression WHERE x_user_id = ?", (uid,)).fetchone()
        conn.close()

        assert p_row["state"] == "rejected"
        assert p_row["state_reason"] == "do_not_contact"
        assert supp_row is not None
        assert supp_row["source"] == "manual"


def test_withdraw_and_reapprove(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        pid = cur.fetchone()["id"]
        d = latest_draft(conn, pid)
        conn.close()

        # Approve
        client.post(f"/prospects/{pid}/approve", data={"text": d["text"]}, follow_redirects=True)

        conn = connect(review_setup)
        oid = conn.execute("SELECT id FROM outreach WHERE prospect_id = ?", (pid,)).fetchone()["id"]
        conn.close()

        # Withdraw
        w_resp = client.post(f"/outreach/{oid}/withdraw", follow_redirects=True)
        assert w_resp.status_code == 200
        assert "Approval withdrawn" in w_resp.text
        assert "Withdrawn" in w_resp.text
        assert "Re-approve" in w_resp.text

        # Re-approve
        r_resp = client.post(f"/outreach/{oid}/reapprove", follow_redirects=True)
        assert r_resp.status_code == 200
        assert "Message re-approved" in r_resp.text
        assert "Waiting to send" in r_resp.text
        assert "Withdraw" in r_resp.text


def test_model_text_is_escaped_on_review_page(review_setup):
    conn = connect(review_setup)
    cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
    pid = cur.fetchone()["id"]
    xss = "<script>alert(1)</script>"
    conn.execute(
        "UPDATE drafts SET text = ?, validator_flags = ? WHERE prospect_id = ?",
        (f"Hello {xss} world.", json.dumps([{"level": "block", "code": "banned_phrase", "detail": xss}]), pid),
    )
    conn.commit()
    conn.close()

    with TestClient(app) as client:
        resp = client.get("/campaigns/1/review")
        assert resp.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
        # Ensure raw <script>alert(1)</script> does not appear
        assert "<script>alert(1)</script>" not in resp.text


def test_multiline_model_draft_can_be_approved_when_browser_sends_crlf(tmp_path, monkeypatch):
    db_file = tmp_path / "crlf_web.db"
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
            'We are developing tools for embodied AI agents in Southeast Asia.',
            'robot, LLM, agent', 100, 'demo_seed_investor', 100, 1, 200, 40, 60, 50.0, ?
        )
        """,
        (now_str,),
    )
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    fake_llm = FakeLLM()
    run_discovery(conn, 1, reader, fake_llm, settings, clock, simulated=True)

    class MultilineLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema):
            res = super().call_tool(system, user, tool_name, tool_schema)
            if tool_name == "draft_dm" and res.data.get("decision") == "draft":
                data = dict(res.data)
                data["message"] = data["message"].replace(". ", ".\n\n")
                return LLMResult(
                    data=data,
                    input_tokens=res.input_tokens,
                    output_tokens=res.output_tokens,
                )
            return res

    cur = conn.execute(
        "SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'scored' AND score >= 60 LIMIT 1"
    )
    pid = cur.fetchone()["id"]
    outcome = draft_prospect(conn, pid, MultilineLLM(), settings, clock, simulated=True)
    assert outcome == "drafted"
    d = latest_draft(conn, pid)
    assert d is not None
    assert "\n\n" in d["text"]
    assert "\r" not in d["text"]
    conn.close()

    with TestClient(app) as client:
        # Browser submits CRLF
        submitted = d["text"].replace("\n", "\r\n")
        resp = client.post(
            f"/prospects/{pid}/approve",
            data={"text": submitted},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "tab=approved" in resp.headers["location"]

        conn = connect(str(db_file))
        outreach_row = conn.execute(
            "SELECT * FROM outreach WHERE prospect_id = ?", (pid,)
        ).fetchone()
        conn.close()
        assert outreach_row is not None
        assert "\r" not in outreach_row["final_text"]
        assert "\n\n" in outreach_row["final_text"]


def test_edit_then_approve_with_crlf_round_trip(review_setup):
    with TestClient(app) as client:
        conn = connect(review_setup)
        cur = conn.execute("SELECT id FROM prospects WHERE campaign_id = 1 AND state = 'drafted' LIMIT 1")
        pid = cur.fetchone()["id"]
        conn.close()

        # 1. Edit with CRLF
        edited_text = (
            "Hey Alice,\r\n\r\n"
            "This is a human-edited message with multiple CRLF line endings.\r\n\r\n"
            "Let us know if you would like to connect! No worries if busy."
        )
        edit_resp = client.post(
            f"/prospects/{pid}/edit",
            data={"text": edited_text},
            follow_redirects=False,
        )
        assert edit_resp.status_code == 303
        assert "Draft%20edit%20saved" in edit_resp.headers["location"]

        # Verify stored draft has LF only
        conn = connect(review_setup)
        d = latest_draft(conn, pid)
        conn.close()
        assert d is not None
        assert d["edited_by_human"] == 1
        assert "\r" not in d["text"]
        assert "\n\n" in d["text"]

        # 2. Browser submits CRLF when clicking Approve
        browser_submitted = d["text"].replace("\n", "\r\n")
        app_resp = client.post(
            f"/prospects/{pid}/approve",
            data={"text": browser_submitted},
            follow_redirects=False,
        )
        assert app_resp.status_code == 303
        assert "tab=approved" in app_resp.headers["location"]

        # 3. Outreach row has clean LF
        conn = connect(review_setup)
        outreach_row = conn.execute(
            "SELECT * FROM outreach WHERE prospect_id = ?", (pid,)
        ).fetchone()
        conn.close()
        assert outreach_row is not None
        assert "\r" not in outreach_row["final_text"]
        assert "\n\n" in outreach_row["final_text"]
