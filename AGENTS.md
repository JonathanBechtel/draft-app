# Product Overview, Purpose, and Agent Context

DraftGuru is a lightweight but sophisticated **NBA Draft analytics application** designed for fans, fantasy players, and data-curious users who want quick, visual, and statistically grounded insights about draft prospects. It blends **player bios**, **production data**, **combine & anthropometric metrics**, **consensus mock drafts**, and **nearest-neighbor similarity comps** into fast, attractive, shareable pages.

The core product ideas:

## 🎯 What the App *Is*
DraftGuru is a **public-facing analytics site** with the look and feel of a retro-style scouting dashboard.  
It provides:
- **Player Pages** with bios, stylized images, percentile bars, comps, and consensus rank.
- **Player-vs-Player Compare** pages for head-to-head scouting and fantasy debates.
- **Consensus Mock Draft** combining multiple public mocks into a blended, robust ranking.
- **News Feed** that aggregates per-player updates from curated RSS sources.
- **One-click share cards** (Player, Compare, Consensus, News) exported as clean PNGs for social platforms.
- **An Admin area** for manually curating data, controlling ingestion sources, and managing assets.

The philosophy:  
**Fast · Clean · Trustworthy · Lightweight · Easy to share · Low friction for AI-generated code.**

## 🧠 What the App is Meant to Do
DraftGuru is built to be:
- A **canonical database** of draft prospects with stable IDs and rigorous source mapping.
- A **reliable analytics layer** producing percentiles, z-scores, ranks, and similarity metrics via scheduled offline computation.
- A **simple, predictable API** for player lookups, comparisons, and consensus data.
- A **high-quality, low-JS frontend** using Jinja templates and vanilla CSS+JS (no bundler).
- A **share engine** for generating PNG cards from SVG specs.

Agents working on this repo should understand:
- The app has **strict design constraints** (light retro analytics aesthetic).
- The backend is intentionally **boring and conventional**, optimized for AI-assisted refactors.
- The frontend avoids complexity: **no frameworks**, no build step, pure HTML/CSS/JS.
- All analytics (percentiles, normalization, KNN) happen **offline** and are stored in the DB.
- Performance matters: page loads must be **very fast** and database reads **simple**.

## 📘 Business & Monetization Context
DraftGuru has multiple potential business models—affiliate integrations, fantasy-style games, premium scouting tools, etc. These are documented separately in **`BUSINESS_MODELS.MD`** (to be added).  
Agents may occasionally need to reference these business considerations to:
- understand why certain metrics matter,
- justify design decisions,
- support new user-facing features with sound product reasoning.

Whenever a change could influence monetization, growth, or user retention, refer to the upcoming business document for guidance.

---

# Definition of Done

**No task is complete until all checks pass.** Before considering any implementation finished:

1. **Run `make precommit`** — fix all ruff and formatting errors
2. **Run `mypy app --ignore-missing-imports`** — fix ALL type errors in the entire `app/` directory
   - Pre-commit only checks staged files; CI checks everything
   - Type changes can break files you didn't directly modify
   - This command must exit cleanly with no errors
