import sqlite3
import urllib.parse
import zoneinfo
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clock import SystemClock, iso
from app.config import Settings, get_settings
from app.deps import current_sender, get_conn
from app.sending import run_send_batch
from app import approval
from app.routes import templates, _common_context
from app.state import TransitionError

router = APIRouter()

OUTREACH_REASONS = {
    "recipient_not_accepting": (
        "This person does not accept message requests from your account.",
        "Nothing to do now. Use Check again if they follow you or open their DMs.",
    ),
    "sender_not_verified": (
        "This person only accepts message requests from verified accounts.",
        "Verify the sending account on X, then use Check again.",
    ),
    "precheck_not_accepting": (
        "X reports that this person cannot receive DMs from your account.",
        "Nothing to do now. Use Check again later.",
    ),
    "suppressed": ("This person is on your do-not-contact list.", "No action."),
    "rate_limited": (
        "X asked the app to slow down before this message was sent.",
        "Press Send approved again later.",
    ),
    "interrupted": (
        "The app stopped while this message was being sent.",
        "Open the conversation on X and check whether it arrived.",
    ),
    "transport_error": (
        "The connection failed while sending. X may or may not have delivered it.",
        "Open the conversation on X and check whether it arrived.",
    ),
    "http_5xx": (
        "X returned a server error while sending. It may or may not have delivered it.",
        "Open the conversation on X and check whether it arrived.",
    ),
    "missing_event_id": (
        "X accepted the request but returned no message id.",
        "Open the conversation on X and check whether it arrived.",
    ),
    "auth_error": (
        "X rejected the app's access to your account.",
        "Reconnect X (top right), then use Retry.",
    ),
    "not_on_live_allowlist": (
        "This person is not on GROUNDLINE_LIVE_ALLOWED_HANDLES.",
        "Add the handle to the list and restart, or leave it unsent.",
    ),
}
DEFAULT_OUTREACH_REASON = (
    "The send did not complete.",
    "Use Retry. If it fails again, check the Costs page and X status.",
)

TAB_STATES = {
    "queue": ("approved",),
    "sent": ("sent",),
    "replies": ("replied",),
    "attention": ("unknown", "failed", "blocked"),
}


