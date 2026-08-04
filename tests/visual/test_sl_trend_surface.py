"""Visual contract checks for the scope-parameterized SL trend card.

These checks mount the *production* artifacts: the payload comes from the read
service's own serializer, the markup from the shipped Jinja macro, and the CSS
and JS from the running server. A hand-rolled replica of any of those three
would let real drift pass here while breaking the page, so nothing about the
card is re-implemented in this module.

The mount is content-set rather than route-driven so the chart stays reviewable
on a developer database with no seeded player or competition rows.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import Page, expect

from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.services.sources.summer_league.metric_trends import trend_points_to_context
from app.templating import register_template_filters

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"

# ``competition:<id>`` is the only competition-scope shape the read service's
# ``_scope_filter`` accepts; a year-bearing key would be rejected in production.
SCOPE_KEY = "competition:42"
SCOPE_LABEL = "2026 Las Vegas Summer League"

VALUES: dict[str, list[float]] = {
    "gmsc": [0.42, 0.58, 0.77],
    "ts_pct": [0.51, 0.54, 0.57],
    "bpm": [1.8, 2.4, 3.1],
}
AS_OF = datetime(2026, 7, 13, 12, 0)


@lru_cache(maxsize=1)
def _trend_card_macro() -> Callable[..., Any]:
    """Return the shipped ``trend_card`` macro from ``app/templates``."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    register_template_filters(env)
    return env.get_template("partials/trend-card.html").module.trend_card


def _payload(*, player_id: int | None = 7, single_point: bool = False) -> dict:
    """Serialize a deterministic three-metric trend through the read service."""
    days = (
        [date(2026, 7, 10)]
        if single_point
        else [date(2026, 7, 10), date(2026, 7, 11), date(2026, 7, 12)]
    )
    points = [
        TrendPoint(
            metric_key=key,
            effective_day=day,
            value=VALUES[key][index],
            cohort_band=TrendCohortBand(
                median=VALUES[key][index],
                q1=VALUES[key][index] - 0.15,
                q3=VALUES[key][index] + 0.15,
            ),
            as_of=AS_OF,
        )
        for key in VALUES
        for index, day in enumerate(days)
    ]
    payload = trend_points_to_context(
        points,
        scope_key=SCOPE_KEY,
        scope_label=SCOPE_LABEL,
        player_id=player_id,
    )
    assert payload is not None
    return payload


def _mount(page: Page, base_url: str, payload: dict) -> None:
    """Mount the real trend-card macro with the production static assets."""
    card = str(_trend_card_macro()(payload))
    html = f"""
    <!doctype html>
    <html><head><base href="{base_url}/">
      <link rel="stylesheet" href="{base_url}/static/main.css">
      <link rel="stylesheet" href="{base_url}/static/css/sl-trend.css">
    </head><body>
      <main style="max-width: 900px; margin: 1rem auto; padding: 0 .75rem">
        {card}
      </main>
      <script src="{base_url}/static/js/sl-trend.js"></script>
    </body></html>
    """
    page.set_content(html)
    page.wait_for_selector("[data-trend-chart]")
    expected_lines = len(payload["metrics"])
    page.wait_for_function(
        "count => document.querySelectorAll('[data-trend-line]').length === count",
        arg=expected_lines,
    )


