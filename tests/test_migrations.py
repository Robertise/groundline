import sqlite3
from app.db import connect, init_db, SCHEMA_SQL


def test_fresh_db_reaches_version_2(tmp_path):
    db_file = tmp_path / "fresh.db"
    conn = connect(str(db_file))
    init_db(conn)

    cur = conn.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == 2

    # Check that new tables and columns exist
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    assert "discovery_runs" in tables
    assert "oauth_states" in tables

    # Check new columns in campaigns, x_users, prospects, ledger
    cur = conn.execute("PRAGMA table_info(campaigns)")
    cols = {r["name"] for r in cur.fetchall()}
    assert "segment" in cols
    assert "handles" in cols
    assert "search_keywords" in cols

    cur = conn.execute("PRAGMA table_info(x_users)")
    cols = {r["name"] for r in cur.fetchall()}
    assert "protected" in cols
    assert "location" in cols
    assert "followers_count" in cols
    assert "posts_fetched_at" in cols

    cur = conn.execute("PRAGMA table_info(prospects)")
    cols = {r["name"] for r in cur.fetchall()}
    assert "sources" in cols
    assert "segment" in cols
    assert "role" in cols
    assert "caution" in cols

    cur = conn.execute("PRAGMA table_info(ledger)")
    cols = {r["name"] for r in cur.fetchall()}
    assert "run_id" in cols
    assert "detail" in cols
    assert "simulated" in cols
    conn.close()


def test_version_1_db_migrates_to_2_without_data_loss(tmp_path):
    db_file = tmp_path / "v1.db"
    conn = connect(str(db_file))

    # Manually initialize v1 database
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA user_version = 1")

    # Insert test data
    conn.execute(
        "INSERT INTO sender_accounts (x_user_id, username, daily_send_cap, created_at) VALUES ('123', 'sender1', 20, '2026-11-13T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO campaigns (sender_account_id, name, brief, budget_usd, created_at) VALUES (1, 'Camp 1', 'Brief 1', 5.0, '2026-11-13T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO x_users (x_user_id, username, name, fetched_at) VALUES ('u1', 'user1', 'User 1', '2026-11-13T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO prospects (campaign_id, x_user_id, state, created_at, updated_at) VALUES (1, 'u1', 'discovered', '2026-11-13T00:00:00Z', '2026-11-13T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ledger (campaign_id, endpoint, resource_count, unit_cost_usd, cost_usd, created_at) VALUES (1, 'users_lookup', 1, 0.010, 0.010, '2026-11-13T00:00:00Z')"
    )

    # Run init_db to migrate from v1 to v2
    init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

    # Verify data integrity
    camp = conn.execute("SELECT * FROM campaigns WHERE id = 1").fetchone()
    assert camp["name"] == "Camp 1"
    assert camp["segment"] == ""
    assert camp["search_max_posts"] == 100

    prospect = conn.execute("SELECT * FROM prospects WHERE id = 1").fetchone()
    assert prospect["state"] == "discovered"
    assert prospect["sources"] == "[]"

    ledg = conn.execute("SELECT * FROM ledger WHERE id = 1").fetchone()
    assert ledg["endpoint"] == "users_lookup"
    assert ledg["simulated"] == 0

    conn.close()
