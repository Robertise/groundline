from dataclasses import dataclass
import json
import re
import sqlite3

from app import db, ledger
from app.clock import iso
from app.evidence import normalize
from app.ledger import BudgetExceeded
from app.llm.base import LLMResult, LLMError, coerce_json_fields
from app.prompts import DRAFT_SYSTEM, DRAFT_TOOL, JUDGE_SYSTEM, JUDGE_TOOL
from app.state import transition, TransitionError

MIN_CHARS = 150
MAX_CHARS = 600
MAX_ATTEMPTS = 3
SIMILARITY_THRESHOLD = 0.5  # Jaccard similarity of word 5-gram sets
BATCH_LIMIT = 10  # drafts per click on "Draft all"
DRAFT_COST_ESTIMATE_USD = 0.01  # used for the budget check before each prospect

BANNED_PHRASES = (
    "big fan",
    "huge fan",
    "long-time follower",
    "longtime follower",
    "been following you",
    "been following your work for",
    "as we discussed",
    "as discussed",
    "great meeting you",
    "great to meet you",
    "nice meeting you",
    "good to reconnect",
    "reconnect",
    "hope you remember",
    "per our conversation",
    "like we talked",
    "as you know",
    "loved chatting",
    "guaranteed",
    "act now",
    "limited time",
)

LINK_PATTERN = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(com|io|ai|co|net|org|xyz)\b)", re.I
)

PLAIN_FLAG_TEXTS = {
    "empty": "The message is empty",
    "too_short": "Shorter than 150 characters",
    "too_long": "Longer than 600 characters",
    "contains_link": "Contains a link",
    "banned_phrase": "Uses a phrase that pretends familiarity or sounds like spam",
    "no_claims": "Does not point to anything the person posted",
    "unknown_evidence_id": "Refers to evidence that does not exist",
    "claim_not_in_message": "A listed claim is not in the message",
    "too_similar": "Very similar to the draft for another person",
    "unsupported_sentence": "Says something about the person that the evidence does not support",
    "fake_familiarity": "Sounds like you already know the person",
    "missing_opt_out": "Does not make it easy to say no",
    "sensitive_trait": "Mentions a sensitive personal trait",
    "judge_error": "The second check could not run",
}


@dataclass(frozen=True)
class Flag:
    level: str  # "block" or "warn"
    code: str
    detail: str = ""


def get_word_5grams(text: str) -> set[tuple[str, ...]]:
    words = normalize(text).split()
    if len(words) < 5:
        return set()
    return {tuple(words[i : i + 5]) for i in range(len(words) - 4)}


def jaccard_similarity(s1: set, s2: set) -> float:
    if not s1 or not s2:
        return 0.0
    intersection = len(s1 & s2)
    union = len(s1 | s2)
    return intersection / union if union > 0 else 0.0


def evidence_items(conn: sqlite3.Connection, prospect_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT id, source, post_id, quote, url FROM evidence WHERE prospect_id = ? ORDER BY id",
        (prospect_id,),
    )
    rows = cur.fetchall()
    seen_quotes = set()
    items = []
    idx = 1
    for r in rows:
        norm_q = normalize(r["quote"])
        if norm_q not in seen_quotes:
            seen_quotes.add(norm_q)
            items.append(
                {
                    "id": f"E{idx}",
                    "source": r["source"],
                    "text": r["quote"],
                    "url": r["url"],
                }
            )
            idx += 1
    return items


def check_draft(
    message: str,
    claims: list[dict],
    evidence_ids: set[str],
    other_messages: dict[str, str],
    human_edit: bool = False,
) -> list[Flag]:
    flags: list[Flag] = []

    # 1. empty
    if not message or not message.strip():
        flags.append(Flag("block", "empty"))
    else:
        # 2. length checks
        if len(message) < MIN_CHARS:
            flags.append(Flag("warn" if human_edit else "block", "too_short"))
        elif len(message) > MAX_CHARS:
            level = "block" if (not human_edit or len(message) > 1000) else "warn"
            flags.append(Flag(level, "too_long"))

    # 3. link check
    if LINK_PATTERN.search(message):
        flags.append(Flag("block", "contains_link"))

    # 4. banned phrases
    norm_msg = normalize(message)
    for bp in BANNED_PHRASES:
        if bp in norm_msg:
            flags.append(Flag("block", "banned_phrase", bp))

    # 5. claims checks (only when not human_edit)
    if not human_edit:
        if not claims:
            flags.append(Flag("block", "no_claims"))
        else:
            for c in claims:
                eid = c.get("evidence_id")
                if eid not in evidence_ids:
                    flags.append(Flag("block", "unknown_evidence_id", str(eid)))
                ctext = c.get("text", "")
                if not ctext or normalize(ctext) not in norm_msg:
                    flags.append(Flag("block", "claim_not_in_message", str(ctext)))

    # 6. similarity check
    my_5grams = get_word_5grams(message)
    if my_5grams:
        for other_uname, other_msg in other_messages.items():
            other_5grams = get_word_5grams(other_msg)
            sim = jaccard_similarity(my_5grams, other_5grams)
            if sim >= SIMILARITY_THRESHOLD:
                flags.append(Flag("warn", "too_similar", other_uname))

    return flags