def _get_outreach_row_or_404(
    conn: sqlite3.Connection, oid: int, sender_id: int
) -> sqlite3.Row:
    cur = conn.execute(
        "SELECT * FROM outreach WHERE id = ? AND sender_account_id = ?",
        (oid, sender_id),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Outreach message not found")
    return row


@router.get("/limits", response_class=HTMLResponse)
def limits_page(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    ctx = _common_context(request, conn, settings)
    ctx["daily_cap"] = settings.daily_send_cap
    return templates.TemplateResponse(request=request, name="limits.html", context=ctx)


@router.get("/outreach", response_class=HTMLResponse)
def outreach_page(
    request: Request,
    tab: str = "queue",
    msg: str | None = None,
    error: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    if tab not in TAB_STATES:
        tab = "queue"

    allowed_handles = (
        sorted(
            {
                h.strip().lstrip("@").lower()
                for h in settings.live_allowed_handles.split(",")
                if h.strip()
            }
        )
        if settings.mode == "live"
        else []
    )

    sender = current_sender(conn, settings)
    if not sender:
        counts = {"queue": 0, "sent": 0, "replies": 0, "attention": 0}
        ctx = _common_context(request, conn, settings, msg=msg, error=error)
        ctx.update(
            {
                "tab": tab,
                "rows": [],
                "counts": counts,
                "sent_today": 0,
                "daily_cap": settings.daily_send_cap,
                "sending_now": False,
                "pace_seconds": settings.min_seconds_between_sends,
                "discovery_problems": [],
                "allowed_handles": [],
            }
        )
        return templates.TemplateResponse(request=request, name="outreach.html", context=ctx)

    sender_id = sender["id"]

    cur_q = conn.execute(
        "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND state = 'approved'",
        (sender_id,),
    )
    count_q = cur_q.fetchone()[0]

    cur_s = conn.execute(
        "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND state = 'sent'",
        (sender_id,),
    )
    count_s = cur_s.fetchone()[0]

    cur_r = conn.execute(
        "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND state = 'replied'",
        (sender_id,),
    )
    count_r = cur_r.fetchone()[0]

    cur_a = conn.execute(
        "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND state IN ('unknown', 'failed', 'blocked')",
        (sender_id,),
    )
    count_a = cur_a.fetchone()[0]

    counts = {
        "queue": count_q,
        "sent": count_s,
        "replies": count_r,
        "attention": count_a,
    }

    daily_cap = (
        min(sender["daily_send_cap"], settings.daily_send_cap)
        if sender["daily_send_cap"] is not None
        else settings.daily_send_cap
    )

    clock = SystemClock()
    tz = zoneinfo.ZoneInfo(settings.timezone)
    now_utc = clock.now()
    now_local = now_utc.astimezone(tz)
    start_of_day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_today_utc_iso = iso(start_of_day_local)

    cur_today = conn.execute(
        "SELECT COUNT(*) FROM outreach WHERE sender_account_id = ? AND "
        "((sent_at IS NOT NULL AND sent_at >= ?) OR state IN ('sending','unknown'))",
        (sender_id, start_of_today_utc_iso),
    )
    sent_today = cur_today.fetchone()[0]

    cur_lock = conn.execute(
        "SELECT 1 FROM send_locks WHERE sender_account_id = ? AND expires_at > ?",
        (sender_id, iso(now_utc)),
    )
    sending_now = cur_lock.fetchone() is not None

    states = TAB_STATES[tab]
    placeholders = ",".join("?" for _ in states)
    sql = f"""
    SELECT o.id, o.state, o.state_reason, o.final_text, o.approved_at, o.sent_at, o.updated_at, o.attempts,
           o.x_dm_conversation_id, u.username, u.name, c.id AS campaign_id, c.name AS campaign_name,
           (SELECT r.text FROM replies r WHERE r.outreach_id = o.id ORDER BY r.id DESC LIMIT 1) AS reply_note
    FROM outreach o
    JOIN prospects p ON o.prospect_id = p.id
    JOIN campaigns c ON p.campaign_id = c.id
    JOIN x_users u ON o.recipient_x_user_id = u.x_user_id
    WHERE o.sender_account_id = ? AND o.state IN ({placeholders})
    ORDER BY o.updated_at DESC, o.id DESC
    """
    cur_rows = conn.execute(sql, (sender_id, *states))
    raw_rows = cur_rows.fetchall()

    rows = []
    for r in raw_rows:
        row_dict = dict(r)
        state_reason = row_dict.get("state_reason") or ""
        reason_info = OUTREACH_REASONS.get(state_reason, DEFAULT_OUTREACH_REASON)
        row_dict["reason_text"] = reason_info[0]
        row_dict["next_step"] = reason_info[1]
        username = row_dict.get("username") or ""
        row_dict["profile_url"] = f"https://x.com/{username}"
        conv_id = row_dict.get("x_dm_conversation_id")
        row_dict["conversation_url"] = (
            f"https://x.com/messages/{conv_id}" if conv_id else None
        )
        row_dict["held_by_allowlist"] = bool(allowed_handles) and (
            username.lower() not in allowed_handles
        )
        rows.append(row_dict)

    discovery_problems = []
    if tab == "attention":
        cur_dp = conn.execute(
            """
            SELECT d.id, d.status, d.error, d.finished_at, c.id AS campaign_id, c.name AS campaign_name
            FROM discovery_runs d JOIN campaigns c ON d.campaign_id = c.id
            WHERE c.sender_account_id = ?
              AND d.id = (SELECT MAX(id) FROM discovery_runs WHERE campaign_id = c.id)
              AND d.status IN ('budget_reached', 'rate_limited', 'auth_error', 'failed')
            """,
            (sender_id,),
        )
        discovery_problems = cur_dp.fetchall()

    ctx = _common_context(request, conn, settings, msg=msg, error=error)
    ctx.update(
        {
            "tab": tab,
            "rows": rows,
            "counts": counts,
            "sent_today": sent_today,
            "daily_cap": daily_cap,
            "sending_now": sending_now,
            "pace_seconds": settings.min_seconds_between_sends,
            "discovery_problems": discovery_problems,
            "allowed_handles": allowed_handles,
        }
    )
    return templates.TemplateResponse(request=request, name="outreach.html", context=ctx)


@router.post("/outreach/send")
def outreach_send(
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    if settings.mode == "dry_run":
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('Dry-run mode never sends. Set GROUNDLINE_MODE=live to send.')}",
            status_code=303,
        )
    sender = current_sender(conn, settings)
    if not sender:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('Connect your X account first')}",
            status_code=303,
        )
    now_utc = SystemClock().now()
    cur_lock = conn.execute(
        "SELECT 1 FROM send_locks WHERE sender_account_id = ? AND expires_at > ?",
        (sender["id"], iso(now_utc)),
    )
    if cur_lock.fetchone() is not None:
        return RedirectResponse(
            url=f"/outreach?tab=queue&msg={urllib.parse.quote('Sending is already in progress. Refresh to see progress.')}",
            status_code=303,
        )
    cur_approved = conn.execute(
        "SELECT 1 FROM outreach WHERE sender_account_id = ? AND state = 'approved'",
        (sender["id"],),
    )
    if cur_approved.fetchone() is None:
        return RedirectResponse(
            url=f"/outreach?tab=queue&msg={urllib.parse.quote('Nothing approved to send.')}",
            status_code=303,
        )
    background_tasks.add_task(run_send_batch, settings, sender["id"])
    return RedirectResponse(
        url=f"/outreach?tab=sent&msg={urllib.parse.quote('Sending started. Messages go out one at a time. Refresh this page to see progress.')}",
        status_code=303,
    )


