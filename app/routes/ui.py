"""UI Routes - Renders Jinja templates for the frontend."""

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.player_service import get_player_profile_by_slug
from app.utils.db_async import get_session
from app.utils.images import get_player_photo_url

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
async def home(
    request: Request,
    style: Optional[str] = Query(
        None, description="Image style: default, vector, comic, retro"
    ),
    db: AsyncSession = Depends(get_session),
):
    """Render the Homepage with consensus mock draft, prospects, VS arena, and news feed."""
    from sqlalchemy import select

    from app.schemas.players_master import PlayerMaster

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

    # Look up player IDs from database for image URL generation
    slugs = [p["slug"] for p in mock_picks]
    result = await db.execute(
        select(PlayerMaster.id, PlayerMaster.slug, PlayerMaster.display_name).where(  # type: ignore[call-overload]
            PlayerMaster.slug.in_(slugs)  # type: ignore[union-attr]
        )
    )
    player_id_map = {row.slug: (row.id, row.display_name) for row in result}

    players = []
    for p in mock_picks:
        player_info = player_id_map.get(p["slug"])
        if player_info:
            player_id, display_name = player_info
            img_url = get_player_photo_url(player_id, display_name, style)
        else:
            # Fallback to placeholder if player not in database
            img_url = f"https://placehold.co/320x420/edf2f7/1f2937?text={str(p['name']).replace(' ', '+')}"

        players.append(
            {
                "name": p["name"],
                "slug": p["slug"],
                "position": p["position"],
                "college": p["college"],
                "img": img_url,
                "change": p["change"],
                "measurables": {
                    "ht": 80 + (int(p["pick"]) % 3),
                    "ws": 84 + (int(p["pick"]) % 5),
                    "vert": 34 + (int(p["pick"]) % 7),
                },
            }
        )

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

    # Build a slug->id map for JS to use when generating image URLs dynamically
    slug_to_id = {slug: info[0] for slug, info in player_id_map.items()}

    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "mock_picks": mock_picks,
            "players": players,
            "feed_items": feed_items,
            "footer_columns": FOOTER_COLUMNS,
            "current_year": datetime.now().year,
            "image_style": style,  # Current image style for JS
            "player_id_map": slug_to_id,  # slug -> player_id for JS image URLs
        },
    )


@router.get("/players/{slug}", response_class=HTMLResponse)
async def player_detail(
    request: Request,
    slug: str,
    style: Optional[str] = Query(
        None, description="Image style: default, vector, comic, retro"
    ),
    db: AsyncSession = Depends(get_session),
):
    """Render the Player Detail page with bio, scoreboard, percentiles, comps, and news.

    Uses slug-based routing (e.g., /players/cooper-flagg).
    For duplicate names, append a numeric suffix (e.g., john-smith-2).
    """
    # Fetch player profile from database
    player_profile = await get_player_profile_by_slug(db, slug)

    if not player_profile:
        raise HTTPException(status_code=404, detail="Player not found")

    # Helper to filter out literal "null"/empty strings from raw data
    def clean_null(value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or text.lower() in {"null", "none"}:
            return None
        return text

    # Build player dict for template
    player_name = player_profile.display_name or "Unknown Player"
    player = {
        "id": player_profile.id,
        "slug": player_profile.slug,
        "name": player_name,
        "position": player_profile.position,
        "college": clean_null(player_profile.school),
        "high_school": clean_null(player_profile.high_school),
        "shoots": clean_null(player_profile.shoots),
        "height": player_profile.height_formatted,
        "weight": player_profile.weight_formatted,
        "age": player_profile.age_formatted,
        "hometown": player_profile.hometown,
        "wingspan": player_profile.wingspan_formatted,
        # Use style param if provided, otherwise use default from profile
        "photo_url": (
            get_player_photo_url(player_profile.id, player_profile.display_name, style)
            if style
            else player_profile.photo_url
        ),
        # Metrics set to None to hide scoreboard (no data sources yet)
        "metrics": {
            "consensusRank": None,
            "consensusChange": None,
            "buzzScore": None,
            "truePosition": None,
            "trueRange": None,
            "winsAdded": None,
            "trendDirection": None,
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

    # Comparison data is fetched via API (GET /api/players/{slug}/similar)
    comparison_data: list = []

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
            "image_style": style,  # Current image style for JS
        },
    )
