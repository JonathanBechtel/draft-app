# Draft Guru

FastAPI + SQLModel skeleton for a draft analytics app. Async DB access, clean startup/shutdown, and simple Players endpoints.

## Quick Start

- Create environment (Conda) — installs app dependencies plus dev/test tools:
  - `conda env create -f environment.yml`
  - `conda activate draftguru`

- Configure `.env` (see `.env.example`):
  - `DATABASE_URL` (e.g., Postgres URI)
  - `SECRET_KEY` (any non-empty string for now)
  - Optional: `ENV=dev`, `DEBUG=true`, `LOG_LEVEL=DEBUG`, `ACCESS_LOG=true`, `SQL_ECHO=true`

- Run the app:
  - Dev (auto-reload): `make dev`
  - Prod-like: `make run`
  - Override host/port: `HOST=127.0.0.1 PORT=9000 make dev`

Alternative run commands:
- `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
- `python -m uvicorn app.main:app --reload`
- `python app/main.py` (no reload)
- Optional pip install (outside Conda):
  - `pip install -e .` (runtime only)
  - `pip install -e .[dev]` (runtime + lint/test tooling)

- Run database migrations with Alembic (requires `DATABASE_URL` pointing at the target Neon branch or Postgres instance):
  - `export DATABASE_URL="postgresql+asyncpg://user:pass@host/db?sslmode=require"`
  - `make mig.up` — upgrade to the latest revision
  - `make mig.revision m="describe change"` — autogenerate a new revision file
  - `make mig.history` — view migration history
  - `make mig.current` — show the current revision applied to the database
  - `make mig.down` — revert the most recent revision (use with care!)

- Pin versions for CI (pip-friendly):
  - Generate a constraints file from your active env that avoids conda-specific file URLs:
    ```bash
    conda run --no-capture-output -n draftguru python - <<'PY'
    try:
        from importlib.metadata import distributions  # Python 3.8+
    except Exception:  # pragma: no cover
        from importlib_metadata import distributions  # backport
    pins = sorted({f"{d.metadata['Name']}=={d.version}" for d in distributions() if d.metadata.get('Name')})
    print("\n".join(pins))
    PY
    ```
    Save the output to `constraints.txt` and commit it.
  - CI prefers `constraints.txt`; if missing, it falls back to a pip-friendly `requirements.txt` (no `@ file:` entries). Otherwise it installs from project metadata.

## Testing

Integration tests target the same Postgres stack used in development. Configure a safe database (for example, a dedicated Neon branch) and opt in explicitly before running pytest:

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/draftguru_test"
export PYTEST_ALLOW_DB=1
pytest
```

`TEST_DATABASE_URL` overrides `DATABASE_URL` for the suite; omit it to reuse your default .env value. The guard flag prevents accidentally pointing tests at production. The fixtures create and drop tables around each test run, so the target database must be writable.

## Endpoints

- `GET /health`: simple health check
- `GET /players`: list players
- `POST /players`: create player (request body: PlayerCreate)
- `DELETE /players/{player_id}`: delete player (204 on success, 404 if missing)

## Notes

- Lifespan: In development, the app creates missing tables at startup; on shutdown, it disposes the DB engine.
- DB: Async SQLAlchemy + SQLModel; write operations are wrapped in `async with db.begin():` for atomic commits/rollbacks.
- Schemas: Separate create/read models to avoid overposting and to keep response shapes stable.

## Scraper

This repo includes a CLI scraper for NBA Draft Combine data (shooting, anthro, agility). See docs/scraper.md for installation, usage, and troubleshooting.
