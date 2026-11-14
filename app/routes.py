import json
from pathlib import Path
import sqlite3
import urllib.parse
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.clock import SystemClock, iso
from app.config import Settings, get_settings, missing_live_settings
from app.deps import build_llm, build_reader, current_sender, get_conn
from app.discovery import run_discovery
from app.ledger import estimate_campaign_max_cost, spent
from app import db

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
        p_dict["evidence_items"] = [dict(ev) for ev in ev_cur.fetchall()]
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
