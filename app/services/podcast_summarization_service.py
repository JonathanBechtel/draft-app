"""AI-powered podcast episode summarization and classification service.

Uses Gemini to check draft relevance, generate summaries, classify episodes
into tags, and extract mentioned player names.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.schemas.podcast_episodes import PodcastEpisodeTag

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EpisodeAnalysis:
    """Structured output from AI episode analysis."""

    summary: str
    tag: PodcastEpisodeTag
    mentioned_players: list[str]


RELEVANCE_CHECK_PROMPT = """You are a sports content filter for DraftGuru, an NBA Draft analytics site.

Determine whether this podcast episode is about or substantially discusses the NBA Draft, draft prospects, or college basketball players projected for the draft.

Answer with valid JSON only:
{"is_draft_relevant": true}
or
{"is_draft_relevant": false}"""

EPISODE_ANALYSIS_PROMPT = """You are a sports podcast editor for DraftGuru, an NBA Draft analytics site.

Analyze this podcast episode and provide:
1. A compelling 1-2 sentence summary that captures the key topic
2. A classification tag for the episode type
3. A list of NBA draft prospects mentioned by name

Tags (choose exactly one):
- "Interview": Guest interview with a prospect, scout, GM, or analyst
- "Draft Analysis": Prospect evaluations, rankings, tier discussions
- "Mock Draft": Mock draft walkthrough or pick-by-pick projection
- "Game Breakdown": Film review or game recap focusing on prospect performance
- "Trade & Intel": Rumors, workouts, measurements, behind-the-scenes draft chatter
- "Prospect Debate": Head-to-head comparisons, "who goes first?" style arguments
- "Mailbag": Listener Q&A, fan questions, community interaction
- "Event Preview": Combine, tournament, All-Star, draft night, or other event previews/recaps

Guidelines:
- If the description is minimal, base your analysis on the episode title
- Keep summaries punchy (1-2 sentences), focus on the hook
- Extract full prospect names (e.g., "Cooper Flagg", not "Flagg")
- Only include prospect/college players, not NBA veterans or coaches
- Return empty list if no prospects are mentioned

Respond with valid JSON only:
{"summary": "...", "tag": "...", "mentioned_players": ["Name 1"]}"""


class PodcastSummarizationService:
    """Handles AI-powered podcast episode analysis via Gemini API."""

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
        """Check if a podcast episode is relevant to the NBA Draft.

        Lightweight Gemini call used for general (non-draft-focused) shows
        when keyword pre-filter did not match.

        Args:
            title: Episode title
            description: Episode description

        Returns:
            True if draft-relevant, False otherwise (including on error)
        """
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

            response_text = response.text if response.text else ""
            return _parse_relevance_response(response_text)

        except Exception as e:
            logger.error(f"Relevance check failed for '{title[:50]}': {e}")
            return False

    async def analyze_episode(self, title: str, description: str) -> EpisodeAnalysis:
        """Analyze a podcast episode and return summary + tag + mentioned players.

        Args:
            title: Episode title
            description: Episode description

        Returns:
            EpisodeAnalysis with summary, tag, and mentioned_players
        """
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
                        types.Part.from_text(text=EPISODE_ANALYSIS_PROMPT)
                    ],
                    temperature=0.3,
                ),
            )

            response_text = response.text if response.text else ""
            analysis = _parse_analysis_response(response_text)
            logger.info(f"Analyzed '{title[:30]}...': tag={analysis.tag.value}")
            return analysis

        except Exception as e:
            logger.error(f"Failed to analyze episode '{title[:50]}': {e}")
            return EpisodeAnalysis(
                summary=description[:200] if description else title,
                tag=PodcastEpisodeTag.DRAFT_ANALYSIS,
                mentioned_players=[],
            )


def _parse_relevance_response(response_text: str) -> bool:
    """Parse Gemini relevance check response.

    Args:
        response_text: Raw text response from Gemini

    Returns:
        True if draft-relevant, False otherwise
    """
    text = _strip_markdown_fences(response_text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse relevance JSON: {text[:100]}")
        return False

    return bool(data.get("is_draft_relevant", False))


def _parse_analysis_response(response_text: str) -> EpisodeAnalysis:
    """Parse Gemini episode analysis response.

    Args:
        response_text: Raw text response from Gemini

    Returns:
        EpisodeAnalysis parsed from JSON

    Raises:
        ValueError: If response cannot be parsed as valid JSON
    """
    text = _strip_markdown_fences(response_text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON response: {text[:100]}") from e

    summary = data.get("summary", "")
    tag_str = data.get("tag", "Draft Analysis")

    tag_map = {
        "Interview": PodcastEpisodeTag.INTERVIEW,
        "Draft Analysis": PodcastEpisodeTag.DRAFT_ANALYSIS,
        "Mock Draft": PodcastEpisodeTag.MOCK_DRAFT,
        "Game Breakdown": PodcastEpisodeTag.GAME_BREAKDOWN,
        "Trade & Intel": PodcastEpisodeTag.TRADE_INTEL,
        "Prospect Debate": PodcastEpisodeTag.PROSPECT_DEBATE,
        "Mailbag": PodcastEpisodeTag.MAILBAG,
        "Event Preview": PodcastEpisodeTag.EVENT_PREVIEW,
    }
    tag = tag_map.get(tag_str, PodcastEpisodeTag.DRAFT_ANALYSIS)

    raw_players = data.get("mentioned_players", [])
    mentioned_players: list[str] = []
    if isinstance(raw_players, list):
        mentioned_players = [
            str(p).strip() for p in raw_players if isinstance(p, str) and p.strip()
        ]

    return EpisodeAnalysis(
        summary=summary, tag=tag, mentioned_players=mentioned_players
    )


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code block fences from text."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# Singleton instance
podcast_summarization_service = PodcastSummarizationService()
