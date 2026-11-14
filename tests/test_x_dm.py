import json
from datetime import datetime, timezone
from cryptography.fernet import Fernet
import httpx
import pytest

from app.clock import FrozenClock
from app.config import Settings
from app.crypto import TokenCipher
from app.db import connect, init_db
from app.x.base import (
    REASON_RATE_LIMITED,
    REASON_RECIPIENT_NOT_ACCEPTING,
    REASON_SENDER_NOT_VERIFIED,
)
from app.x.client import XClient
from app.x.dm import (
    XDMTransport,
    REASON_AUTH_ERROR,
    REASON_MISSING_EVENT_ID,
    REASON_NOT_ON_ALLOWLIST,
)


@pytest.fixture
def dm_setup(tmp_path):
    db_file = tmp_path / "x_dm.db"
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


def test_send_dm_201_returns_sent_with_ids(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            201,
            json={"data": {"dm_conversation_id": "111-222", "dm_event_id": "333"}},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "sent"
    assert outcome.dm_event_id == "333"
    assert outcome.dm_conversation_id == "111-222"


def test_send_dm_posts_text_to_dm_conversations_endpoint(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        assert request.method == "POST"
        assert request.url.path == "/2/dm_conversations/with/222/messages"
        body = json.loads(request.content.decode("utf-8"))
        assert body == {"text": "Hello world"}
        assert request.headers["authorization"] == "Bearer acc_tok_1"
        return httpx.Response(
            201,
            json={"data": {"dm_conversation_id": "111-222", "dm_event_id": "333"}},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello world")
    assert outcome.kind == "sent"


def test_send_dm_403_recipient_not_accepting_is_blocked(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            403,
            json={
                "detail": "You do not have permission to DM one or more participants.",
                "status": 403,
                "title": "Forbidden",
                "type": "https://api.x.com/2/problems/dm-recipient-not-accepting",
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "blocked"
    assert outcome.reason == REASON_RECIPIENT_NOT_ACCEPTING


def test_send_dm_403_sender_not_verified_is_blocked(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            403,
            json={
                "detail": "You do not have permission to DM one or more participants.",
                "status": 403,
                "title": "Forbidden",
                "type": "https://api.x.com/2/problems/dm-sender-not-verified",
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "blocked"
    assert outcome.reason == REASON_SENDER_NOT_VERIFIED


def test_send_dm_other_403_is_failed_http_403(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            403,
            json={
                "detail": "Action not permitted.",
                "status": 403,
                "title": "Forbidden",
                "type": "about:blank",
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "failed"
    assert outcome.reason == "http_403"


def test_send_dm_429_is_rate_limited(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            429,
            headers={"x-rate-limit-reset": "1700000000"},
            json={"detail": "Too many requests"},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "rate_limited"
    assert outcome.reason == REASON_RATE_LIMITED


def test_send_dm_500_is_unknown(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            500,
            json={"detail": "Internal server error", "type": "server_error"},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "unknown"
    assert outcome.reason == "http_5xx"


def test_send_dm_400_is_failed(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            400,
            json={"detail": "Bad request", "type": "bad_request"},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "failed"
    assert outcome.reason == "http_400"


def test_send_dm_timeout_propagates(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        raise httpx.ReadTimeout("timed out")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    with pytest.raises(httpx.HTTPError):
        transport.send_dm(1, "222", "Hello")


def test_send_dm_201_without_event_id_is_unknown(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            201,
            json={"data": {"dm_conversation_id": "111-222"}},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    outcome = transport.send_dm(1, "222", "Hello")
    assert outcome.kind == "unknown"
    assert outcome.reason == REASON_MISSING_EVENT_ID


def test_send_dm_records_one_dm_send_ledger_row(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(
            201,
            json={"data": {"dm_conversation_id": "111-222", "dm_event_id": "333"}},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    transport.send_dm(1, "222", "Hello")
    cur = conn.execute(
        "SELECT endpoint, resource_count, cost_usd FROM ledger WHERE endpoint = 'dm_send'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "dm_send"
    assert rows[0]["resource_count"] == 1
    assert rows[0]["cost_usd"] == 0.010


def test_check_recipient_true_false_and_missing(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        if request.url.path == "/2/users/1":
            return httpx.Response(
                200,
                json={"data": {"id": "1", "username": "u1", "receives_your_dm": True}},
            )
        elif request.url.path == "/2/users/2":
            return httpx.Response(
                200,
                json={"data": {"id": "2", "username": "u2", "receives_your_dm": False}},
            )
        elif request.url.path == "/2/users/3":
            return httpx.Response(
                200,
                json={"data": {"id": "3", "username": "u3"}},
            )
        return httpx.Response(404, json={"detail": "Not found"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    r1 = transport.check_recipient(1, "1")
    assert r1.can_dm is True

    r2 = transport.check_recipient(1, "2")
    assert r2.can_dm is False

    r3 = transport.check_recipient(1, "3")
    assert r3.can_dm is None


def test_check_recipient_api_error_is_unknown_not_an_exception(dm_setup):
    conn, cipher, settings, clock = dm_setup

    def handler(request: httpx.Request):
        return httpx.Response(500, json={"detail": "Server error", "type": "server_error"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client)

    reach = transport.check_recipient(1, "999")
    assert reach.can_dm is None
    assert len(reach.detail) > 0


def test_allowlist_stops_unlisted_recipient_before_any_http_call(dm_setup):
    conn, cipher, settings, clock = dm_setup

    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": {}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = XClient(conn, 1, cipher, settings, clock, http=http)
    transport = XDMTransport(client, allowed_user_ids={"101", "102"})

    reach = transport.check_recipient(1, "999")
    assert reach.can_dm is None
    assert reach.detail == REASON_NOT_ON_ALLOWLIST

    outcome = transport.send_dm(1, "999", "Hello")
    assert outcome.kind == "failed"
    assert outcome.reason == REASON_NOT_ON_ALLOWLIST

    assert calls == 0
