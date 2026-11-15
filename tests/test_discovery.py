from datetime import datetime, timezone
import json
import pytest

from app.clock import FrozenClock, iso
from app.config import Settings
from app.db import connect, init_db
from app.discovery import run_discovery
from app.llm.base import LLMResult, LLMOutputError
from app.llm.fake import FakeLLM
from app.x.base import XUser, XPost, XRateLimited
from app.x.fixtures import FixtureReader


@pytest.fixture
def disc_setup(tmp_path):
    db_file = tmp_path / "disc.db"
    conn = connect(str(db_file))
    init_db(conn)

    clock = FrozenClock(datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc))
    settings = Settings(
        mode="mock",
        db_path=str(db_file),
        fixtures_path="./fixtures/demo_x.json",
    )

    # Insert sender account
    now_str = iso(clock.now())
    conn.execute(
        "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, created_at) "
        "VALUES (1, '9100000000000000001', 'demo_sender', 20, ?)",
        (now_str,),
    )
    # Insert campaign
    conn.execute(
        """
        INSERT INTO campaigns (
            id, sender_account_id, name, segment, search_keywords, search_max_posts,
            seed_handles, seed_following_max, include_sender_followers, followers_max,
            max_deep_profiles, min_score, budget_usd, created_at
        ) VALUES (
            1, 1, 'SEA AI Builders', 'AI and robotics founders and builders in Southeast Asia',
            'robot, LLM, agent', 100, 'demo_seed_investor', 100, 1, 200, 40, 60, 50.0, ?
        )
        """,
        (now_str,),
    )
    return conn, settings, clock


