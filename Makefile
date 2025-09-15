HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: dev run

# Start FastAPI with auto-reload (development)
dev:
	python -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

# Start FastAPI without reload (production-like)
run:
	python -m uvicorn app.main:app --host $(HOST) --port $(PORT)
