"""
Main entry point for FastAPI application.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routes import players, ui
from app.utils.db_async import init_db

from app.logging import setup_logging
from app.config import settings

import logging
logger = logging.getLogger(__name__)

setup_logging(level=settings.log_level, access_log=settings.access_log)

# load in app details
app = FastAPI(title = "Mini Draft Guru")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.state.templates = Jinja2Templates(directory="app/templates")
app.include_router(players.router)
app.include_router(ui.router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Running init_db()…")
    try:
        await init_db()
        logger.info("DB ready.")
    except Exception:
        logger.exception("init_db failed")
        raise
    yield

@app.get("/health")
async def health_check():
    """Health Check Endpoint"""
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)