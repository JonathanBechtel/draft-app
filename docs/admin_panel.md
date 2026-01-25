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

#### 2.2 NewsItem CRUD
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/news-items` | List with filters |
| GET | `/admin/news-items/{id}` | Edit form |
| POST | `/admin/news-items/{id}` | Update |
| POST | `/admin/news-items/{id}/delete` | Delete |

**Features**:
- Pagination with configurable limit (default 25, max 100)
- Filters: source, tag, date range
- Tag and player association editing
- No create route (items ingested from RSS feeds)

#### 2.3 PlayerMaster CRUD (In Progress)
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/players` | List with filters |
| GET | `/admin/players/new` | Create form |
| POST | `/admin/players` | Create |
| GET | `/admin/players/{id}` | Edit form |
| POST | `/admin/players/{id}` | Update |
| POST | `/admin/players/{id}/delete` | Delete |

**Implemented**:
- Pagination with search and draft year filters
- Comprehensive player fields: names, biographical info, draft info, NBA career
- Form validation for required fields and date/number parsing
- Foreign key constraint checking before delete (blocks if player has linked news items)
- Service layer pattern for business logic separation

**Remaining**:
- TBD (feature still in development)

---

## Completed Work

### Files Created

**Routes** (`app/routes/admin/`):
- `__init__.py` - Main router, combines sub-routers, dashboard route
- `helpers.py` - Shared helpers (`base_context`, `require_auth`, `require_admin`)
- `auth.py` - Login, logout, password reset routes
- `account.py` - Account view, password change routes
- `news_sources.py` - NewsSource CRUD routes
- `news_items.py` - NewsItem CRUD routes
- `players.py` - PlayerMaster CRUD routes

**Services** (`app/services/`):
- `admin_player_service.py` - Player business logic (queries, validation, parsing, CRUD)

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
- `news-items/index.html` - News items list with filters
- `news-items/form.html` - News item edit form
- `players/index.html` - Players list with pagination and filters
- `players/form.html` - Player create form
- `players/detail.html` - Player edit form

**Styles** (`app/static/css/`):
- `admin.css` - Comprehensive admin UI styles

**Tests** (`tests/integration/`):
- `test_admin_account.py` - Account management tests
- `test_admin_password_reset_ui.py` - Password reset flow tests
- `test_admin_crud_news_sources.py` - NewsSource CRUD tests
- `test_admin_news_items.py` - NewsItem CRUD tests
- `test_admin_players.py` - PlayerMaster CRUD tests (21 tests covering access control, list/create/edit/delete, filters)

### Architecture Decisions

#### Sub-Router Pattern
The admin routes are organized into sub-routers for maintainability:

```
app/routes/admin/
├── __init__.py      # Main router (prefix="/admin")
├── helpers.py       # Shared authentication helpers
├── auth.py          # Login/logout/password-reset
├── account.py       # Account management (prefix="/account")
├── news_sources.py  # CRUD operations (prefix="/news-sources")
├── news_items.py    # CRUD operations (prefix="/news-items")
└── players.py       # CRUD operations (prefix="/players")
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
The codebase uses explicit `await db.commit()` instead of the preferred `async with db.begin():` pattern documented in CLAUDE.md. This is tracked in GitHub issue #85.

**Reason**: Test fixtures configure sessions with active transactions for isolation, causing conflicts with nested `db.begin()` calls.

---

## Future Enhancements

Potential additions for future phases:

1. **Additional CRUD Tables**
   - Players management (enhance current implementation)
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
