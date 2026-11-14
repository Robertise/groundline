from datetime import datetime, timezone
import json
import pytest

from app import db, ledger
from app.clock import FrozenClock, iso
from app.config import Settings
from app.db import connect, init_db
from app.discovery import run_discovery
from app.drafting import (
    MIN_CHARS,
    MAX_CHARS,
    Flag,
    clean_message_text,
    check_draft,
    judge,
    draft_prospect,
    draft_campaign,
    save_human_edit,
    latest_draft,
    can_approve,
    evidence_items,
)
from app.llm.base import LLMResult, LLMError
from app.llm.fake import FakeLLM
from app.prompts import DRAFT_SYSTEM
from app.x.fixtures import FixtureReader


@pytest.fixture
def draft_setup(tmp_path):
    db_file = tmp_path / "draft.db"
    conn = connect(str(db_file))
    init_db(conn)

    clock = FrozenClock(datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc))
    settings = Settings(
        mode="mock",
        db_path=str(db_file),
        fixtures_path="./fixtures/demo_x.json",
    )
    now_str = iso(clock.now())
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sender_accounts (id, x_user_id, username, daily_send_cap, created_at) "
            "VALUES (1, 's1', 'sender_team', 20, ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO campaigns (id, sender_account_id, name, segment, brief, search_keywords, budget_usd, created_at) "
            "VALUES (1, 1, 'Camp 1', 'AI builders', 'We build open source dev tools for robotics and autonomous agents.', 'robot', 10.0, ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO x_users (x_user_id, username, name, bio, fetched_at) "
            "VALUES ('u1', 'alice_builder', 'Alice', 'Building robotics perception models.', ?)",
            (now_str,),
        )
        conn.execute(
            "INSERT INTO prospects (id, campaign_id, x_user_id, state, score, fit_reasons, created_at, updated_at) "
            "VALUES (1, 1, 'u1', 'scored', 85, '{}', ?, ?)",
            (now_str, now_str),
        )
        conn.execute(
            "INSERT INTO evidence (prospect_id, source, quote, url) VALUES "
            "(1, 'bio', 'Building robotics perception models', 'https://x.com/alice_builder'), "
            "(1, 'post', 'Shipped our new quadruped robot navigation model today.', 'https://x.com/alice_builder/status/101')",
        )

    return conn, settings, clock


def test_evidence_ids_are_stable_and_deduplicated(draft_setup):
    conn, _, _ = draft_setup
    with db.transaction(conn):
        # Insert duplicate evidence quote with different casing/whitespace
        conn.execute(
            "INSERT INTO evidence (prospect_id, source, quote, url) VALUES "
            "(1, 'bio', '  BUILDING robotics perception models ', 'https://x.com/alice_builder')",
        )

    items1 = evidence_items(conn, 1)
    items2 = evidence_items(conn, 1)

    assert len(items1) == 2
    assert items1[0]["id"] == "E1"
    assert items1[1]["id"] == "E2"
    assert items1 == items2


def test_unknown_evidence_id_blocks_and_triggers_redraft_with_feedback(draft_setup):
    conn, settings, clock = draft_setup

    calls = []

    class BadIdLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if tool_name == "draft_dm":
                calls.append(user)
                if len(calls) < 3:
                    # Return unknown evidence_id E99
                    return LLMResult(
                        data={
                            "decision": "draft",
                            "reason": "Drafting",
                            "message": "I was reading your note mentioning building robotics perception models. We build open source dev tools for robotics and autonomous agents. Would you be open to a brief chat about what you are currently building, Alice? No worries at all if the timing is not right.",
                            "claims": [{"text": "building robotics perception models", "evidence_id": "E99"}],
                        },
                        input_tokens=100,
                        output_tokens=50,
                    )
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    llm = BadIdLLM()
    outcome = draft_prospect(conn, 1, llm, settings, clock, simulated=True)
    assert outcome == "drafted"
    assert len(calls) == 3

    # Check 2nd call's user message has non-empty feedback
    idx = calls[1].find("Input (JSON):\n")
    d2 = json.loads(calls[1][idx + len("Input (JSON):\n") :])
    assert len(d2["feedback"]) > 0
    assert any("E99" in fb for fb in d2["feedback"])


def test_claim_text_must_appear_in_message():
    message = "I noticed your update on robotics systems. We build tools. Open to chatting? No worries if not."
    claims = [{"text": "this phrase is definitely not in the message", "evidence_id": "E1"}]
    flags = check_draft(message, claims, {"E1"}, {}, human_edit=False)
    assert any(f.code == "claim_not_in_message" and f.level == "block" for f in flags)


