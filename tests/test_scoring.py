from datetime import datetime, timezone, timedelta
import json
import pytest

from app.llm.base import LLMResult, LLMError, LLMOutputError
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
                    ],
                    "segment_match": {"status": "unknown", "evidence": []},
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


def test_criteria_returned_as_json_string_is_parsed():
    class JsonStringLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            criteria_list = [
                {
                    "name": "role",
                    "met": True,
                    "evidence": [{"source": "bio", "quote": "building open source robotics"}],
                },
                {
                    "name": "domain",
                    "met": True,
                    "evidence": [{"source": "bio", "quote": "building open source robotics"}],
                },
                {
                    "name": "individual",
                    "met": True,
                    "evidence": [{"source": "bio", "quote": "building open source robotics"}],
                },
            ]
            return LLMResult(
                data={
                    "criteria": json.dumps(criteria_list),
                    "role": "builder",
                    "segment": "robotics",
                    "why_fit": json.dumps(["Builds open source robotics."]),
                },
                input_tokens=100,
                output_tokens=50,
            )

    user = XUser(id="u1", username="roboticist", bio="building open source robotics in Singapore")
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    res, _ = score_user(JsonStringLLM(), "robotics", user, [], now)
    assert res["criteria"]["role"]["met"] is True
    assert res["criteria"]["domain"]["met"] is True
    assert res["role"] == "builder"
    assert res["why_fit"] == ["Builds open source robotics."]


def test_malformed_criteria_raise_llm_output_error_with_usage():
    class BadCriteriaLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={"criteria": "not valid json"},
                input_tokens=123,
                output_tokens=45,
            )

    user = XUser(id="u1", username="test", bio="AI researcher")
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with pytest.raises(LLMOutputError) as exc_info:
        score_user(BadCriteriaLLM(), "AI", user, [], now)

    err = exc_info.value
    assert err.input_tokens == 123
    assert err.output_tokens == 45
    assert len(err.raw) > 0


def test_invalid_enum_values_fall_back_to_other():
    class BadEnumLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI researcher"}]}
                    ],
                    "role": "super_ninja",
                    "segment": "quantum_crypto",
                    "caution": 12345,
                },
                input_tokens=50,
                output_tokens=20,
            )

    user = XUser(id="u1", username="test", bio="AI researcher in Singapore")
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    res, _ = score_user(BadEnumLLM(), "AI", user, [], now)
    assert res["role"] == "other"
    assert res["segment"] == "other"
    assert res["caution"] is None


def test_segment_match_with_evidence_adds_campaign_fit_points():
    class MatchLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer working on robotics"}]},
                        {"name": "domain", "met": True, "evidence": [{"source": "bio", "quote": "robotics and autonomous systems"}]},
                        {"name": "individual", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer working on robotics"}]},
                    ],
                    "segment_match": {
                        "status": "match",
                        "evidence": [{"source": "location", "quote": "Singapore"}],
                    },
                    "role": "builder",
                    "segment": "robotics",
                    "why_fit": ["Builds robotics in Singapore."],
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="sg_builder", bio="AI engineer working on robotics and autonomous systems in Singapore", location="Singapore")
    posts = [XPost(id="p1", author_id="u1", text="Latest robotics release.", created_at="2026-11-10T00:00:00Z")]
    res, _ = score_user(MatchLLM(), "robotics in Singapore", user, posts, now)

    assert res["segment_status"] == "match"
    assert len(res["segment_evidence"]) == 1
    assert res["segment_evidence"][0]["source"] == "location"
    assert res["segment_evidence"][0]["quote"] == "Singapore"
    assert res["criteria"]["campaign_fit"]["met"] is True
    # role(25) + domain(30) + individual(15) + active(15) + campaign_fit(15) = 100
    assert res["score"] == 100


