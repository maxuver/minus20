import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from .config import settings
from .models import AlertmanagerWebhook
from .queue import AlertQueue

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("ingest-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.queue = AlertQueue()
    yield
    await app.state.queue.close()


app = FastAPI(
    title="Minus20 ingest-api",
    description="Receives Alertmanager webhooks and enqueues alerts for analysis.",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict:
    try:
        await app.state.queue.ping()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"redis unavailable: {exc}",
        ) from exc
    return {"status": "ready"}


@app.post("/webhook/alertmanager", status_code=status.HTTP_202_ACCEPTED)
async def receive_alertmanager(payload: AlertmanagerWebhook) -> dict:
    """Validate and enqueue every alert in the notification.

    Deliberately does nothing else: analysis must never slow down or block
    ingestion (ADR-0003).
    """
    queued = 0
    for alert in payload.alerts:
        entry = alert.model_dump(mode="json")
        entry["groupKey"] = payload.groupKey
        entry["receiver"] = payload.receiver
        await app.state.queue.publish(entry)
        queued += 1
    logger.info("queued %d alert(s), groupKey=%s", queued, payload.groupKey)
    return {"queued": queued}
