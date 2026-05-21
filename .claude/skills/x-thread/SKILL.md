---
name: x-thread
description: Draft one long-form X (Twitter) thread about an NBA draft prospect using DraftGuru data. Picks an angle, gathers facts and images, asks Claude to author the tweets in analyst/scout voice, and persists a reviewable draft. Pairs with /loop for steady throughput.
allowed-tools: Bash, Read, Write, Edit
---

# X Thread Skill (draft + review)

Generate one X thread per invocation. Each fire:

1. Picks an angle and subject (deduped via the `x_post_history` table).
2. Gathers DB facts + images via `scripts/x_threads/gather.py`.
3. Authors 5–9 tweets in analyst/scout voice (your job, this turn).
4. Persists the draft via `scripts/x_threads/save_draft.py`.
5. Reports the draft directory to the user for review.

Posting to X is **not** wired up yet; `post_to_x.py` is a stub. Drafts are saved
to `scripts/x_threads/drafts/<date>/<time>_<angle>_<slug>/` for manual review.

## Step-by-step

### 1. Pick + gather

Run the gather script. It picks an angle, queries the DB, renders any share
cards, and writes everything to a draft directory. The directory path is the
last line of stdout.

```bash
conda run -n draftguru python -m scripts.x_threads.gather
```

Optional flags: `--angle spotlight|h2h|outlier|news_tag`, `--window-days N`.

Capture the printed path. Then read the `gather.json` inside that directory —
it contains the angle, headline, player(s), facts, comps, news (when relevant),
and the relative paths to any pre-rendered images.

If the script exits non-zero with `no_viable_angle`, stop and tell the user
the dedup window has caught up to the pool — they can widen `--window-days`
or wait. Don't try a different angle silently.

### 2. Author the thread

Read `gather.json`. Draft **5–9 tweets** in analyst/scout voice. Each tweet
≤ 280 characters. Format the tweets file as one tweet per block, separated
by a line containing only `---`:

```
Lead tweet text.

Optional second paragraph inside the same tweet.
---
Second tweet.
---
Third tweet.
```

Write that file to `<draft_dir>/tweets.txt`.

**Voice rules.** Confident, data-forward, mild swagger. The reader should feel
like they're reading a scout's notebook, not a hype machine.

- Lead with the most surprising number, not the player's name. ("7'4\" wingspan
  on a 6'5\" frame — that's the second-biggest plus-wingspan in the 2025 class.")
- Use specific numbers with units, not adjectives. ("32 inch standing vert",
  not "elite athleticism").
- Tie facts to scout-relevant implications. ("That reach gives him a center's
  contest radius at the 3.")
- Earn the comp. If you cite a similar player, say why the comp lands.
- Skip exclamation points. Skip emoji. No "🚨" or "🔥".
- End the thread with a forward look or open question, not a CTA.

**Voice anchors** (study these before you write):

```
Outlier — Cooper Flagg (Duke)

7'5" wingspan on a 6'8.5" frame — fourth-best plus-wingspan
in the 2025 combine sample (n=78).

That puts him in the same anthro neighborhood as Jaylin Williams,
Tari Eason, and Cam Whitmore — wings whose length compensates
for sub-6'9" measured height.

The question with Flagg was never the frame. It's the jumper.
Spot-up shooting sits at the 41st percentile in the combine drills.
At his projected role, that's a workable but not bankable number.
```

```
H2H — Dylan Harper vs Ace Bailey

Same draft class, same projected lottery range, very different
profiles.

Harper: 99th percentile wingspan, 92nd percentile standing reach,
38th percentile vertical. A length guard, not an athlete guard.

Bailey: 71st percentile wingspan, 96th percentile max vertical,
89th percentile lane agility. The athleticism comes off the floor,
not from the frame.

The split shows up cleanly in the comp lists. Harper's neighbors
are wing-sized lead handlers; Bailey's are shot-creator athletes.

Two different bets on what survives the NBA transition.
```

(These are voice anchors, not facts to copy. Use the numbers from gather.json.)

### 3. Save the draft

```bash
conda run -n draftguru python -m scripts.x_threads.save_draft \
  --draft-dir <draft_dir> \
  --tweets-file <draft_dir>/tweets.txt
```

The script:
- Validates each tweet is ≤ 280 chars (use `--allow-long` only if you mean to).
- Writes `thread.txt` to the draft dir.
- Inserts an `x_post_history` row with `status=draft`.

If a tweet is over 280 chars, the script refuses to save and prints which
tweet. Trim the tweet and re-run; don't reach for `--allow-long`.

### 4. Report

Tell the user:
- The chosen angle and subject.
- The draft directory path.
- A one-line preview (the lead tweet).
- The `x_post_history.id` (from the save_draft JSON output).

Stop after that. Do not attempt to post — `post_to_x.py` is intentionally
stubbed until X API credentials exist.

## When something goes wrong

- **Gather fails with no images**: The skill still works; the thread can stand
  on text. Don't retry just to get images.
- **DB connection error**: Surface it and stop. Don't write a draft without
  data — analyst-voice tweets need real numbers.
- **Outlier finder picks a player whose narrative feels off**: Re-run with
  `--angle <different>` once, not in a loop. The dedup table will protect
  against churn over time.

## Files this skill touches

- `scripts/x_threads/gather.py` — picks angle, gathers data, renders images
- `scripts/x_threads/save_draft.py` — persists tweets + logs `x_post_history`
- `scripts/x_threads/post_to_x.py` — stub for future posting
- `scripts/x_threads/drafts/` — output directory (gitignored)
- `app/services/x_threads/` — picker, outlier finder, gatherer, image builder
- `app/templates/x_threads/` — custom SVG templates (outlier, riser)
- `app/schemas/x_post_history.py` — dedup + audit table

## Loop usage

For autonomous throughput, wrap this skill in `/loop`:

```
/loop 90m /x-thread
```

90 minutes is a reasonable cadence — leaves time for review between drafts
and lets the dedup window do its job. Shorter cadences will exhaust the
viable pool quickly.
