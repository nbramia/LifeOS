# Journal Analytics — Emotion Wheel

**Status:** Complete
**Owner:** Journal
**Last Updated:** 2026-08-12

A static "emotion wheel" view over the daily journal (`#212`). The daily journal logs a `feeling:` field per entry plus up to two more levels of follow-up detail; this feature aggregates that chain across a selectable time window and renders it as a radial wheel, so "which emotions have dominated lately?" is answerable at a glance instead of by re-reading entries.

This is unrelated to the CRM's two-tier `SourceEntity`/`PersonEntity` model (see [data-model.md](data-model.md)) — journal entries are plain vault notes, not CRM interactions, and this view reads the vault directly rather than going through entity resolution or the search index.

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

---

## Related Documents

### Specifications
- [crm-analytics.md](crm-analytics.md) — Sibling analytics dashboards (Family/Me/Birthdays/Relationship); this feature follows the same "pre-aggregated response, window-parameter-with-graceful-fallback" pattern
- [data-model.md](data-model.md) — The CRM's two-tier entity model, which this feature is explicitly independent of

### Code References
- [api/routes/journal.py](../../../api/routes/journal.py) — Aggregation endpoint, chain parser, wheel builder
- [web/journal.html](../../../web/journal.html) — Frontend view
- [tests/test_journal_emotions.py](../../../tests/test_journal_emotions.py) — Unit coverage (synthetic fixtures only)
- [tests/test_journal_wheel_ui_browser.py](../../../tests/test_journal_wheel_ui_browser.py) — Self-contained browser test
