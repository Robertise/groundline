from datetime import datetime, timezone, timedelta
import httpx
import pytest
from cryptography.fernet import Fernet

from app.clock import FrozenClock, iso
from app.config import Settings
from app.crypto import TokenCipher
from app.db import connect, init_db
from app.x.oauth import start, finish, OAuthError, SCOPES


@pytest.fixture
def test_setup(tmp_path):
    db_file = tmp_path / "oauth_test.db"
    conn = connect(str(db_file))
    init_db(conn)

    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    settings = Settings(
        x_client_id="test_client_id",
        x_client_secret="test_client_secret",
        x_redirect_uri="http://127.0.0.1:8000/callback",
        secret_key=key,
    )
    clock = FrozenClock(datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc))
    return conn, settings, cipher, clock


def test_connect_url_contains_pkce_and_state(test_setup):
    conn, settings, cipher, clock = test_setup
    now_str = iso(clock.now())

    url = start(conn, settings, now_str)
    assert url.startswith("https://x.com/i/oauth2/authorize?")
    assert "response_type=code" in url
    assert "client_id=test_client_id" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "state=" in url

    cur = conn.execute("SELECT state, code_verifier, created_at FROM oauth_states")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["created_at"] == now_str


def test_callback_rejects_unknown_state(test_setup):
    conn, settings, cipher, clock = test_setup
    with pytest.raises(OAuthError):
        finish(conn, settings, cipher, clock, code="auth_code_1", state="non_existent_state")


def test_callback_rejects_missing_dm_write_scope(test_setup):
    conn, settings, cipher, clock = test_setup
    now_str = iso(clock.now())
    start(conn, settings, now_str)

    cur = conn.execute("SELECT state FROM oauth_states")
    state = cur.fetchone()["state"]

    def handler(request: httpx.Request):
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "acc_1",
                    "refresh_token": "ref_1",
                    "scope": "tweet.read users.read",  # missing dm.write
                },
            )
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(OAuthError) as exc:
        finish(conn, settings, cipher, clock, code="valid_code", state=state, http=http)

    assert "Direct message" in str(exc.value)


def test_callback_stores_encrypted_tokens_and_sender(test_setup):
    conn, settings, cipher, clock = test_setup
    now_str = iso(clock.now())
    start(conn, settings, now_str)

    cur = conn.execute("SELECT state FROM oauth_states")
    state = cur.fetchone()["state"]

    def handler(request: httpx.Request):
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "acc_valid_123",
                    "refresh_token": "ref_valid_456",
                    "scope": SCOPES,
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/2/users/me":
            return httpx.Response(
                200,
                json={"data": {"id": "888888", "username": "verified_user"}},
            )
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sender_id = finish(
        conn, settings, cipher, clock, code="valid_code", state=state, http=http
    )
    assert sender_id > 0

    cur = conn.execute("SELECT * FROM sender_accounts WHERE id = ?", (sender_id,))
    row = cur.fetchone()
    assert row["x_user_id"] == "888888"
    assert row["username"] == "verified_user"

    decrypted = cipher.decrypt(row["token_ciphertext"])
    assert decrypted["access_token"] == "acc_valid_123"
    assert decrypted["refresh_token"] == "ref_valid_456"

    # State must be consumed and deleted
    cur = conn.execute("SELECT COUNT(*) FROM oauth_states")
    assert cur.fetchone()[0] == 0
