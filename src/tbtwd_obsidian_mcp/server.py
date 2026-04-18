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
focus area, and backlog — your L0 orientation context.

READING:
- list_types: discover entity categories (goal, system, concept, decision, feature, drift) with counts.
- query: find entities by entity_type, status, tag, or project. Results sorted by project relevance.
- get_context: drill into an entity by name, frontmatter ID, or GUID. Returns full content + linked synopses.
- check_links: scan for broken [[wiki-links]].

WHEN TO READ:
- Before design decisions — check if a related decision or concept exists.
- Before implementing — read the relevant goal and system entities.
- When the user references something that might be in the vault — look it up.
- Follow [[wiki-links]] in entity bodies to discover related context.

WRITING:
- update_memory: update an entity's YAML frontmatter. Auto-validates links, commits, and pushes to GitHub.

WHEN TO WRITE:
- Status changes: when work moves forward, update status.
- Decisions: when a design choice is made, persist it.
- Drift: when open questions or risks surface, flag them.
- Metadata: when new relationships emerge, update tags, serves, depends-on.

RULES:
- Don't fabricate entity content — read first, update what exists.
- Don't skip get_brief — orientation before action.
- Don't update guid fields — permanent identifiers.
- Don't bulk-update without user awareness — mention what you're persisting.

ENTITY STRUCTURE: Entities live in type folders (concept/, decision/, drift/, feature/, \
goal/, system/). Each is a markdown file with YAML frontmatter (guid, id, title, status, \
type, project, tags, serves, depends-on) and a markdown body with [[wiki-links]].\
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
def get_context(entity_id: str) -> str:
    """Return the full entity for the given ID plus one-level-deep linked synopses.

    Args:
        entity_id: Entity identifier — a filename slug (e.g. 'storage-layer'),
            a path (e.g. 'systems/storage-layer'), or a frontmatter ID (e.g. 'S-1').

    Returns entity content with frontmatter, body, and synopsis of each
    directly linked entity (~400-800 tokens).
    """
    vault = _get_vault()
    ctx = vault.get_context(entity_id)
    return json.dumps(ctx, indent=2, default=str)


@mcp.tool()
def query(
    tag: str | None = None,
    goal: str | None = None,
    status: str | None = None,
    entity_type: str | None = None,
    project: str | None = None,
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

    Returns a compact list of matching IDs with one-line synopses.
    Use get_context() to drill into specific results.
    """
    vault = _get_vault()
    results = vault.query(tag=tag, goal=goal, status=status, entity_type=entity_type, project=project)
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
