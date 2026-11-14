import logging
import time

from app.clock import SystemClock
from app.config import Settings
from app.db import connect
from app.deps import build_transport
from app.sender import send_batch, BatchResult

logger = logging.getLogger(__name__)


def run_send_batch(settings: Settings, sender_account_id: int) -> BatchResult | None:
    """Runs one send batch on its own connection. Never raises."""
    conn = connect(settings.db_path)
    try:
        clock = SystemClock()
        transport = build_transport(conn, settings, clock)
        sleep = (lambda _s: None) if settings.mode == "mock" else time.sleep
        result = send_batch(conn, sender_account_id, transport, clock, settings, sleep=sleep)
        logger.info("send batch finished: %s", result)
        return result
    except Exception:
        logger.exception("send batch failed")
        return None
    finally:
        conn.close()
