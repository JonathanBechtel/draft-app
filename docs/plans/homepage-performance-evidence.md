# Homepage performance evidence

## Scope

This document records the first implementation pass for issue #561. The pass
uses the configured dev database as the working target, per the implementation
decision that dev and prod are currently close enough in size to make the first
optimization pass useful. It does not replace production-derived read-branch
verification.

## Baseline and current result

The in-process route profiler rendered `/` anonymously with `SQL_ECHO=false`:

| State | SQL statements | Notes |
| --- | ---: | --- |
| Dev, active/recap baseline | 65 | Before the homepage reductions in this change |
| Dev, active/recap current | 35 | After shared homepage reads, unused-count removal, and bounded review-path reads |
| Representative off-window fixture ceiling | 32 | Enforced by the route-budget test |

The baseline run took approximately 6.4 seconds end-to-end, with approximately
5.4 seconds spent in SQL calls. The current run took approximately 5.2 seconds
end-to-end, with approximately 4.3 seconds spent in SQL calls, and returned
HTTP 200 with the homepage content intact. The query count is down by 30
statements, or about 46%.

## Changes measured

- Reused the consensus board already loaded by the homepage across movers,
  controversy, spotlight, and attribution panels.
- Added request-scoped caches for the consensus snapshot, source analytics,
  boards, board entries, and sources.
- Joined the expanded trending player, status, and latest college-stat reads.
- Joined combine-grade lookup to the player/year snapshot relationship.
- Combined trending content mix and dominant tags into one aggregate union
  read, while keeping recent mention previews in bounded top-N queries.
- Skipped the news total count and video feed count/page subquery on the
  homepage, where totals are not rendered.

## Remaining gap

The dev ceiling is now committed as 32 statements off-window and 35 statements
for the in-window full-page budget. This is materially below the prior 52/55
ratchets but above the issue's final 15-statement contract. The remaining work
is a larger homepage read-model consolidation across the still-independent
Desk, consensus, trending, news, podcast, and film-room reads.

Before launch, rerun the same cold/warm and plan capture against a disposable
production-derived Neon read branch. Compare query shape, row counts, index
usage, and latency; fix any distribution- or indexing-specific gaps there.

No new player-data store was introduced. The work reuses existing canonical
assertions and read services, preserving the global player-journey graph's
assertion/projection boundary.
