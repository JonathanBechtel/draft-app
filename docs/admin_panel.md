# Admin Panel Implementation

This document describes the admin panel for DraftGuru, including the implementation plan, completed phases, and architecture decisions.

## Overview

The admin panel provides authenticated staff users with tools to manage the application. It supports two roles:

- **Admin**: Full access to all features including CRUD operations on reference tables
- **Worker**: Limited access to dashboard and account management only

## Implementation Plan

### Phase 1: Account Management & UI Polish

**Goal**: Create a cohesive admin UI with authentication, account management, and password reset functionality.

#### 1.1 Admin Base Layout
- Created shared admin layout (`base.html`) with sidebar navigation
- Implemented role-based navigation visibility (workers don't see admin-only sections)
- Added shared CSS (`admin.css`) with consistent styling

#### 1.2 Account View Page
- Route: `GET /admin/account`
- Displays current user info: email, role, created date, last login, password changed date

#### 1.3 Password Change (Authenticated)
- Routes: `GET/POST /admin/account/change-password`
- Validates current password before allowing change
- Preserves current session while invalidating others

#### 1.4 Password Reset UI (Unauthenticated)
- Routes: `GET/POST /admin/password-reset`, `GET/POST /admin/password-reset/confirm`
- Token-based reset flow with email outbox integration
- Generic responses to prevent user enumeration

#### 1.5 Dashboard Updates
- Updated dashboard to use new admin base layout
- Added navigation and consistent styling

---

### Phase 2: Basic Table Editing (CRUD)

**Goal**: Implement CRUD functionality for the NewsSource reference table.

#### 2.1 NewsSource CRUD
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/news-sources` | List all sources |
| GET | `/admin/news-sources/new` | Create form |
| POST | `/admin/news-sources` | Create |
| GET | `/admin/news-sources/{id}` | Edit form |
| POST | `/admin/news-sources/{id}` | Update |
| POST | `/admin/news-sources/{id}/delete` | Delete |

**Features**:
- Form validation with error display
- Duplicate feed URL detection
- Foreign key constraint checking before delete
- Success/error flash messages

---

## Completed Work

### Files Created

**Routes** (`app/routes/admin/`):
- `__init__.py` - Main router, combines sub-routers, dashboard route
- `helpers.py` - Shared helpers (`base_context`, `require_auth`, `require_admin`)
- `auth.py` - Login, logout, password reset routes
- `account.py` - Account view, password change routes
- `news_sources.py` - NewsSource CRUD routes

**Templates** (`app/templates/admin/`):
- `base.html` - Admin layout with sidebar
- `index.html` - Dashboard
- `account.html` - Account info display
- `change-password.html` - Password change form
- `login.html` - Login form (updated)
- `password-reset-request.html` - Reset request form
- `password-reset-confirm.html` - Reset confirmation form
- `password-reset-success.html` - Reset success page
- `news-sources/index.html` - News sources list
- `news-sources/form.html` - News source create/edit form

**Styles** (`app/static/css/`):
- `admin.css` - Comprehensive admin UI styles

**Tests** (`tests/integration/`):
- `test_admin_account.py` - Account management tests
- `test_admin_password_reset_ui.py` - Password reset flow tests
- `test_admin_crud_news_sources.py` - CRUD operation tests

### Architecture Decisions

#### Sub-Router Pattern
The admin routes are organized into sub-routers for maintainability:

```
app/routes/admin/
├── __init__.py      # Main router (prefix="/admin")
├── helpers.py       # Shared authentication helpers
├── auth.py          # Login/logout/password-reset
├── account.py       # Account management (prefix="/account")
└── news_sources.py  # CRUD operations (prefix="/news-sources")
```

#### Authentication Helpers
- `get_current_user()` - Retrieves user from session cookie
- `require_auth()` - Redirects to login if not authenticated
- `require_admin()` - Redirects if not admin role

#### CSS Organization
All admin styles use BEM-style naming:
- `.admin-layout`, `.admin-sidebar`, `.admin-main`
- `.admin-card`, `.admin-form`, `.admin-btn`
- `.admin-table`, `.admin-badge`, `.admin-alert`
- Modifiers: `--primary`, `--block`, `--stacked`, etc.

---

## Known Issues & Technical Debt

### Database Transaction Pattern
Request-bounded application code (FastAPI routes + services) follows the preferred transaction convention:

- Use explicit scopes like `async with db.begin(): ...`
- Avoid scattered `await db.commit()` / `await db.rollback()` in request paths

This used to be blocked by the integration test harness (it pre-opened transactions/savepoints for rollback isolation), which caused nested `db.begin()` calls to raise `InvalidRequestError`. The test harness has since been updated to be production-like (TRUNCATE isolation + fresh sessions per request), so request code can use `db.begin()` normally.

Note: non-request code (CLI/scripts) may still use explicit commits/rollbacks until it’s refactored.

---

## Future Enhancements

Potential additions for future phases:

1. **Additional CRUD Tables**
   - Players management
   - Draft picks management
   - User/staff management (admin only)

2. **Audit Logging**
   - Track who changed what and when
   - Display change history in admin UI

3. **Bulk Operations**
   - Import/export CSV for reference tables
   - Bulk activate/deactivate

4. **Dashboard Widgets**
   - Recent news ingestion stats
   - Active sources count
   - Error/warning alerts

---

## Running the Admin Panel

1. Start the development server:
   ```bash
   make dev
   ```

2. Navigate to `http://localhost:8080/admin`

3. Log in with staff credentials

4. Use the sidebar to navigate between sections

---

## Testing

Run admin-specific tests:
```bash
pytest tests/integration/test_admin*.py -v
```

Run all integration tests:
```bash
pytest tests/integration -q
```
