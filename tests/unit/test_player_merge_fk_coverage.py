"""Every FK to ``players_master`` must be classified for the merge path.

Failure this descends from
--------------------------
``player_merge_service``'s child-table list is maintained by hand, and it silently drifted as
``summer_league_*``, shot-event and participation tables added foreign keys to
``players_master``. Merging a player who holds Summer League data hard-fails on a RESTRICT FK,
in the middle of a time-sensitive identity cleanup. Nothing enforced that a new FK-bearing
table got registered, so the next table will do it again.
See ``docs/plans/programmatic-code-discipline.md`` §3.4 and backlog 4.4.

What this test enforces
-----------------------
The FK graph supplies the **audit universe**; a human classifies each edge exactly once. Every
column referencing ``players_master`` must be one of:

* **reassigned** — registered with the merge service, so its rows move to the surviving player;
* **cascade** — declared ``ondelete="CASCADE"``, so its rows intentionally die with the
  discarded identity;
* **pending** — itemized in ``_PENDING_CLASSIFICATION`` below, which may only shrink.

Deliberately *not* done: deriving the reassignment list from metadata. Reflective reassignment
would resurrect rows that the cascade semantics intend to delete, which is why classification
stays a human decision and this test only checks that the decision is total.
"""

from __future__ import annotations

import pytest
from sqlmodel import SQLModel

from app.services.player_merge_service import (
    _CHILD_TABLES,
    _SIMILARITY_ANCHOR,
    _SIMILARITY_COMPARISON,
)
from app.utils.db_async import load_schema_modules


PARENT_TABLE = "players_master"

# A few specs carry a synthetic table name because the merge path special-cases them.
# `source_analytics_outlier` is not a table: it is the spec for
# `source_analytics.biggest_outlier_player_id`, which is NULLed rather than reassigned.
_SPEC_TABLE_ALIASES = {"source_analytics_outlier": "source_analytics"}

# Edges known to be unclassified, each with the decision still owed. This list is a ratchet:
# entries may be removed as edges get classified, never added. A new FK arriving unclassified
# fails the build, and classifying one of these without removing it here also fails — so the
# list cannot quietly go stale in either direction.
#
# Every entry below is a live merge bug today: merging a player holding any of this data hits
# a RESTRICT FK. Fixing them means deciding reassign-vs-cascade per edge and, for the
# reassignment cases, working out the uniqueness/conflict columns — real merge behavior that
# belongs with integration tests against representative data, not folded into a guardrail.
# Edges still awaiting a classification decision. This list is a ratchet: entries may be
# removed as edges get classified, never added.
#
# Currently EMPTY — every FK to players_master is now either registered for reassignment or
# declared ondelete=CASCADE. The 13 entries that lived here (summer_league_*, shot events,
# play-by-play, participation, draft_results, player_affiliations) were classified as
# reassignments once the constraint analysis showed only one of them could collide.
_PENDING_CLASSIFICATION: frozenset[tuple[str, str]] = frozenset()


def _registered_columns() -> set[tuple[str, str]]:
    """Return the ``(table, column)`` pairs the merge service reassigns."""
    specs = [*_CHILD_TABLES, _SIMILARITY_ANCHOR, _SIMILARITY_COMPARISON]
    return {
        (_SPEC_TABLE_ALIASES.get(spec.table, spec.table), spec.player_column)
        for spec in specs
    }


def _foreign_keys_to_players_master() -> dict[tuple[str, str], str]:
    """Map every ``(table, column)`` referencing ``players_master`` to its ondelete rule."""
    load_schema_modules()
    edges: dict[tuple[str, str], str] = {}
    for table in SQLModel.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name != PARENT_TABLE:
                continue
            edges[(table.name, fk.parent.name)] = (fk.ondelete or "").upper()
    return edges


@pytest.fixture(scope="module")
def fk_edges() -> dict[tuple[str, str], str]:
    """The full FK graph pointing at ``players_master``."""
    edges = _foreign_keys_to_players_master()
    assert edges, (
        "expected to discover FKs to players_master; schema loading may have failed"
    )
    return edges


def _classify(
    edge: tuple[str, str], ondelete: str, registered: set[tuple[str, str]]
) -> str:
    """Return the classification bucket for one FK edge."""
    if edge in registered:
        return "reassigned"
    if ondelete == "CASCADE":
        return "cascade"
    return "unclassified"


def test_every_fk_to_players_master_is_classified(fk_edges):
    """No FK may be RESTRICT-and-unregistered unless explicitly listed as pending.

    This is the check that would have caught the original drift the day the first
    ``summer_league_*`` FK landed.
    """
    registered = _registered_columns()
    unclassified = {
        edge
        for edge, ondelete in fk_edges.items()
        if _classify(edge, ondelete, registered) == "unclassified"
    }
    surprises = unclassified - _PENDING_CLASSIFICATION

    assert not surprises, (
        "New FK(s) to players_master are neither registered with player_merge_service "
        "nor ondelete=CASCADE. Merging a player holding this data will fail on a RESTRICT "
        "FK. Classify each one:\n"
        + "\n".join(f"  - {table}.{column}" for table, column in sorted(surprises))
        + "\n\nRegister it in player_merge_service._CHILD_TABLES to reassign its rows, or "
        "declare the FK ondelete='CASCADE' (with a migration) if the rows should die with "
        "the discarded identity."
    )


def test_pending_classification_list_has_no_stale_entries(fk_edges):
    """The pending list may only shrink — it must not outlive the work it tracks.

    Without this, an edge could be classified in the service while its pending entry stayed
    behind, and the list would slowly stop describing reality.
    """
    registered = _registered_columns()
    stale = {
        edge
        for edge in _PENDING_CLASSIFICATION
        if edge not in fk_edges
        or _classify(edge, fk_edges[edge], registered) != "unclassified"
    }

    assert not stale, (
        "These entries in _PENDING_CLASSIFICATION are no longer unclassified (or no longer "
        "exist). Remove them:\n"
        + "\n".join(f"  - {table}.{column}" for table, column in sorted(stale))
    )


def test_every_registered_spec_matches_a_real_foreign_key(fk_edges):
    """A registered table that no longer has the FK is dead configuration.

    Catches the reverse drift: a table renamed or dropped while its merge spec lingers,
    which would make the merge issue UPDATEs against a column that is no longer there.
    """
    orphaned = _registered_columns() - set(fk_edges)

    assert not orphaned, (
        "player_merge_service registers (table, column) pairs with no FK to players_master:\n"
        + "\n".join(f"  - {table}.{column}" for table, column in sorted(orphaned))
        + "\n\nIf the spec name is intentionally synthetic, add it to _SPEC_TABLE_ALIASES."
    )
