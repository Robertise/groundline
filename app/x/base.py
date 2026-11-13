from dataclasses import dataclass
from typing import Literal, Protocol

# Reason codes. Use these exact strings everywhere.
REASON_RECIPIENT_NOT_ACCEPTING = "recipient_not_accepting"  # X problem type dm-recipient-not-accepting
REASON_SENDER_NOT_VERIFIED = "sender_not_verified"  # X problem type dm-sender-not-verified
REASON_PRECHECK_NOT_ACCEPTING = "precheck_not_accepting"  # receives_your_dm was false
REASON_SUPPRESSED = "suppressed"
REASON_RATE_LIMITED = "rate_limited"
REASON_INTERRUPTED = "interrupted"  # found in 'sending' at startup
REASON_TRANSPORT_ERROR = "transport_error"  # exception during send: delivery uncertain


@dataclass(frozen=True)
class Reachability:
    can_dm: bool | None  # None = could not determine
    detail: str = ""


@dataclass(frozen=True)
class SendOutcome:
    kind: Literal["sent", "blocked", "failed", "rate_limited", "unknown"]
    reason: str | None = None
    detail: str = ""
    dm_event_id: str | None = None
    dm_conversation_id: str | None = None


class XTransport(Protocol):
    def check_recipient(
        self, sender_account_id: int, recipient_x_user_id: str
    ) -> Reachability: ...
    def send_dm(
        self, sender_account_id: int, recipient_x_user_id: str, text: str
    ) -> SendOutcome: ...
