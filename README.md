# tbtwd-obsidian-mcp

MCP server for **The Brain That Wouldn't Die** — persistent structured memory for AI agents, exposed via the Model Context Protocol over an Obsidian-style markdown vault.

## Tools (13)

### Orientation

| Tool | Purpose | Budget |
|------|---------|--------|
| `get_brief()` | L0 bootstrap: active project, goals, focus area, suggested next steps | ~300 tokens |
| `list_types()` | Type registry with descriptions and entity counts | ~200 tokens |

### Retrieval

| Tool | Purpose | Budget |
|------|---------|--------|
| `get_context(entity_id)` | Full entity + one-level-deep linked synopses | ~400–800 tokens |
| `query(tag?, goal?, status?, entity_type?, project?)` | Frontmatter scan with project-relevance sorting | ~100–500 tokens |
| `search(text, entity_type?, tag?, max_results?)` | Keyword search with title 2x weighting and context snippets | ~200–800 tokens |
| `get_relevant_context(topic, max_entities?, include_types?)` | Single-call aggregation: entities + decisions + drift + coverage gaps | ~500–2000 tokens |

### Knowledge Synthesis Pipeline

| Tool | Purpose |
|------|---------|
| `get_extraction_schema()` | Returns the candidate format for structured knowledge extraction |
| `list_tags()` | Controlled tag vocabulary from `tags.yml` |
| `match_concepts(candidates)` | Fuzzy-match candidates against existing entities → disposition (new/merge/ambiguous) |
| `synthesize(candidates)` | Create or merge candidates into vault with additive-only strategy |

### Write

| Tool | Purpose |
|------|---------|
| `update_memory(entity_id, fields)` | Update entity YAML frontmatter with link validation |

### Validation

| Tool | Purpose |
|------|---------|
| `check_links()` | Scan all files for broken `[[wiki-links]]` |
| `validate_action(action, rationale)` | Pre-action conflict check against vault decisions, lessons, patterns |

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