# Project Instructions — The Brain That Wouldn't Die

## Vault-First Development Protocol

This workspace develops and uses the **tbtwd-obsidian-mcp** server — a persistent memory vault exposed via the Model Context Protocol. The vault is the authoritative source of design decisions, concepts, patterns, lessons, and project state.

**BLOCKING REQUIREMENT:** Before making any design decision, implementing any feature, or changing architecture, you MUST consult the vault. The vault contains accumulated project knowledge that supersedes assumptions.

## Session Start Protocol

1. **Always call `get_brief` first.** This returns the active project, goals, focus area, and backlog. Do not skip this step.
2. **Determine scope** — identify which systems, concepts, or decisions relate to the current task.
3. **Query the vault** — use `search`, `query`, or `get_context` to load relevant entities before acting.
4. **Cite vault sources** — reference entity titles (e.g., "per [[Tiered Memory Architecture]]") when your actions are informed by vault knowledge.

## Decision Audit Trail

When making a decision or choosing an implementation approach:
- **Search the vault first** for existing decisions, patterns, or lessons that apply.
- **Cite the vault entity** if one exists: "Per [[Decision Title]], using approach X because..."
- **Flag gaps explicitly** if no vault precedent exists: "No vault precedent found for X — this may warrant a new decision/drift entity."
- After significant decisions, **persist new knowledge** back to the vault using the synthesis pipeline.

## Knowledge Persistence

When new insights, patterns, or decisions emerge during work:
1. Call `get_extraction_schema` for the candidate format.
2. Call `list_tags` for the controlled vocabulary.
3. Extract candidates as atomic claims with specific titles.
4. Call `match_concepts` to check for existing matches.
5. Call `synthesize` to persist resolved candidates.

Do not let knowledge evaporate at session end. If something was learned, persist it.

## Architecture Awareness

Key vault entities to be aware of:
- **Tiered Memory Architecture** — L0/L1/L2 loading protocol
- **Link-Addressable Memory** — wiki-links as traversable relationships
- **Concept Ingestion Pipeline** — extract → match → synthesize
- **Claim-Based Concept Decomposition** — atomic, falsifiable claims as the unit of knowledge
- **Background Push Threading** — git push runs in daemon threads to avoid blocking

## MCP Server Development

When modifying the MCP server code (`src/tbtwd_obsidian_mcp/`):
- The server is stateless — files are the source of truth, no shadow state
- Tools should return scoped, minimal context to stay within token budgets
- Search the vault for related `lesson` and `procedure` entities before debugging issues
- Check `drift` entities for known open questions before implementing uncertain features
