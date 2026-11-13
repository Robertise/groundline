from datetime import datetime, timezone, timedelta
import pytest

from app.llm.base import LLMResult, LLMError
from app.llm.bedrock import BedrockLLM
from app.scoring import compute_score, score_user, screen_bios, WEIGHTS
from app.x.base import XUser, XPost


def test_score_is_computed_in_code_from_criteria():
    criteria = {
        "role": True,          # 25
        "domain": True,        # 30
        "active": True,        # 15
        "individual": True,    # 15
        "campaign_fit": False, # 0
    }
    score = compute_score(criteria)
    assert score == 85

    all_false = {k: False for k in WEIGHTS}
    assert compute_score(all_false) == 0

    all_true = {k: True for k in WEIGHTS}
    assert compute_score(all_true) == 100


def test_criterion_without_surviving_evidence_is_not_met():
    class StubLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        # LLM claims met=True, but evidence quote is completely invented!
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "completely invented quote not in bio"}]},
                        {"name": "domain", "met": True, "evidence": [{"source": "bio", "quote": "robotics perception systems"}]},
                        {"name": "individual", "met": True, "evidence": []},
                        {"name": "campaign_fit", "met": False, "evidence": []},
                    ],
                    "role": "builder",
                    "segment": "robotics",
                    "why_fit": ["Matches domain."],
                    "caution": None,
                },
                input_tokens=100,
                output_tokens=50,
            )

    user = XUser(id="1", username="test_u", bio="Building robotics perception systems in Singapore.")
    now = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone.utc)
    res, _ = score_user(StubLLM(), "robotics", user, [], now)

    # role has invalid evidence, so it must NOT be met
    assert res["criteria"]["role"]["met"] is False
    # domain has valid evidence, so it IS met
    assert res["criteria"]["domain"]["met"] is True


def test_active_criterion_uses_post_dates_not_llm():
    class StubLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "engineer in Singapore"}]},
                    ],
                    "role": "builder",
                    "segment": "robotics",
                    "why_fit": [],
                    "caution": None,
                },
                input_tokens=50,
                output_tokens=30,
            )

    now = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="1", username="test_u", bio="Software engineer in Singapore.")

    # Recent post within 30 days
    recent_posts = [XPost(id="p1", author_id="1", text="Working on robots.", created_at="2026-11-01T00:00:00.000000Z")]
    res_recent, _ = score_user(StubLLM(), "robotics", user, recent_posts, now)
    assert res_recent["criteria"]["active"]["met"] is True
    assert len(res_recent["criteria"]["active"]["evidence"]) == 1

    # Old post older than 30 days
    old_posts = [XPost(id="p2", author_id="1", text="Old robot post.", created_at="2026-09-01T00:00:00.000000Z")]
    res_old, _ = score_user(StubLLM(), "robotics", user, old_posts, now)
    assert res_old["criteria"]["active"]["met"] is False
    assert len(res_old["criteria"]["active"]["evidence"]) == 0


def test_unsupported_tier1_quote_turns_pass_into_fail():
    class HallucinatingLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "results": [
                        {"x_user_id": "u1", "pass": True, "reason": "Works on AI", "quote": "invented quote about AI models"},
                    ]
                },
                input_tokens=50,
                output_tokens=20,
            )

    user = XUser(id="u1", username="alice", bio="Food and travel blogger in Bangkok.")
    results, _ = screen_bios(HallucinatingLLM(), "AI", [user], matched_posts={})
    assert results["u1"]["pass"] is False
    assert results["u1"]["reason"] == "unsupported_quote"


def test_bedrock_adapter_parses_tool_use_and_usage():
    class FakeBedrockClient:
        def converse(self, **kwargs):
            return {
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"name": "test_tool", "input": {"score": 90, "valid": True}}}
                        ]
                    }
                },
                "usage": {"inputTokens": 320, "outputTokens": 85},
            }

    bedrock = BedrockLLM("test_model", "us-east-1", client=FakeBedrockClient())
    res = bedrock.call_tool("system", "user", "test_tool", {})
    assert res.data == {"score": 90, "valid": True}
    assert res.input_tokens == 320
    assert res.output_tokens == 85


def test_bedrock_adapter_raises_without_tool_use():
    class EmptyBedrockClient:
        def converse(self, **kwargs):
            return {
                "output": {"message": {"content": [{"text": "Just plain text without tool use."}]}},
                "usage": {"inputTokens": 50, "outputTokens": 20},
            }

    bedrock = BedrockLLM("test_model", "us-east-1", client=EmptyBedrockClient())
    with pytest.raises(LLMError) as exc:
        bedrock.call_tool("system", "user", "test_tool", {})
    assert "No toolUse block" in str(exc.value)
