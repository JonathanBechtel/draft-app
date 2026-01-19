# DraftGuru Staff Auth + Admin Panel (Phased Plan)

This doc is the implementation spec that matches the integration tests added for the initial “staff-only” auth system. The goal is to enable an admin panel and protect existing staff-only operations without building end-user auth.

## Goals

- Staff-only login system (email + password) for `/admin/*`.
- Session-based auth (cookie) with idle timeout + “remember me”.
- Password reset (now) without wiring a real email provider (use DB outbox).
- Dataset-level permissions for workers; admins can do everything.
- Protect existing news admin endpoints without changing their URLs:
  - `/api/news/sources`
  - `/api/news/ingest`

## Non-goals (for now)

- End-user login / signup.
- OAuth (Google/Facebook/etc). Keep room for it later, but don’t build it now.
- Full admin UI for every dataset (start with auth + ability to access protected endpoints).

## Roles & Permissions

- `admin`: implicit access to everything (no per-dataset grants required).
- `worker`: access controlled per dataset with two boolean flags:
  - `view`
  - `edit`

Treat datasets as they exist today (no opinionated grouping):

- `news_sources`:
  - `GET /api/news/sources` requires `news_sources:view`
  - `POST /api/news/sources` requires `news_sources:edit`
- `news_ingestion`:
  - `POST /api/news/ingest` requires `news_ingestion:edit`

## HTTP/Auth Contract (what tests expect)

### UI routes

- `GET /admin`:
  - Logged out → redirect to `/admin/login?next=/admin`
  - Logged in → `200`
- `GET /admin/login` → `200` HTML containing inputs `name="email"` and `name="password"`
- `POST /admin/login` (form-encoded):
  - Invalid credentials → `200` HTML containing “invalid” (generic; no user enumeration) and **no** `Set-Cookie`
  - Valid credentials → redirect to `/admin` and sets cookie `dg_admin_session=...`
  - `next` is allowed only for local paths; external URLs must redirect to `/admin`
- `POST /admin/logout`:
  - Redirects to `/admin/login`
  - Clears cookie and revokes the server-side session

### Session policy

- Cookie name: `dg_admin_session`
- “Remember me”:
  - Off → session cookie (no `Max-Age=` in `Set-Cookie`)
  - On → persistent cookie (includes `Max-Age=`) and a longer server-side expiry
- Expiry windows (tests are intentionally loose):
  - Non-remember `expires_at - created_at < 2 days`
  - Remember `expires_at - created_at > 20 days`
- Idle timeout:
  - If `last_seen_at` is too old, the session is invalid and `/admin` redirects to `/admin/login`

### Password reset (outbox-backed)

- `POST /admin/password-reset` (form-encoded):
  - Always returns `200` HTML containing “if an account exists”
  - Only for real users: creates an outbox email row with a reset token/link
- `POST /admin/password-reset/confirm` (form-encoded):
  - Valid token → redirects (302/303), changes password, revokes existing sessions
  - Token is one-time use; reuse returns `400` or `410`

### API auth behavior

- Staff-only API endpoints return JSON errors:
  - Logged out → `401`
  - Logged in but insufficient permission → `403`

## Data Model (tables required by tests)

Create these tables in Postgres (via SQLModel in `app/schemas/` + Alembic if needed):

### `auth_users`

- `id` (PK)
- `email` (unique, stored as `casefold()`; login is case-insensitive)
- `role` (`admin` or `worker`)
- `is_active` (bool)
- `password_hash` (string; initial implementation uses PBKDF2-SHA256 string format)
- `created_at`, `updated_at`
- Optional but recommended: `last_login_at`, `password_changed_at`

### `auth_sessions`

- `id` (PK)
- `user_id` (FK → `auth_users.id`)
- `token_hash` (unique; store only a hash/peppered hash, never raw token)
- `created_at`
- `last_seen_at`
- `expires_at`
- `revoked_at` (nullable)
- Optional: `ip`, `user_agent`, `remember_me`

### `auth_dataset_permissions`

- `user_id` (FK)
- `dataset` (string, e.g. `news_sources`)
- `can_view` (bool)
- `can_edit` (bool)
- `created_at`, `updated_at`
- Unique constraint: `(user_id, dataset)` (tests use `ON CONFLICT (user_id, dataset)`)

### `auth_email_outbox`

- `id` (PK)
- `to_email` (string)
- `subject` (string)
- `body` (text containing a reset link with `token=...`)
- `created_at`
- Optional: `sent_at` (nullable), `provider` (nullable)

### Recommended (not directly asserted by tests, but needed)

`auth_password_reset_tokens`:

