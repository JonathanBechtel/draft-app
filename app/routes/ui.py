from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render the Home Page"""

    players = []
    return request.app.state.TemplateResponse("base.html",
            {"request": request, "players": players})