def judge(
    llm, message: str, bio: str, evidence: list[dict]
) -> tuple[list[Flag], LLMResult | None]:
    ev_input = [
        {"id": it["id"], "source": it["source"], "text": it["text"]}
        for it in evidence
    ]
    user_msg = "Input (JSON):\n" + json.dumps(
        {"draft": message, "recipient_bio": bio, "evidence": ev_input},
        ensure_ascii=False,
    )

    try:
        res = llm.call_tool(
            system=JUDGE_SYSTEM,
            user=user_msg,
            tool_name="judge_draft",
            tool_schema=JUDGE_TOOL,
        )
    except LLMError as e:
        return [Flag("warn", "judge_error", str(e))], None

    data = res.data
    if not isinstance(data, dict):
        return [Flag("warn", "judge_error", "Model output data is not a dict")], res

    data = coerce_json_fields(data, list_fields=("sentences",))
    flags: list[Flag] = []

    sentences = data.get("sentences", [])
    if isinstance(sentences, list):
        for s in sentences:
            if (
                isinstance(s, dict)
                and s.get("about_recipient") is True
                and not s.get("supported")
            ):
                flags.append(
                    Flag("warn", "unsupported_sentence", s.get("text", ""))
                )

    if data.get("fake_familiarity") is True:
        flags.append(Flag("warn", "fake_familiarity"))
    if data.get("has_soft_opt_out") is False:
        flags.append(Flag("warn", "missing_opt_out"))
    if data.get("sensitive_trait") is True:
        flags.append(Flag("warn", "sensitive_trait"))

    return flags, res


