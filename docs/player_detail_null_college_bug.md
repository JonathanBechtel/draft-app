# Player Detail "null" College Bug

Context: Rudy Gobert (and some international players) show `c • null • 7'1" • 258 lbs` on the player detail bio line. Multiple attempts to strip `"null"` were ineffective; the string still renders in the browser even after hard refresh/incognito.

## What we tried
- **Sanitize at service layer**: Added `_normalize_nullable` and SQL `nullif(...)` in `get_player_profile_by_slug` to coerce `"null"`/empty strings to `None` before building `PlayerProfileRead`. Result: no visible change.
- **Route-level cleaning**: Added `clean_null` helper in `app/routes/ui.py` to normalize `"null"`/`"none"`/empty strings before template context. Result: no visible change.
- **Template guards**: Conditionals in `player-detail.html` to skip values equal to `"null"`/`"none"`. Result: no visible change.
- **Client-side fix (kept)**: Added a `clean()` helper in `app/static/js/player-detail.js` and rebuilt the primary bio line from cleaned parts to prevent `"null"` from rendering if the client-side script overwrites the server-rendered text. Result: still seeing `"null"` in UI.

## Current suspicion
- The `"null"` is likely coming from the injected `window.PLAYER_DATA` JSON or another script overwriting the bio line after page load. If the script or data is cached/hashed elsewhere (e.g., CDN, old bundle), the updated JS may not be loaded.
- Could also be a data issue where `school` is literally the string `"null"` in the DB and is being serialized to JSON, with the client code still writing it before our clean() runs (or clean() not running due to a different entry point).

## Next steps to diagnose
1) Inspect the actual JSON delivered: open devtools console on the page and run `window.PLAYER_DATA` to see the `college` field value. If it's `"null"`, fix serialization; if `null`, focus on JS overwrites.
2) Confirm the new JS is loaded: verify `clean` exists by checking `ScoreboardModule.populatePlayerData.toString()` or adding a console log. If not, cache bust `/static/js/player-detail.js`.
3) Temporarily comment out the JS rewrite of `playerPrimaryMeta` and reload; if the server-rendered template shows correctly, the culprit is the client script. If it still shows `"null"`, the issue is in the server-rendered meta.
4) Add a one-time inline log in the template to dump `player.college` and the `meta_parts` join to confirm server-side data at render time.

## Status
- Issue persists despite normalization attempts. The only retained change related to this is the JS `clean()` helper in `app/static/js/player-detail.js`. All DB-level/template sanitizations were removed to avoid churn.
