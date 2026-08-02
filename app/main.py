"""Main entry point for FastAPI application."""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import DBAPIError
from starlette.responses import Response
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.routes import (
    admin,
    consensus,
    export,
    news,
    players,
    podcasts,
    seo,
    share,
    stats,
    summer_league,
    summer_league_trends,
    ui,
    videos,
)
from app.utils.db_async import (
    check_database_readiness,
    init_db,
    dispose_engine,
    describe_database_url,
    DATABASE_URL,
)

from app.logging_config import setup_logging
from app.config import settings
from app.templating import register_template_filters

import logging

logger = logging.getLogger(__name__)

setup_logging(level=settings.log_level, access_log=settings.access_log)

access_logger = logging.getLogger("app.access")


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


if settings.log_requests:

    @app.middleware("http")
    async def log_request_metadata(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        # Skip noisy paths so the bot signal isn't drowned out. Covers both health
        # probes, which monitoring polls on a fixed interval.
        path = request.url.path
        if path == "/health" or path.startswith(("/health/", "/static/")):
            return await call_next(request)

        # Fly populates Fly-Client-IP with the real client; X-Forwarded-For is
        # the fallback if anything else fronts the app later.
        client_ip = (
            request.headers.get("fly-client-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "-")
        )
        ua = request.headers.get("user-agent", "-")
        response = await call_next(request)
        access_logger.info(
            '%s "%s %s" %d ua="%s"',
            client_ip,
            request.method,
            path,
            response.status_code,
            ua,
        )
        return response


app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.state.templates = Jinja2Templates(directory="app/templates")
register_template_filters(app.state.templates.env)
app.include_router(consensus.router)
app.include_router(export.router)
app.include_router(news.router)
app.include_router(podcasts.router)
app.include_router(videos.router)
app.include_router(players.router)
app.include_router(share.router)
app.include_router(summer_league.router)
app.include_router(summer_league_trends.router)
app.include_router(stats.router)
app.include_router(seo.router)
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
    """Liveness probe — process is up and serving.

    Deliberately touches no database. This is what an orchestrator should restart a
    machine on, and a database outage is not a reason to cycle every web machine.
    Use ``/health/db`` to answer "is this instance actually able to serve traffic".
    """
    return {"status": "ok"}


@app.get("/health/db")
async def database_health_check():
    """Readiness probe — the instance can reach the database and get a connection.

    Incident #669 ran for ~96 minutes with DB-backed public routes returning 500 while
    ``/health`` stayed green the entire time, because it exercises no query. This probe
    is the operational signal that could have gone red: it runs a bounded ``SELECT 1``
    through the application's own pool and reports 503 when that fails, so a saturated
    pool or an unreachable database is visible to monitoring rather than inferred from
    user reports.
    """
    report = await check_database_readiness()
    payload = {
        "status": "ok" if report.database_ok else "unavailable",
        "database_ok": report.database_ok,
        "latency_ms": report.latency_ms,
        "pool": report.pool,
    }
    if report.error:
        payload["error"] = report.error
    return JSONResponse(status_code=200 if report.database_ok else 503, content=payload)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
