from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.clock import FrozenClock, iso
from app.db import connect
from app import db
from app.ledger import record
from app.main import app


def test_create_campaign_requires_a_source(tmp_path, monkeypatch):
    db_file = tmp_path / "web_campaign_source.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    with TestClient(app) as client:
        # Submit without any discovery source
        resp = client.post(
            "/campaigns",
            data={
                "name": "Campaign Without Sources",
                "segment": "AI builders",
                "brief": "Demo brief",
                "handles": "",
                "search_keywords": "",
                "seed_handles": "",
                # include_sender_followers omitted
                "search_max_posts": "100",
                "seed_following_max": "100",
                "followers_max": "200",
                "max_deep_profiles": "40",
                "min_score": "60",
                "budget_usd": "5.0",
            },
        )
        assert resp.status_code == 400
        assert "At least one discovery source must be provided" in resp.text

        conn = connect(str(db_file))
        c_cnt = conn.execute("SELECT count(*) FROM campaigns").fetchone()[0]
        conn.close()
        assert c_cnt == 0


def test_run_discovery_in_mock_mode_shows_scored_prospects(tmp_path, monkeypatch):
    db_file = tmp_path / "web_mock_discovery.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    with TestClient(app) as client:
        # Create campaign with search keywords and seed handles
        create_resp = client.post(
            "/campaigns",
            data={
                "name": "AI Founders Camp",
                "segment": "AI and robotics",
                "brief": "Meeting founders",
                "search_keywords": "robot, LLM, agent",
                "seed_handles": "demo_seed_investor",
                "include_sender_followers": "1",
                "search_max_posts": "50",
                "seed_following_max": "50",
                "followers_max": "50",
                "max_deep_profiles": "40",
                "min_score": "60",
                "budget_usd": "10.0",
            },
            follow_redirects=True,
        )
        assert create_resp.status_code == 200
        assert "AI Founders Camp" in create_resp.text

        # Run discovery
        run_resp = client.post("/campaigns/1/run", follow_redirects=True)
        assert run_resp.status_code == 200
        assert "Discovery completed" in run_resp.text

        # Detail page should show scored prospects
        assert (
            "@demo_robotics_founder_sg" in run_resp.text
            or "@demo_llm_researcher_hn" in run_resp.text
            or "@demo_devtools_founder_jkt" in run_resp.text
        )
        assert "Scored (" in run_resp.text


def test_second_run_while_running_is_refused(tmp_path, monkeypatch):
    db_file = tmp_path / "web_concurrent_run.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    with TestClient(app) as client:
        # Create campaign
        client.post(
            "/campaigns",
            data={
                "name": "Busy Campaign",
                "segment": "Robotics",
                "search_keywords": "robot",
            },
            follow_redirects=True,
        )

        # Manually insert a running discovery run started right now
        conn = connect(str(db_file))
        now_str = iso(datetime.now(timezone.utc))
        with db.transaction(conn):
            conn.execute(
                """
                INSERT INTO discovery_runs (campaign_id, status, started_at, counts)
                VALUES (1, 'running', ?, '{}')
                """,
                (now_str,),
            )
        conn.close()

        # Try to run discovery again
        run_resp = client.post("/campaigns/1/run", follow_redirects=True)
        assert run_resp.status_code == 200
        assert "already in progress" in run_resp.text.lower()

        # Check that no second run was inserted
        conn = connect(str(db_file))
        runs = conn.execute("SELECT count(*) FROM discovery_runs WHERE campaign_id = 1").fetchone()[0]
        conn.close()
        assert runs == 1


