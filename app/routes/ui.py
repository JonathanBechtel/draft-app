"""UI Routes - Renders Jinja templates for the frontend."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

# Footer columns - shared across all pages
FOOTER_COLUMNS = [
    {
        "title": "About",
        "links": [
            {"text": "Our Story", "url": "#"},
            {"text": "Team", "url": "#"},
            {"text": "Careers", "url": "#"},
            {"text": "Press", "url": "#"},
        ],
    },
    {
        "title": "Resources",
        "links": [
            {"text": "Draft Guide", "url": "#"},
            {"text": "Mock Drafts", "url": "#"},
            {"text": "Player Rankings", "url": "#"},
            {"text": "Analytics", "url": "#"},
        ],
    },
    {
        "title": "Community",
        "links": [
            {"text": "Forums", "url": "#"},
            {"text": "Discord", "url": "#"},
            {"text": "Twitter", "url": "#"},
            {"text": "Newsletter", "url": "#"},
        ],
    },
    {
        "title": "Legal",
        "links": [
            {"text": "Terms of Service", "url": "#"},
            {"text": "Privacy Policy", "url": "#"},
            {"text": "Cookie Policy", "url": "#"},
            {"text": "Disclaimer", "url": "#"},
        ],
    },
]


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render the Homepage with consensus mock draft, prospects, VS arena, and news feed."""

    # Hardcoded placeholder data - backend connection added later
    mock_picks: list[dict[str, Any]] = [
        {
            "pick": 1,
            "name": "Cooper Flagg",
            "slug": "cooper-flagg",
            "position": "F",
            "college": "Duke",
            "avgRank": 1.2,
            "change": 1,
        },
        {
            "pick": 2,
            "name": "Ace Bailey",
            "slug": "ace-bailey",
            "position": "G/F",
            "college": "Rutgers",
            "avgRank": 2.8,
            "change": -1,
        },
        {
            "pick": 3,
            "name": "Dylan Harper",
            "slug": "dylan-harper",
            "position": "G",
            "college": "Rutgers",
            "avgRank": 3.4,
            "change": 2,
        },
        {
            "pick": 4,
            "name": "VJ Edgecombe",
            "slug": "vj-edgecombe",
            "position": "G",
            "college": "Baylor",
            "avgRank": 4.1,
            "change": 0,
        },
        {
            "pick": 5,
            "name": "Kon Knueppel",
            "slug": "kon-knueppel",
            "position": "G/F",
            "college": "Duke",
            "avgRank": 5.7,
            "change": -2,
        },
    ]

    players = [
        {
            "name": p["name"],
            "slug": p["slug"],
            "position": p["position"],
            "college": p["college"],
            "img": f"https://placehold.co/320x420/edf2f7/1f2937?text={str(p['name']).replace(' ', '+')}",
            "change": p["change"],
            "measurables": {
                "ht": 80 + (int(p["pick"]) % 3),
                "ws": 84 + (int(p["pick"]) % 5),
                "vert": 34 + (int(p["pick"]) % 7),
            },
        }
        for p in mock_picks
    ]

    feed_items = [
        {
            "source": "@DraftExpress",
            "title": "Ace Bailey wingspan causes chaos at Elite Camp",
            "time": "3m",
            "tag": "Riser",
        },
        {
            "source": "No Ceilings",
            "title": "Dylan Harper rim deterrence study: early returns",
            "time": "12m",
            "tag": "Riser",
        },
        {
            "source": "The Ringer",
            "title": "VJ Edgecombe fit questions with top-5 teams",
            "time": "27m",
            "tag": "Faller",
        },
        {
            "source": "BR",
            "title": "Flagg off-ball value and scheme versatility",
            "time": "1h",
            "tag": "Riser",
        },
    ]

    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "mock_picks": mock_picks,
            "players": players,
            "feed_items": feed_items,
            "footer_columns": FOOTER_COLUMNS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/players/{slug}", response_class=HTMLResponse)
