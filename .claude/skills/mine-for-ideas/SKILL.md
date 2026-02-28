---
name: mine-for-ideas
description: Analyze an open-source repo and surface ideas/patterns LifeOS could learn from
argument-hint: <github-url> [context/guidance]
---

# Mine for Ideas: Open-Source Intelligence

Arguments: `$ARGUMENTS`

Parameters:
- **github-url** (required) — Full GitHub URL to the repo, must be the first argument (e.g., `https://github.com/org/repo`)
- **context** (optional) — Freetext guidance on what to focus on, what to compare, or what questions to answer. Everything after the URL is treated as context. Examples:
  - `pay special attention to their entity resolution approach vs. ours`
  - `how do they handle hybrid search? we use ChromaDB + FTS5 via RRF`
  - `their sync pipeline looks interesting, especially incremental indexing`

  If omitted, analyze the repo holistically for anything relevant to LifeOS.

## LifeOS Context

Project context for understanding what's relevant:

!`cat AGENTS.md`

## Instructions

You are mining an open-source project for ideas, patterns, and approaches that could benefit LifeOS. This is **architectural intelligence** — you're comparing engineering worldviews, not listing features. The goal is to understand how another team solved problems we also face, identify where their thinking reveals blind spots in ours, and flag where adopting their approach would create new problems we'd need to solve.

### Step 1: Parse arguments

The arguments string starts with a GitHub URL, followed by optional freetext context. Parse them as:
- **URL**: The first token matching `https://github.com/...`
- **Context**: Everything after the URL — freetext guidance on what to focus on, compare, or answer

If no URL is provided, stop and ask for one.

Parse the owner/repo from the URL (e.g., `https://github.com/chroma-core/chroma` -> `chroma-core/chroma`).

If context was provided, use it to guide exploration depth and focus areas. The context shapes which parts of the target repo to explore most deeply and which LifeOS specs to compare against. Weave it into the subagent prompts in Steps 2 and 3.

### Step 2: Explore the target repo

Use a thorough Explore subagent to analyze the target repository. The subagent should:

1. **Read the README** via WebFetch (`https://github.com/{owner}/{repo}`)
2. **Explore the repo structure** via the GitHub API:
   ```
   gh api repos/{owner}/{repo}/contents --jq '.[].name'
   ```
3. **Read key source files** — look for core modules, main abstractions, interesting patterns. Use:
   ```
   gh api repos/{owner}/{repo}/contents/{path} --jq '.content' | base64 -d
   ```
4. **Read documentation** if it exists (docs/, README, ARCHITECTURE.md, etc.)

If the user provided context/guidance, use it to direct exploration — concentrate on the areas they called out, compare against the aspects they're curious about, and answer the questions they raised. Otherwise, explore broadly and identify what's most relevant to LifeOS.

The subagent should extract:
- Core architecture and design philosophy
- Key abstractions and data structures — not just names, but *why* they're shaped that way
- How they handle problems LifeOS also faces (entity resolution, hybrid search, sync pipelines, personal data indexing, agentic chat)
- What their fundamental mental model is (batch vs live? pipeline vs service? mutable vs append-only? cloud vs local-first?)
- Novel or creative patterns, especially ones that challenge our assumptions
- Testing approaches worth noting

### Step 3: Understand LifeOS's current approach

Use a second subagent to read the relevant LifeOS specs and implementation for the same domain areas. Focus on:
- Product specs in `docs/specs/product/`
- Technical specs in `docs/specs/technical/`
- ADRs in `docs/adr/`
- Existing implementation in `api/` and `config/`

The subagent should focus on understanding LifeOS's **architectural assumptions and constraints** — not just what the code looks like, but what invariants we've committed to (two-tier SourceEntity/PersonEntity model, hybrid search via RRF, local-only data, ChromaDB + FTS5, five-phase sync pipeline, agentic chat with tool calls, macOS-native integrations).

### Step 4: Synthesize the comparison

Run both subagents in parallel. Once both complete, produce the analysis below.

**Important**: Run the exploration and LifeOS context subagents concurrently for efficiency.

### Step 5: Write the analysis

This analysis has two parts: **understanding** (what are the real differences?) and **judgment** (what should we do about them?). Both matter. Don't skip to recommendations without first building deep understanding.

Produce the following sections. Be direct, specific, and opinionated. Write at the level of an architect who has internalized both systems.

---

#### Repo Overview

2-3 sentences on what the repo does and its tech stack. Include stars/activity if notable.

#### Fundamental Differences

This is the most important section. For each major difference between their approach and ours (aim for 3-6):

