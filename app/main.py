import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config import get_settings
from app.clock import SystemClock
from app.db import connect, init_db
from app.sender import recover_on_startup

logger = logging.getLogger(__name__)

recovered_count = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global recovered_count
    settings = get_settings()
    conn = connect(settings.db_path)
    init_db(conn)
    recovered_count = recover_on_startup(conn, SystemClock())
    logger.info("Recovered %d rows on startup", recovered_count)
    yield
    conn.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "mode": settings.mode,
        "recovered_on_startup": recovered_count,
    }
