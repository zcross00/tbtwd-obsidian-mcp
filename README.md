# tbtwd-obsidian-mcp

MCP server for **The Brain That Wouldn't Die** — scoped retrieval and write tools over an Obsidian-style memory vault.

## Tools

| Tool | Purpose |
|------|---------|
| `get_brief()` | L0 bootstrap context from `brief.yml` (~200–400 tokens) |
| `get_context(id)` | Entity + one-level-deep linked synopses (~400–800 tokens) |
| `query(tag?, goal?, status?, entity_type?)` | Scan frontmatter, return matching synopses |
| `update_memory(id, fields)` | Update entity frontmatter with link validation |
| `check_links()` | Find all broken `[[wiki-links]]` across the vault |

## Setup

```bash
# Install in development mode
pip install -e .
```

## Configuration

Set the `BRAIN_VAULT_PATH` environment variable to the root of your Brain vault:

```bash
export BRAIN_VAULT_PATH=/path/to/TheBrainThatWouldntDie
```

## Running

```bash
# Run directly
tbtwd-obsidian-mcp

# Or via the MCP CLI
mcp run tbtwd_obsidian_mcp.server:mcp
```

## VS Code / Copilot MCP Configuration

Add to your `.vscode/mcp.json` or user MCP settings:

```json
{
  "servers": {
    "tbtwd-obsidian": {
      "command": "tbtwd-obsidian-mcp",
      "env": {
        "BRAIN_VAULT_PATH": "c:\\Users\\zcros\\development\\TheBrainThatWouldntDie"
      }
    }
  }
}
```