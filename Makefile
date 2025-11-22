HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: dev run mig.revision mig.up mig.down mig.history mig.current scrape ingest metrics bio.scrape bio.ingest

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
BBIO ?= $(shell ls -t $(OUT)/bbio_*.csv 2>/dev/null | head -n1)
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
POSITION_MATRIX ?=
MATRIX_SKIP_BASELINE ?=
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
	$(if $(POSITION_MATRIX), --position-matrix $(POSITION_MATRIX),) \
	$(if $(MATRIX_SKIP_BASELINE), --matrix-skip-baseline,) \
	$(if $(CATEGORIES), --categories $(CATEGORIES),) \
	$(if $(RUN_KEY), --run-key $(RUN_KEY),) \
	$(if $(MIN_SAMPLE), --min-sample $(MIN_SAMPLE),) \
	$(if $(NOTES), --notes "$(NOTES)",) \
	$(if $(DRY), --dry-run,) \
	$(if $(REPLACE), --replace-run,) \
	$(METRIC_ARGS)

# Basketball-Reference player bios: scrape and ingest
LETTERS ?=
ALL ?=
THROTTLE ?= 3
CACHE ?= scraper/cache/players
FIX ?=
FROM_INDEX_DIR ?=
FROM_PLAYER_DIR ?=
FROM_INDEX_FILE ?=
FROM_PLAYER_FILE ?=
CREATE_MISSING ?= 1

bio.scrape:
	$(PYTHON) scripts/bbref_bio_scraper.py $(if $(ALL),--all,) $(if $(LETTERS),--letters $(LETTERS),) --out-dir $(OUT) --throttle $(THROTTLE) $(if $(FROM_INDEX_DIR),--from-index-dir $(FROM_INDEX_DIR),) $(if $(FROM_PLAYER_DIR),--from-player-dir $(FROM_PLAYER_DIR),) $(if $(FROM_INDEX_FILE),--from-index-file $(FROM_INDEX_FILE),) $(if $(FROM_PLAYER_FILE),--from-player-file $(FROM_PLAYER_FILE),) $(if $(EXTRA_SLUGS),--extra-slugs $(EXTRA_SLUGS),) $(if $(EXTRA_SLUGS_FILE),--extra-slugs-file $(EXTRA_SLUGS_FILE),)

bio.ingest:
	@if [ -z "$(BBIO)" ]; then \
		echo "[error] No bbio CSV found. Pass BBIO=path/to/csv or run make bio.scrape first." >&2; \
		exit 1; \
	fi
	$(PYTHON) scripts/ingest_player_bios.py --file $(BBIO) --cache-dir $(CACHE) $(if $(DRY),--dry-run,) $(if $(VERBOSE),--verbose,) $(if $(OVERWRITE_MASTER),--overwrite-master,) $(if $(CREATE_MISSING),--create-missing,) $(if $(FIX),--fix-ambiguities $(FIX),)

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
