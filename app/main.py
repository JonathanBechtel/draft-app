"""Main entry point for FastAPI application."""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import DBAPIError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.routes import admin, export, news, players, podcasts, share, ui
from app.utils.db_async import (
    init_db,
    dispose_engine,
    describe_database_url,
    DATABASE_URL,
)

from app.logging_config import setup_logging
from app.config import settings

import logging

logger = logging.getLogger(__name__)

setup_logging(level=settings.log_level, access_log=settings.access_log)


@asynccontextmanager
async def lifespan(app: FastAPI):
    should_init_db = (
        settings.is_dev and settings.auto_init_db and not os.getenv("FLY_APP_NAME")
    )

    if should_init_db:
        logger.info("Running init_db()…")
        logger.info(f"DB target: {describe_database_url(DATABASE_URL)}")
        try:
            await init_db()
            logger.info("DB ready.")
        except Exception:
            logger.exception("init_db failed")
            raise
    else:
        logger.info(
            "Skipping init_db(); auto_init_db disabled or managed deployment detected"
        )

    # Hand control to the application
    yield

    # Shutdown: dispose engine cleanly
    try:
        logger.info("Disposing DB engine…")
        await dispose_engine()
        logger.info("DB engine disposed.")
    except Exception:
        logger.exception("Failed to dispose DB engine")


# load in app details
app = FastAPI(title="Mini Draft Guru", lifespan=lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])  # type: ignore[arg-type]
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.state.templates = Jinja2Templates(directory="app/templates")
app.include_router(export.router)
app.include_router(news.router)
app.include_router(podcasts.router)
app.include_router(players.router)
app.include_router(share.router)
app.include_router(ui.router)
app.include_router(admin.router)


@app.exception_handler(DBAPIError)
async def handle_dbapi_errors(request, exc: DBAPIError):  # type: ignore[no-untyped-def]
    message = str(exc)
    cache_error_markers = (
        "cache lookup failed for type",
        "InvalidCachedStatementError",
        "cached statement plan is invalid",
    )
    if any(marker in message for marker in cache_error_markers):
        # Self-heal after schema changes (e.g., enum migrations) by forcing new
        # connections on the next request.
        await dispose_engine()
        if request.method in {"GET", "HEAD"}:
            return RedirectResponse(url=str(request.url), status_code=307)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Database schema changed; connection caches were reset. Retry the request."
            },
        )
    raise exc


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
