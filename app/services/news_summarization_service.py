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
    tag: NewsItemTag  # Riser, Faller, Analysis, or Highlight


# System prompt for article analysis
ARTICLE_ANALYSIS_PROMPT = """You are a sports news editor for DraftGuru, an NBA Draft analytics site.

Your task is to analyze NBA draft-related articles and provide:
1. A compelling 1-2 sentence summary/byline that makes readers want to click
2. A classification tag for the article

Tag definitions:
- "Riser": Article about a player's draft stock rising, breakout performance, or improved perception
- "Faller": Article about a player's draft stock falling, poor performance, or concerns
- "Highlight": Article featuring standout games, impressive stats, or notable achievements
- "Analysis": General scouting reports, player evaluations, or draft analysis (default if unclear)

Guidelines for summaries:
- Keep it punchy and engaging (1-2 sentences max)
- Focus on the key insight or takeaway
- Avoid generic phrases like "This article discusses..."
- Write in an active, compelling voice

Respond with valid JSON only: {"summary": "...", "tag": "..."}
The tag must be exactly one of: "Riser", "Faller", "Analysis", "Highlight"
"""


class NewsSummarizationService:
    """Handles AI-powered article analysis via Gemini API."""

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        """Lazily initialize the Gemini client."""
        if self._client is None:
            if not settings.gemini_api_key:
                raise ValueError("GEMINI_API_KEY not configured")
            self._client = genai.Client(api_key=settings.gemini_api_key)
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
            response = self.client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=user_prompt)],
                    ),
                ],
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
                tag=NewsItemTag.ANALYSIS,
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
            "Riser": NewsItemTag.RISER,
            "Faller": NewsItemTag.FALLER,
            "Analysis": NewsItemTag.ANALYSIS,
            "Highlight": NewsItemTag.HIGHLIGHT,
        }
        tag = tag_map.get(tag_str, NewsItemTag.ANALYSIS)

        return ArticleAnalysis(summary=summary, tag=tag)


# Singleton instance for convenience
news_summarization_service = NewsSummarizationService()