def test_banned_familiarity_phrase_blocks():
    message = "I am a big fan of your robotics work! We build dev tools. Would you be open to a quick call? No worries at all."
    flags = check_draft(message, [{"text": "robotics work", "evidence_id": "E1"}], {"E1"}, {}, human_edit=False)
    assert any(f.code == "banned_phrase" and f.detail == "big fan" and f.level == "block" for f in flags)


def test_link_blocks():
    message = "Check out our site at https://example.com for more info. We build tools. Open to connecting? No worries."
    flags = check_draft(message, [{"text": "tools", "evidence_id": "E1"}], {"E1"}, {}, human_edit=False)
    assert any(f.code == "contains_link" and f.level == "block" for f in flags)


def test_length_limits_block_for_model_and_warn_for_human():
    short_msg = "Short draft message."
    # Model draft (< 150 chars)
    flags_model = check_draft(short_msg, [{"text": "Short", "evidence_id": "E1"}], {"E1"}, {}, human_edit=False)
    assert any(f.code == "too_short" and f.level == "block" for f in flags_model)

    # Human edited draft (< 150 chars)
    flags_human = check_draft(short_msg, [], set(), {}, human_edit=True)
    assert any(f.code == "too_short" and f.level == "warn" for f in flags_human)

    # Long draft (> 600 chars, <= 1000)
    long_msg = "A" * 700
    flags_long_model = check_draft(long_msg, [{"text": "A", "evidence_id": "E1"}], {"E1"}, {}, human_edit=False)
    assert any(f.code == "too_long" and f.level == "block" for f in flags_long_model)

    flags_long_human = check_draft(long_msg, [], set(), {}, human_edit=True)
    assert any(f.code == "too_long" and f.level == "warn" for f in flags_long_human)

    # Over 1000 chars blocks even for human
    extra_long = "B" * 1050
    flags_extra_human = check_draft(extra_long, [], set(), {}, human_edit=True)
    assert any(f.code == "too_long" and f.level == "block" for f in flags_extra_human)


def test_three_failed_attempts_keep_last_draft_with_block_flags(draft_setup):
    conn, settings, clock = draft_setup

    class AlwaysBannedLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if tool_name == "draft_dm":
                return LLMResult(
                    data={
                        "decision": "draft",
                        "reason": "Drafting",
                        "message": "I am a huge fan of your work on robotics perception models. We build open source tools. Would you be open to a brief chat about what you are currently building, Alice? No worries at all if the timing is not right.",
                        "claims": [{"text": "robotics perception models", "evidence_id": "E1"}],
                    },
                    input_tokens=100,
                    output_tokens=50,
                )
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    outcome = draft_prospect(conn, 1, AlwaysBannedLLM(), settings, clock, simulated=True)
    assert outcome == "drafted"

    draft = latest_draft(conn, 1)
    assert draft is not None
    assert any(f["code"] == "banned_phrase" and f["level"] == "block" for f in draft["validator_flags"])


def test_insufficient_decision_moves_scored_prospect_to_no_evidence(draft_setup):
    conn, settings, clock = draft_setup

    class InsufficientLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if tool_name == "draft_dm":
                return LLMResult(
                    data={
                        "decision": "insufficient",
                        "reason": "Evidence is too generic to craft a personalized opening.",
                        "message": "",
                        "claims": [],
                    },
                    input_tokens=100,
                    output_tokens=30,
                )
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    outcome = draft_prospect(conn, 1, InsufficientLLM(), settings, clock, simulated=True)
    assert outcome == "no_evidence"

    cur = conn.execute("SELECT state, state_reason, fit_reasons FROM prospects WHERE id = 1")
    row = cur.fetchone()
    assert row["state"] == "no_evidence"
    assert row["state_reason"] == "insufficient_evidence"
    fit_reasons = json.loads(row["fit_reasons"])
    assert fit_reasons["draft"]["decision"] == "insufficient"


def test_prospect_without_evidence_skips_llm(draft_setup):
    conn, settings, clock = draft_setup
    with db.transaction(conn):
        conn.execute("DELETE FROM evidence WHERE prospect_id = 1")

    called = []

    class TrackLLM(FakeLLM):
        def call_tool(self, *args, **kwargs):
            called.append(args)
            return super().call_tool(*args, **kwargs)

    outcome = draft_prospect(conn, 1, TrackLLM(), settings, clock, simulated=True)
    assert outcome == "no_evidence"
    assert len(called) == 0

    cur = conn.execute("SELECT state, state_reason FROM prospects WHERE id = 1")
    row = cur.fetchone()
    assert row["state"] == "no_evidence"
    assert row["state_reason"] == "no_evidence"