async def player_detail(request: Request, slug: str):
    """Render the Player Detail page with bio, scoreboard, percentiles, comps, and news.

    Uses slug-based routing (e.g., /players/cooper-flagg).
    For duplicate names, append a numeric suffix (e.g., john-smith-2).
    """

    # Hardcoded placeholder data - backend connection added later
    # In production, fetch player by slug from database
    player = {
        "id": 1,
        "slug": slug,
        "name": "Cooper Flagg",
        "position": "Forward",
        "college": "Duke",
        "height": "6'9\"",
        "weight": "205 lbs",
        "age": 18,
        "class": "Freshman",
        "hometown": "Newport, ME",
        "wingspan": "7'2\"",
        "photo_url": "https://placehold.co/400x533/edf2f7/1f2937?text=Cooper+Flagg",
        "metrics": {
            "consensusRank": 1,
            "consensusChange": 1,
            "buzzScore": 94,
            "truePosition": 1.0,
            "trueRange": 0.3,
            "winsAdded": 8.2,
            "trendDirection": "rising",
        },
    }

    percentile_data = {
        "anthropometrics": [
            {"metric": "Height", "value": "6'9\"", "percentile": 92, "unit": ""},
            {"metric": "Weight", "value": "205", "percentile": 78, "unit": " lbs"},
            {"metric": "Wingspan", "value": "7'2\"", "percentile": 95, "unit": ""},
            {
                "metric": "Standing Reach",
                "value": "9'2\"",
                "percentile": 94,
                "unit": "",
            },
        ],
        "combinePerformance": [
            {
                "metric": "Lane Agility",
                "value": "10.84",
                "percentile": 89,
                "unit": " sec",
            },
            {"metric": "3/4 Sprint", "value": "3.15", "percentile": 91, "unit": " sec"},
            {
                "metric": "Max Vertical",
                "value": "36.0",
                "percentile": 87,
                "unit": " in",
            },
            {
                "metric": "Standing Vertical",
                "value": "32.5",
                "percentile": 85,
                "unit": " in",
            },
        ],
        "advancedStats": [
            {
                "metric": "Points Per Game",
                "value": "21.4",
                "percentile": 96,
                "unit": " PPG",
            },
            {
                "metric": "Rebounds Per Game",
                "value": "8.9",
                "percentile": 93,
                "unit": " RPG",
            },
            {
                "metric": "Assists Per Game",
                "value": "4.2",
                "percentile": 88,
                "unit": " APG",
            },
            {"metric": "PER", "value": "28.6", "percentile": 97, "unit": ""},
        ],
    }

    comparison_data = [
        {
            "name": "Chet Holmgren",
            "position": "F/C",
            "school": "Gonzaga (2022)",
            "similarity": 92,
            "img": "https://placehold.co/320x420/edf2f7/1f2937?text=Chet+Holmgren",
            "stats": {"ht": "84", "ws": "90", "vert": "28"},
        },
        {
            "name": "Jaren Jackson Jr.",
            "position": "F",
            "school": "Michigan State (2018)",
            "similarity": 87,
            "img": "https://placehold.co/320x420/edf2f7/1f2937?text=Jaren+Jackson+Jr",
            "stats": {"ht": "83", "ws": "88", "vert": "33"},
        },
        {
            "name": "Evan Mobley",
            "position": "F/C",
            "school": "USC (2021)",
            "similarity": 85,
            "img": "https://placehold.co/320x420/edf2f7/1f2937?text=Evan+Mobley",
            "stats": {"ht": "84", "ws": "87", "vert": "30"},
        },
        {
            "name": "Paolo Banchero",
            "position": "F",
            "school": "Duke (2022)",
            "similarity": 78,
            "img": "https://placehold.co/320x420/edf2f7/1f2937?text=Paolo+Banchero",
            "stats": {"ht": "82", "ws": "85", "vert": "29"},
        },
    ]

    player_feed = [
        {
            "source": "@DraftExpress",
            "title": "Flagg dominates Duke scrimmage with 28 points",
            "time": "2h",
            "tag": "Highlight",
        },
        {
            "source": "The Athletic",
            "title": "Breaking down Flagg's defensive versatility",
            "time": "5h",
            "tag": "Analysis",
        },
        {
            "source": "ESPN",
            "title": "Why Flagg is the consensus #1 pick",
            "time": "1d",
            "tag": "Riser",
        },
    ]

    return request.app.state.templates.TemplateResponse(
        "player-detail.html",
        {
            "request": request,
            "player": player,
            "percentile_data": percentile_data,
            "comparison_data": comparison_data,
            "player_feed": player_feed,
            "footer_columns": FOOTER_COLUMNS,
            "current_year": datetime.now().year,
        },
    )
