"""Dataclasses passed between x_threads services and the gather CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PlayerCard:
    """Compact player descriptor used inside angle payloads."""

    id: int
    slug: str
    display_name: str
    school: Optional[str] = None
    draft_year: Optional[int] = None
    position: Optional[str] = None


@dataclass
class StatFact:
    """One numeric fact about a player, ready to drop into a tweet."""

    label: str
    value: str
    percentile: Optional[float] = None
    rank: Optional[int] = None
    population_size: Optional[int] = None
    context: Optional[str] = None


@dataclass
class CompFact:
    """One similarity comp for the player."""

    slug: str
    display_name: str
    school: Optional[str]
    similarity_score: float


@dataclass
class OutlierResult:
    """Output of the outlier finder for the OUTLIER angle."""

    player: PlayerCard
    subtype: str
    headline: str
    stats: list[StatFact]
    support_text: str


@dataclass
class AnglePick:
    """The chosen angle plus the subject(s) to write about."""

    angle: str
    players: list[PlayerCard]
    news_item_id: Optional[int] = None
    notes: str = ""


@dataclass
class GatherResult:
    """Full payload returned by the gather CLI to the skill."""

    angle: str
    headline: str
    players: list[PlayerCard]
    facts: list[StatFact] = field(default_factory=list)
    comps: list[CompFact] = field(default_factory=list)
    news: Optional[dict[str, Any]] = None
    images: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
