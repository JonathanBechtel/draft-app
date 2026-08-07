HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: dev run mig.revision mig.up mig.down mig.history mig.current scrape ingest metrics bio.scrape bio.ingest draft-ingest
.PHONY: news-seed nba-seed nba-logos
.PHONY: college-mapping college-seed-data college-seed college-backfill college-logos

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
OUT ?= data/scraper-output
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

# Ingest actual draft-night results into draft_results (idempotent upsert by
# year+pick). Reads the canonical scripts/data/draft_results_<year>.txt file;
# the draft year is inferred from the file name. Picks resolve against this
# environment's player DB — unmatched names are reported, not fatal. This is the
# same script the deploy workflows run; see docs/draft_results_runbook.md.
# Usage:
#   make draft-ingest                  # ingest scripts/data/draft_results_2026.txt
#   make draft-ingest DRAFT_YEAR=2027  # ingest scripts/data/draft_results_2027.txt
#   make draft-ingest DRY=1            # parse + resolve, then roll back (preview)
DRAFT_YEAR ?= 2026
draft-ingest:
	$(PYTHON) scripts/ingest_draft_results.py \
		--file scripts/data/draft_results_$(DRAFT_YEAR).txt \
		$(if $(DRY),--dry-run,)

# Seed curated RSS news sources into the database
news-seed:
	$(PYTHON) scripts/seed_news_sources.py

# Seed NBA teams into database
nba-seed:
	$(PYTHON) scripts/seed_nba_teams.py

# Download and upload NBA team logos
# Usage:
#   make nba-logos                # all teams
#   make nba-logos TEAM=LAL       # single team
#   make nba-logos DRY=1          # dry-run (download + process only)
TEAM ?=
nba-logos:
	$(PYTHON) scripts/collect_nba_logos.py $(if $(DRY),--dry-run,) $(if $(TEAM),--team $(TEAM),)

# College school deduplication, seeding, and logo collection
college-mapping:
	$(PYTHON) scripts/generate_school_mapping.py

college-seed-data:
	$(PYTHON) scripts/generate_school_seed_data.py

college-seed:
	$(PYTHON) scripts/seed_college_schools.py

SCHOOL ?=
college-backfill:
	$(PYTHON) scripts/backfill_school_names.py $(if $(DRY),--dry-run,)

college-logos:
	$(PYTHON) scripts/collect_college_logos.py $(if $(DRY),--dry-run,) $(if $(SCHOOL),--school $(SCHOOL),)

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
	$(PYTHON) -m app.cli.compute_metrics --cohort $(COHORT) \
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
CACHE ?= data/scraper-cache/players
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
.PHONY: fmt lint lint.imports lint.filesize lint.complexity lint.complexity.update lint.migrations lint.stat-constants lint.stale-paths deploy.freshness fix precommit test coverage coverage.diff visual visual.headed
fmt:
	ruff format .

lint:
	ruff check .

# Structural import contracts (docs/plans/programmatic-code-discipline.md §3.1).
# Contracts live under [tool.importlinter] in pyproject.toml; CI runs the same command.
lint.imports:
	lint-imports

# Diff-scoped file-size ratchet (docs/plans/programmatic-code-discipline.md §1.4).
# Enforces against main the way CI does; pre-commit runs the same script in warn mode.
lint.filesize:
	python scripts/check_file_size_ratchet.py --against origin/main --enforce

# Per-file complexity ratchet (docs/plans/programmatic-code-discipline.md §1.6).
# Ruff's per-file-ignores silences a rule for a whole file; this catches growth
# *inside* those files. Counts may fall, never rise.
lint.complexity:
	python scripts/check_complexity_ratchet.py

# Rewrite the baseline after simplifying code (or when an increase is warranted
# and argued in the PR).
lint.complexity.update:
	python scripts/check_complexity_ratchet.py --update

# app/cli (shipped runtime jobs) vs scripts/ (operator tooling) boundary.
# Catches a Fly cron entrypoint pointed at scripts/, and any new app/ -> scripts/
# import. See CLAUDE.md "Executable code lives in two places".
lint.entrypoints:
	python scripts/check_runtime_entrypoints.py

# Stat-constant confinement (docs/plans/programmatic-code-discipline.md §1.3). Phase 2's
# closing ratchet: designated stat coefficients (0.44, the Game Score weights) may appear
# only under app/services/stats/. The one legitimate exemption is read from
# app.services.stats.registry.frozen_exemptions(), not hand-written here.
lint.stat-constants:
	python scripts/check_stat_constants.py

