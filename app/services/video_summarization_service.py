"""AI-powered YouTube video summarization and tagging service."""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.schemas.youtube_videos import YouTubeVideoTag

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoAnalysis:
    """Structured output from AI video analysis."""

    summary: str
    tag: YouTubeVideoTag
    mentioned_players: list[str]


RELEVANCE_CHECK_PROMPT = """You are a sports content filter for DraftGuru, an NBA Draft analytics site.

Determine whether this video is about or substantially discusses the NBA Draft,
draft prospects, or college basketball players projected for the draft.

Answer with valid JSON only:
{"is_draft_relevant": true}
or
{"is_draft_relevant": false}"""

VIDEO_ANALYSIS_PROMPT = """You are a video editor for DraftGuru, an NBA Draft analytics site.

Analyze this YouTube video metadata and provide:
1. A 1-2 sentence summary
2. A single tag classification
3. A list of NBA draft prospects mentioned by name

Tags (choose exactly one):
- "Think Piece": Strategic, macro, or opinion-heavy draft discussion
- "Conversation": Interview, podcast-style discussion, debate, Q&A
- "Scouting Report": Player evaluation, strengths/weaknesses, projection
- "Highlights": Game clip breakdowns or curated highlight runs with analysis
- "Montage": Mostly visual highlight reels with minimal analysis

Guidelines:
- If metadata is sparse, infer from title.
- Keep summary concise and concrete.
- Extract full prospect names only.
- Return [] when no prospects are clearly mentioned.

Respond with valid JSON only:
{"summary": "...", "tag": "...", "mentioned_players": ["Name 1"]}"""


class VideoSummarizationService:
    """Handles AI-powered video analysis via Gemini API."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        """Lazily initialize the Gemini client."""
        if self._client is None:
            api_key = settings.gemini_summarization_api_key or settings.gemini_api_key
            if not api_key:
                raise ValueError(
                    "GEMINI_SUMMARIZATION_API_KEY or GEMINI_API_KEY must be configured"
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    async def check_draft_relevance(self, title: str, description: str) -> bool:
        """Return whether the video is draft relevant."""
        user_prompt = f"Title: {title}\n\nDescription: {description}"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=user_prompt)],
                ),
                config=types.GenerateContentConfig(
                    system_instruction=[
                        types.Part.from_text(text=RELEVANCE_CHECK_PROMPT)
                    ],
                    temperature=0.1,
                ),
            )
            return _parse_relevance_response(response.text or "")
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.error(f"Video relevance check failed for '{title[:60]}': {exc}")
            return False

    async def analyze_video(self, title: str, description: str) -> VideoAnalysis:
        """Analyze video metadata into summary/tag/player mentions."""
        user_prompt = f"Title: {title}\n\nDescription: {description}"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=user_prompt)],
                ),
                config=types.GenerateContentConfig(
                    system_instruction=[
                        types.Part.from_text(text=VIDEO_ANALYSIS_PROMPT)
                    ],
                    temperature=0.3,
                ),
            )
            return _parse_analysis_response(response.text or "")
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.error(f"Video analysis failed for '{title[:60]}': {exc}")
            return VideoAnalysis(
                summary=description[:200] if description else title,
                tag=YouTubeVideoTag.SCOUTING_REPORT,
                mentioned_players=[],
            )


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code block fences from text."""
    stripped = text.strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:]
    if stripped.startswith("```"):
        stripped = stripped[3:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _parse_relevance_response(response_text: str) -> bool:
    """Parse relevance JSON response."""
    text = _strip_markdown_fences(response_text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse video relevance JSON: {text[:100]}")
        return False
    return bool(data.get("is_draft_relevant", False))


def _parse_analysis_response(response_text: str) -> VideoAnalysis:
    """Parse video analysis JSON response."""
    text = _strip_markdown_fences(response_text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON response: {text[:100]}") from exc

    tag_map = {
        "Think Piece": YouTubeVideoTag.THINK_PIECE,
        "Conversation": YouTubeVideoTag.CONVERSATION,
        "Scouting Report": YouTubeVideoTag.SCOUTING_REPORT,
        "Highlights": YouTubeVideoTag.HIGHLIGHTS,
        "Montage": YouTubeVideoTag.MONTAGE,
    }
    raw_players = data.get("mentioned_players", [])
    mentioned_players = (
        [p.strip() for p in raw_players if isinstance(p, str) and p.strip()]
        if isinstance(raw_players, list)
        else []
    )
    return VideoAnalysis(
        summary=str(data.get("summary", "")).strip(),
        tag=tag_map.get(
            str(data.get("tag", "")).strip(), YouTubeVideoTag.SCOUTING_REPORT
        ),
        mentioned_players=mentioned_players,
    )


video_summarization_service = VideoSummarizationService()
