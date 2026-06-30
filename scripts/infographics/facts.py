"""Deterministic fact extraction for autonomous infographic selection.

Computes candidate "story" facts from recap data, scores each for
share-worthiness, and maps it to a template + params. Nothing is hallucinated:
every number comes straight from the data; the only judgment is the score. The
skill can pick the top candidate (``best``) or choose among ``candidates`` for
variety across runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    key: str
    template: str
    params: dict = field(default_factory=dict)
    headline: str = ""
    score: float = 0.0


def candidates(recap: dict) -> list[Candidate]:
    risers = recap.get("biggest_risers", [])
    fallers = recap.get("biggest_fallers", [])
    out: list[Candidate] = []

    if risers:
        top = risers[0]
        d = abs(top["delta"])
        out.append(
            Candidate(
                "steal",
                "hero",
                {"mode": "steal"},
                f"{top['player_name']} was the steal of the draft (+{d})",
                50 + d * 4,
            )
        )
    if fallers:
        top = fallers[0]
        d = abs(top["delta"])
        out.append(
            Candidate(
                "reach",
                "hero",
                {"mode": "reach"},
                f"{top['player_name']} was the biggest reach (-{d})",
                48 + d * 4,
            )
        )
    if risers and fallers:
        spread = abs(risers[0]["delta"]) + abs(fallers[0]["delta"])
        out.append(
            Candidate(
                "movers",
                "leaderboard",
                {},
                "Biggest risers and fallers vs. consensus",
                60 + spread * 1.5,
            )
        )
    # The broad scatter is always available as a safe default.
    out.append(
        Candidate(
            "beat_the_board",
            "scatter",
            {},
            "Where every pick went vs. consensus",
            55.0,
        )
    )

    return sorted(out, key=lambda c: c.score, reverse=True)


def best(recap: dict) -> Candidate | None:
    cands = candidates(recap)
    return cands[0] if cands else None