- `id` (PK)
- `user_id` (FK)
- `token_hash` (unique)
- `created_at`
- `expires_at`
- `used_at` (nullable)

## Phased implementation plan (recommended order)

### Phase 1 — DB tables + test harness plumbing

Deliverables:

- Add SQLModel tables under `app/schemas/`:
  - `auth_users`, `auth_sessions`, `auth_dataset_permissions`, `auth_email_outbox`
  - (recommended) `auth_password_reset_tokens`
- Update `tests/integration/conftest.py` to import the new schema modules so `SQLModel.metadata.create_all` includes them.

Tests (run after Phase 1 to confirm the harness no longer errors on missing tables):

- `tests/integration/test_admin_auth.py` (will still fail on missing routes/logic, but should not crash due to missing tables)
- `tests/integration/test_admin_password_reset.py` (same)
- `tests/integration/test_admin_permissions_news.py` (same)

Implementation notes (completed):

- Added `app/schemas/auth.py` with SQLModel tables: `auth_users`, `auth_sessions`, `auth_dataset_permissions` (composite PK on `user_id` + `dataset` to satisfy `ON CONFLICT (user_id, dataset)`), `auth_email_outbox`, and `auth_password_reset_tokens`.
- Updated `tests/integration/conftest.py` to import `app.schemas.auth` so `SQLModel.metadata.create_all` creates the new tables for the integration test schema.
- Dev/test environment surprise: the base conda env’s stdlib `readline` import was segfaulting; running checks inside the `draftguru` conda env avoided the crash.
- Dev/test environment surprise: `feedparser` wasn’t installed, causing `app/main.py` import errors via `news_ingestion_service`; changed `app/services/news_ingestion_service.py` to lazy-import `feedparser` inside `fetch_rss_feed` (tests monkeypatch that function anyway).
- Running integration tests in this sandbox requires an externally started local Postgres and running `pytest` with escalated permissions (sandbox blocks localhost socket connects).

### Phase 2 — Staff sessions + admin login/logout + `/admin` protection

Deliverables:

- Auth service primitives:
  - PBKDF2-SHA256 verify compatible with `tests/integration/auth_helpers.py`
  - Session issuance + server-side storage (`auth_sessions`)
  - Cookie `dg_admin_session` (HttpOnly, Secure, SameSite=Lax, Path=/)
  - Open redirect guard for `next=`
  - Logout revokes sessions + clears cookie
- Minimal admin router mounted in `app/main.py`:
  - `GET /admin` (redirect to login if logged out)
  - `GET /admin/login` (renders form)
  - `POST /admin/login` (sets session cookie or returns generic invalid)
  - `POST /admin/logout`
- Minimal templates under `app/templates/admin/` (or similar):
  - login page must include inputs `name="email"` and `name="password"`

Tests expected to pass at end of Phase 2:

- `tests/integration/test_admin_auth.py`
  - `TestAdminAuthUI::*`

Implementation notes (completed):

- Added `app/services/admin_auth_service.py` with PBKDF2-SHA256 verification compatible with `tests/integration/auth_helpers.py` and basic session issuance/revocation stored in `auth_sessions` (only a token hash is persisted).
- Added `app/routes/admin.py` and mounted it in `app/main.py` to serve `/admin`, `/admin/login`, and `/admin/logout`.
- Added minimal templates `app/templates/admin/login.html` and `app/templates/admin/index.html`; invalid login renders a generic “invalid” message and sets no cookies.
- Redirect safety: `next=` is sanitized to local paths only; external URLs fall back to `/admin`.
- Cookie behavior: `dg_admin_session` is set `HttpOnly` and `SameSite=Lax`; cookie `Path` is `/` (not `/admin`) so the same staff session can be reused for staff-only `/api/*` endpoints in later phases; `Secure` is disabled in dev/test via `settings.is_dev`.
- Surprise: the integration test DB fixture already runs inside a transaction/savepoint, so using `async with db.begin()` inside auth helpers raised `InvalidRequestError`; switched to `db.add(...)` + `await db.commit()` for session writes.
- Test fix (blocking): `tests/integration/test_admin_auth.py` had an unhashable set literal (`{["/admin"], ...}`) that raised `TypeError` once redirects started working; corrected it to a tuple membership check.

### Phase 3 — Session policy (idle timeout + remember-me)

Deliverables:

- Persist `created_at`, `expires_at`, `last_seen_at`, `revoked_at` in `auth_sessions`
- Implement:
  - Idle timeout (invalidate if `last_seen_at` too old)
  - Remember-me: longer `expires_at` and persistent cookie (`Max-Age=...`)
