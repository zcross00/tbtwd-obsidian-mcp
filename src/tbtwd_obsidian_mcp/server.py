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
    instructions=(
        "Persistent memory vault for The Brain That Wouldn't Die. "
        "Start every session with get_brief for orientation. "
        "Use query to find entities by type/status/tag, get_context to drill in, "
        "and update_memory to persist changes (auto-commits to GitHub). "
        "Entities are organized by type: goal, system, concept, decision, feature, drift."
    ),
)


def _get_vault() -> BrainVault:
    """Resolve the vault from environment variables.

    BRAIN_VAULT_REPO (required): Git remote URL — the vault is cloned/pulled on startup.
    BRAIN_VAULT_PATH (optional): Local override — skip git, use this directory directly.
    """
    repo_url = os.environ.get("BRAIN_VAULT_REPO")
    vault_path = os.environ.get("BRAIN_VAULT_PATH")
    if not repo_url and not vault_path:
        raise RuntimeError(
            "Set BRAIN_VAULT_REPO (git remote URL) or BRAIN_VAULT_PATH (local directory)."
        )
    return BrainVault(repo_url=repo_url, vault_path=vault_path)


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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
