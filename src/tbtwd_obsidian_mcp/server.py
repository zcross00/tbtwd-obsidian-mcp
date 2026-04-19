"""The Brain That Wouldn't Die — Obsidian MCP Server.

A read-through lens over the file-based Brain memory vault.
Exposes scoped retrieval tools via the Model Context Protocol.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("tbtwd-mcp")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)

from tbtwd_obsidian_mcp.storage import BrainVault

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "tbtwd-obsidian-mcp",
    instructions="""\
Persistent memory vault for The Brain That Wouldn't Die.

SESSION START: Always call get_brief first. It returns the active project, goals, \
focus area, backlog, AND suggested next tool calls — your L0 orientation context. \
Follow the next_steps list to load relevant context before acting.

READING:
- list_types: discover entity categories (goal, system, concept, decision, feature, drift, pattern, procedure, lesson) with counts.
- query: find entities by entity_type, status, tag, or project. Results sorted by project relevance.
- search: find entities by keyword across titles and body text. Use when you don't know the exact tag.
- get_context: drill into an entity by name, frontmatter ID, or GUID. Returns full content + linked synopses.
- get_relevant_context: ONE-CALL aggregation — pass a topic and get all relevant entities, decisions, drift, \
and coverage gaps in a single response. Use this instead of separate search → get_context chains.
- check_links: scan for broken [[wiki-links]].

WHEN TO READ:
- Before design decisions — check if a related decision or concept exists.
- Before implementing — read the relevant goal and system entities.
- When encountering a problem — search for lessons and procedures that may already cover it.
- When the user references something that might be in the vault — look it up.
- Follow [[wiki-links]] in entity bodies to discover related context.

PRE-ACTION VALIDATION:
- validate_action: CALL THIS before implementing significant changes. Pass your intended \
action and rationale. The tool checks for conflicting decisions, relevant lessons, \
applicable rules, and existing patterns. Returns 'proceed', 'review', or 'conflict'.
- If status is 'conflict': STOP and review the conflicting entities before proceeding.
- If status is 'review': read the supporting entities and applicable rules for additional context.
- If status is 'proceed': safe to continue, but consider persisting your rationale.
- The 'applicable_rules' field lists enforceable constraints. Rules are NOT suggestions — \
they are hard requirements that MUST be satisfied. Violations are errors.

DECISION AUDIT TRAIL:
- ALWAYS cite vault entities when they inform your decisions: "Per [[Entity Title]], using approach X."
- If no vault precedent exists, EXPLICITLY state: "No vault precedent found for X."
- After significant decisions, persist the rationale using the synthesis pipeline.

WRITING:
- update_memory: update an entity's YAML frontmatter. Auto-validates links, commits, and pushes to GitHub.
- update_body: update or create a named section in an entity's markdown body. Replaces existing \
sections or inserts new ones. Auto-validates links, commits, and pushes.
- archive_entity: move an entity to .trash/ preserving type folder structure. Archived entities \
are excluded from all retrieval by default. Reports incoming links that will break.

