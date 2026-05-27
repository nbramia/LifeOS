# Documentation Strategy

**Status:** Complete
**Last Updated:** 2026-05-27

> **Backlog lives in GitHub issues.** Future work, deferred features, bugs, and enhancements are tracked as GitHub issues — never as `backlog.md` files in this directory. Plan files are reserved for time-bounded execution notes (migration plans, gap analyses, point-in-time issue-drafting context).

**This document defines mandatory documentation standards. All contributors — human and AI — must follow these rules when creating or modifying documentation. Consistency is not optional; it ensures documentation remains navigable, maintainable, and valuable as a shared context layer.**

## Purpose

This strategy defines how we organize and maintain documentation in LifeOS. These are **rules, not guidelines** — following this strategy faithfully is critical to maintaining documentation quality across a collaborative human + AI agent project.

Documentation in LifeOS serves two readers equally:
- **Human contributors** who need to onboard, debug, and design changes.
- **AI agents** (Claude Code, Cursor, Copilot, etc.) that load docs as context when working on the codebase.

Both readers benefit from the same things: short, focused documents; explicit decisions; cross-links instead of duplication; and an unambiguous answer to "where does this kind of information live?"

## Core Principles

1. **Living documents over proposals** — Design docs evolve during implementation rather than being written once and frozen. Update when thinking changes, not when tasks complete.
2. **Modular over monolithic** — Split documents by concern, not by phase or role. A 1,800-line spec is a symptom, not a feature.
3. **Cross-linked over isolated** — Documents reference related docs with a short tagline explaining the relationship, not just a link.
4. **Concise over comprehensive** — Each document has a clear scope; prefer multiple focused docs over one sprawling one.
5. **Consistent over creative** — Follow established patterns and structures. Consistency makes documentation predictable and navigable.
6. **AI-readable** — Be succinct, specific, and clear. Prefer shorter, well-named documents over longer ones. Don't overexplain, but make requirements and decisions explicit.
7. **Single source of truth** — Default to keeping each piece of information in exactly one authoritative location; other documents link to it rather than repeating it. When in doubt about where something belongs, consult the [Content Classification table](#content-classification). When you find duplication, prefer consolidating to the authoritative location and replacing duplicates with cross-references.

## Document Taxonomy

```
docs/
├── AGENTS.md          # THIS FILE (documentation strategy)
├── CLAUDE.md          # @AGENTS.md wrapper
├── vision/            # WHY — project vision, philosophy
├── specs/
│   ├── product/       # WHAT — features, API contracts, data models (consumer perspective)
│   ├── technical/     # HOW (design) — architecture, schema, security, infrastructure
│   └── standards/     # HOW (work) — coding conventions, testing standards, naming rules
├── adr/               # WHY (decisions) — immutable architecture decision records
├── guides/            # HOW (do) — step-by-step operational instructions
├── plans/             # WHEN — active execution work, ephemeral (gitignored)
└── archive/           # HISTORY — superseded documents (gitignored)
```

### Vision Documents

**Location:** `docs/vision/`
**Purpose:** Vision, philosophy, and strategic direction — the WHY behind LifeOS
**Updated:** When strategy changes (rarely)

Vision docs provide foundational context that frames every design decision. For LifeOS, that's primarily the privacy-first, local-first, single-user posture — see [philosophy.md](vision/philosophy.md).

### Specifications

**Location:** `docs/specs/`
**Purpose:** Design documents describing WHAT the system is/should be (target state)
**Updated:** When the design evolves

**Key principle:** Specs are the **vision** — the authoritative design for the system. Not implementation plans, not progress tracking, not task lists. They describe the intended architecture and behavior.

"Living" means the spec updates when *the design* changes, not when *tasks* complete.

- `product/` — What the system does from a consumer perspective. Feature behavior, API contracts, data model semantics. Answers: "What does this entity mean? What can you do with this endpoint?"
- `technical/` — How the system is built from an engineering perspective. Architecture, schema, security implementation, query optimization. Answers: "How is this implemented? What constraints did we hit?"
- `standards/` — Rules and conventions for how we work. Coding standards, naming conventions, testing patterns. Answers: "What conventions must we follow?"

### Architecture Decision Records (ADRs)

**Location:** `docs/adr/`
**Purpose:** Immutable records of significant architectural decisions with context and rationale
**Updated:** Never. ADRs are append-only — create a new ADR to supersede an old one. The only acceptable in-place edit is adding an `Amended by: ADR-NNN` or `Superseded By: ADR-NNN` pointer to the frontmatter.

**Naming:** `NNN-short-title.md` (e.g., `008-managed-agents-cloud-routing.md`). Sequential numbering. Never reuse numbers.

ADRs live at the top level (not under `specs/`) because they span product and technical concerns. A decision about data model semantics is as load-bearing as one about database engines.

### Operational Guides

**Location:** `docs/guides/`
**Purpose:** Step-by-step instructions for specific operator and contributor tasks
**Updated:** When procedures change

Guides are instructional — they tell you how to do something, not why it's designed that way. If a guide starts explaining design rationale, that content belongs in a spec or ADR.

### Plans

**Location:** `docs/plans/` (gitignored — personal/active planning notes)
**Purpose:** Time-bounded execution notes for a specific effort — migration plans, dated gap analyses, point-in-time issue-drafting context.

**Backlog and trackable cross-contributor work belong in GitHub issues, not in `docs/plans/`.** Never create a running `backlog.md`, `todo.md`, or `ideas.md` — file an issue. Plan files exist only for the slice of work that's actively being executed and would clutter the issue tracker (e.g., "phase 2 of the linux migration"), and they're moved to `archive/` the moment that work is done.

Plans are **ephemeral**. They become git history (or, in this repo, never enter the public repo) when complete.

### Archive

**Location:** `docs/archive/` (gitignored)
**Purpose:** Superseded documents, audit notes, and historical investigation working notes retained for local context.

Archive content is **not** part of the public repo. When a durable insight from an archived doc is still load-bearing, promote it: link to the relevant live spec, or extract the durable conclusion into an ADR.

## Content Classification

| Content Type | Belongs In | Anti-Pattern |
|---|---|---|
| Project vision, philosophy, strategy | `vision/` | Not in specs (too stable, too abstract) |
| User-facing feature behavior, API contracts, data model semantics | `specs/product/` | No implementation details, no task lists |
| Architecture, system design, schema, query design, security implementation | `specs/technical/` | No roadmaps, no "Next Steps" |
| Coding conventions, naming rules, testing patterns | `specs/standards/` | Not in guides (standards prescribe, guides instruct) |
| Architectural decisions with rationale and alternatives | `adr/` | Never modify after acceptance — create a new ADR to supersede |
| Setup procedures, troubleshooting, operator how-to | `guides/` | Not design rationale |
| Backlog, deferred features, bugs, enhancements | **GitHub issues** | Never in `backlog.md` or any plan file |
| Time-bounded execution notes (migration plans, dated gap analyses) | `plans/` | Don't put in specs; not a substitute for GitHub issues |
| Audit notes, investigation working files, superseded docs | `archive/` | Don't link from live specs unless promoting a specific insight |

**Rule of thumb:** "Why do we build this?" → vision. "What should it be?" → product spec. "How is it built?" → technical spec. "Why did we decide?" → ADR. "How do I do X?" → guide. "When/how do we get there?" → plan.

**LifeOS-specific classification:**
- Data model semantics (what entities mean, relationships between them) → `specs/product/data-model.md`
- Data model implementation (SQLite schema, ChromaDB collections, indexes) → `specs/technical/`
- API contracts (consumer perspective, request/response shapes) → `specs/product/api-reference.md`
- API internals (route-handler structure, middleware, error handling) → `specs/technical/`
- Privacy/security design decisions → `specs/technical/security-privacy.md`, with corresponding ADRs for the underlying decisions

## Frontmatter Standards

Every document must have frontmatter:

```markdown
**Status:** Draft | Partial | Complete
**Last Updated:** YYYY-MM-DD
```

**Additional fields by document type:**

| Type | Additional Fields |
|---|---|
| ADR | `**Decision:** Accepted \| Superseded \| Deprecated` and (if applicable) `**Superseded By:** ADR-NNN` or `**Amended by:** ADR-NNN` |
| Product Spec | `**Owner:** name-or-area` |
| Technical Spec | `**Owner:** name-or-area` |
| Guide | `**Audience:** Operator \| Contributor \| New users` |
| Plan | `**Target Date:** YYYY-MM-DD` (or `Ongoing`); `**Completed:** YYYY-MM-DD` when archived |

`Status` values:
- **Draft** — Initial draft, not yet reviewed; may have gaps or open questions.
- **Partial** — Some sections complete, others marked TODO; safe to read for the parts that exist.
- **Complete** — Reviewed, accurate as of `Last Updated`, no known gaps.

For ADRs, `Status` reflects document completeness; `Decision` reflects whether the decision is still in force.

## ADR Template

ADRs use the following structure. The retrofit issue (#182) brought existing ADRs into this shape; all new ADRs must follow it.

```markdown
# ADR-NNN: Short Title

**Status:** Complete
**Last Updated:** YYYY-MM-DD
**Decision:** Accepted
**Superseded By:** ADR-NNN  (only if applicable)
**Amended by:** ADR-NNN     (only if applicable)

## Context

The situation, constraints, and forces at the time of the decision. Why this needed to be decided now.

## Decision

One unambiguous statement of what was chosen. No hedging.

## Rationale

Why this choice over the alternatives. Tie back to the constraints in Context.

## Alternatives Considered

Each alternative as its own subsection with a one-paragraph description and an explicit "Rejected because..." line. Recover alternatives from PR descriptions, commit messages, or memory. Where you genuinely don't remember, write "to be filled in by the original decider" rather than fabricating.

### Alternative A

Description.

**Rejected because:** specific reason.

## Consequences

### Positive

- Specific benefits this decision enables.

### Negative

- Specific costs, ongoing maintenance burden, or constraints this decision imposes.

## Related Documents

(Use the 4-bucket Related Documents structure described below.)
```

## Length Guidelines

Not rigid rules, but signals for document health. When a doc passes the "max" column it should be split by concern.

| Document Type | Target Lines | Max Lines |
|---|---|---|
| ADR | 200–500 | 800 |
| Product Spec | 300–700 | 800 |
| Technical Spec | 400–800 | 1,000 |
| Standards | 200–500 | 700 |
| Vision | 300–600 | 800 |
| Guide | 200–500 | 800 |
| Plan | Variable | — |

**Split when:**
- Document exceeds the Max in its column.
- It covers multiple distinct concerns (e.g., one CRM spec covering people, interactions, graph, analytics).
- Different readers need different sections.
- Table of contents has more than ~8 top-level sections.

When splitting, keep a thin **index document** at the original path with pointers to each split and a short overview. Update all inbound links to point at the right split.

## Cross-Linking Standards

Every document must include a "Related Documents" section at the bottom.

**Requirements:**
1. **Bidirectional** — if A links to B, B must link to A. When you add a link in one direction, add the reciprocal link in the same PR.
2. **Contextual** — every link has a "— short tagline" explaining the relationship.
3. **Specific** — link to code with line numbers (`path/to/file.py:120-145`) when relevant.
4. **Use the 4-bucket structure** below for consistency.

**Standard Related Documents template:**

```markdown
## Related Documents

### Design Context
- [ADR-NNN: Decision Name](../adr/NNN-title.md) — Why this approach was chosen
- [Vision: Philosophy](../vision/philosophy.md) — The principle behind this

### Specifications
- [Product Spec](../specs/product/feature.md) — Consumer-facing requirements
- [Technical Spec](../specs/technical/component.md) — Implementation design

### Operational
- [Setup Guide](../guides/feature-setup.md) — How to configure this
- [Configuration](../guides/configuration.md) — Env var reference

### Code References
- [Implementation](../../api/services/foo.py:45-120) — Production code
- [Tests](../../tests/test_foo.py) — Coverage
```

Use only the buckets that apply — omit empty buckets rather than including them as `(none)`.

## Lifecycle Rules

**Specs are living:**
- Update when the design changes, not when tasks complete.
- If a spec describes a target that's not yet built, the `Status: Partial` value is appropriate.
- Specs are not changelogs. Don't add "Added X in PR #N" notes; that's what `git log` and `git blame` are for.

**Plans are ephemeral:**
- A plan ends as either "completed" (move to `archive/` with `Completed: YYYY-MM-DD`) or "superseded" (note the successor and move to `archive/`).
- Do not leave stale plans in `docs/plans/`.

**ADRs are immutable:**
- Never modify the Context / Decision / Rationale / Alternatives sections of an accepted ADR.
- To change a decision, create a new ADR. The old ADR gets `Superseded By: ADR-NNN` added to its frontmatter — that's the only acceptable in-place edit.
- For a smaller change (clarification, scoping refinement that doesn't reverse the decision), the new ADR is an *amendment* and the old ADR gets `Amended by: ADR-NNN`.

**Guides decay:**
- Test commands you document before shipping. Stale guides are worse than missing guides.

## Writing for AI Readability

Documentation is frequently consumed by AI agents during development. Optimize for:

**Clarity:**
- State requirements explicitly. Avoid "should consider" — say "do" or "don't" or "defer because X".
- Use precise technical terminology (route handler, span, collection) rather than vague nouns.
- Avoid ambiguous pronouns; prefer specific nouns even at the cost of repetition.

**Structure:**
- Well-named sections that match their content. A section labeled "Notes" is a smell.
- Frontmatter with status and metadata at the top.
- Tables when they sharpen a comparison; prose otherwise.

**Brevity:**
- Shorter, focused documents over long comprehensive ones.
- Link to details rather than repeating them.
- Keep documents focused on one concern so agents don't waste context loading irrelevant content.

**Anti-patterns:**
- Long narrative explanations when a bulleted list suffices.
- Repeating context already in linked documents.
- Vague statements like "we should consider" (instead: decided yes/no, or defer with reason).
- Burying key decisions in prose.

## LifeOS-Specific Rules

### Privacy-First Documentation

LifeOS handles deeply personal data — emails, messages, photos, finances. Documentation must not become a leak vector.

- **Use obviously synthetic data in all examples and test fixtures.** Names like "Alex Chen", domains like `example.com`, phone numbers like `555-0123`, dollar amounts like `$1,234.56`. No real names, emails, phone numbers, or financial values, even in commit-message screenshots.
- **Security-sensitive implementation details belong in code, not docs.** Reference the code (with line numbers) rather than restating encryption keys, auth tokens, or exact connection strings.
- **Technical specs involving data storage or access** should include a "Privacy Considerations" subsection or link to one in `specs/technical/security-privacy.md`.
- **Don't paste real terminal output** containing real personal data into docs. Sanitize first.

### Open-Source Posture

LifeOS is open-source and single-user / self-hosted. That implies two doc rules beyond the privacy ones:

- **No hardcoded personal values** in examples, configs, or fixtures (paths under `/home/<personal-username>`, machine names, IP addresses). Use placeholders (`<your-username>`, `<your-machine>`).
- **No "broken by default for a fresh downloader" assumptions.** Setup guides should walk through what a fresh clone actually needs, not what's already true on the maintainer's machine.

### Sub-Directory Instruction Files

Every doc subdirectory has an `AGENTS.md` + `CLAUDE.md` pair so any reader (human or agent) can orient inside the subdirectory without re-reading this strategy file.

Existing pairs:
- `docs/adr/AGENTS.md` + `CLAUDE.md`
- `docs/guides/AGENTS.md` + `CLAUDE.md`
- `docs/specs/AGENTS.md` + `CLAUDE.md`
- `docs/specs/product/AGENTS.md` + `CLAUDE.md`
- `docs/specs/technical/AGENTS.md` + `CLAUDE.md`
- `docs/specs/standards/AGENTS.md` + `CLAUDE.md`
- `docs/vision/AGENTS.md` + `CLAUDE.md`
- `docs/plans/AGENTS.md` + `CLAUDE.md` (gitignored directory; only the AGENTS pair is tracked)

**AGENTS.md** is the primary instruction file (universal, works with all AI tools). **CLAUDE.md** is a one-line wrapper: `@AGENTS.md`. The `@` import is relative to the file containing it; since CLAUDE.md and AGENTS.md are always co-located, the import is always literally `@AGENTS.md`.

**AGENTS.md structure (per subdirectory):**

```markdown
[One-line description of directory purpose]

## Contents
- `file.md` — one-line description
...

## Key Principles
- Critical rules that apply to this directory's content

## Related Documents
- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [Other relevant doc] — Why it matters here
```

Aim for ≤30 lines. Subdirectory AGENTS.md files are wayfinding, not extended commentary — keep them tight.

## Maintenance Rules

**When code changes:**
1. Update the relevant design doc in the same PR if the design changed (not the implementation — only when the *intent* changed).
2. Update `Last Updated` in the frontmatter.
3. Check if cross-references need updates.
4. Verify bidirectional links still resolve.

**When documents get long (over the Max in [Length Guidelines](#length-guidelines)):**
1. Identify distinct concerns within the document.
2. Create focused docs for each concern.
3. Keep the original path as a thin index pointing at the splits.
4. Update inbound references to the right split.

**Common violations to avoid:**
- Adding "Next Steps" or task lists to specs or ADRs.
- Mixing planning (roadmaps, tasks) into design documents.
- Creating a running backlog file (`backlog.md`, `todo.md`, `ideas.md`) instead of filing GitHub issues.
- Creating documents without "Related Documents" sections.
- Failing to update cross-references when moving or splitting documents.
- Modifying an accepted ADR in place (instead of superseding).
- Using real personal data in examples.

---

## Related Documents

### Project Context
- [Root AGENTS.md](../AGENTS.md) — Project-level agent reference, development principles, key files
- [Root CLAUDE.md](../CLAUDE.md) — Claude Code-specific configuration

### Specifications
- [vision/philosophy.md](vision/philosophy.md) — The privacy-first, local-first principles that frame doc decisions
- [specs/standards/](specs/standards/) — Coding and testing conventions referenced by docs

### Operational
- [guides/](guides/) — Operator-facing setup and troubleshooting
