This directory contains specifications — design documents describing what the system is and should be (target state).

## Contents

- `product/` — What the system does from a consumer perspective (features, API contracts, data model semantics)
- `technical/` — How the system is built (architecture, schema, security, infrastructure)
- `standards/` — Rules and conventions for how we work (coding standards, testing patterns, naming rules)

## Key Principles

- Specs describe the **vision** — the authoritative design. Not implementation plans, not task lists, not roadmaps.
- "Living" means update when *the design* changes, not when *tasks* complete.
- Every spec must include frontmatter: `Status`, `Last Updated`. Product and technical specs also need `Owner`.
- Never add "Next Steps" or task lists to specs — those belong in `plans/` or GitHub issues.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