- On each authenticated request, update `last_seen_at` (throttle if needed).

Tests expected to pass at end of Phase 3:

- `tests/integration/test_admin_auth.py`
  - `TestSessionPolicy::*`

Implementation notes (completed):

- Implemented idle-timeout enforcement in `app/services/admin_auth_service.py` by invalidating sessions when `last_seen_at` is older than `IDLE_TIMEOUT` (currently `1 day`); this makes the test’s `now - 2 days` update reliably force re-login.
- Added `last_seen_at` refresh on authenticated requests with a small throttle (`LAST_SEEN_UPDATE_THROTTLE = 5 minutes`) to avoid a write on every page hit.
- Implemented remember-me policy end-to-end: `remember=1` issues a longer-lived `expires_at` (`30 days`) and sets a persistent cookie (`Max-Age=...`); non-remember issues a session cookie (no `Max-Age=`) with a short expiry (`1 day`).

### Phase 4 — Password reset (outbox-backed, one-time tokens)

Deliverables:

- Routes:
  - `POST /admin/password-reset` (generic response; for real users enqueue outbox email)
  - `POST /admin/password-reset/confirm` (validate token, set new password, revoke sessions)
- Token model:
  - Store only `token_hash` server-side; token is one-time and expires
  - Reset should revoke all existing sessions for that user
- Outbox:
  - Insert `auth_email_outbox` rows with a reset link containing `token=...`

Tests expected to pass at end of Phase 4:

- `tests/integration/test_admin_password_reset.py`

Implementation notes (completed):

- Added `/admin/password-reset` and `/admin/password-reset/confirm` POST routes in `app/routes/admin.py` with the test-required generic response (“if an account exists”) and redirect-on-success confirm behavior.
- Implemented outbox + token persistence in `app/services/admin_auth_service.py`:
  - `enqueue_password_reset()` inserts `auth_password_reset_tokens` (hashed token, expiry, unused) and an `auth_email_outbox` row containing a link with `token=...`.
  - `confirm_password_reset()` validates unused/unexpired tokens, updates `auth_users.password_hash` (PBKDF2-SHA256 string), sets `password_changed_at`, revokes all existing `auth_sessions` for the user, and marks the token as used (one-time).
- Surprise (test bug): `extract_reset_token()` in `tests/integration/auth_helpers.py` used `\\s` inside a character class, which excluded the letter `s` and truncated tokens nondeterministically; fixed to `\s` so reset tokens can be reliably extracted and confirmed.

### Phase 5 — Dataset permissions + protect existing news admin endpoints

Deliverables:

- Permission evaluation:
  - `admin` always allowed
  - `worker` allowed based on `auth_dataset_permissions` (`can_view`, `can_edit`)
- Add FastAPI dependencies and gate the existing endpoints in `app/routes/news.py`:
  - `GET /api/news/sources` → requires `news_sources:view`
  - `POST /api/news/sources` → requires `news_sources:edit`
  - `POST /api/news/ingest` → requires `news_ingestion:edit`
- Ensure API responses:
  - logged out → `401`
  - insufficient permission → `403`

Tests expected to pass at end of Phase 5:

- `tests/integration/test_admin_permissions_news.py`
- `tests/integration/test_news.py` (the auth-gating additions plus existing source/ingest tests)

Implementation notes (completed):

- Added `app/services/staff_authz.py` with FastAPI dependencies:
  - `get_current_staff_user()` resolves the current user from `dg_admin_session` and raises `401` when missing/invalid.
  - `require_dataset_permission(dataset, action)` enforces `403` for insufficient permissions; `admin` bypasses all checks; `worker` requires a matching `auth_dataset_permissions` row with `can_view`/`can_edit`.
- Gated existing endpoints in `app/routes/news.py` without changing URLs:
  - `GET /api/news/sources` → `news_sources:view`
  - `POST /api/news/sources` → `news_sources:edit`
  - `POST /api/news/ingest` → `news_ingestion:edit`
- API behavior now matches tests: logged out → `401`, logged in but unauthorized → `403`.

### Phase 6 (optional) — Admin provisioning of worker permissions

This is planned but not currently covered by tests in this repo.

Deliverables:

- Admin-only endpoints (or admin UI) to create users and edit dataset grants
- New integration tests (recommended) to cover those endpoints and remove direct DB inserts from tests.

## Relevant tests (must pass when implementation is complete)

- `tests/integration/test_admin_auth.py`
- `tests/integration/test_admin_password_reset.py`
- `tests/integration/test_admin_permissions_news.py`
- Updated: `tests/integration/test_news.py` (sources + ingest now require auth)