def draft_prospect(
    conn: sqlite3.Connection,
    prospect_id: int,
    llm,
    settings,
    clock,
    simulated: bool,
) -> str:
    cur = conn.execute(
        """
        SELECT p.id, p.campaign_id, p.x_user_id, p.state, p.score, p.fit_reasons,
               c.brief, c.budget_usd,
               s.username as sender_username,
               u.username as recipient_username, u.name as recipient_name, u.bio as recipient_bio
        FROM prospects p
        JOIN campaigns c ON p.campaign_id = c.id
        JOIN sender_accounts s ON c.sender_account_id = s.id
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.id = ?
        """,
        (prospect_id,),
    )
    row = cur.fetchone()
    if not row:
        return "skipped"
    if row["state"] not in ("scored", "drafted"):
        return "skipped"

    campaign_id = row["campaign_id"]
    budget_usd = row["budget_usd"]

    ledger.ensure_budget(
        conn, campaign_id, budget_usd, DRAFT_COST_ESTIMATE_USD, simulated
    )

    items = evidence_items(conn, prospect_id)
    has_post_or_bio = any(it["source"] in ("post", "bio") for it in items)
    if not has_post_or_bio:
        if row["state"] == "scored":
            with db.transaction(conn):
                transition(
                    conn,
                    "prospect",
                    prospect_id,
                    "scored",
                    "no_evidence",
                    "system",
                    iso(clock.now()),
                    reason="no_evidence",
                )
            return "no_evidence"
        return "skipped"

    cur_other = conn.execute(
        """
        SELECT u.username, d.text
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        JOIN drafts d ON d.prospect_id = p.id
        WHERE p.campaign_id = ? AND p.id != ? AND p.state IN ('drafted', 'queued')
          AND d.version = (SELECT MAX(version) FROM drafts WHERE prospect_id = p.id)
        """,
        (campaign_id, prospect_id),
    )
    other_messages = {r["username"]: r["text"] for r in cur_other.fetchall()}

    evidence_ids = {it["id"] for it in items}
    ev_for_llm = [
        {"id": it["id"], "source": it["source"], "text": it["text"]}
        for it in items
    ]
    feedback: list[str] = []
    last_message = ""
    last_claims: list[dict] = []
    last_flags: list[Flag] = []

    for attempt in range(MAX_ATTEMPTS):
        input_data = {
            "team_brief": row["brief"] or "",
            "sender": f"@{row['sender_username']}",
            "recipient": {
                "username": row["recipient_username"],
                "name": row["recipient_name"] or "",
                "bio": row["recipient_bio"] or "",
            },
            "evidence": ev_for_llm,
            "feedback": feedback,
        }
        user_msg = "Input (JSON):\n" + json.dumps(
            input_data, ensure_ascii=False
        )

        res = llm.call_tool(
            system=DRAFT_SYSTEM,
            user=user_msg,
            tool_name="draft_dm",
            tool_schema=DRAFT_TOOL,
        )

        ledger.record_llm(
            conn,
            res.input_tokens,
            res.output_tokens,
            settings,
            iso(clock.now()),
            campaign_id=campaign_id,
            run_id=None,
            simulated=simulated,
            detail="draft",
        )

        data = coerce_json_fields(res.data, list_fields=("claims",))
        decision = data.get("decision")
        if decision == "insufficient":
            if row["state"] == "scored":
                fit_reasons = {}
                if row["fit_reasons"]:
                    try:
                        fit_reasons = json.loads(row["fit_reasons"])
                    except Exception:
                        fit_reasons = {}
                fit_reasons["draft"] = {
                    "decision": "insufficient",
                    "reason": data.get("reason", ""),
                }
                with db.transaction(conn):
                    transition(
                        conn,
                        "prospect",
                        prospect_id,
                        "scored",
                        "no_evidence",
                        "system",
                        iso(clock.now()),
                        reason="insufficient_evidence",
                        fit_reasons=json.dumps(fit_reasons, ensure_ascii=False),
                    )
                return "no_evidence"
            return "skipped"

        last_message = data.get("message", "")
        raw_claims = data.get("claims", [])
        last_claims = []
        if isinstance(raw_claims, list):
            for c in raw_claims:
                if isinstance(c, dict) and "text" in c and "evidence_id" in c:
                    last_claims.append(
                        {
                            "text": str(c["text"]),
                            "evidence_id": str(c["evidence_id"]),
                        }
                    )

        last_flags = check_draft(
            last_message,
            last_claims,
            evidence_ids,
            other_messages,
            human_edit=False,
        )
        block_flags = [f for f in last_flags if f.level == "block"]
        if not block_flags:
            break

        feedback = [
            f"{PLAIN_FLAG_TEXTS.get(f.code, f.code)}: {f.detail}".rstrip(": ")
            for f in block_flags
        ]

    judge_flags, judge_res = judge(
        llm, last_message, row["recipient_bio"] or "", items
    )
    if judge_res:
        ledger.record_llm(
            conn,
            judge_res.input_tokens,
            judge_res.output_tokens,
            settings,
            iso(clock.now()),
            campaign_id=campaign_id,
            run_id=None,
            simulated=simulated,
            detail="judge",
        )

    all_flags = last_flags + judge_flags

    cur_ver = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM drafts WHERE prospect_id = ?",
        (prospect_id,),
    )
    next_ver = cur_ver.fetchone()[0]

    flags_json = json.dumps(
        [
            {"level": f.level, "code": f.code, "detail": f.detail}
            for f in all_flags
        ],
        ensure_ascii=False,
    )
    claims_json = json.dumps(last_claims, ensure_ascii=False)
    now_str = iso(clock.now())

    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO drafts (prospect_id, version, text, claims, validator_flags, edited_by_human, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                prospect_id,
                next_ver,
                last_message,
                claims_json,
                flags_json,
                now_str,
            ),
        )
        transition(
            conn, "prospect", prospect_id, row["state"], "drafted", "system", now_str
        )

    return "drafted"


