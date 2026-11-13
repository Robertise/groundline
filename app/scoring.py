from datetime import datetime, timedelta, timezone
import json

from app.evidence import quote_is_supported, validate_evidence
from app.llm.base import LLMResult
from app.prompts import (
    BIO_SCREEN_SYSTEM,
    BIO_SCREEN_TOOL,
    SCORING_SYSTEM,
    SCORING_TOOL,
)
from app.x.base import XUser, XPost

WEIGHTS = {
    "role": 25,
    "domain": 30,
    "active": 15,
    "individual": 15,
    "campaign_fit": 15,
}
ACTIVE_DAYS = 30
INACTIVE_DAYS = 60


def compute_score(criteria: dict[str, bool]) -> int:
    return sum(WEIGHTS.get(k, 0) for k, v in criteria.items() if v)


def screen_bios(
    llm,
    segment: str,
    users: list[XUser],
    matched_posts: dict[str, str],
) -> tuple[dict[str, dict], LLMResult]:
    if not users:
        return {}, LLMResult(data={}, input_tokens=0, output_tokens=0)

    user_batch = users[:20]

    def _call(batch: list[XUser]) -> tuple[dict, LLMResult]:
        profiles = [
            {
                "x_user_id": u.id,
                "username": u.username,
                "bio": u.bio,
                "location": u.location,
                "post": matched_posts.get(u.id, ""),
            }
            for u in batch
        ]
        user_msg = (
            "Target segment: "
            + json.dumps(segment, ensure_ascii=False)
            + "\n\nProfiles (JSON):\n"
            + json.dumps(profiles, ensure_ascii=False)
        )
        res = llm.call_tool(
            system=BIO_SCREEN_SYSTEM,
            user=user_msg,
            tool_name="bio_screen",
            tool_schema=BIO_SCREEN_TOOL,
        )
        return res.data, res

    data, res1 = _call(user_batch)
    total_in = res1.input_tokens
    total_out = res1.output_tokens

    results_by_id = {}
    for r in data.get("results", []):
        uid = str(r["x_user_id"])
        results_by_id[uid] = r

    # Check for missing ids and retry once
    missing = [u for u in user_batch if u.id not in results_by_id]
    if missing:
        retry_data, res2 = _call(missing)
        total_in += res2.input_tokens
        total_out += res2.output_tokens
        for r in retry_data.get("results", []):
            uid = str(r["x_user_id"])
            results_by_id[uid] = r

    # Validate quotes for passed profiles
    users_dict = {u.id: u for u in user_batch}
    verified_results = {}
    for uid, r in results_by_id.items():
        u = users_dict.get(uid)
        if not u:
            continue
        is_pass = r.get("pass", False)
        quote = r.get("quote", "")
        reason = r.get("reason", "")

        if is_pass:
            matched_p = matched_posts.get(uid, "")
            supported = quote_is_supported(quote, u.bio) or (
                matched_p and quote_is_supported(quote, matched_p)
            )
            if not supported:
                is_pass = False
                reason = "unsupported_quote"

        verified_results[uid] = {
            "pass": is_pass,
            "reason": reason,
            "quote": quote,
        }

    total_result = LLMResult(
        data={"results": list(verified_results.values())},
        input_tokens=total_in,
        output_tokens=total_out,
    )
    return verified_results, total_result


def score_user(
    llm,
    segment: str,
    user: XUser,
    posts: list[XPost],
    now: datetime,
) -> tuple[dict, LLMResult]:
    user_data = {
        "username": user.username,
        "bio": user.bio,
        "location": user.location,
        "posts": [
            {"post_id": p.id, "text": p.text, "created_at": p.created_at}
            for p in posts
        ],
    }
    user_msg = (
        "Target segment: "
        + json.dumps(segment, ensure_ascii=False)
        + "\n\nUser (JSON):\n"
        + json.dumps(user_data, ensure_ascii=False)
    )

    llm_res = llm.call_tool(
        system=SCORING_SYSTEM,
        user=user_msg,
        tool_name="score_user",
        tool_schema=SCORING_TOOL,
    )
    data = llm_res.data
    posts_by_id = {str(p.id): p for p in posts}

    criteria_dict = {}
    for crit in data.get("criteria", []):
        cname = crit["name"]
        llm_met = bool(crit.get("met", False))
        raw_evidence = crit.get("evidence", [])
        surviving = validate_evidence(
            raw_evidence,
            bio=user.bio,
            posts_by_id=posts_by_id,
            author_id=user.id,
            username=user.username,
        )
        is_met = llm_met and len(surviving) > 0
        criteria_dict[cname] = {
            "met": is_met,
            "evidence": surviving,
        }

    # active is computed in code, never by the LLM
    active_met = False
    active_evidence = []
    if posts:
        # Find newest post and check if within ACTIVE_DAYS
        sorted_posts = []
        for p in posts:
            try:
                p_dt = datetime.fromisoformat(p.created_at.replace("Z", "+00:00"))
            except Exception:
                p_dt = now
            sorted_posts.append((p_dt, p))
        sorted_posts.sort(key=lambda pair: pair[0], reverse=True)
        newest_dt, newest_post = sorted_posts[0]

        if (now - newest_dt) <= timedelta(days=ACTIVE_DAYS):
            active_met = True
            active_evidence = [
                {
                    "source": "post",
                    "post_id": str(newest_post.id),
                    "quote": newest_post.text,
                    "url": f"https://x.com/{user.username.lstrip('@')}/status/{newest_post.id}",
                }
            ]

    criteria_dict["active"] = {
        "met": active_met,
        "evidence": active_evidence,
    }

    # Ensure all expected criteria are present
    for req_crit in ["role", "domain", "individual", "campaign_fit"]:
        if req_crit not in criteria_dict:
            criteria_dict[req_crit] = {"met": False, "evidence": []}

    crit_bools = {k: v["met"] for k, v in criteria_dict.items()}
    score = compute_score(crit_bools)

    result = {
        "score": score,
        "criteria": criteria_dict,
        "role": data.get("role", "other"),
        "segment": data.get("segment", "other"),
        "why_fit": data.get("why_fit", []),
        "caution": data.get("caution"),
    }
    return result, llm_res