- **Bold title** — name the difference as an architectural choice, not a feature (e.g., "Cloud-native vector ops vs. local ChromaDB", not "They use Pinecone")
- **Their approach**: What they do and *why* their system is shaped this way. Reference specific files, abstractions, or patterns. Explain the mental model, not just the mechanism.
- **Our approach**: What LifeOS does (or plans to do) and why. Reference specific specs, ADRs, or code.
- **Why the difference matters**: What does their choice enable that ours doesn't? What does ours enable that theirs doesn't? Be concrete — talk about specific scenarios, query patterns, or user experiences that differ.

Don't flatten this into "pros and cons." Each difference reflects a tradeoff rooted in different system constraints. Name those constraints.

#### Tradeoff Analysis

For each difference above that initially looks appealing to adopt, stress-test it against LifeOS's constraints. This section is adversarial — it's where you identify what would **break or degrade** if we naively adopted an appealing pattern.

Think about:
- **Privacy**: Does this require sending personal data to external services? LifeOS is local-only by design (AGENTS.md § Privacy Is Non-Negotiable). Claude API is the only exception, used for discrete queries.
- **Stability**: Would this make the system's behavior less predictable over time? Would entities or search results shift without user action?
- **Performance**: Does this add query-time computation where we currently have pre-computed results? Does it conflict with "every operation should be delightfully fast"?
- **Blast radius**: Can a small change cascade into large, unexpected effects? How does this interact with our two-tier data model (SourceEntity -> PersonEntity)?
- **Simplicity**: Is the implementation complexity justified by the benefit, given LifeOS's current stage? (AGENTS.md § Simplicity First)
- **macOS constraints**: Does this work within our macOS-native setup (FDA, launchd, Tailscale, no Docker)?

Be specific. "This could cause instability" is not useful. "A new match rule between SourceEntity A and SourceEntity B could transitively merge two previously-separate PersonEntity clusters without user action, changing CRM results for both" is useful.

#### What to Adopt (and How)

For each idea worth adopting, explain the **adapted version** that works within LifeOS's constraints — not the original pattern, but the version that captures the insight while respecting our architectural invariants. Be concrete about:
- What would change in the schema, spec, or code
- What stays the same
- What new problems the adoption creates and how to handle them

#### What We're Doing Better

Areas where LifeOS's approach is already stronger. Reinforces good decisions. Keep this short — 2-3 items max. Explain *why* our approach is better for our use case, not just that it's different.

#### Net Recommendation

A short synthesis (3-5 sentences). State the single biggest architectural insight, whether it warrants an ADR, spec change, or just awareness. If the answer is "nothing to adopt, but this validated our approach," say that.

---

### Quality bar

The analysis should read like an architect's memo, not a feature comparison table. The reader should come away understanding:
1. How the other project's team *thinks* about the problem (their mental model)
2. Where their thinking reveals something we hadn't considered
3. Whether and how to adapt their ideas without breaking our invariants
4. What we're already doing right that we should protect

Avoid:
- Surface-level pattern-spotting ("they use X, we could use X too")
- Listing features without analyzing tradeoffs
- Generic praise or dismissal
- Recommendations without concrete integration analysis

### Step 6: Offer to create a GitHub issue

After presenting the analysis, ask the user:

> Would you like me to create a GitHub issue to track any of these ideas? I can create one tagged with the `idea` label containing the key findings and references.

If the user says yes, ask which ideas to include (or all), then create the issue:

```
gh issue create \
  --title "Idea: [concise title from net recommendation]" \
  --label "idea" \
  --body "$(cat <<'EOF'
## Source

[Repo Name](github-url) — brief description

## Key Ideas

[Selected ideas from the "What to Adopt" section, with the adapted-for-LifeOS version, not the raw pattern]

## Tradeoffs & Risks

[Key concerns from the tradeoff analysis that must be addressed]

## Suggested Next Step

[ADR, spec change, prototype, or just "keep in mind for when we build X"]

## References

- [Link to specific files/patterns in the source repo]
- [Link to relevant LifeOS specs]

---
*Generated by `/mine-for-ideas`*
EOF
)"
```

Ensure the `idea` label exists first:
```
gh label create idea --description "Ideas mined from external projects" --color "0E8A16" 2>/dev/null || true
```

## Escalation

Stop and tell the user when:
- No GitHub URL provided in arguments
- `gh` API calls fail (auth, rate limit, or network)
- The repo is private or inaccessible
- The repo is too large to meaningfully explore (>500 files in core dirs) — suggest a focus area
