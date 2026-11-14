from datetime import datetime, timedelta, timezone
import json

from app.evidence import quote_is_supported, validate_evidence
from app.llm.base import LLMResult, LLMOutputError, coerce_json_fields
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

ALLOWED_ROLES = {"builder", "founder", "researcher", "other"}
ALLOWED_SEGMENTS = {
    "models",
    "applications",
    "developer_tools",
    "infrastructure",
    "robotics",
    "embodied_ai",
    "other",
}
ALLOWED_CRITERIA_NAMES = {"role", "domain", "individual"}


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

    data = coerce_json_fields(data, list_fields=("results",))
    results_by_id = {}
    raw_results = data.get("results")
    if isinstance(raw_results, list):
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            uid = r.get("x_user_id")
            if not isinstance(uid, str) or not uid:
                continue
            results_by_id[uid] = r

    # Check for missing ids and retry once
    missing = [u for u in user_batch if u.id not in results_by_id]
    if missing:
        retry_data, res2 = _call(missing)
        total_in += res2.input_tokens
        total_out += res2.output_tokens
        retry_data = coerce_json_fields(retry_data, list_fields=("results",))
        raw_retry = retry_data.get("results")
        if isinstance(raw_retry, list):
            for r in raw_retry:
                if not isinstance(r, dict):
                    continue
                uid = r.get("x_user_id")
                if not isinstance(uid, str) or not uid:
                    continue
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
    raw_data = llm_res.data
    if not isinstance(raw_data, dict):
        raise LLMOutputError(
            "Model output data is not a dict",
            input_tokens=llm_res.input_tokens,
            output_tokens=llm_res.output_tokens,
            raw=str(raw_data)[:2000],
        )

    data = coerce_json_fields(
        raw_data,
        list_fields=("criteria", "why_fit"),
        dict_fields=("segment_match",),
    )
    raw_criteria = data.get("criteria")
    if not isinstance(raw_criteria, list):
        raise LLMOutputError(
            "criteria is missing or not a list",
            input_tokens=llm_res.input_tokens,
            output_tokens=llm_res.output_tokens,
            raw=json.dumps(data, ensure_ascii=False, default=str)[:2000],
        )

    posts_by_id = {str(p.id): p for p in posts}

    # Process segment_match
    raw_sm = data.get("segment_match")
    segment_match_missing = False
    segment_status = "unknown"
    segment_evidence = []

    if isinstance(raw_sm, dict):
        sm = coerce_json_fields(raw_sm, list_fields=("evidence",))
        raw_status = sm.get("status")
        raw_ev = sm.get("evidence")
        if raw_status in ("match", "mismatch", "unknown") and isinstance(raw_ev, list):
            valid_ev_list = []
            for ev in raw_ev:
                if (
                    isinstance(ev, dict)
                    and isinstance(ev.get("source"), str)
                    and isinstance(ev.get("quote"), str)
                ):
                    valid_ev_list.append(ev)
            surviving_sm_ev = validate_evidence(
                valid_ev_list,
                bio=user.bio,
                posts_by_id=posts_by_id,
                author_id=user.id,
                username=user.username,
                location=user.location or "",
            )
            has_surviving = len(surviving_sm_ev) > 0
            if raw_status == "match":
                if has_surviving:
                    segment_status = "match"
                    segment_evidence = surviving_sm_ev
                else:
                    segment_status = "unknown"
            elif raw_status == "mismatch":
                if has_surviving:
                    segment_status = "mismatch"
                    segment_evidence = surviving_sm_ev
                else:
                    segment_status = "unknown"
            elif raw_status == "unknown":
                segment_status = "unknown"
                segment_evidence = surviving_sm_ev
        else:
            segment_match_missing = True
    else:
        segment_match_missing = True

    criteria_dict = {}
    valid_crit_count = 0
    for raw_crit in raw_criteria:
        if not isinstance(raw_crit, dict):
            continue
        crit = coerce_json_fields(raw_crit, list_fields=("evidence",))
        cname = crit.get("name")
        if cname not in ALLOWED_CRITERIA_NAMES:
            continue
        cmet = crit.get("met")
        if not isinstance(cmet, bool):
            continue
        cevidence = crit.get("evidence")
        if not isinstance(cevidence, list):
            continue

        valid_evidence = []
        for ev in cevidence:
            if (
                isinstance(ev, dict)
                and isinstance(ev.get("source"), str)
                and isinstance(ev.get("quote"), str)
            ):
                valid_evidence.append(ev)

        valid_crit_count += 1
        surviving = validate_evidence(
            valid_evidence,
            bio=user.bio,
            posts_by_id=posts_by_id,
            author_id=user.id,
            username=user.username,
        )
        is_met = cmet and len(surviving) > 0
        criteria_dict[cname] = {
            "met": is_met,
            "evidence": surviving,
        }

    if valid_crit_count == 0:
        raise LLMOutputError(
            "No valid criterion remaining in model output",
            input_tokens=llm_res.input_tokens,
            output_tokens=llm_res.output_tokens,
            raw=json.dumps(data, ensure_ascii=False, default=str)[:2000],
        )

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

    criteria_dict["campaign_fit"] = {
        "met": (segment_status == "match"),
        "evidence": [],
    }

    # Ensure all expected criteria are present
    for req_crit in ["role", "domain", "individual", "campaign_fit"]:
        if req_crit not in criteria_dict:
            criteria_dict[req_crit] = {"met": False, "evidence": []}

    crit_bools = {k: v["met"] for k, v in criteria_dict.items()}
    score = compute_score(crit_bools)

    raw_why_fit = data.get("why_fit")
    why_fit = []
    if isinstance(raw_why_fit, list):
        why_fit = [item for item in raw_why_fit if isinstance(item, str)][:2]
    elif isinstance(raw_why_fit, str):
        why_fit = [raw_why_fit][:2]

    role = data.get("role")
    if role not in ALLOWED_ROLES:
        role = "other"

    segment_val = data.get("segment")
    if segment_val not in ALLOWED_SEGMENTS:
        segment_val = "other"

    caution = data.get("caution")
    if not isinstance(caution, str):
        caution = None

    result = {
        "score": score,
        "criteria": criteria_dict,
        "segment_status": segment_status,
        "segment_evidence": segment_evidence,
        "role": role,
        "segment": segment_val,
        "why_fit": why_fit,
        "caution": caution,
    }
    if segment_match_missing:
        result["segment_match_missing"] = True
    return result, llm_res
