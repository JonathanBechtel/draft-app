HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: dev run mig.revision mig.up mig.down mig.history mig.current

# Start FastAPI with auto-reload (development)
dev:
	python -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

# Start FastAPI without reload (production-like)
run:
	python -m uvicorn app.main:app --host $(HOST) --port $(PORT)

mig.revision:
	alembic revision --autogenerate -m "$(m)"

mig.up:
	alembic upgrade head

mig.down:
	alembic downgrade -1

mig.history:
	alembic history --verbose

mig.current:
	alembic current