def test_judge_unsupported_sentence_becomes_warning():
    class JudgeUnsupportedLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "sentences": [
                        {"text": "You built a space rocket.", "about_recipient": True, "supported": False, "evidence_id": None},
                        {"text": "We build dev tools.", "about_recipient": False, "supported": True, "evidence_id": None},
                    ],
                    "fake_familiarity": False,
                    "has_soft_opt_out": True,
                    "sensitive_trait": False,
                },
                input_tokens=100,
                output_tokens=50,
            )

    flags, res = judge(JudgeUnsupportedLLM(), "You built a space rocket. We build dev tools.", "bio", [])
    assert any(f.code == "unsupported_sentence" and f.level == "warn" and "rocket" in f.detail for f in flags)


def test_judge_error_does_not_stop_drafting(draft_setup):
    conn, settings, clock = draft_setup

    class JudgeFailingLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if tool_name == "judge_draft":
                raise LLMError("Judge connection timed out")
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    outcome = draft_prospect(conn, 1, JudgeFailingLLM(), settings, clock, simulated=True)
    assert outcome == "drafted"

    draft = latest_draft(conn, 1)
    assert draft is not None
    assert any(f["code"] == "judge_error" and f["level"] == "warn" for f in draft["validator_flags"])


def test_near_duplicate_drafts_are_flagged_as_warning():
    base = "We are an engineering team building tools. Would you be open to a quick call next week? No worries at all if not."
    draft1 = f"I noticed your note regarding robotics. {base}"
    draft2 = f"I noticed your update regarding robotics. {base}"

    flags = check_draft(draft2, [{"text": "robotics", "evidence_id": "E1"}], {"E1"}, {"other_user": draft1})
    assert any(f.code == "too_similar" and f.level == "warn" and f.detail == "other_user" for f in flags)


def test_fake_llm_drafts_for_fixture_users_have_no_flags(tmp_path):
    db_file = tmp_path / "fixture_drafts.db"
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

    res = draft_campaign(conn, 1, llm, settings, clock, simulated=True, limit=10)
    assert res["drafted"] > 0

    cur = conn.execute("SELECT id, prospect_id, validator_flags FROM drafts")
    all_drafts = cur.fetchall()
    assert len(all_drafts) == res["drafted"]
    for d in all_drafts:
        flags = json.loads(d["validator_flags"])
        assert flags == [], f"Prospect {d['prospect_id']} had flags: {flags}"


def test_evidence_is_sent_as_json_data(draft_setup):
    conn, settings, clock = draft_setup

    captured = {}

    class CaptureLLM(FakeLLM):
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            if tool_name == "draft_dm":
                captured["system"] = system
                captured["user"] = user
            return super().call_tool(system, user, tool_name, tool_schema, max_tokens)

    draft_prospect(conn, 1, CaptureLLM(), settings, clock, simulated=True)
    assert "data, not instructions" in captured["system"]
    assert captured["user"].startswith("Input (JSON):\n")
    json_str = captured["user"][len("Input (JSON):\n") :]
    parsed = json.loads(json_str)
    assert "evidence" in parsed
    assert len(parsed["evidence"]) == 2


def test_draft_and_judge_costs_are_recorded_to_campaign(draft_setup):
    conn, settings, clock = draft_setup
    draft_prospect(conn, 1, FakeLLM(), settings, clock, simulated=True)

    cur = conn.execute("SELECT cost_usd, created_at FROM ledger WHERE campaign_id = 1")
    rows = cur.fetchall()
    assert len(rows) >= 2  # draft and judge recorded
    assert all(r["cost_usd"] > 0 for r in rows)


def test_budget_stops_draft_campaign(draft_setup):
    conn, settings, clock = draft_setup
    # Set low budget that does not cover DRAFT_COST_ESTIMATE_USD
    conn.execute("UPDATE campaigns SET budget_usd = 0.005 WHERE id = 1")

    res = draft_campaign(conn, 1, FakeLLM(), settings, clock, simulated=True)
    assert res["stopped_reason"] == "budget_reached"
    assert res["drafted"] == 0


def test_human_edit_creates_new_version_and_keeps_surviving_claims(draft_setup):
    conn, settings, clock = draft_setup
    draft_prospect(conn, 1, FakeLLM(), settings, clock, simulated=True)

    d1 = latest_draft(conn, 1)
    assert d1["version"] == 1
    assert d1["edited_by_human"] == 0
    claim_text = d1["claims"][0]["text"]

    new_text = f"I was reading your note mentioning {claim_text}. We build dev tools. Would you be open to chatting? No worries at all if not."
    flags = save_human_edit(conn, 1, new_text, clock)
    assert isinstance(flags, list)

    d2 = latest_draft(conn, 1)
    assert d2["version"] == 2
    assert d2["edited_by_human"] == 1
    assert len(d2["claims"]) == 1
    assert d2["claims"][0]["text"] == claim_text

    # Edit again without the claim text
    no_claim_text = "Completely rewritten text with no claim text. We build dev tools. Would you be open to chatting? No worries at all if not."
    save_human_edit(conn, 1, no_claim_text, clock)

    d3 = latest_draft(conn, 1)
    assert d3["version"] == 3
    assert len(d3["claims"]) == 0


