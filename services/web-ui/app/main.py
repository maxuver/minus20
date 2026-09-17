"""Read-only web view of the incident history.

Deliberately small: one page, no authentication, no write path. It exists so an
engineer can see what Minus20 concluded and why, without digging through
chat history.

SECURITY: there is no authentication in this iteration. Do not expose this
service outside the cluster — keep it on a ClusterIP Service and reach it with
`kubectl port-forward`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import IncidentReader

logging.basicConfig(level=settings.log_level)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.reader = IncidentReader()
    yield


app = FastAPI(
    title="Minus20 incident history",
    description="Read-only view of analysed incidents.",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict:
    # Checks that the database answers, not that the incidents table exists:
    # on a fresh install the worker has not created it yet, and the UI is
    # still perfectly able to serve an empty page.
    await app.state.reader.ping()
    return {"status": "ready"}


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    namespace: str = Query("", description="filter by namespace"),
    status: str = Query("", description="filter by incident status"),
):
    reader = app.state.reader
    incidents = await reader.list_incidents(namespace=namespace, status=status)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "incidents": incidents,
            "namespaces": await reader.namespaces(),
            "summary": await reader.summary(),
            "selected_namespace": namespace,
            "selected_status": status,
        },
    )
