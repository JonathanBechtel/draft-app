"""Shared numeric constants for Summer League stat derivations.

Kept in one place so the possession-rate math stays consistent across the
player-page, leaders, and explorer services.
"""

# NBA's advanced box score (``boxscoreadvancedv2``) reports ``PACE`` normalized to
# 48 minutes (possessions per 48), even though Summer League games are 40 minutes
# long — verified empirically against the raw payloads (a 40-minute game running
# ~85 possessions reports PACE ~102 = 85 * 48/40). Possessions for a given
# stint are therefore recovered as ``pace * minutes / MINUTES_PER_GAME``.
MINUTES_PER_GAME = 48.0