@router.post("/outreach/{oid}/replied")
def outreach_replied(
    oid: int,
    note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    sender = current_sender(conn, settings)
    if not sender:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('Connect your X account first')}",
            status_code=303,
        )
    _get_outreach_row_or_404(conn, oid, sender["id"])
    now_str = iso(SystemClock().now())
    try:
        approval.mark_replied_with_note(conn, oid, note, now_str)
    except TransitionError:
        return RedirectResponse(
            url=f"/outreach?tab=replies&error={urllib.parse.quote('This message changed state. Refresh and try again.')}",
            status_code=303,
        )
    return RedirectResponse(url="/outreach?tab=replies", status_code=303)


@router.post("/outreach/{oid}/resolve")
def outreach_resolve(
    oid: int,
    delivered: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    sender = current_sender(conn, settings)
    if not sender:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('Connect your X account first')}",
            status_code=303,
        )
    _get_outreach_row_or_404(conn, oid, sender["id"])
    if delivered not in ("yes", "no"):
        return RedirectResponse(
            url=f"/outreach?tab=attention&error={urllib.parse.quote('Invalid value for delivered')}",
            status_code=303,
        )
    now_str = iso(SystemClock().now())
    try:
        approval.resolve_unknown(conn, oid, delivered == "yes", now_str)
    except TransitionError:
        return RedirectResponse(
            url=f"/outreach?tab=attention&error={urllib.parse.quote('This message changed state. Refresh and try again.')}",
            status_code=303,
        )
    return RedirectResponse(url="/outreach?tab=attention", status_code=303)


@router.post("/outreach/{oid}/retry")
def outreach_retry(
    oid: int,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    sender = current_sender(conn, settings)
    if not sender:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('Connect your X account first')}",
            status_code=303,
        )
    _get_outreach_row_or_404(conn, oid, sender["id"])
    now_str = iso(SystemClock().now())
    try:
        approval.retry_failed(conn, oid, now_str)
    except TransitionError:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('This message changed state. Refresh and try again.')}",
            status_code=303,
        )
    return RedirectResponse(url="/outreach?tab=queue", status_code=303)


@router.post("/outreach/{oid}/recheck")
def outreach_recheck(
    oid: int,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    sender = current_sender(conn, settings)
    if not sender:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('Connect your X account first')}",
            status_code=303,
        )
    _get_outreach_row_or_404(conn, oid, sender["id"])
    now_str = iso(SystemClock().now())
    try:
        approval.recheck_blocked(conn, oid, now_str)
    except TransitionError:
        return RedirectResponse(
            url=f"/outreach?tab=queue&error={urllib.parse.quote('This message changed state. Refresh and try again.')}",
            status_code=303,
        )
    return RedirectResponse(url="/outreach?tab=queue", status_code=303)