class TestSlTrendSurfaceVisuals:
    """Capture the desktop, mobile, event, share, and one-point states."""

    def test_desktop_chart_and_tap_tooltip(
        self, page: Page, base_url: str, screenshot
    ) -> None:
        """Desktop chart renders all lanes and exposes point status text."""
        _mount(page, base_url, _payload())
        expect(page.locator(".trend-card__line")).to_have_count(3)
        expect(page.locator(".trend-card__band")).to_have_count(3)
        page.locator(".trend-card__point").first.click()
        expect(page.locator("[data-trend-tooltip]")).to_contain_text("GMSC")
        screenshot.capture_element(".trend-card", "sl_trend_desktop")

    def test_freshness_label_is_human_readable(self, page: Page, base_url: str) -> None:
        """Source currency reads like the Explorer, not as an ISO timestamp."""
        _mount(page, base_url, _payload())
        as_of = page.locator("[data-trend-as-of]")
        expect(as_of).to_have_text("2026-07-13 12:00 UTC")
        assert as_of.get_attribute("datetime") == "2026-07-13T12:00:00"

    def test_card_never_renders_the_internal_scope_key(
        self, page: Page, base_url: str
    ) -> None:
        """No reader-facing text or markup attribute carries the scope key."""
        _mount(page, base_url, _payload())
        assert SCOPE_KEY not in page.locator(".trend-card").inner_text()
        expect(page.locator("[data-trend-scope]")).to_have_count(0)
        expect(page.locator(".trend-card__eyebrow")).to_have_text(SCOPE_LABEL)

    def test_mobile_card_has_no_horizontal_scroll(
        self, mobile_page: Page, base_url: str, screenshot
    ) -> None:
        """390px layout keeps the chart inside the viewport."""
        mobile_page.set_viewport_size({"width": 390, "height": 844})
        _mount(mobile_page, base_url, _payload())
        expect(mobile_page.locator(".trend-card")).to_be_visible()
        assert mobile_page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        screenshot.capture_element(".trend-card", "sl_trend_mobile")

    def test_light_card_holds_under_an_os_dark_preference(
        self, page: Page, base_url: str, screenshot
    ) -> None:
        """DraftGuru is light-only: an OS-dark reader sees the same legible card."""
        page.emulate_media(color_scheme="dark")
        _mount(page, base_url, _payload())
        expect(page.locator(".trend-card__line")).to_have_count(3)
        assert (
            page.locator(".trend-card").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(255, 255, 255)"
        )
        expect(page.locator(".trend-card__meta")).to_be_visible()
        screenshot.capture_element(".trend-card", "sl_trend_os_dark")

    def test_event_scope_is_shareless(
        self, page: Page, base_url: str, screenshot
    ) -> None:
        """Competition/cohort cards render without a player-only share action."""
        _mount(page, base_url, _payload(player_id=None))
        expect(page.locator("[data-trend-share]")).to_have_count(0)
        expect(page.locator(".trend-card__eyebrow")).to_contain_text(SCOPE_LABEL)
        screenshot.capture_element(".trend-card", "sl_trend_event_scope")

    def test_share_posts_sl_trend_component(
        self, page: Page, base_url: str, screenshot
    ) -> None:
        """Player share action posts the scope and metric contract."""

        def fulfill(route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"url": "data:image/png;base64,AA==", "filename": "trend.png"}
                ),
            )

        page.route("**/api/export/image", fulfill)
        _mount(page, base_url, _payload())
        with page.expect_request("**/api/export/image") as request_info:
            page.locator("[data-trend-share]").click()
        request = request_info.value
        request_payload = json.loads(request.post_data)
        assert request_payload["component"] == "sl_trend"
        assert request_payload["context"]["scope_key"] == SCOPE_KEY
        screenshot.capture_element(".trend-card", "sl_trend_share_card")

    def test_failed_share_export_is_visible_not_only_logged(
        self, page: Page, base_url: str, screenshot
    ) -> None:
        """A rejected export tells the reader instead of failing silently."""
        page.route(
            "**/api/export/image", lambda route: route.fulfill(status=500, body="nope")
        )
        _mount(page, base_url, _payload())
        page.locator("[data-trend-share]").click()
        expect(page.locator("[data-trend-share]")).to_have_text(
            "Export failed", timeout=3000
        )
        expect(page.locator("[data-trend-tooltip]")).to_contain_text(
            "Share export failed", timeout=3000
        )
        screenshot.capture_element(".trend-card", "sl_trend_share_failure")

    def test_keyboard_activation_matches_click(self, page: Page, base_url: str) -> None:
        """Focusable points respond to Enter and Space, not only to a mouse."""
        _mount(page, base_url, _payload())
        point = page.locator(".trend-card__point").first
        point.focus()
        point.press("Enter")
        expect(page.locator("[data-trend-tooltip]")).to_contain_text("GMSC")
        page.locator("[data-trend-tooltip]").evaluate("node => node.textContent = ''")
        page.locator(".trend-card__point").nth(1).focus()
        page.locator(".trend-card__point").nth(1).press(" ")
        expect(page.locator("[data-trend-tooltip]")).to_contain_text("GMSC")

    def test_single_point_state_is_explicit(
        self, page: Page, base_url: str, screenshot
    ) -> None:
        """One published day remains visible and is labeled honestly."""
        _mount(page, base_url, _payload(single_point=True))
        expect(page.locator("[data-trend-single]")).to_contain_text(
            "Single-point state"
        )
        expect(page.locator(".trend-card__point")).to_have_count(3)
        screenshot.capture_element(".trend-card", "sl_trend_single_point")
