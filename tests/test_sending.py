from cryptography.fernet import Fernet
import pytest

from app.clock import FrozenClock
from app.config import Settings
from app.crypto import TokenCipher
from app.deps import build_transport
from app.sending import run_send_batch
from app.x.dm import XDMTransport
from app.x.mock import MockTransport
from tests.conftest import seed, approve_all


def test_mock_mode_builds_mock_transport(conn, clock):
    settings = Settings(mode="mock")
    transport = build_transport(conn, settings, clock)
    assert isinstance(transport, MockTransport)


def test_dry_run_mode_refuses_to_build_a_transport(conn, clock):
    settings = Settings(mode="dry_run")
    with pytest.raises(RuntimeError, match="dry_run mode never sends"):
        build_transport(conn, settings, clock)


def test_live_mode_builds_dm_transport_with_allowlist_ids(conn, clock):
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    tokens = {"access_token": "acc", "refresh_token": "ref"}
    encrypted = cipher.encrypt(tokens)
    conn.execute(
        "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, token_ciphertext, created_at) "
        "VALUES (1, '90001', 'test_sender', 20, ?, '2026-11-13T00:00:00Z')",
        (encrypted,),
    )
    conn.execute(
        "INSERT INTO x_users (x_user_id, username, name, fetched_at) VALUES ('u_alice', 'Alice', 'Alice', '2026-11-13T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO x_users (x_user_id, username, name, fetched_at) VALUES ('u_bob', 'bob', 'Bob', '2026-11-13T00:00:00Z')"
    )
    settings = Settings(
        mode="live",
        secret_key=key,
        x_client_id="cid",
        x_client_secret="csec",
        live_allowed_handles="@alice, BOB, nobody",
    )
    transport = build_transport(conn, settings, clock)
    assert isinstance(transport, XDMTransport)
    assert transport.allowed_user_ids == {"u_alice", "u_bob"}


def test_live_mode_without_allowlist_has_none(conn, clock):
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    tokens = {"access_token": "acc", "refresh_token": "ref"}
    encrypted = cipher.encrypt(tokens)
    conn.execute(
        "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, token_ciphertext, created_at) "
        "VALUES (1, '90001', 'test_sender', 20, ?, '2026-11-13T00:00:00Z')",
        (encrypted,),
    )
    settings = Settings(
        mode="live",
        secret_key=key,
        x_client_id="cid",
        x_client_secret="csec",
        live_allowed_handles="",
    )
    transport = build_transport(conn, settings, clock)
    assert isinstance(transport, XDMTransport)
    assert transport.allowed_user_ids is None


def test_run_send_batch_in_mock_mode_sends_approved_rows(conn, db_path, clock):
    seeded = seed(conn, clock)
    approve_all(conn, clock, seeded)

    settings = Settings(mode="mock", db_path=db_path)
    result = run_send_batch(settings, seeded.sender_account_id)
    assert result is not None
    assert result.sent == 5

    cur = conn.execute(
        "SELECT state FROM outreach WHERE sender_account_id = ?",
        (seeded.sender_account_id,),
    )
    states = [r["state"] for r in cur.fetchall()]
    assert len(states) == 5
    assert all(s == "sent" for s in states)


def test_run_send_batch_in_dry_run_returns_none_and_sends_nothing(conn, db_path, clock):
    seeded = seed(conn, clock)
    approve_all(conn, clock, seeded)

    settings = Settings(mode="dry_run", db_path=db_path)
    result = run_send_batch(settings, seeded.sender_account_id)
    assert result is None

    cur = conn.execute(
        "SELECT state FROM outreach WHERE sender_account_id = ?",
        (seeded.sender_account_id,),
    )
    states = [r["state"] for r in cur.fetchall()]
    assert len(states) == 5
    assert all(s == "approved" for s in states)
