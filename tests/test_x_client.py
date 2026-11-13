import json
import pytest
import httpx
from cryptography.fernet import Fernet

from app.crypto import TokenCipher
from app.config import Settings
from app.clock import FrozenClock, iso
from app.db import connect, init_db
from app.x.client import XClient, POST_FIELDS_PARAM
from app.x.base import XRateLimited, XAuthError
from datetime import datetime, timezone


@pytest.fixture
def test_setup(tmp_path):
    db_file = tmp_path / "x_client.db"
    conn = connect(str(db_file))
    init_db(conn)

    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    tokens = {
        "access_token": "acc_tok_1",
        "refresh_token": "ref_tok_1",
        "obtained_at": "2026-11-13T00:00:00Z",
    }
    encrypted = cipher.encrypt(tokens)
    conn.execute(
        "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, token_ciphertext, created_at) "
        "VALUES (1, '90001', 'test_sender', 20, ?, '2026-11-13T00:00:00Z')",
        (encrypted,),
    )
    conn.execute(
        "INSERT INTO campaigns (id, sender_account_id, name, created_at) VALUES (10, 1, 'Camp 10', '2026-11-13T00:00:00Z')"
    )
    settings = Settings(
        x_client_id="x_cid",
        x_client_secret="x_csec",
        secret_key=key,
    )
    clock = FrozenClock(datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc))
    return conn, cipher, settings, clock


def test_users_by_usernames_parses_fields_and_records_cost(test_setup):
    conn, cipher, settings, clock = test_setup

    def handler(request: httpx.Request):
        assert request.url.path == "/2/users/by"
        assert "usernames=alice%2Cbob" in str(request.url) or "usernames=alice,bob" in str(request.url)
        assert request.headers["authorization"] == "Bearer acc_tok_1"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "101",
                        "username": "alice",
                        "name": "Alice Smith",
                        "description": "Robotics engineer",
                        "protected": False,
                        "location": "Singapore",
                        "receives_your_dm": True,
                        "connection_status": ["followed_by"],
                        "public_metrics": {"followers_count": 500},
                    },
                    {
                        "id": "102",
                        "username": "bob",
                        "name": "Bob",
                        "description": "AI researcher",
                        "protected": True,
                        "location": "Hanoi",
                        "receives_your_dm": False,
                        "connection_status": [],
                        "public_metrics": {"followers_count": 250},
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http, cost_context={"campaign_id": 10})

    users = client.users_by_usernames(["@alice", " bob "])
    assert len(users) == 2
    assert users[0].id == "101"
    assert users[0].username == "alice"
    assert users[0].bio == "Robotics engineer"
    assert users[0].protected is False
    assert users[0].location == "Singapore"
    assert users[0].receives_your_dm is True
    assert users[0].connection_status == ("followed_by",)
    assert users[0].followers_count == 500

    assert users[1].id == "102"
    assert users[1].protected is True
    assert users[1].receives_your_dm is False

    cur = conn.execute("SELECT endpoint, resource_count, cost_usd FROM ledger WHERE campaign_id = 10")
    row = cur.fetchone()
    assert row["endpoint"] == "users_lookup"
    assert row["resource_count"] == 2
    assert row["cost_usd"] == 0.020


def test_search_recent_paginates_until_max_and_returns_authors(test_setup):
    conn, cipher, settings, clock = test_setup
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        assert request.url.path == "/2/tweets/search/recent"
        if calls == 1:
            assert "next_token" not in str(request.url)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "p1", "author_id": "u1", "text": "Post 1", "created_at": "2026-11-12T10:00:00.000Z"},
                        {"id": "p2", "author_id": "u2", "text": "Post 2", "created_at": "2026-11-12T11:00:00.000Z"},
                    ],
                    "includes": {
                        "users": [
                            {"id": "u1", "username": "user1", "description": "Bio 1"},
                            {"id": "u2", "username": "user2", "description": "Bio 2"},
                        ]
                    },
                    "meta": {"next_token": "token_page_2"},
                },
            )
        else:
            assert "next_token=token_page_2" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "p3", "author_id": "u1", "text": "Post 3", "created_at": "2026-11-12T12:00:00.000Z"},
                    ],
                    "includes": {
                        "users": [
                            {"id": "u1", "username": "user1", "description": "Bio 1"},
                        ]
                    },
                    "meta": {},
                },
            )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http)

    posts, authors = client.search_recent("robotics", max_posts=3)
    assert len(posts) == 3
    assert posts[0].id == "p1"
    assert posts[2].id == "p3"
    # De-duplicated authors
    assert len(authors) == 2
    author_ids = {a.id for a in authors}
    assert author_ids == {"u1", "u2"}


def test_following_respects_max_users(test_setup):
    conn, cipher, settings, clock = test_setup

    def handler(request: httpx.Request):
        assert "/following" in request.url.path
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": f"f_{i}", "username": f"user_{i}", "description": f"Bio {i}"}
                    for i in range(5)
                ],
                "meta": {"pagination_token": "next_f"},
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http)

    users = client.following("90001", max_users=3)
    assert len(users) == 3


def test_user_posts_sends_exclude_and_post_fields_params(test_setup):
    conn, cipher, settings, clock = test_setup

    def handler(request: httpx.Request):
        assert "/tweets" in request.url.path
        assert "exclude=replies%2Cretweets" in str(request.url) or "exclude=replies,retweets" in str(request.url)
        assert f"{POST_FIELDS_PARAM}=" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "post_1", "author_id": "target_u", "text": "Excited about robots!", "created_at": "2026-11-13T01:00:00.000Z"}
                ],
                "meta": {},
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http)

    posts = client.user_posts("target_u", max_posts=5)
    assert len(posts) == 1
    assert posts[0].id == "post_1"
    assert posts[0].text == "Excited about robots!"


def test_401_refreshes_token_once_and_retries(test_setup):
    conn, cipher, settings, clock = test_setup
    attempts = 0

    def handler(request: httpx.Request):
        nonlocal attempts
        if request.url.path == "/2/oauth2/token":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200,
                json={"access_token": "new_acc_tok", "refresh_token": "new_ref_tok"},
            )
        if request.url.path == "/2/users/me":
            attempts += 1
            if attempts == 1:
                assert request.headers["authorization"] == "Bearer acc_tok_1"
                return httpx.Response(401, json={"type": "unauthorized"})
            else:
                assert request.headers["authorization"] == "Bearer new_acc_tok"
                return httpx.Response(
                    200,
                    json={"data": {"id": "90001", "username": "test_sender"}},
                )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http)

    user = client.me()
    assert user.id == "90001"
    assert attempts == 2

    # Check updated token stored in database
    cur = conn.execute("SELECT token_ciphertext FROM sender_accounts WHERE id = 1")
    updated_data = cipher.decrypt(cur.fetchone()[0])
    assert updated_data["access_token"] == "new_acc_tok"
    assert updated_data["refresh_token"] == "new_ref_tok"


def test_429_raises_rate_limited_with_reset(test_setup):
    conn, cipher, settings, clock = test_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            429,
            headers={"x-rate-limit-reset": "1794567890"},
            json={"type": "rate_limited", "detail": "Too many requests"},
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http)

    with pytest.raises(XRateLimited) as exc:
        client.me()

    assert exc.value.status == 429
    assert exc.value.reset_at is not None


def test_refresh_failure_raises_auth_error(test_setup):
    conn, cipher, settings, clock = test_setup

    def handler(request: httpx.Request):
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(401, json={"type": "unauthorized"})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = XClient(conn, 1, cipher, settings, clock, http=http)

    with pytest.raises(XAuthError):
        client.me()
