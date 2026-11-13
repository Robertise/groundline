from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.clock import FrozenClock, iso
from app.db import connect, init_db
from app import db
from app.state import transition
from app.main import app
from tests.conftest import seed, approve_all


def test_health_endpoint_reports_mode(tmp_path, monkeypatch):
    db_file = tmp_path / "health.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "mock"
        assert data["recovered_on_startup"] == 0


def test_startup_recovers_rows_left_in_sending(tmp_path, monkeypatch):
    db_file = tmp_path / "recovery.db"
    monkeypatch.setenv("GROUNDLINE_DB_PATH", str(db_file))

    c = connect(str(db_file))
    init_db(c)
    dt = datetime(2026, 11, 13, 3, 0, 0, tzinfo=timezone.utc)
    clk = FrozenClock(dt)
    seeded = seed(c, clk, n_recipients=1)
    approve_all(c, clk, seeded)
    now_str = iso(clk.now())
    with db.transaction(c):
        transition(
            c,
            "outreach",
            1,
            "approved",
            "sending",
            "system",
            now_str,
            attempt_started_at=now_str,
        )
    c.close()

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["recovered_on_startup"] == 1
