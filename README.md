# Mini Draft Guru (draft-app)

FastAPI + SQLModel skeleton for a draft analytics app. Async DB access, clean startup/shutdown, and simple Players endpoints.

## Quick Start

- Create environment (Conda):
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

## Endpoints

- `GET /health`: simple health check
- `GET /players`: list players
- `POST /players`: create player (request body: PlayerCreate)
- `DELETE /players/{player_id}`: delete player (204 on success, 404 if missing)

## Notes

- Lifespan: In development, the app creates missing tables at startup; on shutdown, it disposes the DB engine.
- DB: Async SQLAlchemy + SQLModel; write operations are wrapped in `async with db.begin():` for atomic commits/rollbacks.
- Schemas: Separate create/read models to avoid overposting and to keep response shapes stable.
