"""Integration tests for school logo lookup + player-detail rendering."""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.college_schools import CollegeSchool
from app.schemas.players_master import PlayerMaster
from app.services import school_logo_service


@pytest_asyncio.fixture(autouse=True)
async def _reset_logo_cache() -> AsyncGenerator[None, None]:
    """Reset the module-level cache between tests so fixture data is fresh."""
    school_logo_service.clear_cache()
    yield
    school_logo_service.clear_cache()


@pytest_asyncio.fixture
async def seeded_schools(db_session: AsyncSession) -> None:
    """Insert two schools — one with a logo URL, one without."""
    db_session.add_all(
        [
            CollegeSchool(
                name="Duke",
                slug="duke",
                logo_url="/static/img/logos/college/duke.png",
            ),
            CollegeSchool(name="Real Madrid (Spain)", slug="real-madrid"),
        ]
    )
    await db_session.commit()


class TestSchoolLogoService:
    """Verify the lookup service's single + batch interfaces."""

    @pytest.mark.asyncio
    async def test_returns_logo_url_for_known_school(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> None:
        """A school with a registered logo resolves to its URL."""
        url = await school_logo_service.get_logo_url_for_school(db_session, "Duke")
        assert url == "/static/img/logos/college/duke.png"

    @pytest.mark.asyncio
    async def test_returns_none_for_school_without_logo(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> None:
        """Schools registered without a logo URL resolve to None."""
        url = await school_logo_service.get_logo_url_for_school(
            db_session, "Real Madrid (Spain)"
        )
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_school(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> None:
        """Schools not in the lookup table resolve to None."""
        url = await school_logo_service.get_logo_url_for_school(
            db_session, "Not A Real School"
        )
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_input(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> None:
        """Empty or None input short-circuits to None."""
        assert await school_logo_service.get_logo_url_for_school(db_session, "") is None
        assert (
            await school_logo_service.get_logo_url_for_school(db_session, None) is None
        )

    @pytest.mark.asyncio
    async def test_batch_filters_to_only_resolved_schools(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> None:
        """Batch lookup returns only entries that match a registered logo URL."""
        result = await school_logo_service.get_logo_urls_for_schools(
            db_session,
            ["Duke", "Real Madrid (Spain)", "Not A Real School", None, ""],
        )
        assert result == {"Duke": "/static/img/logos/college/duke.png"}


class TestPlayerDetailLogoRender:
    """Verify the player-detail page emits an inline school logo."""

    @pytest_asyncio.fixture
    async def player_with_logo(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> PlayerMaster:
        """Create a player whose school has a registered logo."""
        player = PlayerMaster(display_name="Duke Star", school="Duke")
        db_session.add(player)
        await db_session.commit()
        await db_session.refresh(player)
        return player

    @pytest_asyncio.fixture
    async def player_without_logo(
        self, db_session: AsyncSession, seeded_schools: None
    ) -> PlayerMaster:
        """Create a player whose school has no registered logo (intl club)."""
        player = PlayerMaster(
            display_name="International Pro", school="Real Madrid (Spain)"
        )
        db_session.add(player)
        await db_session.commit()
        await db_session.refresh(player)
        return player

    @pytest.mark.asyncio
    async def test_renders_school_logo_when_available(
        self, app_client: AsyncClient, player_with_logo: PlayerMaster
    ) -> None:
        """Player-detail page includes <img class="school-logo"> when a logo exists."""
        resp = await app_client.get(f"/players/{player_with_logo.slug}")
        assert resp.status_code == 200
        assert 'class="school-logo"' in resp.text
        assert "/static/img/logos/college/duke.png" in resp.text

    @pytest.mark.asyncio
    async def test_omits_school_logo_when_unmatched(
        self, app_client: AsyncClient, player_without_logo: PlayerMaster
    ) -> None:
        """Page still renders cleanly for unmatched schools — no logo img tag."""
        resp = await app_client.get(f"/players/{player_without_logo.slug}")
        assert resp.status_code == 200
        # School text still present, but no .school-logo img
        assert "Real Madrid" in resp.text
        assert 'class="school-logo"' not in resp.text
