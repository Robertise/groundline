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


@dataclass(frozen=True)
class XUser:
    id: str
    username: str
    name: str = ""
    bio: str = ""
    protected: bool = False
    location: str = ""
    followers_count: int | None = None
    receives_your_dm: bool | None = None  # None when X omits it
    connection_status: tuple[str, ...] = ()


@dataclass(frozen=True)
class XPost:
    id: str
    author_id: str
    text: str
    created_at: str  # iso() string


class XApiError(Exception):
    def __init__(self, status: int, problem_type: str = "", detail: str = ""):
        super().__init__(f"X API error {status}: {problem_type} - {detail}")
        self.status = status
        self.problem_type = problem_type
        self.detail = detail


class XRateLimited(XApiError):
    def __init__(self, reset_at: str | None, detail: str = ""):
        super().__init__(429, "rate_limited", detail)
        self.reset_at = reset_at


class XAuthError(XApiError):
    def __init__(
        self, status: int = 401, problem_type: str = "auth_error", detail: str = ""
    ):
        super().__init__(status, problem_type, detail)


class XReader(Protocol):
    def me(self) -> XUser: ...
    def users_by_usernames(self, usernames: list[str]) -> list[XUser]: ...
    def search_recent(
        self, query: str, max_posts: int
    ) -> tuple[list[XPost], list[XUser]]: ...
    def following(self, user_id: str, max_users: int) -> list[XUser]: ...
    def followers(self, user_id: str, max_users: int) -> list[XUser]: ...
    def user_posts(self, user_id: str, max_posts: int) -> list[XPost]: ...

