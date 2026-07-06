"""Query-count budget guard for the public page surfaces.

Renders each public route against the representative dataset and asserts it
stays within its query budget. This is the automated half of the page-perf
workflow: it fails CI the moment a change pushes a page's query count over
budget — catching N+1s and waterfall growth deterministically, without relying
on anyone remembering to profile. The manual half is ``scripts/explain_route.py``
for timing/plans against prod-like data (see the ``analyze-page-perf`` skill).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import ROUTE_BUDGETS
from tests.integration.perf.conftest import SeededData

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("route_template", list(ROUTE_BUDGETS))
async def test_route_within_query_budget(
    route_template: str,
    representative_dataset: SeededData,
    app_client: AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """Each public route must render within its committed query budget.

    A failure here means the route fired more SQL statements than its budget in
    ``budgets.py``. Either eliminate the added queries (likely an N+1 or an
    un-batched serial load) or, if the new query is genuinely required, raise the
    budget in the same diff so the added per-request cost is reviewed.
    """
    url = route_template.format(
        slug=representative_dataset.player_slug,
        year=representative_dataset.sl_year,
        venue=representative_dataset.sl_venue,
        team=representative_dataset.sl_team,
        game_id=representative_dataset.sl_game_id,
    )
    budget = ROUTE_BUDGETS[route_template]

    # Budgets measure a route's steady-state query count. Render once untracked
    # first so one-time process-level cache fills (e.g. the school-logo map)
    # don't count — otherwise the measured number depends on which tests
    # happened to run earlier in this worker process.
    warmup = await app_client.get(url)
    assert warmup.status_code == 200, (
        f"{url} returned {warmup.status_code} on the warm-up render."
    )

    with count_queries(async_engine) as captured:
        response = await app_client.get(url)

    assert response.status_code == 200, (
        f"{url} returned {response.status_code}; expected 200 so the full query "
        f"path renders."
    )

    if len(captured) > budget:
        listing = "\n".join(
            f"  {i + 1:>2}. {' '.join(stmt.split())[:120]}"
            for i, stmt in enumerate(captured)
        )
        pytest.fail(
            f"{url} issued {len(captured)} queries, over its budget of {budget}.\n"
            f"If this is an accidental N+1 / extra serial query, fix it. If the "
            f"query is genuinely needed, raise ROUTE_BUDGETS[{route_template!r}] "
            f"in tests/integration/perf/budgets.py in this same diff.\n"
            f"Run `python scripts/explain_route.py {url}` for timings/plans.\n"
            f"Captured statements:\n{listing}"
        )
