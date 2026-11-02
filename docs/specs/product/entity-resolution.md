# Entity Resolution

> **Status:** Complete
> **Owner:** CRM
> **Last Updated:** 2026-11-02

How LifeOS links source entities from different data sources to canonical person records.

---

## Resolution Algorithm

The `EntityResolver` uses a multi-pass algorithm. Each pass is tried in order; the first match wins.

**Pass 1: Email Exact Match**
- Looks up email (case-insensitive) in the entity store
- Confidence: 1.0

**Pass 1b: Phone Exact Match**
- Looks up phone in E.164 format (e.g., `+15551234567`)
- Confidence: 1.0

**Pass 2: Alias/Dictionary Lookup**
- Canonicalizes the name via `people_dictionary.json` (maps aliases to canonical names)
- Checks for an exact name match in the entity store (canonical name or alias)
- Confidence: 1.0

**Pass 3: Link Override Match**
- Checks `link_override_store` for disambiguation rules created by previous entity splits
- Matches on name, source type, and context path
- Confidence: 1.0

**Pass 4: Structured Name Matching**
- Parses names into components (first, middle, last) after stripping prefixes (Dr., Mr.) and suffixes (MD, PhD, Jr)
- Scores all existing entities using three-phase scoring (see below)
- Minimum score: 40 points
- Confidence: `min(score / 100, 1.0)`

**Pass 5: Disambiguation**
- If the top two candidates score within 15 points of each other, the match is ambiguous
- A lookup that may create a person (`create_if_missing`) returns no link for an ambiguous name and never creates one; a read-only lookup returns the top match with reduced confidence (0.7x), match type `fuzzy_ambiguous`
- An unknown email or phone alongside an ambiguous name still anchors a new person, so the ambiguous name never becomes another exact match. A phone names them with the number. An email names them from the address when it spells out a multi-word name other than the ambiguous one, and with the address itself otherwise. An ambiguous name with no identifier links to no one
- A person named only after their number or address takes the real multi-word name supplied by a `create_if_missing` call that matches them by that identifier, unless someone else holds that name. A single-word name does not replace it, a real name is never replaced, and a read-only lookup renames no one

---

## Structured Name Scoring (Pass 4)

### Phase 1: Hard Disqualifiers

If both the query and candidate have last names, the last names must match (exact, prefix for initials, or fuzzy >= 85%). If they don't match, the candidate is skipped entirely.

### Phase 2: Component Scoring