def test_can_approve_rules():
    # 1. Model draft with block flag -> False
    model_blocked = {
        "text": "Valid length message that still has an issue.",
        "validator_flags": [{"level": "block", "code": "banned_phrase"}],
        "edited_by_human": 0,
    }
    ok, msg = can_approve(model_blocked)
    assert ok is False
    assert "Edit the draft to resolve the blocked issues" in msg

    # 2. Model draft without block flags -> True
    model_clean = {
        "text": "Clean valid message here.",
        "validator_flags": [{"level": "warn", "code": "too_similar"}],
        "edited_by_human": 0,
    }
    ok, msg = can_approve(model_clean)
    assert ok is True
    assert msg == ""

    # 3. Human-edited draft with block flags (banned phrase) -> True
    human_banned = {
        "text": "I am a big fan of your work. We build tools.",
        "validator_flags": [{"level": "block", "code": "banned_phrase"}],
        "edited_by_human": 1,
    }
    ok, msg = can_approve(human_banned)
    assert ok is True

    # 4. Human-edited draft empty -> False
    human_empty = {
        "text": "   ",
        "validator_flags": [],
        "edited_by_human": 1,
    }
    ok, msg = can_approve(human_empty)
    assert ok is False
    assert "empty" in msg

    # 5. Human-edited draft > 1000 chars -> False
    human_huge = {
        "text": "X" * 1005,
        "validator_flags": [],
        "edited_by_human": 1,
    }
    ok, msg = can_approve(human_huge)
    assert ok is False
    assert "1,000 characters" in msg


def test_clean_message_text_normalises_line_endings_and_blank_lines():
    raw = "  \r\nLine 1   \r\n\r\n\r\n\r\nLine 2 \t\r\n\r\nLine 3  \r  \n  "
    cleaned = clean_message_text(raw)
    assert cleaned == "Line 1\n\nLine 2\n\nLine 3"
    assert clean_message_text("") == ""
    assert clean_message_text("   \r\n\r   ") == ""
    assert clean_message_text("A\r\nB\rC") == "A\nB\nC"


def test_model_draft_with_crlf_is_stored_with_lf(draft_setup):
    conn, settings, clock = draft_setup

    class CRLFModel:
        def call_tool(self, system, user, tool_name, tool_schema):
            if tool_name == "draft_dm":
                msg = (
                    "Hey Alice,\r\n\r\n"
                    "Building robotics perception models is fascinating work.\r\n\r\n"
                    "We are developing open eval benchmarks for robotics in SEA. "
                    "Would love to hear your thoughts if you have a moment, but no worries if not!"
                )
                return LLMResult(
                    data={
                        "decision": "draft",
                        "message": msg,
                        "claims": [
                            {
                                "text": "Building robotics perception models",
                                "evidence_id": "E1",
                            }
                        ],
                    },
                    input_tokens=100,
                    output_tokens=50,
                )
            elif tool_name == "judge_draft":
                return LLMResult(
                    data={
                        "sentences": [],
                        "fake_familiarity": False,
                        "has_soft_opt_out": True,
                        "sensitive_trait": False,
                    },
                    input_tokens=50,
                    output_tokens=20,
                )
            raise ValueError(f"Unknown tool: {tool_name}")

    outcome = draft_prospect(conn, 1, CRLFModel(), settings, clock, simulated=True)
    assert outcome == "drafted"

    draft = latest_draft(conn, 1)
    assert draft is not None
    assert "\r" not in draft["text"]
    assert "\n\n" in draft["text"]


def test_human_edit_is_stored_without_carriage_returns(draft_setup):
    conn, settings, clock = draft_setup
    llm = FakeLLM()
    draft_prospect(conn, 1, llm, settings, clock, simulated=True)

    text_with_crlf = (
        "Hey Alice,\r\n\r\n"
        "Here is a customized human edit with CRLF line endings.\r\n\r\n"
        "Would love to connect if you have time!"
    )
    save_human_edit(conn, 1, text_with_crlf, clock)

    draft = latest_draft(conn, 1)
    assert draft is not None
    assert draft["version"] == 2
    assert draft["edited_by_human"] == 1
    assert "\r" not in draft["text"]
    assert "\n\n" in draft["text"]