# Stale module paths from the phase-4 service reorganization (#797). Phase 4 moved
# app/services/summer_league/ into stats/ backbone/ ingest/ sources/; 37 prose
# references to the old path survived the move. docs/plans/ is exempt -- those
# documents describe the move and must name the old path. Whole-tree, and the
# checker fails rather than passes if it ever scans zero files.
lint.stale-paths:
	python scripts/check_stale_service_paths.py

# Index-build safety in new Alembic revisions (docs/plans/programmatic-code-discipline.md §1.7).
# Diff-scoped against main the way CI is: existing revisions have already run in
# production. Requires CONCURRENTLY *and* an autocommit block, for both op.create_index
# and raw `op.execute("CREATE INDEX ...")`. See incident #669.
lint.migrations:
	python scripts/check_migration_safety.py --against origin/main

# How far behind main is a deployed app? Incident #669 ran 3.5 days behind through the
# entire Summer League window with nothing reporting it. Read-only.
# Usage:
#   make deploy.freshness                       # prod vs origin/main
#   make deploy.freshness FLY_APP=draft-app     # staging
FLY_APP ?= draft-app-prod
deploy.freshness:
	python scripts/check_deploy_freshness.py --app $(FLY_APP) --report-only

fix:
	ruff check --fix .

precommit:
	pre-commit run -a

# Run unit tests
test:
	pytest tests/unit -q

# Run per-route query-count budgets (catches N+1s / waterfall growth).
# Loads .env for the test DB and sets the integration opt-in. See the
# analyze-page-perf skill and tests/integration/perf/.
perf:
	PYTEST_ALLOW_DB=1 scripts/with-db-env.sh conda run -n draftguru env PYTEST_ALLOW_DB=1 pytest tests/integration/perf -q

# EXPLAIN ANALYZE every query a route fires — use when adding/changing a query
# to confirm it is properly indexed. Set EXPLAIN_DATABASE_URL in .env to a Neon
# prod read-branch for faithful plans (dev volume Seq-Scans even with a good
# index); it falls back to DATABASE_URL otherwise. See the analyze-page-perf skill.
# Usage:
#   make explain ROUTE=/
#   make explain ROUTE=/consensus ARGS="--top 5"
#   make explain ROUTE=/news ARGS="--no-plans"
ROUTE ?= /
explain:
	scripts/with-db-env.sh conda run -n draftguru python -m scripts.explain_route $(ROUTE) $(ARGS)

# Run tests with coverage report (terminal + HTML)
# Usage:
#   make coverage              # unit + integration with terminal + HTML report
#   make coverage TESTS=tests/unit  # narrower scope
TESTS ?= tests/unit tests/integration
coverage:
	pytest $(TESTS) -q --cov=app --cov-report=term-missing --cov-report=html
	@echo "HTML report: open htmlcov/index.html"

# Patch (diff) coverage gate: fail if <80% of newly changed lines are covered.
# Mirrors the CI gate. Runs a coverage pass to refresh coverage.xml, then
# compares the working tree against COMPARE_BRANCH (merge-base).
# Usage:
#   make coverage.diff                          # gate against origin/main
#   make coverage.diff COMPARE_BRANCH=origin/foo # different base
#   make coverage.diff TESTS=tests/unit          # narrower scope (DB-free)
COMPARE_BRANCH ?= origin/main
DIFF_COVER_FAIL_UNDER ?= 80
coverage.diff:
	pytest $(TESTS) -q --cov=app --cov-report=xml
	diff-cover coverage.xml --compare-branch=$(COMPARE_BRANCH) --fail-under=$(DIFF_COVER_FAIL_UNDER)

# Run visual tests (requires server running on TEST_BASE_URL or localhost:8000)
# Usage:
#   make visual                          # run all visual tests
#   make visual TEST=test_homepage_full  # run specific test (partial match)
#   TEST_BASE_URL=https://... make visual # test against remote server
TEST ?=
visual:
	pytest tests/visual -v $(if $(TEST),-k $(TEST),)

# Run visual tests with visible browser (for debugging)
visual.headed:
	PLAYWRIGHT_HEADLESS=0 pytest tests/visual -v --headed $(if $(TEST),-k $(TEST),)

# Mobile usability audit: screenshots + overflow/tap-target/scroll checks at a
# phone viewport (see the inspect-mobile skill). Requires a running server.
# Usage:
#   make mobile-audit                                  # default routes vs localhost:8000
#   make mobile-audit BASE=http://localhost:8003       # different server
#   make mobile-audit ROUTES="/ /players/foo" WIDTH=320
BASE ?= http://localhost:8000
WIDTH ?= 390
ROUTES ?=
mobile-audit:
	python scripts/mobile_audit.py sweep --base $(BASE) --width $(WIDTH) $(if $(ROUTES),--routes $(ROUTES),)

# Install Playwright browsers (required once after installing playwright)
playwright.install:
	python -m playwright install chromium

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
