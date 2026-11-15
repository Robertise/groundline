from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import urllib.parse
import zoneinfo
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.clock import SystemClock, iso
from app.config import Settings, get_settings, missing_live_settings
from app.deps import build_llm, build_reader, current_sender, get_conn
from app.discovery import run_discovery
from app.evidence import normalize
from app.ledger import estimate_campaign_max_cost, spent
from app import db, approval
from app.approval import AlreadyContacted, Suppressed
from app.drafting import (
    draft_campaign,
    draft_prospect,
    save_human_edit,
    latest_draft,
    can_approve,
    evidence_items,
    clean_message_text,
    PLAIN_FLAG_TEXTS,
)
from app.state import transition, TransitionError

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def short_time(value) -> str:
    """'2026-11-15T08:10:06.050004Z' -> '2026-11-15 15:10' in the configured time zone."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(zoneinfo.ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


templates.env.filters["short_time"] = short_time

router = APIRouter()

PLAIN_REASONS = {
    "is_sender": "This is your own account",
    "already_contacted": "Already messaged from this account",
    "suppressed": "On your do-not-contact list",
    "protected": "Protected account",
    "cannot_dm": "Does not accept DMs from your account",
    "bio_screen_failed": "Profile does not show AI or robotics work",
    "unsupported_quote": "AI's evidence could not be found in the profile",
    "inactive": "No original posts in the last 60 days",
    "low_score": "Score below the campaign minimum",
    "outside_segment": "Outside the target segment",
    "no_evidence": "Nothing specific enough to write about",
    "insufficient_evidence": "Nothing specific enough to write about",
}

FILTER_SHORT_LABELS = {
    "cannot_dm": "can't receive your DMs",
    "bio_screen_failed": "no AI or robotics work shown",
    "outside_segment": "outside the segment",
    "low_score": "below the minimum score",
    "inactive": "no recent posts",
    "unsupported_quote": "evidence not found in profile",
    "protected": "protected account",
    "is_sender": "your own account",
    "already_contacted": "already messaged",
    "suppressed": "on your do-not-contact list",
}

RUN_PROBLEM_TEXT = {
    "budget_reached": "The campaign budget was reached. Raise the budget or narrow the sources, then run again; finished people are kept.",
    "rate_limited": "X asked the app to slow down. Run again in a few minutes; finished people are kept.",
    "auth_error": "X rejected the app's access to your account. Press Connect X, then run again.",
    "failed": "Something went wrong. The details are in Run history at the bottom of this page.",
}


def campaign_progress(conn: sqlite3.Connection, campaign_id: int) -> dict:
    by_state = {
        r["state"]: r["cnt"]
        for r in conn.execute(
            "SELECT state, count(*) AS cnt FROM prospects WHERE campaign_id = ? GROUP BY state",
            (campaign_id,),
        ).fetchall()
    }
    drafted = conn.execute(
        "SELECT count(DISTINCT d.prospect_id) FROM drafts d JOIN prospects p ON d.prospect_id = p.id "
        "WHERE p.campaign_id = ?",
        (campaign_id,),
    ).fetchone()[0]
    out = {
        r["state"]: r["cnt"]
        for r in conn.execute(
            "SELECT o.state, count(*) AS cnt FROM outreach o JOIN prospects p ON o.prospect_id = p.id "
            "WHERE p.campaign_id = ? GROUP BY o.state",
            (campaign_id,),
        ).fetchall()
    }
    return {
        "found": sum(by_state.values()),
        "kept": sum(by_state.get(s, 0) for s in ("scored", "drafted", "queued", "rejected")),
        "drafted": drafted,
        "approved": sum(v for k, v in out.items() if k != "cancelled"),
        "queued": out.get("approved", 0),
        "sent": out.get("sent", 0) + out.get("replied", 0),
        "replied": out.get("replied", 0),
        "attention": sum(out.get(s, 0) for s in ("unknown", "failed", "blocked")),
    }



def _common_context(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    msg: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "request": request,
        "mode": settings.mode,
        "sender": current_sender(conn, settings),
        "missing_settings": missing_live_settings(settings)
        if settings.mode != "mock"
        else [],
        "msg": msg,
        "error": error,
    }


@router.get("/")
def index():
    return RedirectResponse(url="/campaigns", status_code=302)


@router.get("/campaigns", response_class=HTMLResponse)
def list_campaigns(
    request: Request,
    msg: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    cur = conn.execute("SELECT * FROM campaigns ORDER BY id DESC")
    campaigns_rows = cur.fetchall()

    campaign_list = []
    for c in campaigns_rows:
        cid = c["id"]
        c_dict = dict(c)

        cnt_cur = conn.execute(
            "SELECT state, count(*) as cnt FROM prospects WHERE campaign_id = ? GROUP BY state",
            (cid,),
        )
        c_dict["counts_by_state"] = {r["state"]: r["cnt"] for r in cnt_cur.fetchall()}

        run_cur = conn.execute(
            "SELECT id, status FROM discovery_runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
            (cid,),
        )
        last_run = run_cur.fetchone()
        c_dict["last_run_status"] = last_run["status"] if last_run else None
        last_run_cost = 0.0
        if last_run:
            cost_cur = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE run_id = ?",
                (last_run["id"],),
            )
            last_run_cost = float(cost_cur.fetchone()[0])
        c_dict["last_run_cost"] = last_run_cost
        c_dict["progress"] = campaign_progress(conn, cid)
        campaign_list.append(c_dict)

    ctx = _common_context(request, conn, settings, msg, error)
    ctx["campaigns"] = campaign_list
    return templates.TemplateResponse(request=request, name="campaigns.html", context=ctx)


@router.get("/campaigns/new", response_class=HTMLResponse)
def new_campaign_form(
    request: Request,
    msg: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    ctx = _common_context(request, conn, settings, msg, error)
    ctx["form"] = {}
    ctx["errors"] = []
    return templates.TemplateResponse(request=request, name="campaign_new.html", context=ctx)


@router.post("/campaigns", response_class=HTMLResponse)
async def create_campaign(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    form_data = await request.form()
    name = str(form_data.get("name", "")).strip()
    segment = str(form_data.get("segment", "")).strip()
    brief = str(form_data.get("brief", "")).strip()
    handles = str(form_data.get("handles", "")).strip()
    search_keywords = str(form_data.get("search_keywords", "")).strip()
    search_max_posts_raw = form_data.get("search_max_posts", "100")
    seed_handles = str(form_data.get("seed_handles", "")).strip()
    seed_following_max_raw = form_data.get("seed_following_max", "100")
    include_sender_followers = form_data.get("include_sender_followers") is not None
    followers_max_raw = form_data.get("followers_max", "200")
    max_deep_profiles_raw = form_data.get("max_deep_profiles", "40")
    min_score_raw = form_data.get("min_score", "60")
    budget_usd_raw = form_data.get("budget_usd", "5.0")

    errors: list[str] = []
    if not name:
        errors.append("Campaign name is required.")
    if not segment:
        errors.append("Target segment is required.")

    has_source = bool(
        handles or search_keywords or seed_handles or include_sender_followers
    )
    if not has_source:
        errors.append(
            "At least one discovery source must be provided (handles, search keywords, seeds, or sender followers)."
        )

    def parse_int(val, field_name, min_v, max_v, default):
        try:
            v = int(val)
            if not (min_v <= v <= max_v):
                errors.append(f"{field_name} must be between {min_v} and {max_v}.")
            return v
        except (ValueError, TypeError):
            errors.append(f"{field_name} must be an integer between {min_v} and {max_v}.")
            return default

    search_max_posts = parse_int(search_max_posts_raw, "Search max posts", 1, 1000, 100)
    seed_following_max = parse_int(
        seed_following_max_raw, "Seed following max", 1, 1000, 100
    )
    followers_max = parse_int(followers_max_raw, "Followers max", 1, 1000, 200)
    max_deep_profiles = parse_int(
        max_deep_profiles_raw, "Max deep profiles", 1, 1000, 40
    )
    min_score = parse_int(min_score_raw, "Minimum score", 1, 100, 60)

    try:
        budget_usd = float(budget_usd_raw)
        if not (0.1 <= budget_usd <= 50.0):
            errors.append("Budget must be between 0.1 and 50.0 USD.")
    except (ValueError, TypeError):
        errors.append("Budget must be a valid number between 0.1 and 50.0 USD.")
        budget_usd = 5.0

    if errors:
        ctx = _common_context(request, conn, settings)
        ctx["form"] = dict(form_data)
        ctx["errors"] = errors
        return templates.TemplateResponse(
            request=request, name="campaign_new.html", context=ctx, status_code=400
        )

    sender = current_sender(conn, settings)
    if not sender:
        ctx = _common_context(request, conn, settings)
        ctx["form"] = dict(form_data)
        ctx["errors"] = ["No connected X account. Please connect an account first."]
        return templates.TemplateResponse(
            request=request, name="campaign_new.html", context=ctx, status_code=400
        )

    now_str = iso(SystemClock().now())
    with db.transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO campaigns (
                sender_account_id, name, brief, budget_usd, created_at,
                segment, handles, search_keywords, search_max_posts,
                seed_handles, seed_following_max, include_sender_followers,
                followers_max, max_deep_profiles, min_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sender["id"],
                name,
                brief,
                budget_usd,
                now_str,
                segment,
                handles,
                search_keywords,
                search_max_posts,
                seed_handles,
                seed_following_max,
                1 if include_sender_followers else 0,
                followers_max,
                max_deep_profiles,
                min_score,
            ),
        )
        new_id = cur.lastrowid

    return RedirectResponse(url=f"/campaigns/{new_id}", status_code=303)


@router.get("/campaigns/{id}", response_class=HTMLResponse)
def campaign_detail(
    id: int,
    request: Request,
    view: str = Query("scored"),
    msg: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    cur = conn.execute("SELECT * FROM campaigns WHERE id = ?", (id,))
    campaign = cur.fetchone()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    cost_est = estimate_campaign_max_cost(campaign, settings)
    spent_so_far = spent(conn, id, include_simulated=(settings.mode == "mock"))

    runs_cur = conn.execute(
        """
        SELECT r.*,
               (SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE run_id = r.id) as cost
        FROM discovery_runs r
        WHERE r.campaign_id = ?
        ORDER BY r.id DESC
        """,
        (id,),
    )
    runs_raw = runs_cur.fetchall()
    runs = []
    for r in runs_raw:
        r_dict = dict(r)
        try:
            r_dict["counts_dict"] = json.loads(r["counts"]) if r["counts"] else {}
        except Exception:
            r_dict["counts_dict"] = {}
        runs.append(r_dict)

    scored_cnt = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ? AND state = 'scored'",
        (id,),
    ).fetchone()[0]
    filtered_cnt = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ? AND state = 'filtered_out'",
        (id,),
    ).fetchone()[0]
    pending_cnt = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ? AND state IN ('discovered', 'no_evidence')",
        (id,),
    ).fetchone()[0]
    all_cnt = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ?",
        (id,),
    ).fetchone()[0]
    counts_by_tab = {
        "scored": scored_cnt,
        "filtered": filtered_cnt,
        "pending": pending_cnt,
        "all": all_cnt,
    }

    if view == "filtered":
        where_clause = "AND p.state = 'filtered_out'"
    elif view == "pending":
        where_clause = "AND p.state IN ('discovered', 'no_evidence')"
    elif view == "all":
        where_clause = ""
    else:
        view = "scored"
        where_clause = "AND p.state = 'scored'"

    pros_cur = conn.execute(
        f"""
        SELECT p.*, u.username, u.name, u.bio, u.receives_your_dm, u.location, u.followers_count
        FROM prospects p
        JOIN x_users u ON p.x_user_id = u.x_user_id
        WHERE p.campaign_id = ? {where_clause}
        ORDER BY p.score DESC NULLS LAST, p.id DESC
        """,
        (id,),
    )
    prospects_rows = pros_cur.fetchall()

    prospects = []
    for p in prospects_rows:
        p_dict = dict(p)
        fit_data = {}
        if p["fit_reasons"]:
            try:
                fit_data = json.loads(p["fit_reasons"])
            except Exception:
                fit_data = {}
        why_fit = fit_data.get("why_fit", [])
        if isinstance(why_fit, str):
            why_fit = [why_fit]
        p_dict["why_fit_lines"] = why_fit
        p_dict["fit_reasons"] = fit_data

        sources_list = []
        if p["sources"]:
            try:
                sources_list = json.loads(p["sources"])
            except Exception:
                sources_list = []
        p_dict["sources_list"] = sources_list

        ev_cur = conn.execute(
            "SELECT source, post_id, quote, url FROM evidence WHERE prospect_id = ?",
            (p["id"],),
        )
        raw_ev = [dict(ev) for ev in ev_cur.fetchall()]
        deduped_ev = []
        seen_quotes = set()
        for ev in raw_ev:
            norm_q = normalize(ev.get("quote", ""))
            if norm_q not in seen_quotes:
                seen_quotes.add(norm_q)
                deduped_ev.append(ev)
                if len(deduped_ev) == 4:
                    break
        p_dict["evidence_items"] = deduped_ev
        prospects.append(p_dict)

    ctx = _common_context(request, conn, settings, msg, error)
    ctx["campaign"] = dict(campaign)
    ctx["cost_est"] = cost_est
    ctx["spent_so_far"] = spent_so_far
    ctx["runs"] = runs
    ctx["prospects"] = prospects
    ctx["counts_by_tab"] = counts_by_tab
    ctx["current_view"] = view
    ctx["plain_reasons"] = PLAIN_REASONS
    cur_draft_cnt = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ? AND state = 'drafted'",
        (id,),
    ).fetchone()[0]
    ctx["draft_count"] = cur_draft_cnt

    progress = campaign_progress(conn, id)
    if not runs:
        next_action = "run"
    elif cur_draft_cnt > 0:
        next_action = "review"
    elif counts_by_tab["scored"] > 0:
        next_action = "draft"
    elif progress["queued"] > 0:
        next_action = "send"
    else:
        next_action = "run"

    filter_rows = conn.execute(
        "SELECT state_reason, count(*) AS cnt FROM prospects WHERE campaign_id = ? AND state = 'filtered_out' "
        "GROUP BY state_reason ORDER BY cnt DESC",
        (id,),
    ).fetchall()
    filter_reasons = [
        {"text": FILTER_SHORT_LABELS.get(r["state_reason"], PLAIN_REASONS.get(r["state_reason"], r["state_reason"] or "other")),
         "count": r["cnt"]}
        for r in filter_rows
    ]

    last_run_problem = None
    last_run_errors = []
    if runs:
        latest = runs[0]
        if latest["status"] not in ("completed", "running"):
            last_run_problem = {
                "status": latest["status"],
                "explain": RUN_PROBLEM_TEXT.get(latest["status"], ""),
                "error": latest.get("error") or "",
            }
        last_run_errors = (latest.get("counts_dict") or {}).get("errors") or []

    ctx["progress"] = progress
    ctx["next_action"] = next_action
    ctx["filter_reasons"] = filter_reasons
    ctx["last_run_problem"] = last_run_problem
    ctx["last_run_errors"] = last_run_errors

    return templates.TemplateResponse(
        request=request, name="campaign_detail.html", context=ctx
    )


@router.post("/campaigns/{id}/run")
def run_campaign_discovery(
    id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    cur = conn.execute("SELECT * FROM campaigns WHERE id = ?", (id,))
    campaign = cur.fetchone()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    clock = SystemClock()
    simulated = settings.mode == "mock"
    reader = build_reader(conn, settings, clock)
    llm = build_llm(settings)

    try:
        run_id = run_discovery(
            conn=conn,
            campaign_id=id,
            reader=reader,
            llm=llm,
            settings=settings,
            clock=clock,
            simulated=simulated,
        )
    except RuntimeError as e:
        if "already in progress" in str(e).lower():
            err_msg = "A discovery run is already in progress for this campaign"
            return RedirectResponse(
                url=f"/campaigns/{id}?error={urllib.parse.quote(err_msg)}",
                status_code=303,
            )
        raise

    run_cur = conn.execute("SELECT * FROM discovery_runs WHERE id = ?", (run_id,))
    run_row = run_cur.fetchone()
    status = run_row["status"]
    try:
        counts = json.loads(run_row["counts"]) if run_row["counts"] else {}
    except Exception:
        counts = {}

    cost_cur = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE run_id = ?",
        (run_id,),
    )
    run_cost = float(cost_cur.fetchone()[0])

    found = counts.get("found", 0)
    scored = counts.get("scored", 0)
    filt_data = counts.get("filtered", {})
    filtered_sum = sum(filt_data.values()) if isinstance(filt_data, dict) else 0

    status_words = {
        "completed": "Discovery completed",
        "budget_reached": "Discovery stopped (budget reached)",
        "rate_limited": "Discovery stopped (rate limited)",
        "auth_error": "Discovery stopped (auth error)",
        "failed": "Discovery failed",
    }.get(status, f"Discovery {status}")

    msg = f"{status_words}: {found} found, {scored} scored, {filtered_sum} filtered (${run_cost:.4f} spent)"
    return RedirectResponse(
        url=f"/campaigns/{id}?msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.get("/costs", response_class=HTMLResponse)
def view_costs(
    request: Request,
    msg: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    real_total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE simulated = 0"
    ).fetchone()[0]
    simulated_total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE simulated = 1"
    ).fetchone()[0]

    real_by_endpoint = conn.execute(
        """
        SELECT endpoint, SUM(resource_count) as total_units, SUM(cost_usd) as total_cost
        FROM ledger
        WHERE simulated = 0
        GROUP BY endpoint
        ORDER BY total_cost DESC
        """
    ).fetchall()

    real_by_day = conn.execute(
        """
        SELECT SUBSTR(created_at, 1, 10) as day, SUM(resource_count) as total_units, SUM(cost_usd) as total_cost
        FROM ledger
        WHERE simulated = 0
        GROUP BY SUBSTR(created_at, 1, 10)
        ORDER BY day DESC
        """
    ).fetchall()

    sim_by_endpoint = conn.execute(
        """
        SELECT endpoint, SUM(resource_count) as total_units, SUM(cost_usd) as total_cost
        FROM ledger
        WHERE simulated = 1
        GROUP BY endpoint
        ORDER BY total_cost DESC
        """
    ).fetchall()

    sim_by_day = conn.execute(
        """
        SELECT SUBSTR(created_at, 1, 10) as day, SUM(resource_count) as total_units, SUM(cost_usd) as total_cost
        FROM ledger
        WHERE simulated = 1
        GROUP BY SUBSTR(created_at, 1, 10)
        ORDER BY day DESC
        """
    ).fetchall()

    ctx = _common_context(request, conn, settings, msg, error)
    ctx["real_total_cost"] = float(real_total)
    ctx["simulated_total_cost"] = float(simulated_total)
    ctx["real_by_endpoint"] = [dict(r) for r in real_by_endpoint]
    ctx["real_by_day"] = [dict(r) for r in real_by_day]
    ctx["simulated_by_endpoint"] = [dict(r) for r in sim_by_endpoint]
    ctx["simulated_by_day"] = [dict(r) for r in sim_by_day]

    return templates.TemplateResponse(request=request, name="costs.html", context=ctx)


@router.post("/campaigns/{id}/draft")
def draft_campaign_messages(
    id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    cur = conn.execute("SELECT * FROM campaigns WHERE id = ?", (id,))
    campaign = cur.fetchone()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    clock = SystemClock()
    simulated = settings.mode == "mock"
    llm = build_llm(settings)

    res = draft_campaign(conn, id, llm, settings, clock, simulated=simulated)
    if res.get("stopped_reason") == "budget_reached":
        msg = f"Campaign budget was reached. Drafted {res['drafted']}, no evidence for {res['no_evidence']}."
    else:
        msg = f"Drafted {res['drafted']}, no evidence for {res['no_evidence']}, skipped {res['skipped']}, errors {res['errors']}."

    return RedirectResponse(
        url=f"/campaigns/{id}?msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.get("/campaigns/{id}/review")
def review_campaign_drafts(
    id: int,
    request: Request,
    tab: str = Query("drafts"),
    msg: str | None = Query(None),
    error: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    cur = conn.execute("SELECT * FROM campaigns WHERE id = ?", (id,))
    campaign = cur.fetchone()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    cnt_drafts = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ? AND state = 'drafted'",
        (id,),
    ).fetchone()[0]
    cnt_approved = conn.execute(
        """
        SELECT count(*) FROM outreach o
        JOIN prospects p ON o.prospect_id = p.id
        WHERE p.campaign_id = ? AND o.state IN ('approved', 'cancelled')
        """,
        (id,),
    ).fetchone()[0]
    cnt_no_ev = conn.execute(
        "SELECT count(*) FROM prospects WHERE campaign_id = ? AND state = 'no_evidence'",
        (id,),
    ).fetchone()[0]

    ctx = _common_context(request, conn, settings, msg, error)
    ctx["campaign"] = dict(campaign)
    ctx["current_tab"] = tab if tab in ("drafts", "approved", "no_evidence") else "drafts"
    ctx["count_drafts"] = cnt_drafts
    ctx["count_approved"] = cnt_approved
    ctx["count_no_evidence"] = cnt_no_ev
    ctx["plain_reasons"] = PLAIN_REASONS

    if ctx["current_tab"] == "drafts":
        pros_cur = conn.execute(
            """
            SELECT p.*, u.username, u.name, u.bio, u.receives_your_dm, u.connection_status
            FROM prospects p
            JOIN x_users u ON p.x_user_id = u.x_user_id
            WHERE p.campaign_id = ? AND p.state = 'drafted'
            ORDER BY p.score DESC NULLS LAST, p.id DESC
            """,
            (id,),
        )
        drafts_list = []
        for p_row in pros_cur.fetchall():
            p_dict = dict(p_row)
            d = latest_draft(conn, p_dict["id"])
            if not d:
                continue

            if p_dict["receives_your_dm"] is True or p_dict["receives_your_dm"] == 1:
                can_dm = "yes"
            elif p_dict["receives_your_dm"] is False or p_dict["receives_your_dm"] == 0:
                can_dm = "no"
            else:
                can_dm = "unknown"

            conn_status = []
            if p_dict["connection_status"]:
                try:
                    conn_status = json.loads(p_dict["connection_status"])
                except Exception:
                    conn_status = []
            follows_you = "yes" if "followed_by" in conn_status else "no"

            fit_data = {}
            if p_dict["fit_reasons"]:
                try:
                    fit_data = json.loads(p_dict["fit_reasons"])
                except Exception:
                    fit_data = {}
            why_fit = fit_data.get("why_fit", [])
            if isinstance(why_fit, str):
                why_fit = [why_fit]

            ev_list = evidence_items(conn, p_dict["id"])
            ok, reason = can_approve(d)

            formatted_flags = []
            for f in d.get("validator_flags", []):
                code = f.get("code")
                level = f.get("level")
                detail = f.get("detail", "")
                text = PLAIN_FLAG_TEXTS.get(code, code)
                formatted_flags.append(
                    {
                        "level": level,
                        "code": code,
                        "detail": detail,
                        "text": text,
                    }
                )

            drafts_list.append(
                {
                    "prospect": p_dict,
                    "can_dm": can_dm,
                    "follows_you": follows_you,
                    "why_fit": why_fit,
                    "evidence": ev_list,
                    "draft": d,
                    "char_count": len(clean_message_text(d.get("text", ""))),
                    "can_approve_ok": ok,
                    "can_approve_reason": reason,
                    "flags": formatted_flags,
                }
            )
        ctx["drafts"] = drafts_list

    elif ctx["current_tab"] == "approved":
        app_cur = conn.execute(
            """
            SELECT o.id, o.state, o.final_text, o.approved_at, u.username, u.name
            FROM outreach o
            JOIN prospects p ON o.prospect_id = p.id
            JOIN x_users u ON p.x_user_id = u.x_user_id
            WHERE p.campaign_id = ? AND o.state IN ('approved', 'cancelled')
            ORDER BY o.id DESC
            """,
            (id,),
        )
        ctx["approved_list"] = [dict(r) for r in app_cur.fetchall()]

    elif ctx["current_tab"] == "no_evidence":
        no_ev_cur = conn.execute(
            """
            SELECT p.*, u.username, u.name, u.bio
            FROM prospects p
            JOIN x_users u ON p.x_user_id = u.x_user_id
            WHERE p.campaign_id = ? AND p.state = 'no_evidence'
            ORDER BY p.score DESC NULLS LAST, p.id DESC
            """,
            (id,),
        )
        no_ev_list = []
        for p_row in no_ev_cur.fetchall():
            p_dict = dict(p_row)
            fit_data = {}
            if p_dict["fit_reasons"]:
                try:
                    fit_data = json.loads(p_dict["fit_reasons"])
                except Exception:
                    fit_data = {}
            why_fit = fit_data.get("why_fit", [])
            if isinstance(why_fit, str):
                why_fit = [why_fit]
            p_dict["why_fit_lines"] = why_fit
            no_ev_list.append(p_dict)
        ctx["no_evidence_list"] = no_ev_list

    ctx["progress"] = campaign_progress(conn, id)
    return templates.TemplateResponse(
        request=request, name="review.html", context=ctx
    )


@router.post("/prospects/{pid}/draft")
def redraft_single_prospect(
    pid: int,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    cur = conn.execute("SELECT campaign_id FROM prospects WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    cid = row["campaign_id"]

    clock = SystemClock()
    simulated = settings.mode == "mock"
    llm = build_llm(settings)

    try:
        outcome = draft_prospect(conn, pid, llm, settings, clock, simulated=simulated)
    except Exception as e:
        err_msg = f"Redraft failed: {e}"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(err_msg)}",
            status_code=303,
        )

    if outcome == "no_evidence":
        msg = "Nothing specific enough to write about"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=no_evidence&msg={urllib.parse.quote(msg)}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=drafts&msg={urllib.parse.quote('Draft updated')}",
        status_code=303,
    )


@router.post("/prospects/{pid}/edit")
def edit_prospect_draft(
    pid: int,
    text: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    cur = conn.execute("SELECT campaign_id FROM prospects WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    cid = row["campaign_id"]

    clock = SystemClock()
    try:
        save_human_edit(conn, pid, text, clock)
    except Exception as e:
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(str(e))}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=drafts&msg={urllib.parse.quote('Draft edit saved')}",
        status_code=303,
    )


@router.post("/prospects/{pid}/approve")
def approve_prospect_draft(
    pid: int,
    text: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    cur = conn.execute("SELECT campaign_id FROM prospects WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    cid = row["campaign_id"]

    d = latest_draft(conn, pid)
    if not d:
        err = "Save your edit before approving"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(err)}",
            status_code=303,
        )

    stored_cleaned = clean_message_text(d.get("text", ""))
    submitted_cleaned = clean_message_text(text)
    if stored_cleaned != submitted_cleaned:
        err = "Save your edit before approving"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(err)}",
            status_code=303,
        )

    ok, reason = can_approve(d)
    if not ok:
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(reason)}",
            status_code=303,
        )

    clock = SystemClock()
    now_str = iso(clock.now())
    try:
        approval.approve_prospect(conn, pid, stored_cleaned, now_str)
    except AlreadyContacted:
        err = "Already messaged from this account"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(err)}",
            status_code=303,
        )
    except Suppressed:
        err = "On your do-not-contact list"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(err)}",
            status_code=303,
        )
    except TransitionError:
        err = "This draft was already handled"
        return RedirectResponse(
            url=f"/campaigns/{cid}/review?tab=drafts&error={urllib.parse.quote(err)}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=approved&msg={urllib.parse.quote('Draft approved')}",
        status_code=303,
    )


@router.post("/prospects/{pid}/reject")
def reject_prospect_draft(
    pid: int,
    reason: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    cur = conn.execute("SELECT campaign_id FROM prospects WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    cid = row["campaign_id"]

    clock = SystemClock()
    now_str = iso(clock.now())
    clean_reason = reason.strip()[:200] if reason else None
    with db.transaction(conn):
        transition(
            conn,
            "prospect",
            pid,
            "drafted",
            "rejected",
            "human",
            now_str,
            reason=clean_reason,
        )

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=drafts&msg={urllib.parse.quote('Prospect rejected')}",
        status_code=303,
    )


@router.post("/prospects/{pid}/do-not-contact")
def do_not_contact_prospect(
    pid: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    cur = conn.execute(
        "SELECT campaign_id, x_user_id, state FROM prospects WHERE id = ?",
        (pid,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Prospect not found")
    cid = row["campaign_id"]
    x_user_id = row["x_user_id"]

    clock = SystemClock()
    now_str = iso(clock.now())
    approval.add_suppression(conn, x_user_id, "manual", "manual", now_str)
    if row["state"] == "drafted":
        with db.transaction(conn):
            transition(
                conn,
                "prospect",
                pid,
                "drafted",
                "rejected",
                "human",
                now_str,
                reason="do_not_contact",
            )

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=drafts&msg={urllib.parse.quote('Added to do-not-contact list')}",
        status_code=303,
    )


@router.post("/outreach/{oid}/withdraw")
def withdraw_outreach(
    oid: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    cur = conn.execute(
        "SELECT p.campaign_id FROM outreach o JOIN prospects p ON o.prospect_id = p.id WHERE o.id = ?",
        (oid,),
    )
    row = cur.fetchone()
    cid = row["campaign_id"] if row else 1

    clock = SystemClock()
    now_str = iso(clock.now())
    approval.cancel_approval(conn, oid, now_str)

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=approved&msg={urllib.parse.quote('Approval withdrawn')}",
        status_code=303,
    )


@router.post("/outreach/{oid}/reapprove")
def reapprove_outreach(
    oid: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    cur = conn.execute(
        "SELECT p.campaign_id FROM outreach o JOIN prospects p ON o.prospect_id = p.id WHERE o.id = ?",
        (oid,),
    )
    row = cur.fetchone()
    cid = row["campaign_id"] if row else 1

    clock = SystemClock()
    now_str = iso(clock.now())
    approval.reapprove(conn, oid, now_str)

    return RedirectResponse(
        url=f"/campaigns/{cid}/review?tab=approved&msg={urllib.parse.quote('Message re-approved')}",
        status_code=303,
    )

