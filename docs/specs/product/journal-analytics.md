# Journal Analytics

**Status:** Complete
**Owner:** Journal
**Last Updated:** 2026-08-12

A set of views over the daily journal, in two generations:

- The **emotion wheel** (`#212`) — a static, single-window aggregation answering "what did I feel most often". The daily journal logs a `feeling:` field per entry plus up to two more levels of follow-up detail; the wheel aggregates that chain across a selectable time window and renders it as a radial wheel.
- Three **trend views**, added as a direct response to feedback on the wheel: it says nothing about *trajectory*, and with entries spanning months in sparse, non-contiguous bursts, "trajectory" mostly means "how sparse and clustered is this, actually" before anything else. See [Trend Views](#trend-views) below. A fourth view (felt-vs-recorded connection) was built, then removed after operator feedback that it wasn't useful — see [Removed: Felt vs. Recorded Connection](#removed-felt-vs-recorded-connection).

This is unrelated to the CRM's two-tier `SourceEntity`/`PersonEntity` model (see [data-model.md](data-model.md)) — journal entries are plain vault notes, not CRM interactions, and these views read the vault directly rather than going through the search index. Removing felt-vs-recorded connection means this is now true without exception: nothing in this feature touches entity resolution, the search index, or `data/interactions.db`.

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
    - [View D: The Scalar Stack](#view-d-the-scalar-stack)
11. [Removed: Felt vs. Recorded Connection](#removed-felt-vs-recorded-connection)
12. [Rejected: A Transition/Markov View](#rejected-a-transitionmarkov-view)
13. [Known Data Quirk: "Bad feelings" vs. `bad_feeling`](#known-data-quirk-bad-feelings-vs-bad_feeling)

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

Views added in response to feedback that the wheel above says nothing about trajectory. They live in a separate module (`api/routes/journal_trends.py`) and a separate page (`web/journal-trends.html`, linked from the wheel and from the LifeOS home page), rather than folding into `journal.py` / `journal.html` — the unexplored wheel pulls in a data source the original wheel never needed (`data/gsheet_sync.db`), and `journal.py` was already a complete, self-contained 372-line unit before this work started. All of them reuse rather than reimplement the wheel's window semantics (`window_bounds` / `_canonical_window`), file walk (`_iter_valid_journal_files` / `collect_window`), and value-disclosure policy (`_is_plausible_label`) — see the module docstring in `journal_trends.py` for exactly which names are imported from `journal.py` and why.

None of these views read `resonant_moment` (free text, the most sensitive field in the file) or `one_word` — the same boundary the wheel already draws.

### Shared Grid Semantics

Two of these views (the strip, the scalar stack) need every calendar day in a range, not just the days that happen to have an entry — a full grid, with gaps rendered as gaps. The wheel's existing `window_bounds("all-time")` returns `start=None` ("no lower bound"), which is right for an aggregation that only ever needs a total, but can't drive a finite grid.

For grid views, "all-time" instead means **from the earliest entry actually in the window** — the tightest honest span, not an arbitrary long one. `day` / `week` / `month` / `quarter` keep `window_bounds`'s fixed bounds exactly as-is, including when the window is empty: a short window's grid does not shrink just because it happens to contain no entries, since an all-gap grid for a short window is itself useful information ("nothing happened in the last 7 days," rendered as 7 visible gap cells, not as an absence of a view). This logic lives in `journal_trends._grid_span`.

The grid's *end* is always the window's end (`today`, for `all-time`/`day`/`week`, or the fixed window boundary otherwise) — never the date of the last entry. For a journal that's gone quiet for weeks, the trailing gap between the last real entry and today is exactly the kind of thing this whole feature exists to surface, not to trim away.

### View A: The Strip

**Endpoint:** `GET /api/journal/strip?window=<window>`

One cell per calendar day across the grid (see above), colored by that day's primary (level-1) emotion. Every day gets a cell in one of three distinct states — never just two:

- **Gap** (`has_entry: false`) — no journal file that day at all.
- **Entry, no feeling** (`has_entry: true, primary_emotion: null`) — a file exists but had no parseable `feeling:`.
- **Entry with a feeling** (`has_entry: true, primary_emotion: "<value>"`) — colored by the same deterministic hash-to-hue scheme the wheel uses (`fnv1aHue`, duplicated into `journal-trends.html` rather than factored into a shared JS file — see below), so a given emotion word gets the same color on both pages.

**Rendered as GitHub-contribution-style squares**, matching the structure and metrics of the CRM's interaction heatmap (`.heatmap-grid`/`.heatmap-week`/`.heatmap-day` in `web/crm.html`): weeks as flex columns (`.strip-week`, `gap: 2px`), each day an ~11px square with a 2px border radius (`.strip-day`), month labels running along the top computed the same way the CRM heatmap computes them (a label starts a new column whenever the calendar month changes). This was a deliberate revision from an earlier flat, unaligned horizontal strip of thin bars — the squares-in-week-columns layout reads as the same visual language as the CRM's heatmap, so the two pages feel like one product rather than two different charting conventions for what is structurally the same kind of data (one value per calendar day).

**The one deliberate difference from the CRM heatmap: no intensity ramp.** The CRM heatmap encodes *how much* happened on a day via five `data-level` opacity steps. This view has nothing to encode intensity with — a day either has a journal entry with a feeling, or it doesn't — so color instead encodes *which* emotion (a flat, fully-opaque color per primary emotion, from the same `fnv1aHue` scheme the strip already used before this revision). There is no lighter/darker shading of that color anywhere in this view; "entry, no feeling" is a third state rendered as a dashed-outline empty cell, not a shade between "gap" and "entry with a feeling."

This is deliberately the plainest possible rendering of sparsity, just as squares rather than bars: the point is to make the *bursts* (a cluster of colored squares, then a long unbroken run of gap squares, then another cluster) immediately visible without any per-cell decoding effort. At n=1, the grid is a single colored square inside however many gap squares the window contains — still legible, and the gaps around it are exactly as informative as the one real entry. At n=0 (no entries in the window at all, only possible for `all-time` since shorter windows always render *some* grid), the view shows a plain "no entries in this window" message instead of an empty or all-gap grid, which would otherwise look identical to "the grid exists but everything happens to be a gap."

### View B: The Unexplored Wheel

**Endpoint:** `GET /api/journal/taxonomy?window=<window>`

The existing wheel can only draw the emotion words that were actually logged — it structurally cannot show what *wasn't* reached for. This view renders the full form taxonomy (every branch the Google Form offers a follow-up question for), with used branches proportional to frequency and unused branches as dim outlines, answering "what vocabulary do I actually use, out of what's available."

**The taxonomy is derived, never hardcoded.** A manual inspection of one real vault's `data/gsheet_sync.db` found roughly 48 branch words (`Fearful`, `Scared`, `Angry`, `Let down`, `Sad`, `Happy`, `Bad`, and so on) — but that list is specific to one person's Google Form configuration and is not checked in anywhere; hardcoding it would (a) bake a specific person's private form structure into an open-source codebase (see the project's own contribution guidelines on personal-value hardcoding) and (b) silently go stale the instant the form changes, exactly the failure mode the wheel's own value-disclosure policy was built to avoid for free text. Instead, `journal_trends._derive_taxonomy_labels` reads `data/gsheet_sync.db` -> `synced_rows.raw_data` (the JSON blob of raw Google Form column names -> answers that `api/services/gsheet_sync.py` stores for every synced row) and extracts every column name that looks like a branch follow-up question — anything ending in `feelings`/`feeling`, case-insensitively, with an optional trailing `?` (Google Forms often keeps the question mark in the header). The branch label is whatever precedes that suffix, taken verbatim from the form's own column text. Every row ever synced is scanned, not just the newest, so a branch the form used to offer but has since removed still shows up as "available, unused" instead of disappearing.

Extracted branch names pass through the wheel's existing `_is_plausible_label` shape check before becoming a label — defense in depth, since this source is form structure rather than journal free text, but a corrupted or unexpected row shouldn't be able to inject an oversized "branch" into the response either.

Usage frequency comes from flattening every parsed emotion chain in the window (all positions, not just the root) into a value → count table, then matching case-insensitively against the derived branch labels. A value that was actually logged but doesn't match any derived branch — a stray non-branch value like `Not sure`, the `Unrecognized` bucket from the disclosure policy, or genuine taxonomy drift if the form changed after the sheet was last synced — is never dropped; it appears in a separate `extra_used` list instead of being folded into a branch it doesn't actually belong to.

**Degraded mode.** When `data/gsheet_sync.db` doesn't exist, has no `synced_rows` table, or has the table but zero matching columns, `_derive_taxonomy_labels` returns `[]` and the response sets `taxonomy_source: "used-only"`: `branches` is empty, and every observed value appears in `extra_used` instead — which is exactly the old wheel's flat legend, not a failure state. This is the expected behavior for anyone running LifeOS without the Google Sheets journal sync configured at all, which is why the underlying database open is read-only (`mode=ro`) and existence-checked first: this endpoint must never have the side effect of creating `data/gsheet_sync.db` on a machine that never configured that sync.

At n=0 (no entries in the window), every derived branch renders as unused (a full ring of dim outlines, nothing lit) rather than an empty view — "here is the vocabulary available; none of it has been used in this window" is a meaningful, distinct answer from "no data exists."

**"Not sure" is excluded entirely** — from `branches`, from `extra_used`, from the wheel, from the legend, and from every count — per direct operator feedback: it's a non-answer to "what did you feel", not an emotion, so it shouldn't get a wedge, a color, or a spot in a family alongside things that *are* emotions. It's filtered case-insensitively wherever it could appear: as a derived form-column label (defensive; the real form doesn't currently emit one), as a raw logged value, and — for grouping purposes — as either a chain's root (`feeling: Not sure`) or one of its non-root values (e.g. `Sad` → ... → `Not sure`, several steps into an otherwise-real chain). An entry whose only recorded value is "Not sure" still counts toward `emotion_entries` (it *is* a parseable answer to "did you log a feeling") but contributes no branch, no `extra_used` entry, and no vote to the grouping below.

**Branches are grouped by primary emotion, both on the wheel and in the legend, with one colour family per primary.** This was added after feedback that the flat ring and flat legend made it hard to see which primary a branch belonged to. Every `TaxonomyBranch` in the response now carries a `group` field, and the response as a whole carries `group_order` (a fixed list: the seven known primaries, then `"Unplaced"`, always in that order — never re-sorted by size, so the layout is stable across requests).

- **The seven known primaries** (`journal_trends._KNOWN_PRIMARIES`): `Angry`, `Bad`, `Disgusted`, `Happy`, `Sad` — the root `feeling:` values actually observed across the real vault's entries — plus `Fearful` and `Surprised`, which never appear as a root but do have their own `"<Primary> feelings"` follow-up column in the form, meaning the operator has simply never selected them as a top-level feeling yet. `Not sure` is a root value too, but is excluded per above rather than treated as an eighth primary. This list is operator-supplied ground truth, not derived — nothing in the data can tell you that `Fearful` is a primary if it's never been chosen as one, so there was no way to derive this list purely from journal or sheet contents.
- **A branch that is itself one of the seven primaries groups under itself**, regardless of anything else — checked before any vote lookup.
- **Every other branch is grouped by majority vote from real observed chains**, never by the form's column order. An earlier attempt at this grouping assumed the form's `"<Node> feelings"` columns are laid out depth-first (so column order alone would reveal the tree) — that assumption is false: in form order, `Playful` (index 29) comes immediately before `Happy` (index 30), even though `Playful` is one of `Happy`'s children in the standard wheel this form derives from. Column order reflects however the form's author arranged the questions, not the tree structure. Instead, `journal_trends._derive_branch_groups` walks every parsed emotion chain (`parse_emotion_chain`) from **every journal entry ever written** — deliberately not window-scoped, since which primary a branch belongs to is a structural property of the form, not something that should shift depending on which time window happens to be selected — and, for every non-root value in a chain, casts a vote for that chain's root as the value's primary. A branch's group is whichever root has the most votes; ties break alphabetically for determinism. Chains rooted at "Not sure" cast no votes at all, and "Not sure" appearing anywhere inside a chain is never itself recorded as something to vote *for*.
- **A branch with zero votes — never observed as a non-root value in any real chain — lands in `"Unplaced"`, not a guessed primary.** This was an explicit choice over building a fallback mapping from a published Plutchik-derivative taxonomy to fill the gap: a visible "don't know yet" bucket is honest; a wrong parent silently asserted with confidence is not, and this feature exists specifically to avoid confident-looking wrong answers. If the operator wants a fallback mapping in the future, it should be a separable, explicitly-labeled second pass — layered on top of, never blended into, the derived votes above — so a form change can't silently corrupt it.
- On the wheel, groups render as contiguous arcs of wedges with a small visible gap before the next group, so a glance at the ring shows the family boundaries; the legend mirrors this with one header per present group and its branches listed underneath. Each present group gets its own **colour family**: one hue per group (hashed from the group name, stable across reloads), with lightness/saturation varying per branch within that family — a hash-derived variation, not the form's data, so a branch's shade doesn't shift on its own from request to request. `"Unplaced"` gets a neutral, zero-saturation grey family rather than a hue, since it isn't a real emotion family. (Feedback item 1's "no variable shading" rule is scoped to the strip's coloring, not this wheel — colour families are exactly what was asked for here.)

### View D: The Scalar Stack

**Endpoint:** `GET /api/journal/scalars?window=<window>`

`mood`, `stress`, `sleep`, and `body` as one point per entry-day, laid out on the exact same grid as [the strip](#view-a-the-strip) (same `_grid_span` call, same start/end) so the two views' x-axes line up when viewed together. Unlike the emotion vocabulary in view B, these four field names are fixed by the feature request itself, not derived — there's no "the form might rename mood to something else" concern driving this toward dynamic discovery the way there was for the emotion branches.

**No fitted trend, no interpolation across a gap.** The frontend (`web/journal-trends.html`) draws a dot for every entry and a connecting line between two consecutive dots *only if they are exactly one calendar day apart*. With entries clustered in bursts weeks or months apart, this means most of any given sparkline is dots with no connecting line at all — which is correct: a line between two points three weeks apart would visually assert a trend the two points alone cannot support. This mirrors the wheel's own refusal to force chains to a fixed depth, and is the same "no confident-looking wrong answer at low n" principle the rest of this feature has been built around.

**Every pairwise correlation is shown as a scatter, not a bare coefficient.** The original version of this view rendered the mood/stress relationship as a single line of text — `mood vs. stress: r = +0.43 (n=32)` plus a caveat — and nothing else. Operator feedback was direct: a bare Pearson coefficient with no picture communicates nothing to anyone who isn't already fluent in reading one, and it *looks* authoritative while doing so — a confident-looking number standing in for an actual relationship, which is exactly the failure mode this whole feature exists to avoid elsewhere. The fix was to show the relationship rather than assert it:

- **Six pairwise scatters, one per distinct pair of the four scalar fields** (`mood`/`stress`, `mood`/`sleep`, `mood`/`body`, `stress`/`sleep`, `stress`/`body`, `sleep`/`body` — 4 choose 2). A grid of six was chosen over one scatter with a pair selector because at this sample size (dozens, not hundreds, of entries), seeing all six relationships at once is more useful than clicking through them one at a time — the operator can spot which pairs move together and which don't without six separate page interactions. Each cell is individually legible and labeled: its own title (`"mood vs. stress"`), its own axis labels, and axis ticks at the observed min/max so the plot reads without a shared external scale.
- **One dot per entry with both values logged, no fitted line.** At the sample sizes this journal produces, a regression line would imply a confidence the data can't support — the same "no fitted trend" rule the sparklines above already follow, applied to a second chart type.
- **Each cell leads with a plain-language sentence**, not the coefficient — e.g. *"On days rated higher for mood, stress also tends to be higher."* (or "...tends to be lower" for a negative relationship, or "show no clear pattern together in this window" near zero, or an explicit "not enough paired entries" statement when `n < 2`). This is generated client-side from the sign and magnitude of `r`, in `web/journal-trends.html`.
- **The scale-direction caveat is stated once, above the whole grid, not six times.** The form doesn't label which direction of any of the four self-reported scales is "better" — a positive relationship might mean two things genuinely move together, or that one field is effectively reverse-coded relative to the other. That ambiguity is identical for every one of the six pairs, so repeating the same sentence in six footnotes would be noise, not information; it's stated once, above the grid.
- **`r` is demoted to a small technical footnote under each chart, with its `n`.** It's still computed and returned (`GET /api/journal/scalars` returns a `correlations` list of six `ScalarCorrelation` objects — `pair`, `n`, `r`, `caveat` — computed with `statistics.correlation`, Python stdlib), but it's no longer the headline anywhere on the page.

Per pair, `journal_trends._pair_correlation`:

- `n < 2` → `r: null`, caveat states there isn't enough paired data to compute anything for that specific pair.
- `n >= 2` but zero variance in either field → `r: null`, caveat states the correlation is undefined for that reason specifically (not just "not enough data").
- Otherwise, `r` is the Pearson correlation coefficient, and the pair's `caveat` is a short "co-movement only, over N paired entries" note — the *shared* scale-direction disclaimer above is not repeated here per pair, for the reason stated above. A manual reading of one real vault's 32 entries found mood/stress `r ≈ +0.43` (higher mood on higher-stress days) — the reason this feature leads with a picture and a plain sentence rather than that number: read alone, it's counterintuitive, and there's no way from the journal data to tell "resilience" apart from "the scale runs backwards from what I assumed."

At n=1, a pair's scatter renders as a single dot — a data point, not a trend, and its sentence states there isn't enough paired data yet. At n=0, all four series are empty and every pair's scatter shows "no paired data in this window" instead of an empty plot that could be misread as "no relationship."

## Removed: Felt vs. Recorded Connection

A fourth trend view — felt-vs-recorded connection (`GET /api/journal/connections`) — paired each `connection_<name>` self-report against the actual interaction count with that person from `data/interactions.db`, resolved via `api/services/entity_resolver.py`. It was removed after direct operator feedback that it wasn't useful, without a specific alternative requested — so it's gone, not replaced. Its letter (View C) is retired rather than reused, so anyone looking at old links or discussion of this feature can tell it's an intentional removal, not a renumbering.

Removed along with the endpoint: its response models (`ConnectionResolution`, `ConnectionDay`, `ConnectionField`, `JournalConnectionsResponse`), its helper functions (`_parse_bool_field`, `_interaction_counts_by_day`, `_resolve_connection_name`), the page section and its rendering code in `web/journal-trends.html`, and its test coverage in `tests/test_journal_trends.py`.

**This was also the only thing in `journal_trends.py` that needed `api/services/entity_resolver.py` or touched `data/interactions.db` at all.** Removing it drops both dependencies from this module entirely — a real reduction in privacy surface, not just a line-count one: the strip, the unexplored wheel, and the scalar stack now only ever read the vault's own journal files and (for the unexplored wheel) `data/gsheet_sync.db`'s form structure. Nothing in this feature resolves a name to a person or reads interaction history anymore.

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
- [api/routes/journal_trends.py](../../../api/routes/journal_trends.py) — The strip, the unexplored wheel, and the scalar stack
- [web/journal-trends.html](../../../web/journal-trends.html) — Trend views frontend page
- [tests/test_journal_trends.py](../../../tests/test_journal_trends.py) — Trend views unit coverage (synthetic fixtures only — never `data/`)
- [api/services/gsheet_sync.py](../../../api/services/gsheet_sync.py) — `data/gsheet_sync.db` schema (`synced_rows.raw_data`) read by view B