def test_segment_match_without_surviving_evidence_is_unknown():
    class FakeMatchLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer"}]},
                    ],
                    "segment_match": {
                        "status": "match",
                        "evidence": [{"source": "location", "quote": "Bangkok"}],
                    },
                    "role": "builder",
                    "segment": "robotics",
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="test_user", bio="AI engineer", location="Singapore")
    res, _ = score_user(FakeMatchLLM(), "robotics in Bangkok", user, [], now)

    assert res["segment_status"] == "unknown"
    assert res["segment_evidence"] == []
    assert res["criteria"]["campaign_fit"]["met"] is False


def test_segment_mismatch_with_location_evidence_sets_status_mismatch():
    class MismatchLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer in SF"}]},
                    ],
                    "segment_match": {
                        "status": "mismatch",
                        "evidence": [{"source": "location", "quote": "San Francisco, CA"}],
                    },
                    "role": "builder",
                    "segment": "robotics",
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="sf_builder", bio="AI engineer in SF", location="San Francisco, CA")
    res, _ = score_user(MismatchLLM(), "robotics in SEA", user, [], now)

    assert res["segment_status"] == "mismatch"
    assert len(res["segment_evidence"]) == 1
    assert res["segment_evidence"][0]["quote"] == "San Francisco, CA"
    assert res["criteria"]["campaign_fit"]["met"] is False


def test_segment_mismatch_without_evidence_is_unknown():
    class HallucinatedMismatchLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer"}]},
                    ],
                    "segment_match": {
                        "status": "mismatch",
                        "evidence": [{"source": "location", "quote": "London, UK"}],
                    },
                    "role": "builder",
                    "segment": "robotics",
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="mysterious_builder", bio="AI engineer", location="Tokyo")
    res, _ = score_user(HallucinatedMismatchLLM(), "robotics in SEA", user, [], now)

    assert res["segment_status"] == "unknown"
    assert res["segment_evidence"] == []
    assert res["criteria"]["campaign_fit"]["met"] is False


def test_missing_segment_match_defaults_to_unknown_without_error():
    class NoSegmentMatchLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer"}]},
                    ],
                    "role": "builder",
                    "segment": "robotics",
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="test_u", bio="AI engineer", location="Singapore")
    res, _ = score_user(NoSegmentMatchLLM(), "robotics in SEA", user, [], now)

    assert res["segment_status"] == "unknown"
    assert res["segment_evidence"] == []
    assert res.get("segment_match_missing") is True
    assert res["criteria"]["campaign_fit"]["met"] is False


def test_segment_match_given_as_json_string_is_parsed():
    class StringSegmentMatchLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer"}]},
                    ],
                    "segment_match": json.dumps({
                        "status": "match",
                        "evidence": [{"source": "location", "quote": "Singapore"}],
                    }),
                    "role": "builder",
                    "segment": "robotics",
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="test_u", bio="AI engineer", location="Singapore")
    res, _ = score_user(StringSegmentMatchLLM(), "robotics in SEA", user, [], now)

    assert res["segment_status"] == "match"
    assert len(res["segment_evidence"]) == 1
    assert res["segment_evidence"][0]["quote"] == "Singapore"
    assert res["criteria"]["campaign_fit"]["met"] is True


def test_campaign_fit_criterion_from_model_is_ignored():
    class ExtraCritLLM:
        def call_tool(self, system, user, tool_name, tool_schema, max_tokens=2000):
            return LLMResult(
                data={
                    "criteria": [
                        {"name": "role", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer"}]},
                        {"name": "campaign_fit", "met": True, "evidence": [{"source": "bio", "quote": "AI engineer"}]},
                    ],
                    "segment_match": {
                        "status": "unknown",
                        "evidence": [],
                    },
                    "role": "builder",
                    "segment": "robotics",
                },
                input_tokens=100,
                output_tokens=50,
            )

    now = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    user = XUser(id="u1", username="test_u", bio="AI engineer", location="Singapore")
    res, _ = score_user(ExtraCritLLM(), "robotics in SEA", user, [], now)

    assert res["segment_status"] == "unknown"
    assert res["criteria"]["campaign_fit"]["met"] is False
    assert res["criteria"]["campaign_fit"]["evidence"] == []
