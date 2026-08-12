# Journal Analytics

**Status:** Complete
**Owner:** Journal
**Last Updated:** 2026-08-12

A set of views over the daily journal, in two generations:

- The **emotion wheel** (`#212`) — a static, single-window aggregation answering "what did I feel most often". The daily journal logs a `feeling:` field per entry plus up to two more levels of follow-up detail; the wheel aggregates that chain across a selectable time window and renders it as a radial wheel.
- Four **trend views**, added as a direct response to feedback on the wheel: it says nothing about *trajectory*, and with entries spanning months in sparse, non-contiguous bursts, "trajectory" mostly means "how sparse and clustered is this, actually" before anything else. See [Trend Views](#trend-views) below.

This is unrelated to the CRM's two-tier `SourceEntity`/`PersonEntity` model (see [data-model.md](data-model.md)) — journal entries are plain vault notes, not CRM interactions, and these views read the vault directly rather than going through the search index. The one exception is [Felt vs. Recorded Connection](#view-c-felt-vs-recorded-connection), which deliberately does cross into entity resolution and interaction history — see that section for why and how it stays privacy-safe.

---

## Table of Contents

1. [Data Source](#data-source)
2. [The Emotion Chain](#the-emotion-chain)
3. [Handling "Not sure"](#handling-not-sure)
4. [Aggregation API](#aggregation-api)
5. [Value Disclosure Policy](#value-disclosure-policy)
6. [File Access Safety](#file-access-safety)
7. [Emotion Wheel View](#emotion-wheel-view)
8. [Sample Size Honesty](#sample-size-honesty)
9. [Deviations From a Literal Plutchik Wheel](#deviations-from-a-literal-plutchik-wheel)
10. [Trend Views](#trend-views)
    - [Shared Grid Semantics](#shared-grid-semantics)
    - [View A: The Strip](#view-a-the-strip)
    - [View B: The Unexplored Wheel](#view-b-the-unexplored-wheel)
    - [View C: Felt vs. Recorded Connection](#view-c-felt-vs-recorded-connection)
    - [View D: The Scalar Stack](#view-d-the-scalar-stack)
11. [Rejected: A Transition/Markov View](#rejected-a-transitionmarkov-view)
12. [Known Data Quirk: "Bad feelings" vs. `bad_feeling`](#known-data-quirk-bad-feelings-vs-bad_feeling)

---

## Data Source

Daily journal entries live at `<vault_path>/Personal/Journal/YYYY-MM-DD.md`, one file per day the entry was made (not every calendar day has one). Each entry has YAML frontmatter with a `date:` field and a `feeling:` field, plus other logged fields (`mood`, `stress`, `sleep`, `body`, `alcohol`, `eating`, `caffeine`, `exercise`, `one_word`) that this feature does not use — the issue asked for the emotion wheel, not a general journal dashboard.

## The Emotion Chain

`feeling:` is the root of up to a three-level chain, where each level's value names the frontmatter key that holds the next level:

```yaml
feeling: Happy
happy_feelings: Cozy
cozy_feelings: Sunny
```

(Values here are illustrative, not real journal content.) This reads as **Happy → Cozy → Sunny**. The child key is built by lowercasing the parent's value and appending `_feelings` — except that **some branches use the singular `_feeling` instead of `_feelings`**. The scoping note for this issue assumed only the `Bad` branch was singular; the real vault has at least one more singular-keyed branch beyond `Bad`. Rather than hardcode a list of which values are irregular, the parser (`api/routes/journal.py::_next_link`) tries the plural key first and falls back to the singular key for **every** value, so any future irregular branch resolves without a code change:

```yaml
feeling: Bad
bad_feeling: Wobbly          # singular key
wobbly_feelings: Fizzy
```

Chains **terminate at any depth** — many real entries stop after level 1 or level 2 because no follow-up field was filled in. The aggregator does not assume depth 3; a chain is just whatever keys resolve before the lookup returns nothing.

Values can be **multi-word** (e.g. "Not sure"). The slug used for the next lookup replaces spaces with underscores (`not_sure_feelings`), so a value being multi-word doesn't break the chain whether it appears as a value to look up *or* as the basis of a lookup key for its own children.

## Handling "Not sure"

"Not sure" is a legitimate value the journal template offers at every level — it is not missing data, and it can appear:

- **As a root value with no children** (the person didn't have anything more specific to say about their overall feeling that day).
- **As a mid-chain or leaf value under an entirely different branch** (e.g. under a `Bad` or `Sad` root, several steps into the chain).

The aggregation tree (`build_wheel`) keys nodes by their **position in the chain**, not by value alone, so every occurrence of "Not sure" renders as its own wedge in whichever branch it actually appeared — a root-level "Not sure" and a leaf-level "Not sure" three steps into a `Sad` chain are visually and numerically distinct, never merged into one bucket. This was a deliberate choice over the alternative (bucketing all "Not sure" occurrences together): merging would imply an "uncertain about everything" reading that the source data doesn't support — each occurrence reflects uncertainty about a specific, different question (overall mood vs. the specific texture of an already-named feeling).

## Aggregation API

**Endpoint:** `GET /api/journal/emotions?window=<window>`

`window` is one of `day`, `week`, `month`, `quarter`, `all-time` (defaults to `all-time` — with a data set this sparse, a narrow default window would often show nothing). An unrecognized value falls back to `month` rather than a 400, matching the existing CRM period-parameter convention (see [crm-analytics.md](crm-analytics.md)).

```json
{
  "window": "month",
  "start_date": "2026-06-01",
  "end_date": "2026-06-30",
  "total_entries": 5,
  "emotion_entries": 4,
  "wheel": [
    {
      "value": "Happy",
      "count": 3,
      "children": [
        {"value": "Cozy", "count": 2, "children": [
          {"value": "Sunny", "count": 2, "children": []}
        ]},
        {"value": "Giddy", "count": 1, "children": []}
      ]
    },
    {
      "value": "Bad",
      "count": 1,
      "children": [{"value": "Wobbly", "count": 1, "children": []}]
    }
  ]
}
```

(Sample data above is invented — see [Privacy-First Documentation](../../AGENTS.md#privacy-first-documentation).)

`wheel` is a pre-aggregated tree (value / count / children), sorted by count descending then alphabetically — the frontend renders it directly rather than re-deriving structure from raw rows, matching the response shape used by the CRM interaction dashboards.

The response tracks two counts, and callers must not confuse them:

- `total_entries` — every valid dated journal file in the window (see [File Access Safety](#file-access-safety) for what "valid" means).
- `emotion_entries` — the subset of those that had a parseable `feeling:` value. Wheel node counts and percentages are always fractions of `emotion_entries`, not `total_entries`.

An earlier version of this endpoint had a single `entry_count` field that actually meant `emotion_entries`, so a window of mostly feeling-less entries (say, 6 of 7 dated files with no `feeling:` at all) silently looked like "1 entry, 100% coverage" instead of what it really was — 1 of 7 entries carrying emotion data. That's the same "missing data presented as a complete small answer" failure mode described in [Sample Size Honesty](#sample-size-honesty), just one level up (missing *fields*, not missing *entries*) — worth naming explicitly since it's exactly the class of bug this feature exists to avoid, not fix elsewhere.

A child's `count` can be less than its parent's `count`: that's not a bug, it's the chain terminating early for some of the parent's entries. The wheel view (below) renders this as empty space in the outer ring rather than forcing every branch down to a fixed depth.

An unrecognized `window` value is normalized to `"month"` *before* both computing the date bounds and populating the response's `window` field, so `?window=banana` returns month-bounded data labeled `"window": "month"` — never an echoed-back value that doesn't match what was actually computed.

## Value Disclosure Policy

`feeling:` and its follow-up fields are free text as far as the parser is concerned — today's Google Form only offers a fixed set of buttons, but a hand-edited entry (or a future form change) could put anything there, including a full sentence about a specific real event. Because chain values become public display text (wedge label, legend row, tooltip, and the raw JSON response), returning them verbatim would turn a display bug into a disclosure risk the moment someone edits a file by hand.

The fix is **not** an allowlist of the current ~6 primary and ~17 secondary values. That taxonomy comes from the Google Form and changes independently of this code; hardcoding it would silently start dropping or misclassifying real data the moment the form's options change — a worse failure than the one being fixed, and exactly the kind of brittle-to-the-source-format mistake this codebase has spent effort removing elsewhere.

Instead, every chain value passes through a conservative **shape policy** (`_is_plausible_label` in `api/routes/journal.py`) before it can be displayed:

- At most 30 characters.
- At most 3 words.
- No line breaks.

Every real observed value is a single word, or "Not sure" (two words) — these caps have generous headroom over that, so ordinary vocabulary growth never trips them. A value that fails the check is replaced with a neutral `"Unrecognized"` label; the raw text is never returned in the API response or rendered anywhere. Multiple different failing values in the same window collapse into one shared `"Unrecognized"` wedge (their counts sum) rather than each getting its own.

Separately, the number of *distinct* values (excluding the shared `"Unrecognized"` bucket) that can each get their own wedge in one response is capped at 50 (`build_wheel`'s `max_distinct_values`). The shape policy alone doesn't bound this: a corrupted file could in principle contain hundreds of distinct short strings that each individually pass the shape check. Past the cap, further not-yet-seen values are folded into `"Unrecognized"` too, so the wheel stays bounded regardless of how much distinct garbage a corrupted file contains. Real vocabulary (~23 words) sits far under this cap in normal operation.

Traversal itself (which frontmatter key to look up next) always uses the raw, pre-policy value — a free-text value can't sensibly continue the chain in practice anyway, but even if some future entry made it look like it could, the chain must still never leak the text that got it there.

## File Access Safety

The aggregator reads whatever files are directly inside `<vault_path>/Personal/Journal/`, so `_iter_valid_journal_files` (`api/routes/journal.py`) treats a file as a trustworthy journal entry only if all of the following hold:

- **Filename is canonical.** The file must be named `YYYY-MM-DD.md`; the date comes from the filename, never from frontmatter alone. This also means a file that doesn't match the pattern at all (an `Index.md`, a README, anything else someone drops in the folder) is never admitted just because it happens to carry `date:` and `feeling:` fields.
- **Not a symlink, and the resolved path stays inside the real journal directory.** A symlink placed in the journal folder pointing at, say, a therapy note elsewhere in the vault must not be followed and aggregated just because it landed in this directory — that would be a real privacy-boundary violation, not just a data-quality one. Both checks are redundant with each other by design (defense in depth): rejecting symlinks outright catches the direct case, and the resolved-path check catches indirect cases (e.g. a symlinked ancestor directory).
- **Frontmatter `date:` agrees with the filename, if present at all.** If the two disagree, the entry isn't safely attributable to either date, so it's skipped rather than silently trusting one or the other.
- **Readable as UTF-8 and parseable as frontmatter.** A file with invalid UTF-8 bytes or malformed YAML is skipped, not raised — one bad file must not turn the whole endpoint into a 500.

Every request re-reads and re-parses every `*.md` file directly in the journal directory (no caching, no index). That's a non-issue at the real-world scale here — dozens of entries as of this writing — and stays that way as long as the journal is what it is: a hand-written daily note, not a high-volume log. It's the ceiling on this design, recorded here so a future reader with a much larger journal (or a different data source entirely) knows to revisit it rather than rediscover it.

## Emotion Wheel View

**URL:** `/journal` (linked from the LifeOS home page)

A single self-contained page (`web/journal.html`, vanilla JS + inline SVG, no build step) with:

- A **window selector** (day / week / month / quarter / all-time pills).
- A **sample-size banner** stating `total_entries`, `emotion_entries` (when they differ), and the date range for the current window in plain text above the wheel — see [Sample Size Honesty](#sample-size-honesty).
- The **wheel itself**: a radial partition (sunburst) diagram. Each ring is one level of the chain; a wedge's angular width is proportional to its count relative to its parent's count, so early-terminating chains naturally leave a visible gap in the outer ring rather than needing special-case handling.
- A **legend** listing each top-level emotion with its count and percentage of the window, as a text-based fallback for anyone who prefers reading counts to reading wedge sizes.
- Hover tooltips on wedges showing the full chain path, count, and percentage of the window.

Color is assigned by hashing each value's text to a hue (`fnv1aHue` in `web/journal.html`), not by a hardcoded emotion→color table — so any new value the journal template introduces gets a stable, consistent color with no code change.

## Sample Size Honesty

The journal has produced entries sporadically since it started, and any given window can easily contain zero, one, or a handful of entries — and not every entry that exists carries emotion data. A wheel built from three entries visually resembles a wheel built from three hundred if nothing calls that out — the same "presenting a thin answer as a complete one" failure mode this codebase has been auditing for elsewhere in chat responses this week.

The view addresses this in four ways:

1. The sample-size banner **always** states both counts and the date range, in the same place, regardless of window size — it's not a footnote.
2. When `total_entries` and `emotion_entries` differ, the banner says so explicitly ("N of M entries had emotion data") instead of only reporting the emotion-bearing count as if it were the entry count — see the [Aggregation API](#aggregation-api) section for why this distinction exists at all.
3. Below a threshold (fewer than 5 emotion-bearing entries), the banner adds an explicit caveat ("small sample, read the wheel loosely") rather than letting a thin wheel pass as a confident one. The threshold is keyed to `emotion_entries`, since that's the denominator the wheel's percentages actually use.
4. **Zero entries, or zero with emotion data,** renders no wheel at all — an empty ring would visually claim "nothing happened" when the true state is "no data" (or "data without a logged feeling"), so the view shows a plain text message distinguishing the two instead.

## Deviations From a Literal Plutchik Wheel

The issue suggested "the emotion wheel (likely Plutchik or a derivative)." A literal Plutchik wheel has eight fixed primary emotions (joy, trust, fear, surprise, sadness, disgust, anger, anticipation) with fixed color relationships and intensity rings. The journal's actual vocabulary (`Happy`, `Bad`, `Not sure`, `Sad`, `Disgusted`, `Angry`, plus 17+ distinct level-2/3 words) doesn't map cleanly onto Plutchik's eight primaries — forcing a mapping would mean inventing a taxonomy the data doesn't actually express (e.g. deciding whether "Bad" means Plutchik's sadness, disgust, or something else).

Instead, this view is the "derivative" the issue allowed for: a radial partition driven directly by whatever chain values actually appear in the data, with no assumed taxonomy. It reads visually like a Plutchik-style wheel (concentric rings, wedge-per-emotion) without requiring an invented mapping, and it automatically accommodates new level-1 values the journal template might introduce later.

The animated/temporal view the issue also proposed was scoped out: with entries spanning `~5` months non-contiguously (dozens, not hundreds, of entries as of this writing), a day-by-day animation would spend most of its runtime on days with no entry at all. The static, window-selectable wheel is a better fit for how sparse and irregular the data actually is; a temporal view is a candidate for a follow-up issue once entry volume grows.

That follow-up arrived sooner than "once entry volume grows" — operator feedback on the wheel itself was that the sparsity is the story worth telling *now*, not something to defer until there's more data to smooth over it. See [Trend Views](#trend-views) below, especially [View A: The Strip](#view-a-the-strip), which is the non-animated answer to the same "what does the timeline actually look like" question.

---

## Trend Views

Four additional views, added in response to feedback that the wheel above says nothing about trajectory. All four live in a separate module (`api/routes/journal_trends.py`) and a separate page (`web/journal-trends.html`, linked from the wheel and from the LifeOS home page), rather than folding into `journal.py` / `journal.html` — each pulls in a data source the wheel never needed (`data/gsheet_sync.db` for view B; `data/interactions.db` and `api/services/entity_resolver.py` for view C), and `journal.py` was already a complete, self-contained 372-line unit before this work started. All four reuse rather than reimplement the wheel's window semantics (`window_bounds` / `_canonical_window`), file walk (`_iter_valid_journal_files` / `collect_window`), and value-disclosure policy (`_is_plausible_label`) — see the module docstring in `journal_trends.py` for exactly which names are imported from `journal.py` and why.

None of the four read `resonant_moment` (free text, the most sensitive field in the file) or `one_word` — the same boundary the wheel already draws.

### Shared Grid Semantics

Two of the four views (the strip, the scalar stack) need every calendar day in a range, not just the days that happen to have an entry — a full grid, with gaps rendered as gaps. The wheel's existing `window_bounds("all-time")` returns `start=None` ("no lower bound"), which is right for an aggregation that only ever needs a total, but can't drive a finite grid.

For grid views, "all-time" instead means **from the earliest entry actually in the window** — the tightest honest span, not an arbitrary long one. `day` / `week` / `month` / `quarter` keep `window_bounds`'s fixed bounds exactly as-is, including when the window is empty: a short window's grid does not shrink just because it happens to contain no entries, since an all-gap grid for a short window is itself useful information ("nothing happened in the last 7 days," rendered as 7 visible gap cells, not as an absence of a view). This logic lives in `journal_trends._grid_span`.

The grid's *end* is always the window's end (`today`, for `all-time`/`day`/`week`, or the fixed window boundary otherwise) — never the date of the last entry. For a journal that's gone quiet for weeks, the trailing gap between the last real entry and today is exactly the kind of thing this whole feature exists to surface, not to trim away.

### View A: The Strip

**Endpoint:** `GET /api/journal/strip?window=<window>`

One cell per calendar day across the grid (see above), colored by that day's primary (level-1) emotion. Every day gets a cell in one of three distinct states — never just two:

- **Gap** (`has_entry: false`) — no journal file that day at all.
- **Entry, no feeling** (`has_entry: true, primary_emotion: null`) — a file exists but had no parseable `feeling:`.
- **Entry with a feeling** (`has_entry: true, primary_emotion: "<value>"`) — colored by the same deterministic hash-to-hue scheme the wheel uses (`fnv1aHue`, duplicated into `journal-trends.html` rather than factored into a shared JS file — see below), so a given emotion word gets the same color on both pages.

This is deliberately the plainest possible rendering of sparsity: a flat horizontal strip, not a calendar-month grid or a heatmap-by-week-row, because the point is to make the *bursts* (a cluster of colored cells, then a long unbroken run of gap cells, then another cluster) immediately visible without any per-cell decoding effort. At n=1, the strip is a single colored cell inside however many gap cells the window contains — still legible, and the gaps around it are exactly as informative as the one real entry. At n=0 (no entries in the window at all, only possible for `all-time` since shorter windows always render *some* grid), the view shows a plain "no entries in this window" message instead of an empty or all-gap strip, which would otherwise look identical to "the grid exists but everything happens to be a gap."

### View B: The Unexplored Wheel

**Endpoint:** `GET /api/journal/taxonomy?window=<window>`

The existing wheel can only draw the emotion words that were actually logged — it structurally cannot show what *wasn't* reached for. This view renders the full form taxonomy (every branch the Google Form offers a follow-up question for), with used branches proportional to frequency and unused branches as dim outlines, answering "what vocabulary do I actually use, out of what's available."

**The taxonomy is derived, never hardcoded.** A manual inspection of one real vault's `data/gsheet_sync.db` found roughly 48 branch words (`Fearful`, `Scared`, `Angry`, `Let down`, `Sad`, `Happy`, `Bad`, and so on) — but that list is specific to one person's Google Form configuration and is not checked in anywhere; hardcoding it would (a) bake a specific person's private form structure into an open-source codebase (see the project's own contribution guidelines on personal-value hardcoding) and (b) silently go stale the instant the form changes, exactly the failure mode the wheel's own value-disclosure policy was built to avoid for free text. Instead, `journal_trends._derive_taxonomy_labels` reads `data/gsheet_sync.db` -> `synced_rows.raw_data` (the JSON blob of raw Google Form column names -> answers that `api/services/gsheet_sync.py` stores for every synced row) and extracts every column name that looks like a branch follow-up question — anything ending in `feelings`/`feeling`, case-insensitively, with an optional trailing `?` (Google Forms often keeps the question mark in the header). The branch label is whatever precedes that suffix, taken verbatim from the form's own column text. Every row ever synced is scanned, not just the newest, so a branch the form used to offer but has since removed still shows up as "available, unused" instead of disappearing.

Extracted branch names pass through the wheel's existing `_is_plausible_label` shape check before becoming a label — defense in depth, since this source is form structure rather than journal free text, but a corrupted or unexpected row shouldn't be able to inject an oversized "branch" into the response either.

Usage frequency comes from flattening every parsed emotion chain in the window (all positions, not just the root) into a value → count table, then matching case-insensitively against the derived branch labels. A value that was actually logged but doesn't match any derived branch — a stray non-branch value like `Not sure`, the `Unrecognized` bucket from the disclosure policy, or genuine taxonomy drift if the form changed after the sheet was last synced — is never dropped; it appears in a separate `extra_used` list instead of being folded into a branch it doesn't actually belong to.

**Degraded mode.** When `data/gsheet_sync.db` doesn't exist, has no `synced_rows` table, or has the table but zero matching columns, `_derive_taxonomy_labels` returns `[]` and the response sets `taxonomy_source: "used-only"`: `branches` is empty, and every observed value appears in `extra_used` instead — which is exactly the old wheel's flat legend, not a failure state. This is the expected behavior for anyone running LifeOS without the Google Sheets journal sync configured at all, which is why the underlying database open is read-only (`mode=ro`) and existence-checked first: this endpoint must never have the side effect of creating `data/gsheet_sync.db` on a machine that never configured that sync.

At n=0 (no entries in the window), every derived branch renders as unused (a full ring of dim outlines, nothing lit) rather than an empty view — "here is the vocabulary available; none of it has been used in this window" is a meaningful, distinct answer from "no data exists."

### View C: Felt vs. Recorded Connection

**Endpoint:** `GET /api/journal/connections?window=<window>`

For every `connection_<name>` field found in the window's entries (discovered dynamically per window, never a hardcoded name list — the real vault happens to use `taylor` and `malea` today, but nothing in the code assumes those specific slugs), this view pairs the self-reported boolean against the actual interaction count with that person from `data/interactions.db`, resolved via `api/services/entity_resolver.py`.

**Framing, stated on the page itself:** in-person connection leaves no digital trace, so a day marked "connected" with zero recorded interactions is not an error — it's information about *channel*, not accuracy. The view never labels a divergence as wrong in either direction. The more actionable direction is the reverse: a day *not* marked as connecting that has a nonzero interaction count, meaning a real exchange happened through a channel the self-report didn't credit. The frontend flags these rows explicitly (`← recorded, not marked felt`) and totals them per field; agreement/disagreement in the other direction is shown as plain data, not flagged as anything.

**Name resolution is disclosed, not silently trusted.** `connection_<name>` is resolved via `EntityResolver.resolve_by_name(name, create_if_missing=False)`, and the result is reported as one of four statuses rather than a bare match:

- `resolved` — a single confident match (confidence ≥ 0.5, unambiguous).
- `low_confidence` — a match was found, but below the 0.5 confidence threshold; counts are shown but the page marks them for cautious reading.
- `ambiguous` — the resolver found multiple similarly-scored candidates and returned its best guess with `disambiguation_applied=True`; the page shows the guess's name and counts, explicitly labeled as a guess to treat cautiously.
- `unresolved` — no confident match at all. `resolve_by_name` collapses two different real outcomes into this same `None` return (genuinely no match, vs. so ambiguous that even the "pick a lower-confidence winner" path in the resolver gives up) — this view can't tell those apart from the return value alone, and reports both the same way: no interaction counts, because neither can safely be attached to a specific person's history.

This is why the naive approach the issue warned against — a `LIKE '<name>%'` prefix match on `person_entities` — never appears here. A vault that has ingested receipts and newsletters alongside people will happily return a merchant such as "Rowan Outfitters" ahead of the actual person named Rowan, because a prefix match has no notion of which rows describe people the operator knows. Every resolution instead goes through the full three-pass resolver (email/phone anchor, then structured fuzzy name matching with disambiguation), and every non-`resolved` outcome is surfaced on the page rather than silently picked.

**Privacy: counts only, never content.** `journal_trends._interaction_counts_by_day` selects only `person_id` and `timestamp` from `data/interactions.db`, grouped by calendar day — it never touches `title` or `snippet`, so there is no interaction content in memory at any point in this code path, let alone in the response. Like the taxonomy view, the database is opened read-only and existence-checked first, so a machine with no interaction history yet never has `data/interactions.db` created as a side effect of viewing this page.

Self-reported values are parsed permissively (`journal_trends._parse_bool_field` accepts a real YAML boolean, and a handful of common `yes`/`no`/`true`/`false`/`1`/`0` spellings) because no real journal data was available to confirm the form's exact emitted type while building this — the project's own rule is to never read the real journal, in code or in tests. A value that doesn't parse is skipped for that day and counted separately (`unparseable_entries`) rather than guessed at.

At n=1, a field with a single logged day renders as one row — self-report plus count, with the resolution status banner above it. At n=0 (no `connection_*` fields present anywhere in the window), the view shows "No connection_<name> fields found in this window" rather than an empty table.

### View D: The Scalar Stack

**Endpoint:** `GET /api/journal/scalars?window=<window>`

`mood`, `stress`, `sleep`, and `body` as one point per entry-day, laid out on the exact same grid as [the strip](#view-a-the-strip) (same `_grid_span` call, same start/end) so the two views' x-axes line up when viewed together. Unlike the emotion vocabulary in view B, these four field names are fixed by the feature request itself, not derived — there's no "the form might rename mood to something else" concern driving this toward dynamic discovery the way there was for the emotion branches.

**No fitted trend, no interpolation across a gap.** The frontend (`web/journal-trends.html`) draws a dot for every entry and a connecting line between two consecutive dots *only if they are exactly one calendar day apart*. With entries clustered in bursts weeks or months apart, this means most of any given sparkline is dots with no connecting line at all — which is correct: a line between two points three weeks apart would visually assert a trend the two points alone cannot support. This mirrors the wheel's own refusal to force chains to a fixed depth, and is the same "no confident-looking wrong answer at low n" principle the rest of this feature has been built around.

**The mood/stress correlation is computed and stated, not implied.** The design brief for this feature was explicit that a correlation like this must be surfaced directly on the page rather than left to a viewer's eyeball reading of a trend line — so `GET /api/journal/scalars` returns a `correlation` object (`n`, `r`, and a `caveat` string) computed with `statistics.correlation` (Python stdlib) over every entry-day where both `mood` and `stress` are present and numeric:

- `n < 2` → `r: null`, caveat states there isn't enough paired data to compute anything.
- `n >= 2` but zero variance in either field → `r: null`, caveat states the correlation is undefined for that reason specifically (not just "not enough data").
- Otherwise, `r` is the Pearson correlation coefficient, and the caveat **always** accompanies it: *the form does not label which direction of the mood or stress scale is "better" — a positive correlation may mean mood holds up under load, or it may mean one field is effectively reverse-coded relative to the other.* A manual reading of one real vault's 32 entries found `r ≈ +0.43` (higher mood on higher-stress days) — the caveat exists specifically because that number reads as counterintuitive without it, and there's no way from the journal data alone to tell "resilience" apart from "the scale runs backwards from what I assumed."

At n=1, every series renders as a single dot with no line — a data point, not a trend. At n=0, all four series are empty and the correlation reports `n=0` with the same "not enough data" caveat as any other undersized window, rather than a special-cased empty state.

## Rejected: A Transition/Markov View

A natural-seeming fifth view — "how does one feeling tend to lead to the next" — was considered and rejected. Entries are non-contiguous: "the next entry" after a given day is sometimes tomorrow and sometimes three weeks later, so a transition/chord diagram built from consecutive *entries* (rather than consecutive *days*) would encode **when the operator happened to sit down and write**, not how feeling actually moved day to day. A chord thick enough to look meaningful could easily represent two entries a month apart with nothing in between — visually identical to two entries logged back-to-back, and indistinguishable from real emotional continuity in the diagram itself. That's the same "confident-looking wrong answer at low n" failure this whole feature set exists to avoid, just wearing a different chart type. If the journal ever reaches a steady daily (or near-daily) cadence, a transition view becomes meaningful again and is a reasonable follow-up issue at that point — not before.

## Known Data Quirk: "Bad feelings" vs. `bad_feeling`

The Google Form question backing the `Bad` branch is titled **"Bad feelings"** (plural), matching every other branch's naming convention. The vault frontmatter key it maps to, however, is the singular `bad_feeling` (see [The Emotion Chain](#the-emotion-chain) above) — a one-off mismatch in the private, gitignored `config/gsheet_sync.yaml` field mapping for that one column, not a form inconsistency or a parser bug. `journal.py`'s `_next_link` already handles this at the chain-parsing level by trying the plural key first and falling back to the singular one for every value, so the wheel and the strip are unaffected. View B's taxonomy derivation (`_derive_taxonomy_labels`) is *also* unaffected by this particular quirk, but for a different reason: it reads the raw form column text ("Bad feelings") directly out of `data/gsheet_sync.db`, never the mapped vault key, so the singular/plural mismatch in the private YAML mapping never enters that code path at all.

---

## Related Documents

### Specifications
- [crm-analytics.md](crm-analytics.md) — Sibling analytics dashboards (Family/Me/Birthdays/Relationship); this feature follows the same "pre-aggregated response, window-parameter-with-graceful-fallback" pattern
- [data-model.md](data-model.md) — The CRM's two-tier entity model, which this feature is explicitly independent of

### Code References
- [api/routes/journal.py](../../../api/routes/journal.py) — Wheel aggregation endpoint, chain parser, wheel builder; also the shared window/file-walk/disclosure-policy logic the trend views reuse
- [web/journal.html](../../../web/journal.html) — Wheel frontend view
- [tests/test_journal_emotions.py](../../../tests/test_journal_emotions.py) — Wheel unit coverage (synthetic fixtures only)
- [tests/test_journal_wheel_ui_browser.py](../../../tests/test_journal_wheel_ui_browser.py) — Wheel self-contained browser test
- [api/routes/journal_trends.py](../../../api/routes/journal_trends.py) — The strip, the unexplored wheel, felt-vs-recorded connection, and the scalar stack
- [web/journal-trends.html](../../../web/journal-trends.html) — Trend views frontend page
- [tests/test_journal_trends.py](../../../tests/test_journal_trends.py) — Trend views unit coverage (synthetic fixtures, a temp interactions database, and a fake entity resolver — never `data/`)
- [api/services/entity_resolver.py](../../../api/services/entity_resolver.py) — Three-pass name resolution used by view C
- [api/services/interaction_store.py](../../../api/services/interaction_store.py) — `data/interactions.db` access used by view C
- [api/services/gsheet_sync.py](../../../api/services/gsheet_sync.py) — `data/gsheet_sync.db` schema (`synced_rows.raw_data`) read by view B