| Component | Condition | Points |
|-----------|-----------|--------|
| **Last name** | Exact match | 50 |
| | Initial prefix match | 35 |
| | Fuzzy match (>= 85%) | 25 |
| **First name** | Exact match | 25 |
| | Nickname match (via `nickname_lookup`) | 20 |
| | Fuzzy match (>= 85%) | 20 |
| | Initial prefix match | 10 |
| **Middle name** | Exact match | 10 |
| | Fuzzy match (>= 85%) | 7 |
| **First/middle cross-match** | Query first = entity middle (exact) | 15 |
| | Query first = entity middle (fuzzy) | 12 |
| **Alias bonus** | Best alias first-name match (if canonical didn't match) | up to 25 |

Both first and last name must have some match for full-name queries. If neither first name nor alias matched, the candidate is skipped.

### Phase 3: Context and Bonus Scoring

| Boost | Condition | Points |
|-------|-----------|--------|
| Context boost | Vault path matches entity's vault contexts | +30 |
| Recency boost | Entity last seen within 30 days | +10 |
| Relationship strength | `strength * 0.25`, capped at 25 | 0-25 |
| Relationship strength (first-name-only) | Above value * 1.5 | 0-37.5 |

---

## First-Name-Only Resolution

When the query is a single word (e.g., "Sarah" with no last name), special disambiguation logic applies after scoring:

| Scenario | Behavior |
|----------|----------|
| Single candidate | +15 bonus points, match type `first_name_unique` |
| One candidate passes min threshold (40) | +10 bonus points, match type `first_name_context_clear` |
| Multiple passing, score gap >= 20 | Top candidate gets +10 bonus |
| Multiple passing, close scores, one has relationship_strength >= 14 | That candidate gets +15 bonus |
| Multiple passing, close scores, several at >= 14, one has a strength lead >= 30 | That candidate gets +10 bonus |
| Multiple passing, truly ambiguous | Ambiguous: returns no link and creates no one |
| Multiple candidates, none passing | Ambiguous: returns no link and creates no one |

A bare name that matches no one, or a single candidate below the threshold, is an unknown name, and `create_if_missing` creates no one from it (see [Creating People](#creating-people)). A hidden one-word row does not answer to its name in any of this; a hidden full name does, so syncs cannot recreate the person.

---

## Creating People

`create_if_missing` creates a person from a name alone only when the name reads as a full name (`is_creatable_person_name`): more than one word, no more than six, with no digits or address, path, or markup characters, and no word from `NON_NAME_WORDS` (organization and note-heading words such as "Foundation", "Members", "Next Steps"). A single word never creates a person, on any resolver path. An unknown email or phone alongside any other name still creates one, named from the address or number as under Pass 5.

A person's own full name typed into the CRM (splitting a person, rejecting a link into a new person) is the operator's decision and creates that person directly.

---

## Mentions in Notes

A vault or Granola note names people in several ways. A full name goes through the resolver above. A first name alone, or a name with only an initial on one side, is ambiguous and goes to the mention resolver (`api/services/mention_resolver.py`), which reads it in the context of its note, per note. It links only on a clear winner and never creates anyone. A mention that means no one clearly keeps an unlinked source entity, and the sweeps that re-match unlinked entities by name alone (`link_source_entities.py`, `relink_source_entities.py`) skip vault and Granola rows.

Valid targets are real people: visible, unmerged, reading as a person. A one-word row with no email or phone is never a target; a one-word row with only an email is a sender, not someone a note calls by first name; the owner's own first name resolves to no one. Link overrides apply first, and a mention a human confirmed or rejected keeps that decision through reindexing.

Each candidate collects points from whichever of these signals apply. Weights are the `MENTION_*` constants in `config/relationship_weights.py`.

| Signal | Applies when | Points |
|--------|--------------|--------|
| `name_first` | Mention equals their first name | 10 |
| `name_alias` | Mention equals an alias on them | 10 |
| `name_nickname` | Mention is only a known variant of their first name, and nobody goes by it as given | -20 |
| `name_initial` | The other half of "Robin G" or "R Gamma" agrees | 15 |
| `only_candidate` | Nobody else real could be meant | 15 |
| `dictionary_category` | The people dictionary lists the name in their category | 15 |
| `domain_match` / `domain_mismatch` | Their contact evidence fits, or goes against, the note's work or personal context | +10 / -15 |
| `family_in_personal` | Family, in a personal note | 20 |
| `employer_email` | They have an address at the note's employer | 30 |
| `employer_history` | Three or more work-account emails, calendar events, or Slack messages with them during that tenure | 20 |
| `employer_company` | Their company names that employer | 15 |
| `other_employer` | Their only known employer domain is a different one | -10 |
| `interaction_near` | Closest non-note interaction within 7 / 30 / 90 days of the note's date | 25 / 15 / 8 |
| `meeting_same_day` | In a meeting note, they were on a small calendar event within a day whose title agrees with the note's | 30 |
| `full_name_in_note` | Their full name or email address appears in the note | 50 |
| `co_mention` | Recorded relationship with a person the note names in full: 15 at pair strength 20 or more, otherwise 6, capped at 30 | up to 30 |
| `person_path` | Their full name is in the note's path | 40 |
| `folder_links` | Three or more notes in this folder name them in full or carry a confirmed link to them | 15 |

The top candidate links when they reach `MENTION_MIN_SCORE` (50) and lead the runner-up by `MENTION_MIN_LEAD` (25). Anything less links to no one. A missed link costs little and a wrong one costs a lot, so no single weak signal clears the bar.

**Note context.** The note's folder decides work or personal: `Work/` and an employer's folder read as work, `Personal/` as personal, and `vault_tags.domain` fills in for folders that say nothing. A work tag on a note in `Personal/` leaves it undecided, and an undecided note earns no domain signal.

**Employer.** `config/employment_history.json` (gitignored; template `config/employment_history.example.json`) lists the owner's employers: name, email domains, start and end dates, vault folders. A note's employer comes from its folder, or, for a dated work note in no employer's folder, from the employers current on its date; overlapping tenures all count. With no history configured, the employer signals stay silent and the others carry on, so a fresh install links less but no worse.

**Note date.** The date comes from `extract_note_date` (filename, then frontmatter `created`, `date`, `created_at`, `creation_date`, then body text), or file time only past `LIFEOS_VAULT_MTIME_TRUSTED_AFTER`. An undated note earns no interaction signal.

---

## Nickname Lookup

The `config/nickname_lookup.py` module provides bidirectional lookup between formal names and common nicknames (e.g., Benjamin/Ben, Michael/Mike, Katherine/Kate). Used during first-name matching to award 20 points for nickname variants.

## People Dictionary

The `config/people_dictionary.json` file maps known aliases to canonical names. It is checked before fuzzy matching (Pass 2) to resolve known aliases immediately with full confidence. Requires a server restart after edits.

## Link Overrides

Link overrides are disambiguation rules stored in the `link_override_store`. When an entity is split (e.g., two "Sarah"s distinguished by context), an override is created so future resolution for that name+context combination routes directly to the correct entity.

---

## Domain-to-Context Mapping

Configured in `config/people_config.py`. Maps email domains to vault contexts and categories, enabling the context boost during name matching.

## Scoring Configuration

All scoring constants are defined in `config/relationship_weights.py`:

| Constant | Value | Purpose |
|----------|-------|---------|
| `CONTEXT_BOOST_POINTS` | 30 | Points for vault path match |
| `RECENCY_BOOST_POINTS` | 10 | Points for entities with a recent real interaction (`last_seen`) |
| `RECENCY_BOOST_THRESHOLD_DAYS` | 365 | Days since that interaction to qualify as "recent" |
| `MIN_MATCH_SCORE` | 40 | Minimum score for a valid match |
| `DISAMBIGUATION_THRESHOLD` | 15 | Score gap below which match is ambiguous |
| `RELATIONSHIP_STRENGTH_BOOST_MAX` | 25 | Max relationship boost points |
| `RELATIONSHIP_STRENGTH_BOOST_WEIGHT` | 0.40 | Multiplier: strength * weight = points |
| `FIRST_NAME_ONLY_BOOST_MULTIPLIER` | 1.5 | Extra relationship boost for first-name queries |
| `CLOSE_RELATIONSHIP_MIN_STRENGTH` | 14 | Strength that makes a first-name candidate "close" |
| `CLEARLY_CLOSER_MIN_STRENGTH_LEAD` | 30 | Strength lead that settles a tie between close candidates |
| `MENTION_MIN_SCORE` | 50 | Minimum score for a note mention to link |
| `MENTION_MIN_LEAD` | 25 | Minimum lead over the runner-up for a note mention to link |

## Related Documents

- [Data Model](data-model.md) -- Two-tier data model (SourceEntity / PersonEntity)
- [Data & Sync](../technical/data-and-sync.md) -- Sync pipeline, entity processing, and what indexing skips
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) -- Why SourceEntity and PersonEntity are separate
