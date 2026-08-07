"""Ticket #798: an organization relationship can end and later resume.

As shipped in #781, ``organization_relationships`` was unique on
``(from_organization_id, to_organization_id, relationship_type)`` forever, so a
club that was ``ACADEMY_OF`` a parent, separated, and re-affiliated had no way to
be recorded as two intervals -- contradicting P2 of
``docs/plans/north-star-architecture.md``. #798 replaced that constraint with
``PlayerAffiliation``'s supersession shape plus a partial unique index scoped to
the single *open* current edge. See the ``OrganizationRelationship`` class
docstring for the design rationale.

Every test here is written to fail against the pre-#798 schema, and to fail
*semantically* rather than merely crash:

* The second interval is genuinely inserted and committed, so the old blanket
  unique constraint would raise ``IntegrityError`` here.
* Values are read back with **raw SQL** naming the new columns. SQLModel silently
  discards unknown constructor kwargs, so a test that only round-tripped through
  the ORM would still pass if ``recorded_at`` / ``superseded_at`` were missing
  from the table.
* ``expunge_all()`` runs before any ORM re-read. The integration ``db_session``
  fixture sets ``expire_on_commit=False``, so without it a ``select()`` returns
  the very objects the test constructed and proves nothing about persistence.

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.organization import (
    OrgKind,
    Organization,
    OrganizationRelationship,
    OrgRelationshipType,
)

# The "current relationship" read: the one open, uncorrected edge for a pair.
# Mirrors the predicate behind uq_organization_relationships_current exactly.
_CURRENT_EDGE_SQL = text(
    """
    SELECT id, effective_start, effective_end, recorded_at,
           supersedes_id, superseded_at, retracted_at
    FROM organization_relationships
    WHERE from_organization_id = :from_id
      AND to_organization_id = :to_id
      AND relationship_type::text = :rel_type
      AND superseded_at IS NULL
      AND retracted_at IS NULL
      AND effective_end IS NULL
    """
)


async def _make_org_pair(
    db: AsyncSession, *, prefix: str
) -> tuple[int, int]:
    """Seed a parent club and a feeder club, returning their ids."""
    parent = Organization(
        org_kind=OrgKind.CLUB, name=f"{prefix} Parent", slug=f"{prefix}-parent"
    )
    feeder = Organization(
        org_kind=OrgKind.ACADEMY, name=f"{prefix} Feeder", slug=f"{prefix}-feeder"
    )
    db.add(parent)
    db.add(feeder)
    await db.flush()
    assert parent.id is not None and feeder.id is not None
    return feeder.id, parent.id


@pytest.mark.asyncio
async def test_relationship_can_end_and_resume_as_two_intervals(
    db_session: AsyncSession,
) -> None:
    """The same (from, to, type) triple accepts two non-overlapping intervals.

    This is the ticket. Under the pre-#798 schema the second ``add()`` violates
    ``uq_organization_relationships_from_to_type`` and this test raises
    ``IntegrityError`` on commit.
    """
    feeder_id, parent_id = await _make_org_pair(db_session, prefix="resume")

    closed = OrganizationRelationship(
        from_organization_id=feeder_id,
        to_organization_id=parent_id,
        relationship_type=OrgRelationshipType.ACADEMY_OF,
        effective_start=date(2005, 7, 1),
        effective_end=date(2010, 6, 30),
    )
    resumed = OrganizationRelationship(
        from_organization_id=feeder_id,
        to_organization_id=parent_id,
        relationship_type=OrgRelationshipType.ACADEMY_OF,
        effective_start=date(2015, 7, 1),
        effective_end=None,
    )
    db_session.add(closed)
    db_session.add(resumed)
    await db_session.commit()

    # Raw-SQL read: asserts what is actually in the columns, including the new
    # bitemporal stamps, rather than what the ORM objects happen to hold.
    stored = (
        await db_session.execute(
            text(
                "SELECT effective_start, effective_end, recorded_at, supersedes_id, "
                "superseded_at, retracted_at "
                "FROM organization_relationships "
                "WHERE from_organization_id = :from_id "
                "  AND to_organization_id = :to_id "
                "  AND relationship_type::text = :rel_type "
                "ORDER BY effective_start"
            ),
            {
                "from_id": feeder_id,
                "to_id": parent_id,
                "rel_type": OrgRelationshipType.ACADEMY_OF.value,
            },
        )
    ).all()

    assert len(stored) == 2, "both intervals must survive as distinct rows"
    first, second = stored
    assert (first.effective_start, first.effective_end) == (
        date(2005, 7, 1),
        date(2010, 6, 30),
    )
    assert (second.effective_start, second.effective_end) == (date(2015, 7, 1), None)
    # The gap is real: the closed interval asserts nothing about 2010-2015.
    assert first.effective_end < second.effective_start
    # Neither row is a correction of the other -- a resume is a new assertion.
    for row in stored:
        assert row.recorded_at is not None
        assert row.supersedes_id is None
        assert row.superseded_at is None
        assert row.retracted_at is None

    # Genuine ORM re-read: expire_on_commit=False means the identity map would
    # otherwise hand back the objects constructed above.
    db_session.expunge_all()
    persisted = (
        (
            await db_session.execute(
                select(OrganizationRelationship)
                .where(
                    OrganizationRelationship.from_organization_id == feeder_id,
                    OrganizationRelationship.to_organization_id == parent_id,
                )
                .order_by(OrganizationRelationship.effective_start)
            )
        )
        .scalars()
        .all()
    )
    assert len(persisted) == 2
    assert [row.effective_end for row in persisted] == [date(2010, 6, 30), None]

    # The "current relationship" query returns exactly one row.
    current = (
        await db_session.execute(
            _CURRENT_EDGE_SQL,
            {
                "from_id": feeder_id,
                "to_id": parent_id,
                "rel_type": OrgRelationshipType.ACADEMY_OF.value,
            },
        )
    ).all()
    assert len(current) == 1
    assert current[0].effective_start == date(2015, 7, 1)


@pytest.mark.asyncio
async def test_two_open_edges_for_the_same_triple_are_rejected(
    db_session: AsyncSession,
) -> None:
    """Duplicate protection survives: only one *open* current edge per triple.

    Design A (time-scoped uniqueness) would have admitted this pair -- two rows
    with different starts and open-ended ends, i.e. overlapping intervals for the
    same edge. The partial unique index rejects it.
    """
    feeder_id, parent_id = await _make_org_pair(db_session, prefix="open-dup")
    await db_session.commit()

    db_session.add(
        OrganizationRelationship(
            from_organization_id=feeder_id,
            to_organization_id=parent_id,
            relationship_type=OrgRelationshipType.FEEDS,
            effective_start=date(2020, 1, 1),
        )
    )
    await db_session.commit()

    db_session.add(
        OrganizationRelationship(
            from_organization_id=feeder_id,
            to_organization_id=parent_id,
            relationship_type=OrgRelationshipType.FEEDS,
            effective_start=date(2022, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_superseded_assertion_stays_readable_and_is_not_current(
    db_session: AsyncSession,
) -> None:
    """A correction supersedes rather than mutates, and current-ness follows it.

    This is the capability design A cannot express at all: the wrong assertion is
    retained with its correction trail, and the replacement is the only current
    edge.
    """
    feeder_id, parent_id = await _make_org_pair(db_session, prefix="correction")

    wrong = OrganizationRelationship(
        from_organization_id=feeder_id,
        to_organization_id=parent_id,
        relationship_type=OrgRelationshipType.OWNS,
        effective_start=date(2018, 1, 1),
    )
    db_session.add(wrong)
    await db_session.flush()
    wrong_id = wrong.id
    assert wrong_id is not None

    # Mark the original superseded first: the partial unique index covers open,
    # uncorrected rows, so the replacement can only land once this one is out.
    wrong.superseded_at = datetime(2026, 8, 7, 12, 0, 0)
    db_session.add(
        OrganizationRelationship(
            from_organization_id=feeder_id,
            to_organization_id=parent_id,
            relationship_type=OrgRelationshipType.OWNS,
            effective_start=date(2019, 9, 1),
            supersedes_id=wrong_id,
        )
    )
    await db_session.commit()
    db_session.expunge_all()

    # Both assertions are still on disk -- the corrected one was not destroyed.
    all_rows = (
        await db_session.execute(
            text(
                "SELECT id, effective_start, supersedes_id, superseded_at "
                "FROM organization_relationships "
                "WHERE from_organization_id = :from_id "
                "  AND to_organization_id = :to_id "
                "  AND relationship_type::text = :rel_type "
                "ORDER BY id"
            ),
            {
                "from_id": feeder_id,
                "to_id": parent_id,
                "rel_type": OrgRelationshipType.OWNS.value,
            },
        )
    ).all()
    assert len(all_rows) == 2
    assert all_rows[0].id == wrong_id
    assert all_rows[0].superseded_at is not None
    assert all_rows[1].supersedes_id == wrong_id
    assert all_rows[1].superseded_at is None

    current = (
        await db_session.execute(
            _CURRENT_EDGE_SQL,
            {
                "from_id": feeder_id,
                "to_id": parent_id,
                "rel_type": OrgRelationshipType.OWNS.value,
            },
        )
    ).all()
    assert len(current) == 1
    assert current[0].effective_start == date(2019, 9, 1)
    assert current[0].supersedes_id == wrong_id
