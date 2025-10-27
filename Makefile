HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: dev run mig.revision mig.up mig.down mig.history mig.current scrape ingest metrics

# Start FastAPI with auto-reload (development)
dev:
	python -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

# Start FastAPI without reload (production-like)
run:
	python -m uvicorn app.main:app --host $(HOST) --port $(PORT)

# Scrape NBA Draft Combine data
# Usage:
#   make scrape               # all seasons and sources (default)
#   make scrape YEAR=2024-25  # single season
#   make scrape SOURCE=anthro # specific source (all seasons unless YEAR set)
#   make scrape OUT=outdir    # custom output directory
PYTHON ?= python
SOURCE ?= all
OUT ?= scraper/output
ARGS ?=
scrape:
	$(PYTHON) scripts/nba_draft_scraper.py $(if $(YEAR),--year $(YEAR),) --source $(SOURCE) --out-dir $(OUT) $(ARGS)

# Ingest CSVs into database (dev DB by default via .env)
# Usage:
#   make ingest                  # ingest all sources from default out dir
#   make ingest YEAR=2024-25     # only one season
#   make ingest SOURCE=anthro    # only one source
ingest:
	$(PYTHON) scripts/ingest_combine.py --out-dir $(OUT) $(if $(YEAR),--season $(YEAR),) --source $(SOURCE)

# Derived metrics computation
COHORT ?= current_draft
RUN_KEY ?=
SEASON ?=
POSITION ?=
CATEGORIES ?=
MIN_SAMPLE ?=
NOTES ?=
DRY ?=
REPLACE ?=
METRIC_ARGS ?=

metrics:
	$(PYTHON) -m app.scripts.compute_metrics --cohort $(COHORT) \
	$(if $(SEASON), --season $(SEASON),) \
	$(if $(POSITION), --position-scope $(POSITION),) \
	$(if $(CATEGORIES), --categories $(CATEGORIES),) \
	$(if $(RUN_KEY), --run-key $(RUN_KEY),) \
	$(if $(MIN_SAMPLE), --min-sample $(MIN_SAMPLE),) \
	$(if $(NOTES), --notes "$(NOTES)",) \
	$(if $(DRY), --dry-run,) \
	$(if $(REPLACE), --replace-run,) \
	$(METRIC_ARGS)

# Lint & format
.PHONY: fmt lint fix precommit
fmt:
	ruff format .

lint:
	ruff check .

fix:
	ruff check --fix .

precommit:
	pre-commit run -a

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
