"""Shared Jinja environment configuration.

Registering custom filters in one place keeps the app's template environment
and any standalone environments (e.g. tests that render partials directly)
in agreement. Without this, a template that uses a custom filter renders in
production but blows up under a bare ``Environment`` -- or worse, silently
diverges.
"""

from __future__ import annotations

from jinja2 import Environment

from app.services.event_desk.timeutils import format_et_clock


def register_template_filters(env: Environment) -> None:
    """Register DraftGuru's custom Jinja filters on ``env``.

    Filters:
        ``et_time``: render a naive-UTC datetime as an Eastern wall-clock
            label (``"7:00 PM ET"``). Used for ``summer_league_games``
            tip times, which are stored naive UTC.
    """
    env.filters["et_time"] = format_et_clock
