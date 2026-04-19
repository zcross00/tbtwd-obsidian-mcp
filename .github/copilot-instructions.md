# Project Instructions — The Brain That Wouldn't Die

## Vault-First Development Protocol

This workspace develops and uses the **tbtwd-obsidian** server — a persistent memory vault exposed via the Model Context Protocol. The vault is the authoritative source of design decisions, concepts, patterns, procedures, lessons, and project state across all projects.

**BLOCKING REQUIREMENT:** Before making any design decision, implementing any feature, or changing architecture, you MUST consult the vault. The vault contains accumulated project knowledge that supersedes assumptions.

## Session Start Protocol

1. **Always call `get_brief` first.** This returns the active project, goals, focus area, and backlog.
2. **Query for relevant context** — use `get_relevant_context(topic)` for one-shot aggregation, or `search`/`query`/`get_context` for targeted lookups.
3. **Load the component tree** — `query(entity_type="system", project="...")` to understand what exists in the active project.
4. **Cite vault sources** — reference entity titles (e.g., "per [[Work Implementation]]") when your actions are informed by vault knowledge.

## Procedures

The vault contains refined procedures for common workflows. Query and follow these:
- **[[Work Implementation]]** — full lifecycle from task understanding to delivery
- **[[Work Verification]]** — compile gate → test gate → criteria verification → report
- **[[Work Planning]]** — goals → component tree → gap analysis → work items
- **[[Strategic Review]]** — drift detection, process improvements, phase assessment
- **[[Error Diagnosis]]** — structured triage for compile, test, runtime, and tooling errors
- **[[Safe Refactoring]]** — impact mapping → migration → verification gates
- **[[Design Synchronization]]** — keeping component designs current after changes
- **[[Knowledge Cross-Pollination]]** — enriching existing entities with information from other entities
- **[[Decision Management]]** — recognizing, creating, surfacing, resolving, and reversing decision entities

## Living Component Design

Per [[Living Component Design]], every project has a component tree of `system` entities. These describe actual architecture — what code exists and how it works. Update them after implementation. Split them when they grow complex. Create new ones for new systems.

## Decision Audit Trail

Follow [[Decision Management]] for the full lifecycle. Key points:

- **Search the vault first** for existing decisions, patterns, or lessons.
- **Never silently make a decision** — surface choices, alternatives, and recommendations to the user.
- **Cite the vault entity** if one exists: "Per [[Decision Title]], using approach X because..."
- **Flag gaps explicitly** if no vault precedent exists.
- After significant decisions, **persist new knowledge** via the synthesis pipeline.

## Knowledge Gathering (CRITICAL)

The vault must grow smarter with every session. **Never skip an opportunity to capture useful knowledge.** Actively look for insights worth persisting throughout every task — not just at the end.

Capture: debugging insights, architectural discoveries, process observations, user decisions, corrections to stale vault data, coding patterns observed in the codebase, anything a future session might need to know.

Persistence pipeline:
1. `get_extraction_schema` for the candidate format.
2. `list_tags` for the controlled vocabulary.
3. Extract candidates as atomic claims.
4. `match_concepts` to check for existing matches.
5. `synthesize` to persist.

Do not let knowledge evaporate. If something was learned, persist it.

## Conflict Detection (CRITICAL)

When new information contradicts existing vault knowledge — from code, errors, user statements, or any source — you MUST flag it to the user immediately. Never silently accept or discard either side. The vault could be stale, or the new information could be wrong. State both sides, explain which seems more current and why, and let the user decide. After resolution, update the vault. This applies to ALL contradictions, no matter how minor.

**User input is high-authority.** Treat what the user says as important and likely correct, but always verify against the vault. Never ignore the user because the vault disagrees — and never ignore the vault because the user said something different. Surface every inconsistency, quote both sides, and let the user decide. Then update the vault. Recognizing inconsistencies is the single most valuable thing the agent can do for knowledge integrity.

## MCP Server Development

When modifying the MCP server code (`src/tbtwd_obsidian_mcp/`):
- The server is stateless — files are the source of truth, no shadow state
- Tools should return scoped, minimal context to stay within token budgets
- Search the vault for related `lesson` and `procedure` entities before debugging
- Check `drift` entities for known open questions before implementing uncertain features