QUERYING ARCHIVED ENTITIES:
- query, search, and get_context accept an optional include_archived=True parameter.
- Default is False — archived entities are invisible unless explicitly requested.
- Use include_archived=True only when you need details of a previously archived entity \
(e.g. reviewing an old decision's rationale).

WHEN TO WRITE:
- Status changes: when work moves forward, update status.
- Decisions: when a design choice is made, persist it.
- Drift: when open questions or risks surface, flag them.
- Lessons: when a problem is solved through debugging or experimentation, persist the insight.
- Procedures: when a multi-step process is figured out, persist the steps for reuse.
- Patterns: when a recurring solution is identified, persist the approach.
- Metadata: when new relationships emerge, update tags, serves, depends-on.
- Archival: when a decision has been accepted, implemented, and its constraints extracted \
into rules — archive it. When a drift is resolved. When an entity is fully superseded.

SYNTHESIZING NEW KNOWLEDGE:
When new concepts, decisions, or patterns emerge from conversation or work:
1. Call get_extraction_schema to get the candidate format and rules.
2. Call list_tags to get the controlled tag vocabulary.
3. Extract candidates following the schema — atomic claims, specific titles, valid tags.
4. Call match_concepts with candidates to check for existing matches.
5. For 'ambiguous' matches: call get_context on the matched entity, then decide new vs merge.
6. Call synthesize with resolved candidates to persist them.
This pipeline ensures consistent, deterministic knowledge capture regardless of input source.

RULES:
- Don't fabricate entity content — read first, update what exists.
- Don't skip get_brief — orientation before action.
- Don't skip validate_action — check before significant changes.
- Don't update guid fields — permanent identifiers.
- Don't bulk-update without user awareness — mention what you're persisting.
- Don't use tags outside the controlled vocabulary (tags.yml).
- Don't synthesize without matching first — always preview with match_concepts.
- Don't let knowledge evaporate — if something was learned, persist it.

ENTITY STRUCTURE: Entities live in type folders (concept/, decision/, drift/, feature/, \
goal/, lesson/, pattern/, procedure/, rule/, system/). Each is a markdown file with YAML \
frontmatter (guid, id, title, status, type, project, tags, serves, depends-on) and a \
markdown body with [[wiki-links]]. Rule entities are enforceable constraints — violations \
are errors, not missed optimizations.\
""",
)

_vault_instance: BrainVault | None = None


def _get_vault() -> BrainVault:
    """Resolve the vault from environment variables (cached singleton).

    BRAIN_VAULT_REPO (required): Git remote URL — the vault is cloned/pulled on first access.
    BRAIN_VAULT_PATH (optional): Local override — skip clone, use this directory directly.
    """
    global _vault_instance
    if _vault_instance is not None:
        return _vault_instance

    log.debug("_get_vault: resolving (first call)...")
    t0 = time.monotonic()
    repo_url = os.environ.get("BRAIN_VAULT_REPO")
    vault_path = os.environ.get("BRAIN_VAULT_PATH")
    if not repo_url and not vault_path:
        raise RuntimeError(
            "Set BRAIN_VAULT_REPO (git remote URL) or BRAIN_VAULT_PATH (local directory)."
        )
    _vault_instance = BrainVault(repo_url=repo_url, vault_path=vault_path)
    log.debug("_get_vault: resolved in %.2fs, root=%s", time.monotonic() - t0, _vault_instance.root)
    return _vault_instance


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_brief() -> str:
    """Return the L0 bootstrap context from brief.yml.

    Always the first call in any session. Returns project identity,
    goal titles, current focus area, and backlog summary (~200-400 tokens).
    """
    vault = _get_vault()
    brief = vault.read_brief()
    return json.dumps(brief, indent=2, default=str)


@mcp.tool()
def list_types() -> str:
    """Return the type registry with descriptions and entity counts.

    Shows all entity types defined in the vault (e.g. goal, system, concept)
    with their descriptions, icons, and how many entities each contains.
    Useful for orientation and discovering what kinds of entities exist.
    """
    vault = _get_vault()
    types = vault.read_types()
    return json.dumps(types, indent=2, default=str)


@mcp.tool()
def get_context(entity_id: str, include_archived: bool = False) -> str:
    """Return the full entity for the given ID plus one-level-deep linked synopses.

    Args:
        entity_id: Entity identifier — a filename slug (e.g. 'storage-layer'),
            a path (e.g. 'systems/storage-layer'), or a frontmatter ID (e.g. 'S-1').
        include_archived: When True, also searches entities archived to .trash/.
            Default False — use only when looking up a specific archived entity.

    Returns entity content with frontmatter, body, and synopsis of each
    directly linked entity (~400-800 tokens).
    """
    vault = _get_vault()
    ctx = vault.get_context(entity_id, include_archived=include_archived)
    return json.dumps(ctx, indent=2, default=str)


@mcp.tool()
def query(
    tag: str | None = None,
    goal: str | None = None,
    status: str | None = None,
    entity_type: str | None = None,
    project: str | None = None,
    include_archived: bool = False,
) -> str:
    """Scan frontmatter across all entity files and return matching synopses.

    All filters are optional and combined with AND logic.
    Results are sorted by project relevance: active project entities get
    full synopses, universal entities (no project field) get normal detail,
    and background entities (other projects) get minimal one-liners.

    Args:
        tag: Filter by tag (e.g. "combat", "core-gameplay").
        goal: Filter by linked goal (e.g. "G-2").
        status: Filter by status (e.g. "In Progress", "concept").
        entity_type: Filter by type folder (e.g. "system", "goal", "decision", "concept", "feature", "drift").
        project: Override the active project for relevance sorting. Defaults to active-project from brief.yml.
        include_archived: When True, also includes entities archived to .trash/.
            Default False — use only when you need to find old/retired entities.

    Returns a compact list of matching IDs with one-line synopses.
    Use get_context() to drill into specific results.
    """
    vault = _get_vault()
    results = vault.query(
        tag=tag, goal=goal, status=status, entity_type=entity_type,
        project=project, include_archived=include_archived,
    )
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def update_memory(entity_id: str, fields: dict) -> str:
    """Update an entity's YAML frontmatter fields.

    Args:
        entity_id: Entity identifier — a filename slug, path, or frontmatter ID.
        fields: Dictionary of frontmatter fields to set or overwrite.

    Validates YAML structure and link integrity before persisting.
    Returns confirmation with any warnings (e.g. broken links).
    """
    vault = _get_vault()
    result = vault.update_memory(entity_id, fields)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def update_body(
    entity_id: str,
    field: str,
    content: str | list[str] | None = None,
) -> str:
    """Set or delete a field in an entity's markdown body.

    The server owns all document formatting. You provide the semantic field
    name and content — the server maps it to the correct ``## Heading``,
    enforces canonical section ordering, and manages spacing.

    **Set a field:** ``update_body("Storage Layer", field="intent", content="...")``
    **Delete a field:** ``update_body("Storage Layer", field="intent")``
    **Set preamble:** ``update_body("Storage Layer", field="preamble", content="...")``
    **Set related:** ``update_body("Storage Layer", field="related", content=["Foo", "Bar"])``

    Fields are validated against the entity type's schema (body-schema.yml).
    Unknown fields are rejected with a ValueError listing the allowed fields.

    Args:
        entity_id: Entity identifier — a filename slug, path, frontmatter ID, or GUID.
        field: Semantic field name from the body schema (e.g. "intent", "rationale",
            "preamble", "related", "constraint", "applicable_rules").
            The server maps this to the correct ## heading automatically.
        content: Field content. For most fields, a markdown string (no heading needed).
            For "related", a list of entity names (no [[]] wrapping needed).
            Omit or pass None to delete the field.

    Returns confirmation with action (created, replaced, deleted, or not_found)
    and any link warnings.
    """
    vault = _get_vault()
    result = vault.update_body(entity_id, field=field, content=content)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def archive_entity(entity_id: str) -> str:
    """Move an entity to .trash/ preserving its type folder structure.

    Use this to archive entities that have been fully implemented, resolved,
    or are otherwise no longer needed for active retrieval. Archived entities
    are excluded from search, query, and validate_action.

    The response includes incoming_links — a list of other entities that
    reference this one via wiki-links. These links will become broken after
    archiving. Review them and update or remove references as needed.

    Args:
        entity_id: Entity identifier — a filename slug, path, frontmatter ID, or GUID.

    Returns confirmation with original path, archive path, and any incoming links
    that will break.
    """
    vault = _get_vault()
    result = vault.archive_entity(entity_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def check_links() -> str:
    """Scan all vault files for broken [[wiki-links]].

    Returns a list of broken links with source file and target.
    Use for periodic integrity checks.
    """
    vault = _get_vault()
    broken = vault.check_links()
    if not broken:
        return json.dumps({"status": "ok", "message": "No broken links found"})
    return json.dumps({"status": "broken_links_found", "count": len(broken), "broken": broken}, indent=2)


@mcp.tool()
def search(
    text: str,
    entity_type: str | None = None,
    tag: str | None = None,
    max_results: int = 10,
    include_archived: bool = False,
) -> str:
    """Search entity titles and body text for keywords.

    Use this to find relevant knowledge when you don't know the exact tag or type.
    Especially useful for locating lessons, procedures, and patterns related to
    a problem you're trying to solve. Splits the query into words and scores
    entities by how many query words appear in their title and body.

    Args:
        text: Search query — keywords or phrases to find in entity content.
        entity_type: Optional filter by type (e.g. "lesson", "procedure", "concept").
        tag: Optional filter by tag.
        max_results: Maximum results to return (default 10).
        include_archived: When True, also searches entities archived to .trash/.
            Default False — use only when looking for old/retired knowledge.

    Returns ranked results with title, path, type, relevance score, and a
    context snippet showing where the match was found.
    """
    vault = _get_vault()
    results = vault.search(
        text, entity_type=entity_type, tag=tag,
        max_results=max_results, include_archived=include_archived,
    )
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def list_tags() -> str:
    """Return the controlled tag vocabulary from tags.yml.

    Shows all allowed tags organized by category with descriptions.
    Use before synthesize to ensure candidates use valid tags.
    """
    vault = _get_vault()
    tags = vault.read_tags()
    return json.dumps(tags, indent=2, default=str)


@mcp.tool()
def get_extraction_schema() -> str:
    """Return the extraction schema that defines how to produce concept candidates.

    The schema specifies the exact structure a candidate must have for
    match_concepts and synthesize to accept it. Use this to ensure
    deterministic, consistent concept extraction from any input.
    """
    vault = _get_vault()
    schema = vault.read_extraction_schema()
    return json.dumps(schema, indent=2, default=str)


@mcp.tool()
def match_concepts(candidates: list[dict]) -> str:
    """Match concept candidates against existing vault entities.

    Takes a list of concept candidates (each with at minimum title and tags)
    and returns them enriched with match information:
    - disposition: 'new' (no match), 'merge' (strong match), or 'ambiguous' (needs review)
    - matched_entity: the existing entity details (for merge/ambiguous)
    - match_score: 0.0-1.0 confidence
    - tag_warnings: any tags not in the controlled vocabulary

    Args:
        candidates: List of concept candidates. Each should have:
            - title (str): concept name
            - tags (list[str]): from controlled vocabulary (use list_tags)
            - claims (list[str]): atomic factual statements (optional for matching)
            - relationships (list[str]): wiki-link targets (optional for matching)
            - type (str): entity type, defaults to 'concept'

    Call this BEFORE synthesize to preview what will happen.
    Resolve any 'ambiguous' results before proceeding to synthesize.
    """
    vault = _get_vault()
    results = vault.match_concepts(candidates)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def synthesize(candidates: list[dict]) -> str:
    """Create or merge concept candidates into the vault.

    Each candidate must include a disposition from match_concepts:
    - 'new': creates a new entity file with frontmatter + claims body
    - 'merge': appends novel claims and unions tags into existing entity (additive only)
    - 'ambiguous': skipped — resolve the match first

    Args:
        candidates: List of candidates that have been through match_concepts.
            Required fields: title, disposition, tags, claims.
            For merge: must include matched_entity.path from match_concepts output.
            Optional: relationships (list of wiki-link targets), type (entity type),
                project (project name — defaults to active-project from brief.yml).

    Validates tags against controlled vocabulary. Commits and pushes changes.
    Returns per-candidate results with action taken and any warnings.
    """
    vault = _get_vault()
    results = vault.synthesize(candidates)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def get_relevant_context(
    topic: str,
    max_entities: int = 5,
    include_types: list[str] | None = None,
) -> str:
    """Single-call aggregation: get all vault context relevant to a topic.

    Combines search + query + context loading into one tool call, returning
    the most relevant entities, their linked synopses, related decisions,
    open drift items, and any coverage gaps. Use this instead of separate
    search → get_context chains to reduce tool call overhead.

    Args:
        topic: Natural-language description of what you're working on or
            deciding about. Be specific — "combat action resolution" is
            better than "combat".
        max_entities: Maximum number of full entities to return (default 5).
        include_types: Optional list of entity types to prioritize
            (e.g. ["decision", "lesson", "pattern"]).

    Returns entities with full content, linked synopses, applicable decisions,
    open drift items, and any aspects of the topic with no vault coverage.
    """
    vault = _get_vault()
    results = vault.get_relevant_context(
        topic, max_entities=max_entities, include_types=include_types
    )
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def validate_action(action: str, rationale: str) -> str:
    """Pre-action validation: check the vault for conflicts and rules before acting.

    Call this before implementing significant changes. Searches the vault
    for decisions, patterns, lessons, rules, and drift entries that may
    conflict with or inform your proposed action. Also loads all active
    rules and surfaces those relevant to the action.

    Returns one of three statuses:
    - 'proceed': no conflicts or relevant rules found
    - 'review': found applicable rules, supporting, or informational entities
    - 'conflict': found decisions or lessons that may contradict your plan

    The 'applicable_rules' field contains enforceable constraints that MUST
    be satisfied. Rules are not suggestions — they are hard requirements.

    Args:
        action: What you intend to do (e.g. "Add caching layer to query tool").
        rationale: Why you chose this approach (e.g. "Reduce repeated file I/O").
    """
    vault = _get_vault()
    result = vault.validate_action(action, rationale)
    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server via stdio transport."""
    log.info("Server starting, transport=stdio")
    mcp.run(transport="stdio")
    log.info("Server exited")


if __name__ == "__main__":
    main()
