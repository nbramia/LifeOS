# Entity Resolution

> **Status:** Complete
> **Owner:** CRM
> **Last Updated:** 2026-02-20

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
- Ambiguous matches either create a new disambiguated entity (with context suffix) or return the top match with reduced confidence (0.7x)

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
| Multiple passing, close scores, one has relationship_strength >= 30 | That candidate gets +15 bonus |
| Multiple passing, close scores, one has strength lead >= 25 | That candidate gets +10 bonus |
| Multiple passing, truly ambiguous | Returns empty (no match) |

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
| `RECENCY_BOOST_POINTS` | 10 | Points for recently seen entities |
| `RECENCY_BOOST_THRESHOLD_DAYS` | 30 | Days to qualify as "recent" |
| `MIN_MATCH_SCORE` | 40 | Minimum score for a valid match |
| `DISAMBIGUATION_THRESHOLD` | 15 | Score gap below which match is ambiguous |
| `RELATIONSHIP_STRENGTH_BOOST_MAX` | 25 | Max relationship boost points |
| `RELATIONSHIP_STRENGTH_BOOST_WEIGHT` | 0.25 | Multiplier: strength * weight = points |
| `FIRST_NAME_ONLY_BOOST_MULTIPLIER` | 1.5 | Extra relationship boost for first-name queries |

## Related Documents

- [Data Model](data-model.md) -- Two-tier data model (SourceEntity / PersonEntity)
- [Data & Sync](../technical/data-and-sync.md) -- Sync pipeline and entity processing
- [ADR-003: Two-Tier Data Model](../../adr/003-two-tier-data-model.md) -- Why SourceEntity and PersonEntity are separate