3. **Run relevant tests** — `pytest tests/unit -q` at minimum; `pytest tests/integration -q` if touching DB/routes
4. **For UI changes** — run `make visual` and visually verify screenshots (see [Visual Testing](#visual-testing))

Do not ask if the user wants you to run these checks — run them proactively after completing implementation work. If any check fails, fix the issues before reporting that work is done.

---

# Repository Guidelines

## Project Structure & Module Organization
- `app/` contains FastAPI code split into `routes/`, `models/`, `schemas/`, `services/`, and `utils/`; keep request/response shapes alongside shared field helpers in `app/models/` (for example `app/models/fields.py`) and persistable tables plus mixins in `app/schemas/` (`app/schemas/base.py`).
- `tests/` is split into `unit/` (no DB required) and `integration/` (hits Postgres); mirror the `app/` layout so fixtures and helpers stay close to the code they cover.
- `alembic/` holds migration scripts; use it when schema changes extend beyond SQLModel auto-creation.
- `docs/` stores project notes such as `v_1_roadmap.md`; add any architecture decisions here.

## Tech Stack
- Backend: FastAPI with SQLModel/async SQLAlchemy targeting Postgres (asyncpg driver); Alembic for migrations.
- Models: Pydantic request/response models in `app/models`, SQLModel tables in `app/schemas`.
- Tooling: Python 3.12, Ruff for lint/format, mypy for types, pytest + pytest-asyncio + HTTPX for tests, pre-commit for hooks.
- Serving: Uvicorn entrypoints via `make dev`/`make run`; environment configured from `.env`.

## Design Reference
- Visual language: “light retro analytics” per `docs/style_guide.md`—Russo One for headings, Azeret Mono for data, soft neutrals with punchy accent colors, scoreboard/ticker motifs, and playful hover/micro-interactions.
- Mockups: see `mockups/draftguru_homepage.html` and `mockups/draftguru_player.html` for concrete layout, typography, and component patterns; follow these before introducing new UI patterns.
- Frontend changes should align with the established palette, typography, and card/ticker treatments (scanlines, pixel corners, subtle animations) unless a conscious design update is specified.

## Frontend Implementation Approach
- Keep assets simple: use a shared global CSS/JS (`/static/main.css`, `/static/main.js`) loaded from `base.html`; add per-page overrides via `{% block extra_css %}`/`{% block extra_js %}` and place page-specific files in `app/static/` (e.g., `home.css`, `home.js`).
- Naming: use kebab-case for files (`player-detail.css`, `player-detail.js`) and BEM-style classes for components (e.g., `.card`, `.card__header`, `.card--highlight`); keep JS functions small and page-scoped, initialized on `DOMContentLoaded`.
- No build step: rely on plain CSS and vanilla JS; serve assets via FastAPI static mount (`/static`) and reuse the design tokens defined in `main.css`.

## Build, Test, and Development Commands
- `make dev` boots the FastAPI server with autoreload (`uvicorn app.main:app --reload`) using `HOST` and `PORT` overrides when needed.
- `make run` launches a production-like instance without reloads—use this before shipping changes.
- `conda env create -f environment.yml` followed by `conda activate draftguru` provisions the Python 3.12 toolchain and app dependencies.

## Linting, Formatting, and Pre-commit
- `make fmt` runs `ruff format .`; `make lint` runs `ruff check .`; `make fix` applies autofixes via `ruff check --fix .`.
- `make precommit` executes the repo hooks (`ruff` + `ruff-format` + `mypy --ignore-missing-imports --python-version=3.12`) across the tree; install locally with `pre-commit install`.
- CI runs `mypy app --ignore-missing-imports`; pre-commit also runs mypy but only on the files it sees. If you touch types, run the CI command locally to catch anything that pre-commit might skip.
- Run `make precommit` (or at least `make lint` + `make fmt`) before committing so local changes mirror CI expectations.

## Coding Style & Naming Conventions
- Stick to 4-space indentation, type hints, and descriptive names (`PlayerRead`, `PlayerCreate`) to match existing modules.
- Keep SQLModel definitions in `app/models` for shared validation logic and persistable tables in `app/schemas`; prefer explicit field validators over ad-hoc runtime checks.
- Centralize configuration impact in `app/config.py` and avoid hard-coded secrets—read from `settings`.

## Docstrings & Comments
- Prefer Google-style docstrings for public functions/classes: summary line, Args/Returns/Raises sections as needed.
- For tests, include a short docstring describing the behavior under test, expected outcome, and relevant inputs/fixtures; keep them concise and focused on intent.
- Add inline comments sparingly to clarify non-obvious logic; avoid restating what code already conveys.

## API Patterns
- Keep routes thin in `app/routes/`; route functions should handle request/response wiring while delegating nontrivial logic to services.
- Inject `AsyncSession` via `Depends(get_session)` and wrap writes in `async with db.begin():` so commits/rollbacks are handled automatically; `await db.refresh(obj)` when you need fresh fields before returning.
- Use Pydantic request/response models from `app/models` (e.g., `PlayerCreate`, `PlayerRead`) at the edges and SQLModel tables from `app/schemas` (e.g., `PlayerTable`) for persistence; map between them via `.model_dump()`/constructor and rely on `response_model` to shape outbound payloads.
- Always set `response_model` and explicit `status_code` (e.g., 201 for creates, 204 for deletes) and raise `HTTPException` for error cases like 404.
- For list endpoints, apply deterministic ordering and consider pagination/filters as the surface grows.
- UI routes render templates via `request.app.state.templates.TemplateResponse(...)`; pass `request` in the context to satisfy FastAPI's templating requirement.

## Service Layer Patterns
- Services live in `app/services/` and contain business logic: query building, validation, parsing, and CRUD operations.
- Service functions are stateless: take `AsyncSession` as first parameter and let routes handle commits.
- Use dataclasses for internal DTOs (form data, query results); reserve Pydantic models for API request/response boundaries.

## Testing Guidelines
- Write pytest cases under `tests/` using filenames like `test_players.py`; group async client checks with `pytest.mark.asyncio`.
- Unit tests live under `tests/unit/` and should avoid DB dependencies. Integration tests live under `tests/integration/`.
- Integration tests require a disposable Postgres URL and an explicit opt-in: set `TEST_DATABASE_URL=postgresql+asyncpg://...` and `PYTEST_ALLOW_DB=1`, then run `pytest tests/integration -q`. Fixtures drop/create all SQLModel tables at session start and before each test for isolation, so the target DB must be safe to mutate.
- Use the shared fixtures in `tests/integration/conftest.py` (`app_client`, `db_session`) to exercise routes/services; they override the app’s DB dependency to reuse the test session.
- Aim for meaningful coverage on routers, services, and validators; document complex fixtures inline.
- Philosophy: favor integration-style checks that hit FastAPI via HTTPX and assert both HTTP responses and database state; avoid mocking the database unless a unit test truly needs isolation. Keep fixtures small and deterministic, use factory helpers instead of large seed dumps, and prefer testing behavior over implementation details (e.g., status codes, payload shapes, rows created). For pure utilities, write fast unit tests; for anything touching persistence or app wiring, use the provided async fixtures.
- Practice TDD with integration tests as the primary signal: write or update an integration test that captures the desired behavior before implementing a feature, then add unit tests for pure logic or edge cases as needed. Run the relevant integration subset early and often while iterating.

## Visual Testing

For UI changes, use Playwright to capture screenshots for visual verification. Run `make dev` first, then `make visual` to save screenshots to `tests/visual/screenshots/`. Read the PNGs to verify correctness. Use `make visual.headed` to watch the browser for debugging. See **[docs/visual_testing.md](docs/visual_testing.md)** for details.

## Commit & Pull Request Guidelines
- Follow the prevailing Conventional Commits style (`feat:`, `fix:`, `chore:`) observed in git history; keep subject lines under 72 characters.
- Each PR should describe intent, outline testing evidence (e.g., "`make run` locally"), and link to the tracking issue when available; include screenshots for UI-facing updates (`app/templates`).

## Configuration Tips
- Copy `.env.example` to `.env` and supply `DATABASE_URL`, `SECRET_KEY`, and optional toggles (`DEBUG`, `ACCESS_LOG`, `SQL_ECHO`); never commit real secrets.
- Use `describe_database_url()` logs to verify connection targets, and prefer async-friendly drivers (`postgresql+asyncpg`).
- `AUTO_INIT_DB` defaults to `true` for local convenience but is ignored automatically on Fly deployments; set it to `false` (or change `ENV` to `stage`/`prod`) when relying exclusively on Alembic migrations.

## Migration Workflow
- Define and adjust persistable tables in `app/schemas/`; these SQLModel classes are the canonical schema definition that Alembic inspects.
- Alembic auto-imports every schema module via `alembic/env.py`, so placing a new table module under `app/schemas/` is enough to make it discoverable for autogenerate.
- Generate revisions with `alembic revision --autogenerate -m "<message>"`; review the diff Alembic proposes and edit as needed so the upgrade/downgrade pairs mirror the SQLModel changes.
- **New tables only:** call `SQLModel.metadata.create_all(bind=..., tables=[MyTable.__table__])` in upgrade (and `drop_all` in downgrade) to create or tear down whole tables exactly as defined. Only use this when introducing or fully removing a table.
- **Existing tables:** keep the autogenerate-produced `op.*` statements (`op.add_column`, `op.alter_column`, `op.create_index`, etc.) so the migration performs the minimal DDL to reach the updated SQLModel shape. Never drop/recreate a production table just to change columns or constraints.
- For custom types or data backfills, keep the logic alongside the structural operations and tear down enum types by calling the column's `.type.drop` when needed.
- Test every revision against a disposable database: run `alembic upgrade head`, sanity-check the schema, then `alembic downgrade base` to confirm clean teardowns before sharing the change.

## Infrastructure
DraftGuru runs on **Fly.io** (staging: `draft-app`, prod: `draft-app-prod`) with **Neon Serverless Postgres** (project: `draftguru`; dev and prod are branches of this project). Use `flyctl` and `neonctl` CLIs for ops. See `docs/fly_infrastructure.md` for details.