def test_costs_page_separates_simulated_costs(tmp_path, monkeypatch):
    db_file = tmp_path / "web_costs.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    # Seed DB with real and simulated costs
    with TestClient(app) as client:
        # Create campaign
        client.post(
            "/campaigns",
            data={
                "name": "Cost Test Campaign",
                "segment": "AI",
                "search_keywords": "AI",
            },
            follow_redirects=True,
        )

    conn = connect(str(db_file))
    now_str = "2026-11-13T12:00:00Z"
    # Insert 1 real ledger row and 1 simulated ledger row
    record(conn, "users_lookup", 10, now_str, campaign_id=1, simulated=False, unit_cost=0.01)
    record(conn, "search_posts", 20, now_str, campaign_id=1, simulated=True, unit_cost=0.005)
    conn.close()

    with TestClient(app) as client:
        resp = client.get("/costs")
        assert resp.status_code == 200
        assert (
            "Estimates from published per-unit prices. The X Developer Console is the source of truth."
            in resp.text
        )
        assert "Real Usage &amp; Costs" in resp.text or "Real Usage & Costs" in resp.text
        assert "Simulated Usage &amp; Costs" in resp.text or "Simulated Usage & Costs" in resp.text
        assert "$0.1000" in resp.text  # Real cost (10 * 0.01)
        assert "$0.1000" in resp.text  # Simulated cost (20 * 0.005)


def test_mode_badge_on_every_page(tmp_path, monkeypatch):
    db_file = tmp_path / "web_badges.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))

    # Mock mode
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")
    with TestClient(app) as client:
        for path in ["/campaigns", "/campaigns/new", "/costs"]:
            resp = client.get(path)
            assert resp.status_code == 200
            assert "MOCK — demo data, no external calls" in resp.text

    # Dry-run mode
    monkeypatch.setenv("GROUNDLINE_MODE", "dry_run")
    with TestClient(app) as client:
        for path in ["/campaigns", "/costs"]:
            resp = client.get(path)
            assert resp.status_code == 200
            assert "DRY-RUN — reads real data, never sends" in resp.text

    # Live mode
    monkeypatch.setenv("GROUNDLINE_MODE", "live")
    with TestClient(app) as client:
        for path in ["/campaigns", "/costs"]:
            resp = client.get(path)
            assert resp.status_code == 200
            assert "LIVE" in resp.text


def test_connect_is_refused_in_mock_mode(tmp_path, monkeypatch):
    db_file = tmp_path / "web_connect_mock.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    with TestClient(app) as client:
        resp = client.get("/connect")
        assert resp.status_code == 400
        assert "Connect X is disabled in Mock mode" in resp.text


def test_run_via_page_records_campaign_spend(tmp_path, monkeypatch):
    db_file = tmp_path / "web_spend.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    with TestClient(app) as client:
        create_resp = client.post(
            "/campaigns",
            data={
                "name": "AI Founders Spend Camp",
                "segment": "AI and robotics",
                "brief": "Meeting founders",
                "search_keywords": "robot, LLM, agent",
                "seed_handles": "demo_seed_investor",
                "include_sender_followers": "1",
                "search_max_posts": "50",
                "seed_following_max": "50",
                "followers_max": "50",
                "max_deep_profiles": "40",
                "min_score": "60",
                "budget_usd": "10.0",
            },
            follow_redirects=True,
        )
        assert create_resp.status_code == 200

        run_resp = client.post("/campaigns/1/run", follow_redirects=True)
        assert run_resp.status_code == 200

        conn = connect(str(db_file))
        cur = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE campaign_id = 1"
        )
        total_spent = cur.fetchone()[0]
        conn.close()

        assert total_spent > 0
        spent_formatted = f"${total_spent:.4f}"
        assert f"Spent so far:</strong> {spent_formatted}" in run_resp.text


