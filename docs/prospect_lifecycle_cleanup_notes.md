# Prospect Lifecycle Cleanup Notes

## Context

The `player_lifecycle.is_draft_prospect` flag is a useful product signal. It
distinguishes players who are still in the draft pipeline from players who have
already moved beyond that window, including drafted-not-in-NBA, NBA-active,
inactive former, and historical records.

Session #5 prod/global QA surfaced a data-quality issue: many production rows
are marked as draft prospects while missing `expected_draft_year`, and some old
draft-year records remain prospect-scoped. The issue is not that the flag is
wrong. The issue is that production needs a cleanup pass so the flag can be
trusted as a filter.

## Intended Semantics

Recommended lifecycle meaning:

- Current draft prospect:
  - `is_draft_prospect = true`
  - `expected_draft_year` is set
  - lifecycle is typically `college`, `international_amateur`, `high_school`,
    `recruit`, or `draft_declared`
- Drafted but not in NBA:
  - `is_draft_prospect = false`
  - `draft_status = drafted`
  - lifecycle is typically `drafted_not_in_nba`
- NBA active:
  - `is_draft_prospect = false`
  - lifecycle is `nba_active`
- Historical or inactive:
  - `is_draft_prospect = false`
  - lifecycle is `inactive_former` or an equivalent non-current state
- Unknown:
  - leave ambiguous rows for manual review unless another trusted field makes
    the lifecycle obvious

`is_draft_prospect` should not mean only "has never played in the NBA." A player
can have no NBA minutes and still no longer be a current draft prospect.

## Cleanup Approach

Handle lifecycle cleanup as its own reviewed data-quality pass before production
promotion.

1. Generate a read-only lifecycle cleanup plan CSV.
2. Classify rows with conservative rules:
   - `fix_now`: high-confidence, scriptable lifecycle correction.
   - `manual_review`: not enough information for automatic cleanup.
   - `waive`: acceptable mismatch with an explicit reason.
   - `backlog`: real issue, but not blocking Top 100 promotion.
3. Apply only reviewed high-confidence updates with a dry-run first.
4. Re-run the prod/global prospect integrity audit.

## Proposed Plan Columns

- `player_id`
- `display_name`
- `draft_year`
- `expected_draft_year`
- `draft_status`
- `lifecycle_stage`
- `competition_context`
- `current_affiliation_name`
- `is_draft_prospect`
- `proposed_is_draft_prospect`
- `proposed_expected_draft_year`
- `classification`
- `reason`
- `confidence`

## Conservative Rules

- Frozen Top 100 source rows should remain draft prospects with
  `expected_draft_year = 2026`.
- Rows with `draft_status = drafted`, `lifecycle_stage = nba_active`,
  `drafted_not_in_nba`, or `inactive_former` should usually not be current draft
  prospects.
- Rows with `draft_year < 2025` and `is_draft_prospect = true` are likely stale
  prospect flags unless explicitly waived.
- Rows marked as current prospects but missing `expected_draft_year` should be
  treated as blocking until fixed, waived, or moved out of prospect scope.

## Guardrails

- Do not perform production writes from the planning step.
- Save dry-run and execution logs for any later update script.
- Keep this separate from the Top 100 production promotion script.
- Do not use this cleanup to silently change the frozen Top 100 source snapshot.
