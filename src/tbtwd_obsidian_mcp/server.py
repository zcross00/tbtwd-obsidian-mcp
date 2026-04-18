"""The Brain That Wouldn't Die — Obsidian MCP Server.

A read-through lens over the file-based Brain memory vault.
Exposes scoped retrieval tools via the Model Context Protocol.
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

from tbtwd_obsidian_mcp.storage import BrainVault

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "tbtwd-obsidian-mcp",
    instructions="Scoped retrieval over The Brain That Wouldn't Die memory vault",
)


def _get_vault() -> BrainVault:
    """Resolve the vault path from the environment variable BRAIN_VAULT_PATH."""
    vault_path = os.environ.get("BRAIN_VAULT_PATH")
    if not vault_path:
        raise RuntimeError(
            "BRAIN_VAULT_PATH environment variable is not set. "
            "Point it to the root of your Brain vault directory."
        )
    return BrainVault(vault_path)


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
        entity_type: Filter by entity folder (e.g. "systems", "goals", "decisions", "features", "drift").
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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
