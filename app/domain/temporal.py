"""Temporal and versioning value objects (journey-graph doc #2 §5, doc #3 §1).

Frozen dataclasses, not Pydantic: per repo convention Pydantic is reserved for
API request/response boundaries, and these are internal value objects passed
between read services and their callers. See
``docs/plans/journey-graph-domain-vocabulary.md`` "Temporal & versioning".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Watermark:
    """The freshness contract a projection carries alongside its payload (doc #3 §1).

    P4 (``docs/plans/north-star-architecture.md``): a user-facing "as of" answers
    *how current is the information*, never *when did our job run*. ``source_as_of``
    is therefore the max source-row timestamp the payload was built from, mirroring
    ``DatedVersionMixin.as_of`` (``app/schemas/base.py``) -- the same rule expressed
    once for tables and once for values.

    ``source_as_of`` is deliberately nullable: a watermark that cannot state its
    currency must still be representable, so callers surface it as degraded rather
    than silently rendering over stale data.

    Every user-visible assertion on a card is expected to carry the same
    ``Watermark``; mixing provenances then requires deliberately constructing two,
    which is visible in review rather than emergent.
    """

    source_as_of: datetime | None
    projection_built_at: datetime | None
    projection_version: int | None


@dataclass(frozen=True)
class VersionStamps:
    """The three version stamps that must never be conflated (doc #2 §5).

    Mirrors ``summer_league_environment_profiles``' existing three-stamp
    discipline and is the value-object half of ``DatedVersionMixin``
    (``app/schemas/base.py``) -- the same rule expressed once for tables and once
    for values:

    * ``version`` -- publication sequence within a scope, bumped every rebuild
      whether or not anything changed. Not a content hash and not a logic version.
    * ``registry_version`` -- the metric *definition* version (formulas,
      denominators, rounding, coverage rules).
    * ``calculation_version`` -- the *pipeline* version. Bumped when how inputs
      are pooled or watermarked changes, even with identical metric definitions.

    Collapsing these is the failure they exist to prevent: without the split,
    "did this number change because the formula changed or because the data
    did?" is unanswerable.
    """

    version: int
    registry_version: str
    calculation_version: str


@dataclass(frozen=True)
class Scope:
    """A comparison population a metric or trend is computed against (doc #3 §1).

    A lightweight identity token -- a stable key plus its kind -- never a mirror
    of the row it resolves to. See
    ``docs/plans/journey-graph-domain-vocabulary.md`` "Temporal & versioning".
    """

    scope_key: str
    scope_kind: str