def draft_campaign(
    conn: sqlite3.Connection,
    campaign_id: int,
    llm,
    settings,
    clock,
    simulated: bool,
    limit: int = BATCH_LIMIT,
) -> dict:
    cur = conn.execute(
        """
        SELECT p.id
        FROM prospects p
        WHERE p.campaign_id = ? AND p.state = 'scored'
          AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.prospect_id = p.id)
        ORDER BY p.score DESC NULLS LAST, p.id DESC
        LIMIT ?
        """,
        (campaign_id, limit),
    )
    pids = [r["id"] for r in cur.fetchall()]
    res = {
        "drafted": 0,
        "no_evidence": 0,
        "skipped": 0,
        "errors": 0,
        "stopped_reason": None,
    }
    for pid in pids:
        try:
            outcome = draft_prospect(conn, pid, llm, settings, clock, simulated)
            if outcome in res:
                res[outcome] += 1
        except BudgetExceeded:
            res["stopped_reason"] = "budget_reached"
            break
        except LLMError:
            res["errors"] += 1
            continue
    return res


def save_human_edit(
    conn: sqlite3.Connection, prospect_id: int, text: str, clock
) -> list[Flag]:
    cur = conn.execute(
        "SELECT id, campaign_id, state FROM prospects WHERE id = ?",
        (prospect_id,),
    )
    p_row = cur.fetchone()
    if not p_row or p_row["state"] != "drafted":
        raise TransitionError(f"Prospect {prospect_id} must be drafted to edit")

    cur_draft = conn.execute(
        "SELECT version, claims FROM drafts WHERE prospect_id = ? ORDER BY version DESC LIMIT 1",
        (prospect_id,),
    )
    last_d = cur_draft.fetchone()
    next_ver = (last_d["version"] + 1) if last_d else 1

    prev_claims = []
    if last_d and last_d["claims"]:
        try:
            prev_claims = json.loads(last_d["claims"])
        except Exception:
            prev_claims = []

    norm_text = normalize(text)
    surviving_claims = [
        c
        for c in prev_claims
        if isinstance(c, dict) and normalize(c.get("text", "")) in norm_text
    ]

    cur_other = conn.execute(
        """
        SELECT u.username, d.text
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        JOIN drafts d ON d.prospect_id = p.id
        WHERE p.campaign_id = ? AND p.id != ? AND p.state IN ('drafted', 'queued')
          AND d.version = (SELECT MAX(version) FROM drafts WHERE prospect_id = p.id)
        """,
        (p_row["campaign_id"], prospect_id),
    )
    other_messages = {r["username"]: r["text"] for r in cur_other.fetchall()}

    flags = check_draft(text, [], set(), other_messages, human_edit=True)
    flags_json = json.dumps(
        [
            {"level": f.level, "code": f.code, "detail": f.detail}
            for f in flags
        ],
        ensure_ascii=False,
    )
    claims_json = json.dumps(surviving_claims, ensure_ascii=False)
    now_str = iso(clock.now())

    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO drafts (prospect_id, version, text, claims, validator_flags, edited_by_human, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (prospect_id, next_ver, text, claims_json, flags_json, now_str),
        )
        transition(
            conn, "prospect", prospect_id, "drafted", "drafted", "human", now_str
        )

    return flags


def latest_draft(conn: sqlite3.Connection, prospect_id: int) -> dict | None:
    cur = conn.execute(
        """
        SELECT id, prospect_id, version, text, claims, validator_flags, edited_by_human, created_at
        FROM drafts
        WHERE prospect_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (prospect_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    if isinstance(d["claims"], str):
        try:
            d["claims"] = json.loads(d["claims"])
        except Exception:
            d["claims"] = []
    if isinstance(d["validator_flags"], str):
        try:
            d["validator_flags"] = json.loads(d["validator_flags"])
        except Exception:
            d["validator_flags"] = []
    return d


def can_approve(draft: dict) -> tuple[bool, str]:
    if not draft:
        return False, "No draft to approve"

    text = draft.get("text", "")
    if not text or not text.strip():
        return False, "The message is empty"
    if len(text) > 1000:
        return False, "Longer than 1,000 characters"

    is_human = bool(draft.get("edited_by_human", 0))
    if not is_human:
        raw_flags = draft.get("validator_flags", [])
        if isinstance(raw_flags, str):
            try:
                raw_flags = json.loads(raw_flags)
            except Exception:
                raw_flags = []
        has_block = any(
            (
                f.get("level") == "block"
                if isinstance(f, dict)
                else getattr(f, "level", "") == "block"
            )
            for f in raw_flags
        )
        if has_block:
            return False, "Edit the draft to resolve the blocked issues, or redraft"

    return True, ""
