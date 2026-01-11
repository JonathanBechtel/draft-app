"""AI-powered article summarization and classification service.

Uses Gemini to generate compelling summaries and classify articles
into tags (Riser, Faller, Analysis, Highlight).
"""

import json
import logging
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.schemas.news_items import NewsItemTag

logger = logging.getLogger(__name__)


class ArticleAnalysis(BaseModel):
    """Structured output from AI article analysis."""

    summary: str  # 1-2 sentence compelling byline
    tag: NewsItemTag  # Classification tag for the article type


# System prompt for article analysis
ARTICLE_ANALYSIS_PROMPT = """You are a sports news editor for DraftGuru, an NBA Draft analytics site.

Your task is to analyze NBA draft-related articles and provide:
1. A compelling 1-2 sentence summary/byline that makes readers want to click
2. A classification tag for the article

Tag definitions:
- "Scouting Report": Deep dive on a single prospect (strengths, weaknesses, role projection, film notes)
- "Big Board": Rankings or tiers of prospects (Top 60/100, positional boards, tier lists)
- "Mock Draft": Pick-by-pick projections, lottery mocks, team outcome simulations
- "Tier Update": Movement between tiers, re-grouping of prospects, stock context without single-game focus
- "Game Recap": Prospect performance tied to a specific game, slate, or tournament
- "Film Study": Play-type or tape-driven breakdowns (possessions, schemes, tendencies)
- "Skill Theme": Cross-prospect analysis centered on a trait, archetype, or skill
- "Team Fit": Prospect-to-NBA-team fit, roster context, needs-based analysis
- "Draft Intel": Rumors, workouts, measurements, agent/team chatter, behind-the-scenes info
- "Statistical Analysis": Statistical models, data-driven analysis, comps, methodology

Guidelines for summaries:
- Keep it punchy and engaging (1-2 sentences max)
- Focus on the key insight or takeaway
- Avoid generic phrases like "This article discusses..."
- Write in an active, compelling voice

Respond with valid JSON only: {"summary": "...", "tag": "..."}
The tag must be exactly one of: "Scouting Report", "Big Board", "Mock Draft", "Tier Update", "Game Recap", "Film Study", "Skill Theme", "Team Fit", "Draft Intel", "Statistical Analysis"
"""


class NewsSummarizationService:
    """Handles AI-powered article analysis via Gemini API."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        """Lazily initialize the Gemini client.

        Uses GEMINI_SUMMARIZATION_API_KEY if set, otherwise falls back to
        GEMINI_API_KEY. This allows separate cost tracking for summarization.
        """
        if self._client is None:
            api_key = settings.gemini_summarization_api_key or settings.gemini_api_key
            if not api_key:
                raise ValueError(
                    "GEMINI_SUMMARIZATION_API_KEY or GEMINI_API_KEY must be configured"
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    async def analyze_article(
        self,
        title: str,
        description: str,
        content: Optional[str] = None,
    ) -> ArticleAnalysis:
        """Analyze an article and return summary + tag classification.

        Uses a single Gemini call to generate both the compelling summary
        and the classification tag.

        Args:
            title: Article title
            description: Article description/excerpt
            content: Optional full article content

        Returns:
            ArticleAnalysis with summary and tag
        """
        # Build the user prompt
        user_prompt_parts = [
            f"Title: {title}",
            f"Description: {description}",
        ]
        if content:
            # Truncate content to avoid token limits
            truncated_content = content[:2000]
            user_prompt_parts.append(f"Content excerpt: {truncated_content}")

        user_prompt = "\n\n".join(user_prompt_parts)

        logger.debug(f"Analyzing article: {title[:50]}...")

        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=user_prompt)],
                ),
                config=types.GenerateContentConfig(
                    system_instruction=[
                        types.Part.from_text(text=ARTICLE_ANALYSIS_PROMPT)
                    ],
                    temperature=0.3,  # Lower temperature for more consistent output
                ),
            )

            response_text = response.text if response.text else ""
            logger.debug(f"Gemini response: {response_text}")

            # Parse JSON response
            analysis = self._parse_response(response_text)
            logger.info(f"Analyzed '{title[:30]}...': tag={analysis.tag.value}")
            return analysis

        except Exception as e:
            logger.error(f"Failed to analyze article '{title[:50]}': {e}")
            # Return default analysis on failure
            return ArticleAnalysis(
                summary=description[:200] if description else title,
                tag=NewsItemTag.SCOUTING_REPORT,
            )

    def _parse_response(self, response_text: str) -> ArticleAnalysis:
        """Parse Gemini response into ArticleAnalysis.

        Args:
            response_text: Raw text response from Gemini

        Returns:
            Parsed ArticleAnalysis

        Raises:
            ValueError: If response cannot be parsed
        """
        # Clean up response - remove markdown code blocks if present
        text = response_text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON response: {e}")
            raise ValueError(f"Invalid JSON response: {text[:100]}")

        summary = data.get("summary", "")
        tag_str = data.get("tag", "Analysis")

        # Map tag string to enum
        tag_map = {
            "Scouting Report": NewsItemTag.SCOUTING_REPORT,
            "Big Board": NewsItemTag.BIG_BOARD,
            "Mock Draft": NewsItemTag.MOCK_DRAFT,
            "Tier Update": NewsItemTag.TIER_UPDATE,
            "Game Recap": NewsItemTag.GAME_RECAP,
            "Film Study": NewsItemTag.FILM_STUDY,
            "Skill Theme": NewsItemTag.SKILL_THEME,
            "Team Fit": NewsItemTag.TEAM_FIT,
            "Draft Intel": NewsItemTag.DRAFT_INTEL,
            "Statistical Analysis": NewsItemTag.STATS_ANALYSIS,
        }
        tag = tag_map.get(tag_str, NewsItemTag.SCOUTING_REPORT)

        return ArticleAnalysis(summary=summary, tag=tag)


# Singleton instance for convenience
news_summarization_service = NewsSummarizationService()