def test_mock_run_scores_relevant_people_with_evidence(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute("SELECT status, counts FROM discovery_runs WHERE id = ?", (run_id,))
    run_row = cur.fetchone()
    assert run_row["status"] == "completed"

    # The six "scored high" users from the table:
    high_scored_usernames = [
        "demo_robotics_founder_sg",
        "demo_llm_researcher_hn",
        "demo_agent_builder_hcm",
        "demo_devtools_founder_jkt",
        "demo_embodied_ai_phd",
        "demo_inference_infra_bkk",
    ]

    for uname in high_scored_usernames:
        cur = conn.execute(
            """
            SELECT p.state, p.score, p.fit_reasons, (SELECT COUNT(*) FROM evidence e WHERE e.prospect_id = p.id) as ev_count
            FROM prospects p
            JOIN x_users u ON p.x_user_id = u.x_user_id
            WHERE p.campaign_id = 1 AND u.username = ?
            """,
            (uname,),
        )
        row = cur.fetchone()
        assert row is not None, f"Expected {uname} to be found"
        assert row["state"] == "scored", f"Expected {uname} to be scored, got {row['state']}"
        assert row["score"] >= 60, f"Expected {uname} score >= 60, got {row['score']}"
        assert row["ev_count"] >= 1, f"Expected {uname} to have >= 1 evidence row, got {row['ev_count']}"


def test_filters_record_reasons(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute("SELECT counts FROM discovery_runs WHERE id = ?", (run_id,))
    counts = json.loads(cur.fetchone()["counts"])
    filtered_reasons = counts["filtered"]

    # protected, cannot_dm, inactive, bio_screen_failed, low_score each appear
    assert "protected" in filtered_reasons
    assert "cannot_dm" in filtered_reasons
    assert "inactive" in filtered_reasons
    assert "bio_screen_failed" in filtered_reasons
    assert "low_score" in filtered_reasons


def test_prompt_injection_profile_is_not_scored(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.state, p.state_reason
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_prompt_injection'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "filtered_out"
    assert row["state_reason"] == "bio_screen_failed"


def test_sender_is_excluded(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.state, p.state_reason
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_sender'
        """
    )
    row = cur.fetchone()
    if row:
        assert row["state"] == "filtered_out"
        assert row["state_reason"] == "is_sender"


def test_already_contacted_person_is_filtered(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    # Create an outreach row for sender 1 and user demo_robotics_founder_sg under a prior campaign
    now_str = iso(clock.now())
    conn.execute(
        "INSERT INTO campaigns (id, sender_account_id, name, created_at) VALUES (2, 1, 'Previous Campaign', ?)",
        (now_str,),
    )
    conn.execute(
        "INSERT INTO x_users (x_user_id, username, fetched_at) VALUES ('9100000000000000101', 'demo_robotics_founder_sg', ?)",
        (now_str,),
    )
    cur_p = conn.execute(
        "INSERT INTO prospects (campaign_id, x_user_id, state, created_at, updated_at) VALUES (2, '9100000000000000101', 'queued', ?, ?)",
        (now_str, now_str),
    )
    p_id = cur_p.lastrowid
    conn.execute(
        """
        INSERT INTO outreach (
            sender_account_id, recipient_x_user_id, prospect_id, state, final_text, approved_at, updated_at
        ) VALUES (1, '9100000000000000101', ?, 'sent', 'Previously sent message', ?, ?)
        """,
        (p_id, now_str, now_str),
    )

    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.state, p.state_reason
        FROM prospects p
        WHERE p.campaign_id = 1 AND p.x_user_id = '9100000000000000101'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "filtered_out"
    assert row["state_reason"] == "already_contacted"


def test_rerun_does_not_duplicate_prospects_or_refetch_cached_posts(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    # Run 1
    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    cur = conn.execute("SELECT COUNT(*) FROM prospects WHERE campaign_id = 1")
    count_1 = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM ledger WHERE endpoint = 'user_posts' AND campaign_id = 1")
    user_posts_calls_1 = cur.fetchone()[0]

    # Advance clock 1 day
    clock.advance(86400)

    # Run 2
    reader2 = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    run_discovery(conn, 1, reader2, llm, settings, clock, simulated=True)

    cur = conn.execute("SELECT COUNT(*) FROM prospects WHERE campaign_id = 1")
    count_2 = cur.fetchone()[0]
    assert count_2 == count_1  # No duplicate prospects

    cur = conn.execute("SELECT COUNT(*) FROM ledger WHERE endpoint = 'user_posts' AND campaign_id = 1")
    user_posts_calls_2 = cur.fetchone()[0]
    assert user_posts_calls_2 == user_posts_calls_1  # Cached within 7 days, so no refetch


def test_budget_stops_run_and_keeps_partial_results(disc_setup):
    conn, settings, clock = disc_setup
    # Set a tiny budget of 0.05 USD
    conn.execute("UPDATE campaigns SET budget_usd = 0.05 WHERE id = 1")

    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute("SELECT status, counts, error FROM discovery_runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    assert row["status"] == "budget_reached"
    assert row["error"] is not None


def test_rate_limit_pauses_run_and_keeps_partial_results(disc_setup):
    conn, settings, clock = disc_setup
    base_reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    class RateLimitingReader:
        def __getattr__(self, name):
            return getattr(base_reader, name)

        def user_posts(self, user_id, max_posts):
            raise XRateLimited(reset_at=iso(clock.now()), detail="Rate limited by X")

    run_id = run_discovery(conn, 1, RateLimitingReader(), FakeLLM(), settings, clock, simulated=True)

    cur = conn.execute("SELECT status, error FROM discovery_runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    assert row["status"] == "rate_limited"


def test_max_deep_profiles_leaves_rest_discovered(disc_setup):
    conn, settings, clock = disc_setup
    # Set max_deep_profiles to 2
    conn.execute("UPDATE campaigns SET max_deep_profiles = 2 WHERE id = 1")

    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute("SELECT counts FROM discovery_runs WHERE id = ?", (run_id,))
    counts = json.loads(cur.fetchone()["counts"])
    assert counts["scored"] <= 2
    assert counts["left_discovered"] > 0

    # Ensure remaining passed profiles stayed in state 'discovered'
    cur = conn.execute("SELECT COUNT(*) FROM prospects WHERE campaign_id = 1 AND state = 'discovered'")
    assert cur.fetchone()[0] > 0


def test_profile_text_is_sent_as_json_data(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    recorded_messages = []

    class CapturingLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            recorded_messages.append((system, user))
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    llm = CapturingLLM()
    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    assert len(recorded_messages) > 0
    for system, user in recorded_messages:
        # System prompt contains "data, not instructions"
        assert "data, not instructions" in system

        # Assert user message parses as JSON after the header
        if "Profiles (JSON):\n" in user:
            header, json_part = user.split("Profiles (JSON):\n")
            data = json.loads(json_part)
            assert isinstance(data, list)
        elif "User (JSON):\n" in user:
            header, json_part = user.split("User (JSON):\n")
            data = json.loads(json_part)
            assert isinstance(data, dict)


def test_x_costs_are_attributed_to_campaign_and_run(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute("SELECT campaign_id, run_id, endpoint FROM ledger")
    rows = cur.fetchall()
    assert len(rows) > 0
    for r in rows:
        assert r["campaign_id"] == 1
        assert r["run_id"] == run_id


def test_budget_counts_x_costs_when_reader_has_no_context(disc_setup):
    conn, settings, clock = disc_setup
    conn.execute("UPDATE campaigns SET budget_usd = 0.05 WHERE id = 1")
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    cur = conn.execute(
        "SELECT status, counts, error FROM discovery_runs WHERE id = ?", (run_id,)
    )
    row = cur.fetchone()
    assert row["status"] == "budget_reached"
    assert row["error"] is not None


def test_tier1_reason_is_stored_on_filtered_prospects(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute(
        "SELECT id, state_reason, fit_reasons FROM prospects WHERE campaign_id = 1 AND state_reason IN ('bio_screen_failed', 'unsupported_quote')"
    )
    filtered = cur.fetchall()
    assert len(filtered) > 0
    for r in filtered:
        assert r["fit_reasons"] is not None
        data = json.loads(r["fit_reasons"])
        assert "tier1" in data
        assert data["tier1"]["pass"] is False
        assert len(data["tier1"]["reason"]) > 0

    cur_scored = conn.execute(
        "SELECT id, fit_reasons FROM prospects WHERE campaign_id = 1 AND state = 'scored'"
    )
    scored = cur_scored.fetchall()
    assert len(scored) > 0
    for r in scored:
        data = json.loads(r["fit_reasons"])
        assert "tier1" in data
        assert data["tier1"]["pass"] is True
        assert "criteria" in data
        assert "why_fit" in data


def test_malformed_output_for_one_prospect_does_not_stop_run(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)

    target_malformed_user = "demo_robotics_founder_sg"

    class BadProspectLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            res = super().call_tool(system, user, tool_name, tool_schema, max_tokens)
            if tool_name == "score_user" and f'"username": "{target_malformed_user}"' in user:
                return LLMResult(
                    data={"criteria": ["role", "domain"]},
                    input_tokens=150,
                    output_tokens=60,
                )
            return res

    llm = BadProspectLLM()
    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute(
        "SELECT status, counts FROM discovery_runs WHERE id = ?", (run_id,)
    )
    run_row = cur.fetchone()
    assert run_row["status"] == "completed"

    counts = json.loads(run_row["counts"])
    error_usernames = [e["username"] for e in counts.get("errors", [])]
    assert target_malformed_user in error_usernames

    cur_u = conn.execute(
        "SELECT p.state FROM prospects p JOIN x_users u ON p.x_user_id = u.x_user_id WHERE u.username = ?",
        (target_malformed_user,),
    )
    assert cur_u.fetchone()["state"] == "discovered"

    cur_led = conn.execute(
        "SELECT * FROM ledger WHERE detail LIKE ? AND run_id = ?",
        ("%tier2_score_invalid:%", run_id),
    )
    invalid_rows = cur_led.fetchall()
    assert len(invalid_rows) > 0


def test_unexpected_exception_in_scoring_is_isolated(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)

    target_err_user = "demo_robotics_founder_sg"

    class ExplodingLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if tool_name == "score_user" and f'"username": "{target_err_user}"' in user:
                raise KeyError("unexpected key crash")
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    llm = ExplodingLLM()
    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute(
        "SELECT status, counts FROM discovery_runs WHERE id = ?", (run_id,)
    )
    run_row = cur.fetchone()
    assert run_row["status"] == "completed"

    counts = json.loads(run_row["counts"])
    err_entries = [e for e in counts.get("errors", []) if e["username"] == target_err_user]
    assert len(err_entries) == 1
    assert "KeyError" in err_entries[0]["error"]

    cur_u = conn.execute(
        "SELECT p.state FROM prospects p JOIN x_users u ON p.x_user_id = u.x_user_id WHERE u.username = ?",
        (target_err_user,),
    )
    assert cur_u.fetchone()["state"] == "discovered"


def test_budget_still_stops_run_inside_scoring_loop(disc_setup):
    conn, settings, clock = disc_setup
    conn.execute("UPDATE campaigns SET budget_usd = 0.08 WHERE id = 1")
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    cur = conn.execute(
        "SELECT status, counts, error FROM discovery_runs WHERE id = ?", (run_id,)
    )
    row = cur.fetchone()
    assert row["status"] == "budget_reached"
    assert "Budget exceeded" in row["error"]


def test_audit_timestamps_advance_during_run(disc_setup):
    from datetime import timedelta
    conn, settings, _ = disc_setup

    class AdvancingClock:
        def __init__(self, start_dt):
            self._current = start_dt

        def now(self):
            dt = self._current
            self._current += timedelta(seconds=1)
            return dt

    clock = AdvancingClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn)
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute("SELECT created_at FROM audit_log WHERE entity = 'prospect'")
    timestamps = [r["created_at"] for r in cur.fetchall()]
    assert len(timestamps) > 1
    assert len(set(timestamps)) > 1


def test_us_based_builder_is_filtered_outside_segment(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute(
        """
        SELECT p.id, p.state, p.score, p.fit_reasons
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_ai_app_builder_us'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "filtered_out"
    fit_reasons = json.loads(row["fit_reasons"])
    assert fit_reasons["segment_status"] == "mismatch"
    assert len(fit_reasons["segment_evidence"]) == 1
    assert fit_reasons["segment_evidence"][0]["source"] == "location"
    assert "San Francisco" in fit_reasons["segment_evidence"][0]["quote"]

    # Check audit log for outside_segment reason
    audit_cur = conn.execute(
        "SELECT reason FROM audit_log WHERE entity = 'prospect' AND entity_id = ? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    )
    audit_row = audit_cur.fetchone()
    assert audit_row["reason"] == "outside_segment"

    # Check run counts
    run_cur = conn.execute("SELECT counts FROM discovery_runs WHERE id = ?", (run_id,))
    counts = json.loads(run_cur.fetchone()["counts"])
    assert counts["filtered"].get("outside_segment", 0) >= 1


def test_builder_without_location_is_kept_with_unknown_segment(disc_setup):
    conn, settings, clock = disc_setup

    class NoLocationReader(FixtureReader):
        def search_recent(self, query, max_results=20):
            posts, authors = super().search_recent(query, max_results)
            u = XUser(
                id="999001",
                username="nolocation_builder",
                name="Global Builder",
                bio="AI builder working on autonomous robotics and foundation models.",
                protected=False,
                location="",
                followers_count=1000,
                receives_your_dm=True,
            )
            p = XPost(
                id="p999",
                author_id="999001",
                text="Shipped new open weights robot control models.",
                created_at="2026-11-10T00:00:00Z",
            )
            return posts + [p], authors + [u]

        def user_posts(self, user_id, max_results=10):
            if user_id == "999001":
                return [
                    XPost(
                        id="p999",
                        author_id=user_id,
                        text="Shipped new open weights robot control models.",
                        created_at="2026-11-10T00:00:00Z",
                    )
                ]
            return super().user_posts(user_id, max_results)

    reader = NoLocationReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_id = run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)
    assert run_id > 0

    cur = conn.execute(
        """
        SELECT p.state, p.state_reason, p.score, p.fit_reasons
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'nolocation_builder'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "scored"
    fit_reasons = json.loads(row["fit_reasons"])
    assert fit_reasons["segment_status"] == "unknown"
    assert fit_reasons["segment_evidence"] == []


def test_segment_evidence_is_not_inserted_into_evidence_table(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    # In evidence table, no row has source == 'location'
    cur = conn.execute("SELECT COUNT(*) as loc_count FROM evidence WHERE source = 'location'")
    assert cur.fetchone()["loc_count"] == 0

    # Ensure evidence table still has post and bio evidence rows
    bio_post_cur = conn.execute("SELECT COUNT(*) as bp_count FROM evidence WHERE source IN ('bio', 'post')")
    assert bio_post_cur.fetchone()["bp_count"] > 0


def test_tier1_mismatch_is_filtered_before_deep_profile(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})
    llm = FakeLLM()

    run_discovery(conn, 1, reader, llm, settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.id, p.x_user_id, p.state, p.state_reason
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_ai_app_builder_us'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "filtered_out"
    assert row["state_reason"] == "outside_segment"

    # Audit log has discovered -> filtered_out with reason outside_segment and no row into scored
    audit_rows = conn.execute(
        "SELECT from_state, to_state, reason FROM audit_log WHERE entity = 'prospect' AND entity_id = ? ORDER BY id ASC",
        (row["id"],),
    ).fetchall()
    assert any(
        r["from_state"] == "discovered"
        and r["to_state"] == "filtered_out"
        and r["reason"] == "outside_segment"
        for r in audit_rows
    )
    assert not any(r["to_state"] == "scored" for r in audit_rows)

    # Ledger has no row whose detail is tier2_score:<that x_user_id>
    uid = row["x_user_id"]
    ledger_row = conn.execute(
        "SELECT 1 FROM ledger WHERE campaign_id = 1 AND detail = ?",
        (f"tier2_score:{uid}",),
    ).fetchone()
    assert ledger_row is None


def test_tier1_unverified_mismatch_is_not_filtered(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    class UnverifiedMismatchLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            res = super().call_tool(system, user, tool_name, tool_schema, max_tokens)
            if "Profiles (JSON):" in user:
                data = dict(res.data)
                results = [dict(r) for r in data.get("results", [])]
                for r in results:
                    if r.get("x_user_id") == "9100000000000000101":
                        r["segment_match"] = "mismatch"
                        r["segment_quote"] = "Berlin, Germany"
                data["results"] = results
                return LLMResult(data=data, input_tokens=res.input_tokens, output_tokens=res.output_tokens)
            return res

    run_discovery(conn, 1, reader, UnverifiedMismatchLLM(), settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.state, p.score
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_robotics_founder_sg'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "scored"


def test_tier1_failed_screen_keeps_its_reason_on_mismatch(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    class FailedScreenMismatchLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            res = super().call_tool(system, user, tool_name, tool_schema, max_tokens)
            if "Profiles (JSON):" in user:
                data = dict(res.data)
                results = [dict(r) for r in data.get("results", [])]
                for r in results:
                    if r.get("x_user_id") == "9100000000000000108":
                        r["pass"] = False
                        r["reason"] = "not ai work"
                data["results"] = results
                return LLMResult(data=data, input_tokens=res.input_tokens, output_tokens=res.output_tokens)
            return res

    run_discovery(conn, 1, reader, FailedScreenMismatchLLM(), settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.state, p.state_reason
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_ai_app_builder_us'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "filtered_out"
    assert row["state_reason"] == "bio_screen_failed"


def test_tier2_still_filters_mismatch_when_tier1_is_unknown(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    class Tier1UnknownLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            res = super().call_tool(system, user, tool_name, tool_schema, max_tokens)
            if "Profiles (JSON):" in user:
                data = dict(res.data)
                results = [dict(r) for r in data.get("results", [])]
                for r in results:
                    r["segment_match"] = "unknown"
                    r["segment_quote"] = ""
                data["results"] = results
                return LLMResult(data=data, input_tokens=res.input_tokens, output_tokens=res.output_tokens)
            return res

    run_discovery(conn, 1, reader, Tier1UnknownLLM(), settings, clock, simulated=True)

    cur = conn.execute(
        """
        SELECT p.id, p.state, p.state_reason
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = 'demo_ai_app_builder_us'
        """
    )
    row = cur.fetchone()
    assert row is not None
    assert row["state"] == "filtered_out"
    assert row["state_reason"] == "outside_segment"

    # Audit log has a scored -> filtered_out row for it
    audit_rows = conn.execute(
        "SELECT from_state, to_state, reason FROM audit_log WHERE entity = 'prospect' AND entity_id = ? ORDER BY id ASC",
        (row["id"],),
    ).fetchall()
    assert any(
        r["from_state"] == "scored"
        and r["to_state"] == "filtered_out"
        and r["reason"] == "outside_segment"
        for r in audit_rows
    )



def test_profile_without_screening_result_is_reported_not_hidden(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    target = "demo_robotics_founder_sg"

    answered_ids = set()

    class MissingResultLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            res = super().call_tool(system, user, tool_name, tool_schema, max_tokens)
            if "Profiles (JSON):" in user:
                data = dict(res.data)
                results = [r for r in data.get("results", []) if r.get("x_user_id") != "9100000000000000101"]
                data["results"] = results
                answered_ids.update(r["x_user_id"] for r in results)
                return LLMResult(data=data, input_tokens=res.input_tokens, output_tokens=res.output_tokens)
            return res

    run_id = run_discovery(conn, 1, reader, MissingResultLLM(), settings, clock, simulated=True)

    cur = conn.execute("SELECT counts FROM discovery_runs WHERE id = ?", (run_id,))
    counts = json.loads(cur.fetchone()["counts"])

    # The prospect must stay discovered (not silently skipped)
    cur_p = conn.execute(
        """
        SELECT p.state FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = 1 AND u.username = ?
        """,
        (target,),
    )
    row = cur_p.fetchone()
    assert row is not None
    assert row["state"] == "discovered", f"Expected discovered, got {row['state']}"

    # The error must be recorded
    error_usernames = [e["username"] for e in counts.get("errors", [])]
    assert target in error_usernames, f"{target} not in errors: {counts.get('errors')}"

    no_result_errors = [e for e in counts.get("errors", []) if e.get("error") == "no screening result" and e["username"] == target]
    assert len(no_result_errors) >= 1

    assert counts.get("llm_errors", 0) >= 1

    # screened counts only the profiles that got a screening result
    assert counts["screened"] == len(answered_ids)
    assert "9100000000000000101" not in answered_ids


def test_tier1_calls_hold_at_most_eight_profiles(disc_setup):
    conn, settings, clock = disc_setup
    reader = FixtureReader(settings.fixtures_path, clock, conn=conn, cost_context={"campaign_id": 1})

    tier1_call_sizes = []

    class RecordingLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if "Profiles (JSON):" in user:
                _, json_part = user.split("Profiles (JSON):\n", 1)
                profiles = json.loads(json_part)
                tier1_call_sizes.append(len(profiles))
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    run_discovery(conn, 1, reader, RecordingLLM(), settings, clock, simulated=True)

    assert len(tier1_call_sizes) > 1, f"Expected more than 1 tier-1 call, got {tier1_call_sizes}"
    assert all(s <= 8 for s in tier1_call_sizes), f"A tier-1 call exceeded 8 profiles: {tier1_call_sizes}"