def test_model_reason_is_shown_and_escaped(tmp_path, monkeypatch):
    from app.llm.fake import FakeLLM

    db_file = tmp_path / "web_escape.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    class StubLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            res = super().call_tool(system, user, tool_name, tool_schema, max_tokens)
            if "Profiles (JSON):" in user and "results" in res.data:
                for r in res.data["results"]:
                    r["pass"] = False
                    r["reason"] = "<b>x</b>"
                    r["quote"] = ""
            return res

    monkeypatch.setattr("app.routes.build_llm", lambda s: StubLLM())

    with TestClient(app) as client:
        create_resp = client.post(
            "/campaigns",
            data={
                "name": "Campaign Escape Test",
                "segment": "AI and robotics",
                "brief": "Meeting founders",
                "search_keywords": "robot, LLM, agent",
                "seed_handles": "",
                "search_max_posts": "10",
                "seed_following_max": "50",
                "followers_max": "50",
                "max_deep_profiles": "10",
                "min_score": "60",
                "budget_usd": "10.0",
            },
            follow_redirects=True,
        )
        assert create_resp.status_code == 200

        run_resp = client.post("/campaigns/1/run", follow_redirects=True)
        assert run_resp.status_code == 200

        filtered_page = client.get("/campaigns/1?view=filtered")
        assert filtered_page.status_code == 200
        assert "&lt;b&gt;x&lt;/b&gt;" in filtered_page.text
        assert "<b>x</b>" not in filtered_page.text


def test_evidence_is_deduplicated_in_prospect_table(tmp_path, monkeypatch):
    import json
    from app.db import init_db

    db_file = tmp_path / "web_dedup.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    conn = connect(str(db_file))
    init_db(conn)
    now_str = "2026-11-14T10:00:00+00:00"

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, created_at) "
            "VALUES (1, 's1', 'sender_me', 20, ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO campaigns (id, sender_account_id, name, segment, budget_usd, created_at) "
            "VALUES (1, 1, 'Dedup Camp', 'AI', 10.0, ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO x_users (x_user_id, username, name, bio, fetched_at) "
            "VALUES ('u1', 'alice', 'Alice', 'Bio text', ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO prospects (id, campaign_id, x_user_id, state, score, fit_reasons, created_at, updated_at) "
            "VALUES (1, 1, 'u1', 'scored', 85, ?, ?, ?)",
            (json.dumps({"why_fit": ["Fit quote"]}), now_str, now_str),
        )
        quotes = [
            "Building AI models for robotics",
            "building ai models for robotics",
            "BUILDING   AI MODELS FOR ROBOTICS",
            "Unique quote two here",
            "Unique quote three here",
            "Unique quote four here",
            "Unique quote five should be excluded by max 4",
        ]
        for q in quotes:
            conn.execute(
                "INSERT INTO evidence (prospect_id, source, quote, url) VALUES (1, 'bio', ?, 'https://x.com')",
                (q,),
            )
    conn.close()

    with TestClient(app) as client:
        resp = client.get("/campaigns/1?view=scored")
        assert resp.status_code == 200
        assert "Building AI models for robotics" in resp.text
        assert "building ai models for robotics" not in resp.text
        assert "BUILDING   AI MODELS FOR ROBOTICS" not in resp.text
        assert "Unique quote two here" in resp.text
        assert "Unique quote three here" in resp.text
        assert "Unique quote four here" in resp.text
        assert "Unique quote five should be excluded" not in resp.text


def test_run_errors_are_listed_and_escaped(tmp_path, monkeypatch):
    import json
    from app.db import init_db

    db_file = tmp_path / "web_run_errors.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    conn = connect(str(db_file))
    init_db(conn)
    now_str = "2026-11-14T10:00:00+00:00"

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, created_at) "
            "VALUES (1, 's1', 'sender_me', 20, ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO campaigns (id, sender_account_id, name, segment, budget_usd, created_at) "
            "VALUES (1, 1, 'Error Run Camp', 'AI', 10.0, ?)",
            (now_str,),
        )
        counts_data = {
            "found": 5,
            "scored": 2,
            "screened": 3,
            "left_discovered": 1,
            "errors": [
                {"username": "alice<script>", "error": "malformed <output> error"},
            ],
        }
        conn.execute(
            "INSERT INTO discovery_runs (id, campaign_id, status, started_at, finished_at, counts) "
            "VALUES (1, 1, 'completed', ?, ?, ?)",
            (now_str, now_str, json.dumps(counts_data)),
        )
    conn.close()

    with TestClient(app) as client:
        resp = client.get("/campaigns/1")
        assert resp.status_code == 200
        assert "@alice&lt;script&gt;: malformed &lt;output&gt; error" in resp.text
        assert "<script>" not in resp.text
        assert "<output>" not in resp.text


