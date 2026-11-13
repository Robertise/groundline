import threading
import uuid
from app.x.base import (
    Reachability,
    SendOutcome,
    REASON_RECIPIENT_NOT_ACCEPTING,
    REASON_SENDER_NOT_VERIFIED,
    REASON_RATE_LIMITED,
)


class MockTransportError(Exception):
    pass


class SimulatedCrash(BaseException):
    pass


FAULT_KINDS = (
    "ok",
    "not_accepting",
    "not_verified",
    "rate_limited",
    "http_400",
    "error_before_delivery",
    "timeout_after_delivery",
    "crash_after_delivery",
)


class MockTransport:
    def __init__(
        self,
        faults: list[str] | None = None,
        reachability: dict[str, bool | None] | None = None,
    ):
        self._faults: list[str] = list(faults) if faults is not None else []
        self._reachability: dict[str, bool | None] = (
            dict(reachability) if reachability is not None else {}
        )
        self.delivered: list[tuple[str, str]] = []
        self.send_calls: int = 0
        self.check_calls: int = 0
        self._lock = threading.Lock()
        self._event_counter = 0

    def delivered_count(self, recipient_x_user_id: str) -> int:
        with self._lock:
            return sum(1 for r, _ in self.delivered if r == recipient_x_user_id)

    def check_recipient(
        self, sender_account_id: int, recipient_x_user_id: str
    ) -> Reachability:
        with self._lock:
            self.check_calls += 1
            can_dm = self._reachability.get(recipient_x_user_id, True)
            return Reachability(can_dm=can_dm)

    def send_dm(
        self, sender_account_id: int, recipient_x_user_id: str, text: str
    ) -> SendOutcome:
        with self._lock:
            self.send_calls += 1
            if self._faults:
                kind = self._faults.pop(0)
            else:
                kind = "ok"

            if kind == "ok":
                self._event_counter += 1
                self.delivered.append((recipient_x_user_id, text))
                return SendOutcome(
                    kind="sent",
                    dm_event_id=f"evt_{self._event_counter}_{uuid.uuid4().hex[:8]}",
                    dm_conversation_id=f"{sender_account_id}-{recipient_x_user_id}",
                )
            elif kind == "not_accepting":
                return SendOutcome(
                    kind="blocked", reason=REASON_RECIPIENT_NOT_ACCEPTING
                )
            elif kind == "not_verified":
                return SendOutcome(
                    kind="blocked", reason=REASON_SENDER_NOT_VERIFIED
                )
            elif kind == "rate_limited":
                return SendOutcome(
                    kind="rate_limited", reason=REASON_RATE_LIMITED
                )
            elif kind == "http_400":
                return SendOutcome(kind="failed", reason="http_400")
            elif kind == "error_before_delivery":
                raise MockTransportError("Simulated error before delivery")
            elif kind == "timeout_after_delivery":
                self._event_counter += 1
                self.delivered.append((recipient_x_user_id, text))
                raise TimeoutError("Simulated timeout after delivery")
            elif kind == "crash_after_delivery":
                self._event_counter += 1
                self.delivered.append((recipient_x_user_id, text))
                raise SimulatedCrash("Simulated crash after delivery")
            else:
                raise ValueError(f"Unknown fault kind: {kind}")
