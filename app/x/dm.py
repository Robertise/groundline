import httpx

from app.x.base import (
    Reachability,
    SendOutcome,
    XApiError,
    XAuthError,
    XRateLimited,
    REASON_RECIPIENT_NOT_ACCEPTING,
    REASON_SENDER_NOT_VERIFIED,
    REASON_RATE_LIMITED,
)

REASON_AUTH_ERROR = "auth_error"
REASON_MISSING_EVENT_ID = "missing_event_id"
REASON_NOT_ON_ALLOWLIST = "not_on_live_allowlist"


class XDMTransport:
    """Sends DMs with the connected sender's token. Implements app.x.base.XTransport."""

    def __init__(self, client, allowed_user_ids: set[str] | None = None):
        self.client = client
        self.allowed_user_ids = allowed_user_ids  # None = no allowlist

    def check_recipient(self, sender_account_id: int, recipient_x_user_id: str) -> Reachability:
        if self.allowed_user_ids is not None and recipient_x_user_id not in self.allowed_user_ids:
            return Reachability(can_dm=None, detail=REASON_NOT_ON_ALLOWLIST)
        try:
            user = self.client.user_by_id(recipient_x_user_id)
        except (XApiError, httpx.HTTPError) as exc:
            return Reachability(can_dm=None, detail=repr(exc)[:200])
        if user is None:
            return Reachability(can_dm=None, detail="user_not_found")
        return Reachability(can_dm=user.receives_your_dm)

    def send_dm(self, sender_account_id: int, recipient_x_user_id: str, text: str) -> SendOutcome:
        if self.allowed_user_ids is not None and recipient_x_user_id not in self.allowed_user_ids:
            return SendOutcome("failed", reason=REASON_NOT_ON_ALLOWLIST)
        try:
            data = self.client.send_dm(recipient_x_user_id, text)
        except XRateLimited:
            return SendOutcome("rate_limited", reason=REASON_RATE_LIMITED)
        except XAuthError as exc:
            return SendOutcome("failed", reason=REASON_AUTH_ERROR, detail=exc.detail[:200])
        except XApiError as exc:
            marker = f"{exc.problem_type} {exc.detail}"
            if exc.status == 403 and "dm-recipient-not-accepting" in marker:
                return SendOutcome("blocked", reason=REASON_RECIPIENT_NOT_ACCEPTING)
            elif exc.status == 403 and "dm-sender-not-verified" in marker:
                return SendOutcome("blocked", reason=REASON_SENDER_NOT_VERIFIED)
            elif exc.status >= 500:
                return SendOutcome("unknown", reason="http_5xx", detail=exc.detail[:200])
            else:
                return SendOutcome("failed", reason=f"http_{exc.status}", detail=exc.detail[:200])

        event_id = data.get("dm_event_id")
        if not event_id:
            return SendOutcome("unknown", reason=REASON_MISSING_EVENT_ID)
        return SendOutcome(
            "sent",
            dm_event_id=str(event_id),
            dm_conversation_id=data.get("dm_conversation_id"),
        )