def test_segment_status_and_outside_segment_reason_are_shown(tmp_path, monkeypatch):
    import json
    from app.db import init_db

    db_file = tmp_path / "web_segment.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))
    monkeypatch.setenv("GROUNDLINE_MODE", "mock")

    conn = connect(str(db_file))
    init_db(conn)
    now_str = "2026-11-14T10:00:00+00:00"

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, created_at) "
            "VALUES (1, 's1', 'sender_me', 20, ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO campaigns (id, sender_account_id, name, segment, budget_usd, created_at) "
            "VALUES (1, 1, 'Segment Camp', 'AI in SEA', 10.0, ?)",
            (now_str,),
        )
        # User 1: scored with match
        conn.execute(
            "INSERT INTO x_users (x_user_id, username, name, bio, fetched_at) "
            "VALUES ('u1', 'sg_person', 'SG Person', 'Bio text', ?)",
            (now_str,),
        )
        fit_reasons_1 = {
            "why_fit": ["Matches SEA"],
            "segment_status": "match",
            "segment_evidence": [{"source": "location", "quote": "Singapore"}],
        }
        conn.execute(
            "INSERT INTO prospects (id, campaign_id, x_user_id, state, score, role, segment, fit_reasons, created_at, updated_at) "
            "VALUES (1, 1, 'u1', 'scored', 85, 'builder', 'robotics', ?, ?, ?)",
            (json.dumps(fit_reasons_1), now_str, now_str),
        )

        # User 2: filtered with outside_segment and mismatch with special chars to test escaping
        conn.execute(
            "INSERT INTO x_users (x_user_id, username, name, bio, fetched_at) "
            "VALUES ('u2', 'us_person', 'US Person', 'Bio text', ?)",
            (now_str,),
        )
        fit_reasons_2 = {
            "why_fit": ["Matches domain but outside SEA"],
            "segment_status": "mismatch",
            "segment_evidence": [{"source": "location", "quote": "<script>alert('xss')</script> San Francisco, CA"}],
        }
        conn.execute(
            "INSERT INTO prospects (id, campaign_id, x_user_id, state, state_reason, score, role, segment, fit_reasons, created_at, updated_at) "
            "VALUES (2, 1, 'u2', 'filtered_out', 'outside_segment', 85, 'builder', 'applications', ?, ?, ?)",
            (json.dumps(fit_reasons_2), now_str, now_str),
        )
        conn.execute(
            "INSERT INTO audit_log (entity, entity_id, from_state, to_state, actor, reason, created_at) "
            "VALUES ('prospect', 2, 'scored', 'filtered_out', 'system', 'outside_segment', ?)",
            (now_str,),
        )
    conn.close()

    with TestClient(app) as client:
        # Check scored view shows Segment: match (Singapore)
        scored_resp = client.get("/campaigns/1?view=scored")
        assert scored_resp.status_code == 200
        assert "Segment: match (Singapore)" in scored_resp.text

        # Check filtered view shows Outside the target segment and escaped quote
        filtered_resp = client.get("/campaigns/1?view=filtered")
        assert filtered_resp.status_code == 200
        assert "Outside the target segment" in filtered_resp.text
        assert "Segment: mismatch" in filtered_resp.text
        assert "<script>alert('xss')</script>" not in filtered_resp.text
        assert "&lt;script&gt;alert(" in filtered_resp.text

