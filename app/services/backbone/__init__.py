"""The shared backbone — identity resolution, affiliations, participation.

This is the hub every source spoke writes canonical assertions into and every
read projection reads back from: cross-source player identity resolution,
time-aware affiliation assertions, and participation-grain bridges. Summer
League is the first populated spoke (`app.services.sources.summer_league`);
a second source resolves against this same package rather than growing its
own copy. See ``docs/plans/global-player-journey-graph.md`` and
``docs/plans/summer-league-journey-graph-alignment.md`` §4.
"""
