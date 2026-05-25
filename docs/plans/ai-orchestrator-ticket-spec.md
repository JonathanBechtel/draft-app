# DraftGuru Orchestrator Ticket Spec

This file is the **per-repo override** for the universal `/create-project` skill. Anything below applies specifically to DraftGuru (`JonathanBechtel/draft-app`); the universal skill at `~/.claude/commands/create-project.md` is the fallback for everything not covered here.

The file is also read by orchestrated agents that need to run **browser verification** against this repo — the login recipe and dev-server commands below are the source of truth, not the values inlined in any specific ticket.

---

## Repo defaults

| Setting | Value |
|---|---|
| Owner | `JonathanBechtel` |
| Repo | `JonathanBechtel/draft-app` |
| Default agent model | `sonnet` (work tickets), `opus` (QA gate) |
| Conda env | `draftguru` |
| Test command | `conda run -n draftguru --no-capture-output python -m pytest <path>` |
| Type check | `conda run -n draftguru --no-capture-output mypy app --ignore-missing-imports` |
| Pre-commit | `conda run -n draftguru --no-capture-output pre-commit run --all-files` |
| Dev server | `make dev` (uvicorn with reload on `http://localhost:8000`) |
| Static screenshot harness | `make visual` (outputs to `tests/visual/screenshots/`) |

When `gh` auth is provided via `GH_TOKEN` and the env-var token lacks the `project` scope, prefix `gh` calls with `unset GH_TOKEN && ...` so the keyring credential is used instead. Switch the active keyring account to `JonathanBechtel` if it isn't already.

---

## Test layout

- `tests/unit/` — pure logic, no DB. Mirror `app/` layout.
- `tests/integration/` — DB + FastAPI routes via HTTPX. Requires `TEST_DATABASE_URL` and `PYTEST_ALLOW_DB=1`. Schema modules under `app/schemas/` must be imported by `tests/integration/conftest.py` to be created.
- `tests/visual/` — Playwright-driven static screenshot capture. Outputs under `tests/visual/screenshots/`.

This repo does **not** distinguish `integration/no_deps/` vs `integration/with_deps/`. Don't split the integration tier when generating tickets.

---

## Browser verification recipes

### Dev admin login

**Role:** `admin`

**Credentials source:** `.env` (gitignored). Required env vars:
- `DRAFTGURU_ADMIN_EMAIL`
- `DRAFTGURU_ADMIN_PASSWORD`

The orchestrator harness loads `.env` automatically via `scripts/with-db-env.sh`. If the agent needs them directly inside a Python process, `from app.config import settings` does not surface them — read from `os.environ`.

A dedicated dev-test admin account (`admin@draftguru.local`, separate from the repo owner's personal account) exists for this purpose. The credentials live only in `.env`; do not embed them in tickets, PR descriptions, screenshots, or commit messages.

**Recipe (Playwright MCP):**

1. `make dev` in the background; poll `http://localhost:8000` until it responds.
2. `browser_navigate` → `http://localhost:8000/admin/login`.
3. `browser_snapshot` to confirm the login form is visible (fields `email`, `password`, submit button).
4. `browser_fill_form` → `email = $DRAFTGURU_ADMIN_EMAIL`, `password = $DRAFTGURU_ADMIN_PASSWORD`.
5. `browser_click` the submit button.
6. `browser_wait_for` URL match `/admin/**`.

If `DRAFTGURU_ADMIN_EMAIL` / `DRAFTGURU_ADMIN_PASSWORD` are unset, halt and ask the user to populate `.env` rather than guessing or fabricating credentials.

### Anonymous (public pages)

No login needed. Just `make dev`, `browser_navigate`, and proceed.

### Visual screenshot capture

For static visual regression: `make visual` outputs PNGs to `tests/visual/screenshots/`. For ad-hoc captures during verification, use `browser_take_screenshot` and save under the same directory with a descriptive filename (e.g. `<feature>-<state>.png`).

---

## Ticket template overrides

The universal `/create-project` template applies. Specific overrides for this repo:

- **File paths** in the ticket's "Files to change" section should be absolute-from-repo-root (e.g. `app/schemas/foo.py`, not just `foo.py`).
- **Schemas** live in `app/schemas/` and require an explicit import in `tests/integration/conftest.py`. Every schema ticket should add that import — flag it in the "Files to change" list.
- **Routes** stay thin and delegate to `app/services/`. Tickets that add routes should also touch a service module; flag both.
- **UI tickets** must reference `docs/style_guide.md` for the visual language and use BEM-style class names per `CLAUDE.md`'s "Frontend Implementation Approach" section.
- **Migrations** for whole new tables use `SQLModel.metadata.create_all(bind=..., tables=[X.__table__])` per the "new tables only" guidance in `CLAUDE.md`. Existing-table changes use `op.add_column` / `op.alter_column` / etc.
- **Browser end-to-end** subsection in a UI ticket should reference this file by name for the login recipe ("Use the admin login recipe from `docs/plans/ai-orchestrator-ticket-spec.md`") rather than inlining steps 1–6 above.

---

## Maintenance

When this file changes, sync any structural updates (test directory layout, dev server command, conda env name) into the "Orchestrator workflow" section of `CLAUDE.md` and `AGENTS.md` — both files are read by agents and kept byte-identical for Claude / Codex parity.
