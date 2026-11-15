from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from app import db, ledger
from app.clock import iso
from app.ledger import BudgetExceeded
from app.llm.base import LLM, LLMError, LLMOutputError
from app.scoring import screen_bios, score_user, INACTIVE_DAYS, TIER1_BATCH_SIZE
from app.state import transition, record_insert
from app.x.base import XReader, XRateLimited, XAuthError, XUser


@dataclass
class RunCounts:
    found: int = 0
    new_prospects: int = 0
    filtered: dict[str, int] = field(default_factory=dict)  # reason -> count
    screened: int = 0
    scored: int = 0
    left_discovered: int = 0
    llm_errors: int = 0
    errors: list[dict] = field(default_factory=list)


def run_discovery(
    conn: sqlite3.Connection,
    campaign_id: int,
    reader: XReader,
    llm: LLM,
    settings,
    clock,
    simulated: bool,
) -> int:
    # 1. Check existing running run
    cur = conn.execute(
        "SELECT id, started_at FROM discovery_runs WHERE campaign_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    )
    running_row = cur.fetchone()
    if running_row:
        started_dt = datetime.fromisoformat(
            running_row["started_at"].replace("Z", "+00:00")
        )
        if (clock.now() - started_dt) < timedelta(minutes=30):
            raise RuntimeError("A discovery run is already in progress")

    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO discovery_runs (campaign_id, status, started_at) VALUES (?, 'running', ?)",
            (campaign_id, iso(clock.now())),
        )
        run_id = cur.lastrowid

    reader.set_cost_context(campaign_id, run_id)

    counts = RunCounts()
    end_status = "completed"
    error_msg = None

    try:
        cur = conn.execute(
            "SELECT c.*, s.x_user_id as sender_x_user_id, s.id as s_id "
            "FROM campaigns c "
            "JOIN sender_accounts s ON c.sender_account_id = s.id "
            "WHERE c.id = ?",
            (campaign_id,),
        )
        campaign = cur.fetchone()
        if not campaign:
            raise RuntimeError(f"Campaign {campaign_id} not found")

        # 2. sender = reader.me(); upsert into x_users
        sender = reader.me()
        with db.transaction(conn):
            conn.execute(
                """
                INSERT INTO x_users (
                    x_user_id, username, name, bio, receives_your_dm,
                    connection_status, protected, location, followers_count, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(x_user_id) DO UPDATE SET
                    username = excluded.username,
                    name = excluded.name,
                    bio = excluded.bio,
                    receives_your_dm = excluded.receives_your_dm,
                    connection_status = excluded.connection_status,
                    protected = excluded.protected,
                    location = excluded.location,
                    followers_count = excluded.followers_count,
                    fetched_at = excluded.fetched_at
                """,
                (
                    sender.id,
                    sender.username,
                    sender.name,
                    sender.bio,
                    1 if sender.receives_your_dm else (0 if sender.receives_your_dm is False else None),
                    json.dumps(sender.connection_status),
                    1 if sender.protected else 0,
                    sender.location,
                    sender.followers_count,
                    iso(clock.now()),
                ),
            )

        budget_usd = campaign["budget_usd"]
        all_candidates: dict[str, XUser] = {}
        sources: dict[str, set[str]] = {}
        matched_posts: dict[str, str] = {}
        matched_posts_objs = []

        # 3. Collect candidates
        # Handles
        handles_str = campaign["handles"] or ""
        handles = [h.strip().lstrip("@") for h in handles_str.replace("\n", ",").split(",") if h.strip()]
        if handles:
            step_max = len(handles) * ledger.PRICES_USD["users_lookup"]
            ledger.ensure_budget(conn, campaign_id, budget_usd, step_max, simulated)
            h_users = reader.users_by_usernames(handles)
            for u in h_users:
                all_candidates[u.id] = u
                sources.setdefault(u.id, set()).add("handle")

        # Search
        keywords_str = campaign["search_keywords"] or ""
        if keywords_str.strip():
            raw_kws = [k.strip() for k in keywords_str.split(",") if k.strip()]
            quoted_kws = [f'"{k}"' if " " in k else k for k in raw_kws]
            query = f"({' OR '.join(quoted_kws)}) -is:retweet -is:reply"
            max_posts = campaign["search_max_posts"]
            step_max = max_posts * (ledger.PRICES_USD["search_posts"] + ledger.PRICES_USD["search_authors"])
            ledger.ensure_budget(conn, campaign_id, budget_usd, step_max, simulated)
            posts, authors = reader.search_recent(query, max_posts)
            for p in posts:
                matched_posts[p.author_id] = p.text
                matched_posts_objs.append(p)
            for u in authors:
                all_candidates[u.id] = u
                sources.setdefault(u.id, set()).add("search")

        # Seeds
        seed_str = campaign["seed_handles"] or ""
        seed_handles = [s.strip().lstrip("@") for s in seed_str.replace("\n", ",").split(",") if s.strip()]
        if seed_handles:
            seed_following_max = campaign["seed_following_max"]
            step_max = len(seed_handles) * ledger.PRICES_USD["users_lookup"] + len(seed_handles) * seed_following_max * ledger.PRICES_USD["following"]
            ledger.ensure_budget(conn, campaign_id, budget_usd, step_max, simulated)
            seed_users = reader.users_by_usernames(seed_handles)
            for su in seed_users:
                following_users = reader.following(su.id, seed_following_max)
                for fu in following_users:
                    all_candidates[fu.id] = fu
                    sources.setdefault(fu.id, set()).add(f"seed:{su.username}")

        # Followers
        if campaign["include_sender_followers"]:
            followers_max = campaign["followers_max"]
            step_max = followers_max * ledger.PRICES_USD["followers_owned"]
            ledger.ensure_budget(conn, campaign_id, budget_usd, step_max, simulated)
            s_followers = reader.followers(sender.id, followers_max)
            for fu in s_followers:
                all_candidates[fu.id] = fu
                sources.setdefault(fu.id, set()).add("followers")

        counts.found = len(all_candidates)

        # 4. Upsert candidate profiles and search posts, insert prospects
        for u in all_candidates.values():
            with db.transaction(conn):
                conn.execute(
                    """
                    INSERT INTO x_users (
                        x_user_id, username, name, bio, receives_your_dm,
                        connection_status, protected, location, followers_count, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(x_user_id) DO UPDATE SET
                        username = excluded.username,
                        name = excluded.name,
                        bio = excluded.bio,
                        receives_your_dm = excluded.receives_your_dm,
                        connection_status = excluded.connection_status,
                        protected = excluded.protected,
                        location = excluded.location,
                        followers_count = excluded.followers_count,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        u.id,
                        u.username,
                        u.name,
                        u.bio,
                        1 if u.receives_your_dm else (0 if u.receives_your_dm is False else None),
                        json.dumps(u.connection_status),
                        1 if u.protected else 0,
                        u.location,
                        u.followers_count,
                        iso(clock.now()),
                    ),
                )

        for p in matched_posts_objs:
            clean_author_uname = all_candidates.get(p.author_id, sender).username
            url = f"https://x.com/{clean_author_uname.lstrip('@')}/status/{p.id}"
            with db.transaction(conn):
                conn.execute(
                    """
                    INSERT INTO x_posts (post_id, author_id, text, created_at, url, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(post_id) DO UPDATE SET
                        text = excluded.text,
                        fetched_at = excluded.fetched_at
                    """,
                    (p.id, p.author_id, p.text, p.created_at, url, iso(clock.now())),
                )

        for uid, u in all_candidates.items():
            user_sources = sorted(sources.get(uid, set()))
            cur_p = conn.execute(
                "SELECT id, state, sources FROM prospects WHERE campaign_id = ? AND x_user_id = ?",
                (campaign_id, uid),
            )
            p_row = cur_p.fetchone()
            if p_row:
                old_srcs = json.loads(p_row["sources"] or "[]")
                merged_srcs = sorted(set(old_srcs) | set(user_sources))
                with db.transaction(conn):
                    conn.execute(
                        "UPDATE prospects SET sources = ? WHERE id = ?",
                        (json.dumps(merged_srcs), p_row["id"]),
                    )
            else:
                with db.transaction(conn):
                    cur_ins = conn.execute(
                        "INSERT INTO prospects (campaign_id, x_user_id, state, sources, created_at, updated_at) "
                        "VALUES (?, ?, 'discovered', ?, ?, ?)",
                        (campaign_id, uid, json.dumps(user_sources), iso(clock.now()), iso(clock.now())),
                    )
                    pid = cur_ins.lastrowid
                    record_insert(conn, "prospect", pid, "discovered", "system", iso(clock.now()))
                    counts.new_prospects += 1

        # 5. Apply hard filters in order for all discovered prospects
        cur_disc = conn.execute(
            "SELECT p.id, p.x_user_id, u.username, u.protected, u.receives_your_dm, u.bio, u.location "
            "FROM prospects p "
            "JOIN x_users u ON p.x_user_id = u.x_user_id "
            "WHERE p.campaign_id = ? AND p.state = 'discovered'",
            (campaign_id,),
        )
        discovered_rows = cur_disc.fetchall()

        passed_hard_filters = []
        for row in discovered_rows:
            pid = row["id"]
            uid = row["x_user_id"]
            filter_reason = None

            # 1. is_sender
            if uid == sender.id or uid == campaign["sender_x_user_id"]:
                filter_reason = "is_sender"
            # 2. already_contacted
            elif not filter_reason:
                cur_out = conn.execute(
                    "SELECT 1 FROM outreach WHERE sender_account_id = ? AND recipient_x_user_id = ?",
                    (campaign["sender_account_id"], uid),
                )
                if cur_out.fetchone():
                    filter_reason = "already_contacted"
            # 3. suppressed
            if not filter_reason:
                cur_sup = conn.execute(
                    "SELECT 1 FROM suppression WHERE x_user_id = ?", (uid,)
                )
                if cur_sup.fetchone():
                    filter_reason = "suppressed"
            # 4. protected
            if not filter_reason and row["protected"]:
                filter_reason = "protected"
            # 5. cannot_dm
            if not filter_reason and row["receives_your_dm"] is not None and row["receives_your_dm"] == 0:
                filter_reason = "cannot_dm"

            if filter_reason:
                with db.transaction(conn):
                    transition(
                        conn,
                        "prospect",
                        pid,
                        "discovered",
                        "filtered_out",
                        "system",
                        iso(clock.now()),
                        reason=filter_reason,
                    )
                counts.filtered[filter_reason] = counts.filtered.get(filter_reason, 0) + 1
            else:
                passed_hard_filters.append(row)

        # 6. Tier 1: Screen bios in batches of 20
        passed_tier1 = []
        for i in range(0, len(passed_hard_filters), TIER1_BATCH_SIZE):
            batch_rows = passed_hard_filters[i : i + TIER1_BATCH_SIZE]
            batch_users = [
                all_candidates.get(
                    r["x_user_id"],
                    XUser(id=r["x_user_id"], username=r["username"], bio=r["bio"] or "", location=r["location"] or "")
                )
                for r in batch_rows
            ]

            # LLM cost guard (estimate)
            ledger.ensure_budget(conn, campaign_id, budget_usd, 0.01, simulated)

            try:
                screen_results, llm_res = screen_bios(
                    llm, campaign["segment"], batch_users, matched_posts
                )
                ledger.record_llm(
                    conn,
                    llm_res.input_tokens,
                    llm_res.output_tokens,
                    settings,
                    iso(clock.now()),
                    campaign_id=campaign_id,
                    run_id=run_id,
                    simulated=simulated,
                    detail="tier1_screen",
                )
            except (BudgetExceeded, XRateLimited, XAuthError):
                raise
            except LLMOutputError as e:
                ledger.record_llm(
                    conn,
                    e.input_tokens,
                    e.output_tokens,
                    settings,
                    iso(clock.now()),
                    campaign_id=campaign_id,
                    run_id=run_id,
                    simulated=simulated,
                    detail="tier1_screen_invalid",
                )
                counts.llm_errors += len(batch_users)
                for u in batch_users:
                    counts.errors.append({"username": u.username, "error": "invalid model output"})
                continue
            except Exception as e:
                counts.llm_errors += len(batch_users)
                for u in batch_users:
                    counts.errors.append({"username": u.username, "error": repr(e)[:300]})
                continue

            counts.screened += len(screen_results)

            for r in batch_rows:
                res = screen_results.get(r["x_user_id"])
                if not res:
                    counts.llm_errors += 1
                    counts.errors.append({"username": r["username"], "error": "no screening result"})
                    continue
                if res["pass"]:
                    if res.get("segment_status") == "mismatch" and res.get("segment_evidence"):
                        t1_seg_fit_reasons = json.dumps(
                            {
                                "tier1": {
                                    "pass": True,
                                    "reason": res.get("reason", ""),
                                    "quote": res.get("quote", ""),
                                },
                                "segment_status": "mismatch",
                                "segment_evidence": res["segment_evidence"],
                            },
                            ensure_ascii=False,
                        )
                        with db.transaction(conn):
                            transition(
                                conn,
                                "prospect",
                                r["id"],
                                "discovered",
                                "filtered_out",
                                "system",
                                iso(clock.now()),
                                reason="outside_segment",
                                fit_reasons=t1_seg_fit_reasons,
                            )
                        counts.filtered["outside_segment"] = counts.filtered.get("outside_segment", 0) + 1
                    else:
                        passed_tier1.append((r["id"], r["x_user_id"], res))
                else:
                    fail_reason = (
                        "unsupported_quote"
                        if res.get("reason") == "unsupported_quote"
                        else "bio_screen_failed"
                    )
                    t1_fit_reasons = json.dumps(
                        {
                            "tier1": {
                                "pass": False,
                                "reason": res.get("reason", ""),
                                "quote": res.get("quote", ""),
                            }
                        },
                        ensure_ascii=False,
                    )
                    with db.transaction(conn):
                        transition(
                            conn,
                            "prospect",
                            r["id"],
                            "discovered",
                            "filtered_out",
                            "system",
                            iso(clock.now()),
                            reason=fail_reason,
                            fit_reasons=t1_fit_reasons,
                        )
                    counts.filtered[fail_reason] = counts.filtered.get(fail_reason, 0) + 1

        # 7. Tier 2: Deep profiles
        # Order by number of sources (desc) then followers count (desc)
        def _tier2_sort_key(item):
            pid, uid, _ = item
            src_count = len(sources.get(uid, set()))
            cand = all_candidates.get(uid)
            fol_count = (cand.followers_count or 0) if cand else 0
            return (src_count, fol_count)

        passed_tier1.sort(key=_tier2_sort_key, reverse=True)
        max_deep = campaign["max_deep_profiles"]
        deep_candidates = passed_tier1[:max_deep]
        beyond_max = passed_tier1[max_deep:]
        counts.left_discovered = len(beyond_max)

        for pid, uid, t1_res in deep_candidates:
            u_obj = all_candidates.get(uid)
            if not u_obj:
                continue

            # Fetch posts if not fetched within 7 days
            cur_pfetch = conn.execute(
                "SELECT posts_fetched_at FROM x_users WHERE x_user_id = ?", (uid,)
            )
            pf_row = cur_pfetch.fetchone()
            posts_fetched_at_str = pf_row["posts_fetched_at"] if pf_row else None
            needs_fetch = True
            if posts_fetched_at_str:
                pf_dt = datetime.fromisoformat(posts_fetched_at_str.replace("Z", "+00:00"))
                if (clock.now() - pf_dt) < timedelta(days=7):
                    needs_fetch = False

            user_posts_list = []
            if needs_fetch:
                ledger.ensure_budget(conn, campaign_id, budget_usd, 10 * ledger.PRICES_USD["user_posts"], simulated)
                user_posts_list = reader.user_posts(uid, 10)
                for p in user_posts_list:
                    p_url = f"https://x.com/{u_obj.username.lstrip('@')}/status/{p.id}"
                    with db.transaction(conn):
                        conn.execute(
                            """
                            INSERT INTO x_posts (post_id, author_id, text, created_at, url, fetched_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(post_id) DO UPDATE SET text = excluded.text, fetched_at = excluded.fetched_at
                            """,
                            (p.id, uid, p.text, p.created_at, p_url, iso(clock.now())),
                        )
                with db.transaction(conn):
                    conn.execute(
                        "UPDATE x_users SET posts_fetched_at = ? WHERE x_user_id = ?",
                        (iso(clock.now()), uid),
                    )
            else:
                cur_existing_posts = conn.execute(
                    "SELECT post_id, author_id, text, created_at FROM x_posts WHERE author_id = ? ORDER BY created_at DESC LIMIT 10",
                    (uid,),
                )
                from app.x.base import XPost
                for row_p in cur_existing_posts.fetchall():
                    user_posts_list.append(
                        XPost(
                            id=row_p["post_id"],
                            author_id=row_p["author_id"],
                            text=row_p["text"],
                            created_at=row_p["created_at"],
                        )
                    )

            # Check if any post is within INACTIVE_DAYS (60 days)
            has_recent_post = False
            for p in user_posts_list:
                try:
                    p_dt = datetime.fromisoformat(p.created_at.replace("Z", "+00:00"))
                    if (clock.now() - p_dt) <= timedelta(days=INACTIVE_DAYS):
                        has_recent_post = True
                        break
                except Exception:
                    pass

            if not has_recent_post:
                with db.transaction(conn):
                    transition(
                        conn,
                        "prospect",
                        pid,
                        "discovered",
                        "filtered_out",
                        "system",
                        iso(clock.now()),
                        reason="inactive",
                    )
                counts.filtered["inactive"] = counts.filtered.get("inactive", 0) + 1
                continue

            # Call score_user
            ledger.ensure_budget(conn, campaign_id, budget_usd, 0.015, simulated)
            try:
                score_res, score_llm_res = score_user(
                    llm, campaign["segment"], u_obj, user_posts_list, clock.now()
                )
                ledger.record_llm(
                    conn,
                    score_llm_res.input_tokens,
                    score_llm_res.output_tokens,
                    settings,
                    iso(clock.now()),
                    campaign_id=campaign_id,
                    run_id=run_id,
                    simulated=simulated,
                    detail=f"tier2_score:{uid}",
                )
            except (BudgetExceeded, XRateLimited, XAuthError):
                raise
            except LLMOutputError as e:
                ledger.record_llm(
                    conn,
                    e.input_tokens,
                    e.output_tokens,
                    settings,
                    iso(clock.now()),
                    campaign_id=campaign_id,
                    run_id=run_id,
                    simulated=simulated,
                    detail=f"tier2_score_invalid:{uid}",
                )
                counts.errors.append({"username": u_obj.username, "error": "invalid model output"})
                continue
            except Exception as e:
                counts.llm_errors += 1
                counts.errors.append({"username": u_obj.username, "error": repr(e)[:300]})
                continue

            # Insert evidence rows
            with db.transaction(conn):
                for crit_name, crit_data in score_res["criteria"].items():
                    for ev in crit_data.get("evidence", []):
                        conn.execute(
                            "INSERT INTO evidence (prospect_id, source, post_id, quote, url) VALUES (?, ?, ?, ?, ?)",
                            (
                                pid,
                                ev["source"],
                                ev.get("post_id"),
                                ev["quote"],
                                ev["url"],
                            ),
                        )

            fit_reasons_json = json.dumps(
                {
                    "criteria": score_res["criteria"],
                    "why_fit": score_res["why_fit"],
                    "tier1": {
                        "pass": True,
                        "reason": t1_res.get("reason", ""),
                        "quote": t1_res.get("quote", ""),
                    },
                    "segment_status": score_res.get("segment_status", "unknown"),
                    "segment_evidence": score_res.get("segment_evidence", []),
                },
                ensure_ascii=False,
            )
            with db.transaction(conn):
                transition(
                    conn,
                    "prospect",
                    pid,
                    "discovered",
                    "scored",
                    "system",
                    iso(clock.now()),
                    score=score_res["score"],
                    fit_reasons=fit_reasons_json,
                    segment=score_res["segment"],
                    role=score_res["role"],
                    caution=score_res["caution"],
                )
            counts.scored += 1

            if score_res.get("segment_status") == "mismatch":
                with db.transaction(conn):
                    transition(
                        conn,
                        "prospect",
                        pid,
                        "scored",
                        "filtered_out",
                        "system",
                        iso(clock.now()),
                        reason="outside_segment",
                    )
                counts.filtered["outside_segment"] = (
                    counts.filtered.get("outside_segment", 0) + 1
                )
            elif score_res["score"] < campaign["min_score"]:
                with db.transaction(conn):
                    transition(
                        conn,
                        "prospect",
                        pid,
                        "scored",
                        "filtered_out",
                        "system",
                        iso(clock.now()),
                        reason="low_score",
                    )
                counts.filtered["low_score"] = counts.filtered.get("low_score", 0) + 1

    except BudgetExceeded as e:
        end_status = "budget_reached"
        error_msg = str(e)
    except XRateLimited as e:
        end_status = "rate_limited"
        error_msg = str(e)
    except XAuthError as e:
        end_status = "auth_error"
        error_msg = str(e)
    except Exception as e:
        end_status = "failed"
        error_msg = repr(e)

    finished_str = iso(clock.now())
    with db.transaction(conn):
        conn.execute(
            """
            UPDATE discovery_runs SET
                status = ?,
                finished_at = ?,
                counts = ?,
                error = ?
            WHERE id = ?
            """,
            (
                end_status,
                finished_str,
                json.dumps(asdict(counts)),
                error_msg,
                run_id,
            ),
        )

    return run_id
