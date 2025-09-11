"""
Main entry point for FastAPI application.
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routes import players, ui

# load in app details
app = FastAPI(title = "Mini Draft Guru")
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
    uvicorn.run(app, host="0.0.0.0", port=8000)