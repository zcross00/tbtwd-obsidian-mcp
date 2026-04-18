"""Storage layer — file I/O, YAML/frontmatter parsing, and link resolution over the Brain vault."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# Folders to skip when scanning for entity subfolders
_IGNORED_FOLDERS: set[str] = {"Templates"}


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body) from a file's text content."""
    m = _FRONTMATTER_RE.match(text)
    if m:
        fm = yaml.safe_load(m.group(1)) or {}
        body = text[m.end():]
        return fm, body
    return {}, text


def _serialize_frontmatter(fm: dict[str, Any], body: str) -> str:
    """Re-serialize frontmatter + body into file text."""
    fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False).rstrip("\n")
    return f"---\n{fm_str}\n---\n{body}"


def extract_wikilinks(text: str) -> list[str]:
    """Return all [[wiki-link]] targets found in *text*."""
    return _WIKILINK_RE.findall(text)


# ---------------------------------------------------------------------------
# BrainVault — main interface to the on-disk storage
# ---------------------------------------------------------------------------


class BrainVault:
    """Read/write interface to the Brain vault directory."""

    def __init__(self, vault_path: str | Path) -> None:
        self.root = Path(vault_path).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Vault directory not found: {self.root}")

    # -- brief.yml (L0) ----------------------------------------------------

    def read_brief(self) -> dict[str, Any]:
        """Return the parsed contents of brief.yml."""
        brief_path = self.root / "brief.yml"
        if not brief_path.exists():
            raise FileNotFoundError("brief.yml not found in vault root")
        return yaml.safe_load(brief_path.read_text(encoding="utf-8")) or {}

    def _active_project(self) -> str | None:
        """Return the active-project from brief.yml, or None."""
        try:
            brief = self.read_brief()
        except FileNotFoundError:
            return None
        return brief.get("active-project")

    @staticmethod
    def _entity_relevance(fm: dict[str, Any], active_project: str | None) -> str:
        """Classify relevance: 'active', 'universal', or 'background'.

        - active: entity's project list contains the active project
        - universal: entity has no project field (always relevant)
        - background: entity belongs to other projects only
        """
        projects = fm.get("project")
        if projects is None:
            return "universal"
        if active_project and active_project in projects:
            return "active"
        return "background"

    # -- entity files ------------------------------------------------------

    def _entity_folders(self) -> list[str]:
        """Discover all entity subfolders dynamically (any non-hidden, non-ignored directory)."""
        return [
            d.name
            for d in self.root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in _IGNORED_FOLDERS
        ]

    def _iter_entity_files(self) -> list[tuple[str, Path]]:
        """Return (subfolder_name, path) for every entity file in the vault."""
        results: list[tuple[str, Path]] = []
        for folder_name in self._entity_folders():
            folder = self.root / folder_name
            for p in folder.iterdir():
                if p.suffix in (".md", ".yml") and p.is_file():
                    results.append((folder_name, p))
        return results

    def _resolve_entity_path(self, identifier: str) -> Path | None:
        """Resolve an entity by ID (e.g. 'S-1'), slug (e.g. 'storage-layer'),
        or path (e.g. 'systems/storage-layer') to its file path.
        """
        # If identifier contains a slash, treat as folder/slug
        if "/" in identifier:
            folder, slug = identifier.rsplit("/", 1)
            for ext in ("", ".md", ".yml"):
                p = self.root / folder / f"{slug}{ext}"
                if p.exists():
                    return p
            return None

        # Try matching by filename slug across all entity folders
        for _, path in self._iter_entity_files():
            if path.stem == identifier:
                return path

        # Try matching by frontmatter id field
        for _, path in self._iter_entity_files():
            text = path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)
            if fm.get("id") == identifier:
                return path

        return None

    def read_entity(self, identifier: str) -> dict[str, Any]:
        """Read an entity file and return {id, frontmatter, body, links, path}."""
        path = self._resolve_entity_path(identifier)
        if path is None:
            raise FileNotFoundError(f"Entity '{identifier}' not found in vault")

        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        links = extract_wikilinks(text)

        return {
            "id": fm.get("id", path.stem),
            "frontmatter": fm,
            "body": body.strip(),
            "links": links,
            "path": str(path.relative_to(self.root)),
        }

    def _synopsis(self, identifier: str) -> dict[str, Any] | None:
        """Return a minimal synopsis (title + status) for an entity, or None."""
        try:
            entity = self.read_entity(identifier)
        except FileNotFoundError:
            return None
        fm = entity["frontmatter"]
        return {
            "id": entity["id"],
            "title": fm.get("title", entity["id"]),
            "status": fm.get("status", "unknown"),
        }

    def get_context(self, entity_id: str) -> dict[str, Any]:
        """Return the entity + one-level-deep synopses of linked entities."""
        entity = self.read_entity(entity_id)

        # Resolve linked entities from wiki-links
        linked_synopses: list[dict[str, Any]] = []
        for link_target in entity["links"]:
            # Try full path first (e.g. "goals/token-efficient-orientation"),
            # then just the leaf slug
            syn = self._synopsis(link_target)
            if not syn:
                leaf = link_target.rsplit("/", 1)[-1]
                syn = self._synopsis(leaf)
            if syn:
                linked_synopses.append(syn)

        entity["linked_entities"] = linked_synopses
        return entity

    # -- query across files ------------------------------------------------

    def query(
        self,
        *,
        tag: str | None = None,
        goal: str | None = None,
        status: str | None = None,
        entity_type: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        """Scan frontmatter across all entity files. Return matching synopses.

        Results are sorted by project relevance: active project first,
        then universal (no project field), then background.
        Active/universal entities get full synopses; background entities
        get minimal one-liners.
        """
        active = project or self._active_project()
        matches: list[dict[str, Any]] = []

        for folder_name, path in self._iter_entity_files():
            text = path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)

            # Filter by entity_type (folder-based)
            if entity_type:
                if folder_name != entity_type.lower():
                    continue

            # Filter by status
            if status and fm.get("status", "").lower() != status.lower():
                continue

            # Filter by tag
            if tag:
                file_tags = [t.lower() for t in fm.get("tags", [])]
                if tag.lower() not in file_tags:
                    continue

            # Filter by goal (check serves / goal fields for wiki-links to the goal)
            if goal:
                links = extract_wikilinks(str(fm))
                goal_found = any(goal.upper() in link.upper() for link in links)
                if not goal_found:
                    continue

            entity_id = fm.get("id", path.stem)
            relevance = self._entity_relevance(fm, active)

            entry: dict[str, Any] = {
                "id": entity_id,
                "title": fm.get("title", entity_id),
                "relevance": relevance,
            }
            # Active and universal get full synopsis; background gets minimal
            if relevance != "background":
                entry["status"] = fm.get("status", "unknown")
                entry["tags"] = fm.get("tags", [])
            matches.append(entry)

        # Sort: active first, universal second, background last
        relevance_order = {"active": 0, "universal": 1, "background": 2}
        matches.sort(key=lambda m: relevance_order.get(m["relevance"], 9))

        return matches

    # -- update ------------------------------------------------------------

    def update_memory(self, entity_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Update an entity's frontmatter fields. Returns confirmation + warnings."""
        path = self._resolve_entity_path(entity_id)
        if path is None:
            raise FileNotFoundError(f"Entity {entity_id} not found in vault")

        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        fm.update(fields)

        # Validate links in the updated frontmatter
        new_text = _serialize_frontmatter(fm, body)
        warnings = self._validate_links(new_text)

        path.write_text(new_text, encoding="utf-8")

        return {
            "updated": entity_id,
            "fields": list(fields.keys()),
            "warnings": warnings,
        }

    # -- link checking -----------------------------------------------------

    def _resolve_link(self, target: str) -> bool:
        """Check whether a wiki-link target resolves to an existing file."""
        # Direct path (e.g. "systems/storage-layer")
        for ext in ("", ".md", ".yml"):
            if (self.root / f"{target}{ext}").exists():
                return True
        # Bare slug — search all entity folders
        leaf = target.rsplit("/", 1)[-1]
        for folder_name in self._entity_folders():
            for ext in (".md", ".yml"):
                if (self.root / folder_name / f"{leaf}{ext}").exists():
                    return True
        return False

    def _validate_links(self, text: str) -> list[str]:
        """Return a list of warning strings for any broken links in *text*."""
        warnings: list[str] = []
        for link in extract_wikilinks(text):
            if not self._resolve_link(link):
                warnings.append(f"link [[{link}]] not found")
        return warnings

    def check_links(self) -> list[dict[str, str]]:
        """Scan all files for broken wiki-links. Return [{source, target}]."""
        broken: list[dict[str, str]] = []

        # Check all entity files
        for _, path in self._iter_entity_files():
            text = path.read_text(encoding="utf-8")
            for link in extract_wikilinks(text):
                if not self._resolve_link(link):
                    broken.append({
                        "source": str(path.relative_to(self.root)),
                        "target": link,
                    })

        # Also check brief.yml and any root-level .md files
        for p in self.root.iterdir():
            if p.is_file() and p.suffix in (".md", ".yml", ".yaml"):
                text = p.read_text(encoding="utf-8")
                for link in extract_wikilinks(text):
                    if not self._resolve_link(link):
                        broken.append({
                            "source": p.name,
                            "target": link,
                        })

        return broken
