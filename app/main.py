"""
Main entry point for FastAPI application.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routes import players, ui
from app.utils.db_async import init_db, dispose_engine, describe_database_url, DATABASE_URL

from app.logging_config import setup_logging
from app.config import settings

import logging
logger = logging.getLogger(__name__)

setup_logging(level=settings.log_level, access_log=settings.access_log)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables in development only
    if settings.is_dev:
        logger.info("Running init_db()…")
        logger.info(f"DB target: {describe_database_url(DATABASE_URL)}")
        try:
            await init_db()
            logger.info("DB ready.")
        except Exception:
            logger.exception("init_db failed")
            raise
    else:
        logger.info("Skipping init_db() outside development environment")

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
app = FastAPI(title = "Mini Draft Guru", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.state.templates = Jinja2Templates(directory="app/templates")
app.include_router(players.router)
app.include_router(ui.router)

@app.get("/health")
async def health_check():
    """Health Check Endpoint"""
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
