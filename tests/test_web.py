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
