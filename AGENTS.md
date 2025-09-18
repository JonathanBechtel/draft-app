# Repository Guidelines

## Project Structure & Module Organization
- `app/` contains FastAPI code split into `routes/`, `models/`, `schemas/`, `services/`, and `utils/`; keep request/response shapes alongside shared field helpers in `app/models/` (for example `app/models/fields.py`) and persistable tables plus mixins in `app/schemas/` (`app/schemas/base.py`).
- `tests/` is ready for pytest suites; mirror the `app/` layout so fixtures and helpers stay close to the code they cover.
- `alembic/` holds migration scripts; use it when schema changes extend beyond SQLModel auto-creation.
- `docs/` stores project notes such as `v_1_roadmap.md`; add any architecture decisions here.

## Build, Test, and Development Commands
- `make dev` boots the FastAPI server with autoreload (`uvicorn app.main:app --reload`) using `HOST` and `PORT` overrides when needed.
- `make run` launches a production-like instance without reloads—use this before shipping changes.
- `conda env create -f environment.yml` followed by `conda activate draftguru` provisions the Python 3.12 toolchain and app dependencies.

## Coding Style & Naming Conventions
- Stick to 4-space indentation, type hints, and descriptive names (`PlayerRead`, `PlayerCreate`) to match existing modules.
- Keep SQLModel definitions in `app/models` for shared validation logic and persistable tables in `app/schemas`; prefer explicit field validators over ad-hoc runtime checks.
- Centralize configuration impact in `app/config.py` and avoid hard-coded secrets—read from `settings`.

## Testing Guidelines
- Write pytest cases under `tests/` using filenames like `test_players.py`; group async client checks with `pytest.mark.asyncio`.
- Exercise CRUD flows through FastAPI's async TestClient or HTTPX, and isolate database state via fixtures that create temporary tables.
- Aim for meaningful coverage on routers, services, and validators; document complex fixtures inline.

## Commit & Pull Request Guidelines
- Follow the prevailing Conventional Commits style (`feat:`, `fix:`, `chore:`) observed in git history; keep subject lines under 72 characters.
- Each PR should describe intent, outline testing evidence (e.g., "`make run` locally"), and link to the tracking issue when available; include screenshots for UI-facing updates (`app/templates`).

## Configuration Tips
- Copy `.env.example` to `.env` and supply `DATABASE_URL`, `SECRET_KEY`, and optional toggles (`DEBUG`, `ACCESS_LOG`, `SQL_ECHO`); never commit real secrets.
- Use `describe_database_url()` logs to verify connection targets, and prefer async-friendly drivers (`postgresql+asyncpg`).
