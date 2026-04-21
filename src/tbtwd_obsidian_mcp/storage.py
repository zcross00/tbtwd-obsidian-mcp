"""Storage layer — file I/O, YAML/frontmatter parsing, and link resolution over the Brain vault."""

from __future__ import annotations

import atexit
import hashlib
import logging
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("tbtwd-mcp.storage")

import yaml

# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# Root-level items that are not type folders
_IGNORED_ROOTS: set[str] = {".obsidian", ".vscode", ".git", "Templates"}


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
    """Return all [[wiki-link]] targets found in *text*.

    Skips links inside fenced code blocks and inline code spans.
    """
    # Strip fenced code blocks first, then inline code spans
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`]+`", "", stripped)
    return _WIKILINK_RE.findall(stripped)


# ---------------------------------------------------------------------------
# BrainVault — main interface to the on-disk storage
# ---------------------------------------------------------------------------


class BrainVault:
    """Read/write interface to the Brain vault directory."""

    _CACHE_ROOT = Path.home() / ".cache" / "tbtwd-brain"

    _PUSH_INTERVAL = 60  # seconds between batched pushes

    def __init__(
        self,
        *,
        repo_url: str | None = None,
        vault_path: str | Path | None = None,
        push_interval: float | None = None,
    ) -> None:
        self._repo_url = repo_url
        self._remote_verified = False
        self._git_lock = threading.Lock()
        self._has_unpushed = False
        self._push_stop = threading.Event()
        self._push_interval = push_interval if push_interval is not None else self._PUSH_INTERVAL
        log.debug("BrainVault.__init__(repo_url=%s, vault_path=%s)", repo_url, vault_path)

        if vault_path:
            # Explicit local path — use directly
            self.root = Path(vault_path).resolve()
            if not self.root.is_dir():
                raise FileNotFoundError(f"Vault directory not found: {self.root}")
            # Defer remote setup — only needed on push, verified lazily
        elif repo_url:
            # No local path — clone/pull from remote into cache
            self.root = self._cache_dir(repo_url)
            log.debug("_clone_or_pull start")
            t0 = time.monotonic()
            self._clone_or_pull(repo_url)
            log.debug("_clone_or_pull done in %.2fs", time.monotonic() - t0)
        else:
            raise ValueError(
                "Provide at least one of repo_url or vault_path."
            )

        # Start the batched push timer if we have a remote
        self._push_thread: threading.Thread | None = None
        if self._repo_url:
            self._push_thread = threading.Thread(
                target=self._push_timer_loop, daemon=True
            )
            self._push_thread.start()
            atexit.register(self._shutdown_push)

        log.debug("BrainVault ready, root=%s", self.root)

    # -- git operations ----------------------------------------------------

    @classmethod
    def _cache_dir(cls, repo_url: str) -> Path:
        """Return a deterministic cache directory for a repo URL."""
        slug = hashlib.sha256(repo_url.encode()).hexdigest()[:12]
        return cls._CACHE_ROOT / slug

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a git command in the vault directory."""
        log.debug("git %s", " ".join(args))
        t0 = time.monotonic()
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        log.debug("git %s finished in %.2fs (rc=%d)", args[0], time.monotonic() - t0, result.returncode)
        return result

    def _clone_or_pull(self, repo_url: str) -> None:
        """Clone the repo into the cache, or pull if already present."""
        if (self.root / ".git").is_dir():
            self._ensure_remote(repo_url)
            self._git("fetch", "--depth", "1", "origin", "main")
            self._git("reset", "--hard", "origin/main")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(self.root)],
                capture_output=True,
                text=True,
                timeout=60,
            )

    def _ensure_remote(self, repo_url: str) -> None:
        """Make sure 'origin' points to the configured repo URL (cached after first check)."""
        if self._remote_verified:
            return
        result = self._git("remote", "get-url", "origin")
        if result.returncode != 0:
            self._git("remote", "add", "origin", repo_url)
        elif result.stdout.strip() != repo_url:
            self._git("remote", "set-url", "origin", repo_url)
        self._remote_verified = True

    def _commit_and_push(self, path: Path, message: str) -> dict[str, Any]:
        """Stage a file, commit, and mark for batched push. Returns git status info.

        Commit runs in a background thread so the tool response is not
        blocked. Push happens on the periodic timer (every 60s).
        """
        return self._commit_and_push_batch([path], message)

    def _commit_and_push_batch(self, paths: list[Path], message: str) -> dict[str, Any]:
        """Stage multiple files, commit once, and mark for batched push.

        Commit runs in a background thread with a lock to serialise git
        operations.  Push is deferred to the periodic push timer, which
        batches all pending commits into a single push.
        """
        info: dict[str, Any] = {"git": "skipped"}

        if not self._repo_url:
            return info

        if not paths:
            return info

        rels = [str(p.relative_to(self.root)) for p in paths]

        def _bg_commit() -> None:
            try:
                with self._git_lock:
                    # Stage all files
                    result = self._git("add", *rels)
                    if result.returncode != 0:
                        log.warning("git add failed: %s", result.stderr.strip())
                        return

                    # Commit
                    result = self._git("commit", "-m", message)
                    if result.returncode != 0:
                        combined = (result.stdout + result.stderr).lower()
                        if "nothing to commit" in combined or "nothing added to commit" in combined:
                            log.debug("nothing to commit")
                            return
                        log.warning("git commit failed: %s", result.stderr.strip() or result.stdout.strip())
                        return

                    self._has_unpushed = True
                    log.info("committed %d files, push pending", len(rels))
            except Exception:
                log.exception("background commit error")

        threading.Thread(target=_bg_commit, daemon=True).start()
        info["git"] = "committed_push_pending"
        info["files_staged"] = len(paths)
        return info

    def _push_timer_loop(self) -> None:
        """Periodically push unpushed commits to origin.

        Runs as a daemon thread.  Wakes every ``_push_interval`` seconds
        (default 60) and, if there are unpushed commits, does a single
        ``git push origin main``.
        """
        while not self._push_stop.wait(self._push_interval):
            self._try_push()

    def _try_push(self) -> None:
        """Push to remote if there are unpushed commits."""
        if not self._has_unpushed or not self._repo_url:
            return
        with self._git_lock:
            if not self._has_unpushed:
                return  # another thread pushed while we waited for the lock
            try:
                self._ensure_remote(self._repo_url)
                result = self._git("push", "origin", "main")
                if result.returncode != 0:
                    log.warning("batched push failed: %s", result.stderr.strip())
                else:
                    self._has_unpushed = False
                    log.info("batched push succeeded")
            except Exception:
                log.exception("batched push error")

    def _shutdown_push(self) -> None:
        """Attempt a final push on interpreter shutdown."""
        self._push_stop.set()
        self._try_push()

    # -- brief.yml (L0) ----------------------------------------------------

    def read_brief(self) -> dict[str, Any]:
        """Return the parsed contents of brief.yml with task-dispatch hints.

        Enriches the raw brief with 'next_steps' — suggested tool calls
        based on the current focus area and active project.
        """
        brief_path = self.root / "brief.yml"
        if not brief_path.exists():
            raise FileNotFoundError("brief.yml not found in vault root")
        brief = yaml.safe_load(brief_path.read_text(encoding="utf-8")) or {}

        # Strategy 6: Generate context-aware next steps from focus area
        focus = brief.get("focus", "")
        active_project = brief.get("active-project", "")
        next_steps: list[str] = []

        if focus:
            next_steps.append(
                f'search(text="{focus}") — find vault entities related to current focus'
            )

        if active_project:
            next_steps.append(
                f'query(project="{active_project}", entity_type="system") — load component tree'
            )
            next_steps.append(
                f'query(project="{active_project}", entity_type="goal") — load project goals'
            )

        # Always suggest checking goals
        goals = brief.get("goals", {})
        for goal_id in goals:
            next_steps.append(
                f'get_context("{goal_id}") — load goal details and linked entities'
            )

        brief["next_steps"] = next_steps
        return brief

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

    # -- types.yml registry ------------------------------------------------

    def read_types(self) -> dict[str, Any]:
        """Return the parsed type registry from types.yml.

        Each key is a type name (e.g. 'goal') with description, icon, and
        a count of entities across all locations (root + project dirs).
        """
        types_path = self.root / "types.yml"
        if not types_path.exists():
            raise FileNotFoundError("types.yml not found in vault root")
        registry = yaml.safe_load(types_path.read_text(encoding="utf-8")) or {}

        # Count entities from _iter_entity_files for accuracy
        counts: dict[str, int] = {}
        for type_name, _ in self._iter_entity_files():
            counts[type_name] = counts.get(type_name, 0) + 1

        for type_name, meta in registry.items():
            meta["count"] = counts.get(type_name, 0)
        return registry

    # -- type folders & entity files ----------------------------------------

    def _project_dirs(self) -> dict[str, Path]:
        """Return {project_key: directory_path} from brief.yml 'dir' fields."""
        try:
            brief_path = self.root / "brief.yml"
            if not brief_path.exists():
                return {}
            brief = yaml.safe_load(brief_path.read_text(encoding="utf-8")) or {}
            projects = brief.get("projects", {})
            result: dict[str, Path] = {}
            for proj_key, proj_meta in projects.items():
                dir_name = proj_meta.get("dir")
                if dir_name:
                    result[proj_key] = self.root / dir_name
            return result
        except Exception:
            return {}

    def _project_dir_for(self, project: str | None) -> Path | None:
        """Return the directory path for a given project key, or None."""
        if not project:
            return None
        dirs = self._project_dirs()
        return dirs.get(project)

    def _type_folders(self) -> list[str]:
        """Return the names of all type-based subfolders at the vault root."""
        return [
            d.name
            for d in self.root.iterdir()
            if d.is_dir() and d.name not in _IGNORED_ROOTS and not d.name.startswith(".")
        ]

    def _iter_entity_files(
        self, *, include_archived: bool = False
    ) -> list[tuple[str, Path]]:
        """Return (type_name, path) for every .md entity file in the vault.

        Scans three locations:
        1. Root-level type folders: ``{vault_root}/{type}/*.md``
        2. Project type subfolders: ``{vault_root}/{project_dir}/{type}/*.md``
        3. Project root files: ``{vault_root}/{project_dir}/*.md`` (project entities)

        When *include_archived* is True, also yields entities from ``.trash/``.
        """
        results: list[tuple[str, Path]] = []
        project_dir_names = set()
        proj_dirs = self._project_dirs()
        for proj_path in proj_dirs.values():
            if proj_path.is_dir():
                project_dir_names.add(proj_path.name)

        # 1. Root-level type folders (skip project dirs and ignored roots)
        for d in sorted(self.root.iterdir()):
            if (
                d.is_dir()
                and d.name not in _IGNORED_ROOTS
                and d.name not in project_dir_names
                and not d.name.startswith(".")
            ):
                for p in sorted(d.iterdir()):
                    if p.is_file() and p.suffix == ".md" and p.name != "_index.md":
                        results.append((d.name, p))

        # 2 & 3. Project directories
        for proj_key, proj_path in sorted(proj_dirs.items()):
            if not proj_path.is_dir():
                continue
            # 3. Project root .md files (type: project)
            for p in sorted(proj_path.iterdir()):
                if p.is_file() and p.suffix == ".md":
                    results.append(("project", p))
            # 2. Project type subfolders
            for sub in sorted(proj_path.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    type_name = sub.name
                    for p in sorted(sub.iterdir()):
                        if p.is_file() and p.suffix == ".md" and p.name != "_index.md":
                            results.append((type_name, p))

        if include_archived:
            trash_dir = self.root / ".trash"
            if trash_dir.is_dir():
                self._iter_trash_files(trash_dir, results)
        return results

    @staticmethod
    def _iter_trash_files(
        trash_dir: Path, results: list[tuple[str, Path]]
    ) -> None:
        """Append archived entity files from .trash/ to results.

        Handles both flat ``.trash/{type}/`` and nested
        ``.trash/{project_dir}/{type}/`` structures.
        """
        for item in sorted(trash_dir.iterdir()):
            if not item.is_dir():
                continue
            # Check if this is a type folder (.trash/concept/) or a project
            # folder (.trash/sovereign/concept/)
            has_md_files = any(
                p.is_file() and p.suffix == ".md" for p in item.iterdir()
            )
            if has_md_files:
                # Flat: .trash/{type}/*.md
                for p in sorted(item.iterdir()):
                    if p.is_file() and p.suffix == ".md":
                        results.append((item.name, p))
            else:
                # Nested: .trash/{project_dir}/{type}/*.md
                for type_dir in sorted(item.iterdir()):
                    if type_dir.is_dir():
                        for p in sorted(type_dir.iterdir()):
                            if p.is_file() and p.suffix == ".md":
                                results.append((type_dir.name, p))
        return results

    # -- vault stats -------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return vault health and growth metrics.

        Provides entity counts by type and project, link density,
        and staleness indicators. Useful for measuring vault growth
        over time and identifying areas that need attention.

        Returns a dict with:
        - total_entities: overall count
        - by_type: {type: count}
        - by_project: {project: count}
        - by_type_and_project: {project: {type: count}}
        - link_density: average outgoing wikilinks per entity
        - entities_without_links: count of entities with no [[links]]
        - schema_compliance: count of entities with orphan sections
        """
        entities = self._iter_entity_files()
        schema = self._load_body_schema()

        by_type: dict[str, int] = {}
        by_project: dict[str, int] = {}
        by_type_and_project: dict[str, dict[str, int]] = {}
        total_links = 0
        no_links_count = 0
        orphan_section_count = 0
        total = 0

        for type_name, path in entities:
            total += 1
            by_type[type_name] = by_type.get(type_name, 0) + 1

            try:
                text = path.read_text(encoding="utf-8")
                fm, body = _parse_frontmatter(text)
            except Exception:
                continue

            # Project attribution
            projects = fm.get("project", [])
            if isinstance(projects, str):
                projects = [projects]
            for proj in projects:
                by_project[proj] = by_project.get(proj, 0) + 1
                if proj not in by_type_and_project:
                    by_type_and_project[proj] = {}
                p_types = by_type_and_project[proj]
                p_types[type_name] = p_types.get(type_name, 0) + 1

            # Link density
            links = extract_wikilinks(text)
            total_links += len(links)
            if not links:
                no_links_count += 1

            # Schema compliance: check for orphan sections
            etype = fm.get("type", ["concept"])
            if isinstance(etype, list):
                etype = etype[0]
            allowed_fields = schema.get("types", {}).get(etype, [])
            allowed_headings: set[str] = set()
            for f in allowed_fields:
                h = self._heading_for_field(f, schema)
                if h is not None:
                    allowed_headings.add(h)

            for m in re.finditer(r"^## (.+?)\s*$", body, re.MULTILINE):
                heading = m.group(1).strip()
                if heading not in allowed_headings:
                    orphan_section_count += 1
                    break  # count entity once, not per orphan

        avg_links = round(total_links / total, 1) if total > 0 else 0

        return {
            "total_entities": total,
            "by_type": dict(sorted(by_type.items())),
            "by_project": dict(sorted(by_project.items())),
            "by_type_and_project": {
                k: dict(sorted(v.items()))
                for k, v in sorted(by_type_and_project.items())
            },
            "link_density": {
                "average_links_per_entity": avg_links,
                "entities_without_links": no_links_count,
            },
            "schema_compliance": {
                "entities_with_orphan_sections": orphan_section_count,
            },
        }

    def _resolve_entity_path(
        self, identifier: str, *, include_archived: bool = False
    ) -> Path | None:
        """Resolve an entity by filename stem (e.g. 'Storage Layer'),
        by type/filename path (e.g. 'system/Storage Layer'),
        by frontmatter id (e.g. 'S-1'), or by guid.

        When *include_archived* is True, also searches ``.trash/``.
        """
        # Direct path match (e.g. "system/Storage Layer" or "system/Storage Layer.md")
        for ext in ("", ".md"):
            p = self.root / f"{identifier}{ext}"
            if p.exists() and p.is_file():
                return p

        # Match by filename stem across all type folders
        for _, path in self._iter_entity_files(include_archived=include_archived):
            if path.stem == identifier:
                return path

        # Match by frontmatter id or guid
        for _, path in self._iter_entity_files(include_archived=include_archived):
            text = path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)
            if fm.get("id") == identifier or fm.get("guid") == identifier:
                return path

        return None

    def read_entity(
        self, identifier: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        """Read an entity file and return {guid, id, frontmatter, body, links, path}."""
        path = self._resolve_entity_path(
            identifier, include_archived=include_archived
        )
        if path is None:
            raise FileNotFoundError(f"Entity '{identifier}' not found in vault")

        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        links = extract_wikilinks(text)

        return {
            "guid": fm.get("guid"),
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
            "guid": entity["guid"],
            "id": entity["id"],
            "title": fm.get("title", entity["id"]),
            "status": fm.get("status", "unknown"),
        }

    def get_context(
        self, entity_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        """Return the entity + one-level-deep synopses of linked entities."""
        entity = self.read_entity(entity_id, include_archived=include_archived)

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
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Scan frontmatter across all concept files. Return matching synopses.

        Results are sorted by project relevance: active project first,
        then universal (no project field), then background.
        Active/universal entities get full synopses; background entities
        get minimal one-liners.
        """
        active = project or self._active_project()
        matches: list[dict[str, Any]] = []

        for folder_name, path in self._iter_entity_files(
            include_archived=include_archived
        ):
            text = path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)

            # Filter by entity_type (matches folder name)
            if entity_type:
                if entity_type.lower() != folder_name.lower():
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
            is_archived = ".trash" in path.parts

            entry: dict[str, Any] = {
                "guid": fm.get("guid"),
                "id": entity_id,
                "title": fm.get("title", entity_id),
                "relevance": relevance,
            }
            if is_archived:
                entry["archived"] = True
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

        # Auto-commit and push
        field_names = ", ".join(fields.keys())
        git_info = self._commit_and_push(
            path, f"update {entity_id}: {field_names}"
        )

        return {
            "updated": entity_id,
            "fields": list(fields.keys()),
            "warnings": warnings,
            **git_info,
        }

    # -- body editing ------------------------------------------------------

    _SECTION_RE = re.compile(r"^## ", re.MULTILINE)

    def update_body(
        self,
        entity_id: str,
        field: str,
        content: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Set or delete a named field in an entity's body.

        The body schema (``body-schema.yml``) defines what fields are valid
        for each entity type, their canonical ordering, and how they map
        to ``## Heading`` sections in the markdown document.

        Args:
            entity_id: Entity identifier (slug, path, frontmatter ID, or GUID).
            field: Semantic field name from the body schema (e.g. ``"intent"``,
                ``"preamble"``, ``"related"``).  The server maps this to the
                correct ``## Heading`` in the document.
            content: Field content.  For most fields this is a markdown string.
                For ``"related"`` this is a **list of entity names** (no ``[[]]``
                wrapping needed — the server formats them).
                Pass ``None`` or omit to **delete** the field.

        Returns:
            Confirmation dict with ``updated``, ``field``, ``action``
            (``created`` | ``replaced`` | ``deleted`` | ``not_found``),
            ``warnings``, and git info.

        Raises:
            FileNotFoundError: If entity_id doesn't resolve.
            ValueError: If the field is not in the type's canonical schema
                (only enforced for set operations, not deletes).
        """
        path = self._resolve_entity_path(entity_id)
        if path is None:
            raise FileNotFoundError(f"Entity {entity_id} not found in vault")

        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        # Load schema and determine delete intent early
        schema = self._load_body_schema()
        entity_type = fm.get("type", ["concept"])
        if isinstance(entity_type, list):
            entity_type = entity_type[0]
        allowed_fields = schema.get("types", {}).get(entity_type, [])

        is_delete = content is None or (
            isinstance(content, str) and content.strip() == ""
        ) or (isinstance(content, list) and len(content) == 0)

        # Strict validation only for set operations — deletes can target
        # any existing heading (needed for cleaning up orphan sections)
        if not is_delete and field not in allowed_fields:
            raise ValueError(
                f"Field '{field}' is not valid for entity type '{entity_type}'. "
                f"Allowed fields: {', '.join(allowed_fields)}"
            )

        heading = self._heading_for_field(field, schema)
        field_def = schema.get("fields", {}).get(field, {})
        is_preamble = heading is None  # position: first
        is_wikilinks = field_def.get("format") == "wikilinks"

        # Format content for wikilink fields (e.g. related)
        if is_wikilinks and not is_delete:
            if not isinstance(content, list):
                raise ValueError(
                    f"Field '{field}' requires a list of entity names, "
                    f"got {type(content).__name__}"
                )
            formatted = "\n".join(
                f"- [[{name}]]" for name in content
            )
        elif not is_delete:
            formatted = content.rstrip() if isinstance(content, str) else str(content)

        if is_preamble:
            action = self._update_preamble(body, formatted if not is_delete else None)
            body = action.pop("body")
        elif is_delete:
            action = self._delete_section(body, heading)
            body = action.pop("body")
        else:
            action = self._upsert_section(
                body, heading, formatted, field, allowed_fields, schema
            )
            body = action.pop("body")

        new_text = _serialize_frontmatter(fm, body)
        warnings = self._validate_links(new_text)

        path.write_text(new_text, encoding="utf-8")

        git_info = self._commit_and_push(
            path, f"update body: {entity_id} [{field}]"
        )

        return {
            "updated": entity_id,
            "field": field,
            "action": action["action"],
            "warnings": warnings,
            **git_info,
        }

    # -- body editing helpers ----------------------------------------------

    def _update_preamble(
        self, body: str, content: str | None
    ) -> dict[str, Any]:
        """Set or delete the preamble (text between # Title and first ##)."""
        h1_match = re.search(r"^# .+\n", body, re.MULTILINE)
        first_h2 = self._SECTION_RE.search(body)

        start = h1_match.end() if h1_match else 0
        end = first_h2.start() if first_h2 else len(body)
        existing = body[start:end].strip()

        if content is None:
            # Delete preamble
            body = body[:start] + "\n" + body[end:]
            return {"body": body, "action": "deleted" if existing else "not_found"}

        preamble_text = f"\n{content}\n\n"
        action = "replaced" if existing else "created"
        body = body[:start] + preamble_text + body[end:]
        return {"body": body, "action": action}

    def _delete_section(
        self, body: str, heading: str
    ) -> dict[str, Any]:
        """Delete a ## section entirely (heading + content)."""
        pattern = re.compile(
            rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(body)
        if not match:
            return {"body": body, "action": "not_found"}
        body = body[: match.start()] + body[match.end():]
        return {"body": body, "action": "deleted"}

    def _upsert_section(
        self,
        body: str,
        heading: str,
        content: str,
        field: str,
        allowed_fields: list[str],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Create or replace a ## section at the correct canonical position."""
        section_text = f"## {heading}\n\n{content}\n"

        # Check if section already exists
        pattern = re.compile(
            rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(body)

        if match:
            body = body[: match.start()] + section_text + "\n" + body[match.end():]
            return {"body": body, "action": "replaced"}

        # New section — find the canonical insertion point
        insert_pos = self._find_insertion_point(
            body, field, allowed_fields, schema
        )
        body = body[:insert_pos] + section_text + "\n" + body[insert_pos:]
        return {"body": body, "action": "created"}

    def _find_insertion_point(
        self,
        body: str,
        field: str,
        allowed_fields: list[str],
        schema: dict[str, Any],
    ) -> int:
        """Find where to insert a new section to maintain canonical order.

        Scans the body for existing sections and finds the position after
        the last section that should come before this field in the schema
        order.
        """
        field_idx = allowed_fields.index(field)

        # Build heading→position map for existing sections in the body
        section_positions: list[tuple[int, int, str]] = []  # (order, pos, heading)
        for m in re.finditer(r"^## (.+?)\s*$", body, re.MULTILINE):
            h = m.group(1)
            f = self._field_for_heading(h, schema)
            if f in allowed_fields:
                order = allowed_fields.index(f)
            else:
                # Unknown field — put it at end-1 (before related)
                order = len(allowed_fields) - 1
            section_positions.append((order, m.start(), h))

        # Find the last existing section that should appear before this field
        best_pos = None
        for order, pos, _h in section_positions:
            if order < field_idx:
                # This section comes before our field — insert after it
                # Find the end of this section
                pat = re.compile(
                    rf"^## {re.escape(_h)}\s*\n(.*?)(?=^## |\Z)",
                    re.MULTILINE | re.DOTALL,
                )
                sm = pat.search(body, pos)
                if sm:
                    best_pos = sm.end()

        if best_pos is not None:
            return best_pos

        # Find the first existing section that should appear after this field
        for order, pos, _h in sorted(section_positions, key=lambda x: x[0]):
            if order > field_idx:
                return pos

        # No reference points — append at end
        return len(body.rstrip()) + 1

    # -- body cleanup ------------------------------------------------------

    def clean_body(self, entity_id: str) -> dict[str, Any]:
        """Remove non-schema sections from an entity's body.

        Reads the entity, identifies all ``## Heading`` sections, removes
        any that don't map to an allowed field in the entity type's body
        schema.  Preserves preamble, H1 title, and all schema-defined
        sections in canonical order.

        Args:
            entity_id: Entity identifier (slug, path, frontmatter ID, or GUID).

        Returns:
            Confirmation with list of removed headings and git info.
        """
        path = self._resolve_entity_path(entity_id)
        if path is None:
            raise FileNotFoundError(f"Entity {entity_id} not found in vault")

        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        schema = self._load_body_schema()
        entity_type = fm.get("type", ["concept"])
        if isinstance(entity_type, list):
            entity_type = entity_type[0]
        allowed_fields = schema.get("types", {}).get(entity_type, [])

        # Build set of allowed headings from schema
        allowed_headings: set[str] = set()
        for f in allowed_fields:
            h = self._heading_for_field(f, schema)
            if h is not None:  # skip preamble
                allowed_headings.add(h)

        # Find all ## sections and identify orphans
        removed: list[str] = []
        section_pattern = re.compile(
            r"^(## (.+?)\s*\n(.*?))(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )

        # Work backwards to preserve positions
        matches = list(section_pattern.finditer(body))
        for m in reversed(matches):
            heading = m.group(2).strip()
            if heading not in allowed_headings:
                body = body[: m.start()] + body[m.end():]
                removed.append(heading)

        if not removed:
            return {
                "entity": entity_id,
                "action": "no_change",
                "removed": [],
            }

        # Clean up excessive blank lines
        body = re.sub(r"\n{3,}", "\n\n", body)

        new_text = _serialize_frontmatter(fm, body)
        warnings = self._validate_links(new_text)
        path.write_text(new_text, encoding="utf-8")

        git_info = self._commit_and_push(
            path, f"clean body: {entity_id} (removed {len(removed)} orphan sections)"
        )

        removed.reverse()  # restore original order
        return {
            "entity": entity_id,
            "action": "cleaned",
            "removed": removed,
            "warnings": warnings,
            **git_info,
        }

    # -- archival ----------------------------------------------------------

    def archive_entity(self, entity_id: str) -> dict[str, Any]:
        """Move an entity to .trash/ preserving its directory structure.

        Archived entities are excluded from search, query, and validate_action
        because _iter_entity_files skips dot-prefixed directories.

        For root-level entities: ``.trash/{type}/{file}.md``
        For project entities: ``.trash/{project_dir}/{type}/{file}.md``

        Args:
            entity_id: Entity identifier (slug, path, frontmatter ID, or GUID).

        Returns confirmation with original path, archive path, incoming links,
        and git info.
        """
        path = self._resolve_entity_path(entity_id)
        if path is None:
            raise FileNotFoundError(f"Entity {entity_id} not found in vault")

        # Preserve relative directory structure under .trash/
        rel_path = path.relative_to(self.root)
        trash_dest = self.root / ".trash" / rel_path
        trash_dest.parent.mkdir(parents=True, exist_ok=True)

        if trash_dest.exists():
            raise FileExistsError(
                f"Archive destination already exists: .trash/{rel_path}"
            )

        # Find incoming links (other entities that reference this one)
        entity_stem = path.stem
        incoming: list[str] = []
        for _, other_path in self._iter_entity_files():
            if other_path == path:
                continue
            text = other_path.read_text(encoding="utf-8")
            if f"[[{entity_stem}]]" in text or f"[[{entity_stem}|" in text:
                incoming.append(str(other_path.relative_to(self.root)))

        # Move the file
        import shutil
        shutil.move(str(path), str(trash_dest))

        # Git: stage the deletion and the new file
        rel_old = str(path.relative_to(self.root))
        rel_new = str(trash_dest.relative_to(self.root))
        git_info = self._archive_commit_and_push(
            rel_old, rel_new, f"archive {entity_id}"
        )

        return {
            "archived": entity_id,
            "from": rel_old,
            "to": rel_new,
            "incoming_links": incoming,
            "incoming_link_count": len(incoming),
            **git_info,
        }

    def _archive_commit_and_push(
        self, old_rel: str, new_rel: str, message: str
    ) -> dict[str, Any]:
        """Stage a file move (rm old + add new), commit, and mark for batched push."""
        info: dict[str, Any] = {"git": "skipped"}

        if not self._repo_url:
            return info

        def _bg() -> None:
            try:
                with self._git_lock:
                    # Stage the removal and the addition
                    result = self._git("rm", "--cached", old_rel)
                    if result.returncode != 0:
                        # File may already be untracked; try plain add
                        log.debug("git rm --cached failed, continuing: %s", result.stderr.strip())

                    result = self._git("add", new_rel)
                    if result.returncode != 0:
                        log.warning("git add failed: %s", result.stderr.strip())
                        return

                    # Also stage the removal from the index
                    result = self._git("add", "-A", old_rel)

                    result = self._git("commit", "-m", message)
                    if result.returncode != 0:
                        combined = (result.stdout + result.stderr).lower()
                        if "nothing to commit" in combined:
                            return
                        log.warning("git commit failed: %s", result.stderr.strip() or result.stdout.strip())
                        return

                    self._has_unpushed = True
                    log.info("committed archive, push pending")
            except Exception:
                log.exception("background archive commit error")

        threading.Thread(target=_bg, daemon=True).start()
        info["git"] = "committed_push_pending"
        return info

    # -- link checking -----------------------------------------------------

    def _resolve_link(self, target: str) -> bool:
        """Check whether a wiki-link target resolves to an existing file."""
        # Strip Obsidian pipe alias: [[target|display text]]
        target = target.split("|", 1)[0].strip()
        # Direct path match (e.g. "system/Storage Layer")
        for ext in ("", ".md"):
            if (self.root / f"{target}{ext}").exists():
                return True
        # Obsidian-style: match by filename across all type folders
        for _, path in self._iter_entity_files():
            if path.stem == target:
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

        # Collect all files to check: entity files + brief.yml
        files_to_check: list[Path] = [path for _, path in self._iter_entity_files()]
        brief = self.root / "brief.yml"
        if brief.exists():
            files_to_check.append(brief)

        for path in files_to_check:
            text = path.read_text(encoding="utf-8")
            for link in extract_wikilinks(text):
                if not self._resolve_link(link):
                    broken.append({
                        "source": str(path.relative_to(self.root)),
                        "target": link,
                    })

        return broken

    def check_consistency(self) -> dict[str, Any]:
        """Check the vault for structural inconsistencies.

        Runs multiple checks beyond broken links:
        - Missing required frontmatter fields per types.yml
        - Invalid tags not in the controlled vocabulary
        - Broken serves/depends-on frontmatter references
        - Duplicated entity titles (potential merge candidates)

        Returns a report dict with issues grouped by check type.
        """
        types_config = self._load_types_config()
        valid_tags = self._allowed_tags()

        missing_fields: list[dict[str, Any]] = []
        invalid_tags: list[dict[str, Any]] = []
        broken_refs: list[dict[str, Any]] = []
        title_map: dict[str, list[str]] = {}  # normalized title → [entity paths]

        for type_name, path in self._iter_entity_files():
            rel = str(path.relative_to(self.root))
            try:
                text = path.read_text(encoding="utf-8")
                fm, _ = _parse_frontmatter(text)
            except Exception:
                continue

            # --- Missing required fields ---
            etype = fm.get("type", [type_name])
            if isinstance(etype, list):
                etype_str = etype[0] if etype else type_name
            else:
                etype_str = etype
            type_def = types_config.get(etype_str, {})
            required = type_def.get("required-fields", [])
            for field in required:
                if field not in fm or fm[field] is None:
                    missing_fields.append({
                        "entity": rel,
                        "field": field,
                        "type": etype_str,
                    })

            # --- Invalid tags ---
            entity_tags = fm.get("tags", [])
            if isinstance(entity_tags, str):
                entity_tags = [entity_tags]
            for tag in entity_tags:
                if tag not in valid_tags:
                    invalid_tags.append({
                        "entity": rel,
                        "tag": tag,
                    })

            # --- Broken serves/depends-on references ---
            for ref_field in ("serves", "depends-on"):
                refs = fm.get(ref_field, [])
                if isinstance(refs, str):
                    refs = [refs]
                for ref in refs:
                    # Extract entity name from [[Name]] format
                    link_match = re.match(r"\[\[(.+?)]]", ref)
                    target = link_match.group(1) if link_match else ref
                    if target and not self._resolve_link(target):
                        broken_refs.append({
                            "entity": rel,
                            "field": ref_field,
                            "target": target,
                        })

            # --- Duplicate title detection ---
            title = fm.get("title", path.stem)
            normalized = title.lower().strip()
            if normalized not in title_map:
                title_map[normalized] = []
            title_map[normalized].append(rel)

        duplicates = [
            {"title": title, "entities": paths}
            for title, paths in title_map.items()
            if len(paths) > 1
        ]

        total_issues = (
            len(missing_fields) + len(invalid_tags)
            + len(broken_refs) + len(duplicates)
        )

        return {
            "total_issues": total_issues,
            "missing_required_fields": missing_fields,
            "invalid_tags": invalid_tags,
            "broken_frontmatter_refs": broken_refs,
            "duplicate_titles": duplicates,
        }

    def _load_types_config(self) -> dict[str, Any]:
        """Load types.yml and return the type definitions dict."""
        types_path = self.root / "types.yml"
        if not types_path.exists():
            return {}
        return yaml.safe_load(types_path.read_text(encoding="utf-8")) or {}

    # -- full-text search --------------------------------------------------

    def search(
        self,
        text: str,
        *,
        entity_type: str | None = None,
        tag: str | None = None,
        max_results: int = 10,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Search entity titles and body text for keywords.

        Splits the query into words (3+ chars) and scores each entity by
        how many query words appear in its title and body. Title matches
        are weighted higher than body matches.

        Returns up to *max_results* entries sorted by score descending,
        each with title, path, type, score, tags, and a context snippet.
        """
        if not text or not text.strip():
            return []

        # Tokenize query into significant words (3+ chars, lowercased)
        query_words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]{3,}", text)]
        if not query_words:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []

        for folder_name, path in self._iter_entity_files(
            include_archived=include_archived
        ):
            # Apply entity_type filter early
            if entity_type and entity_type.lower() != folder_name.lower():
                continue

            file_text = path.read_text(encoding="utf-8")
            fm, body = _parse_frontmatter(file_text)

            # Apply tag filter early
            if tag:
                file_tags = [t.lower() for t in fm.get("tags", [])]
                if tag.lower() not in file_tags:
                    continue

            title = fm.get("title", path.stem)
            title_lower = title.lower()
            body_lower = body.lower()

            # Score: title matches worth 2x, body matches worth 1x
            score = 0.0
            matched_words: list[str] = []
            for word in query_words:
                if word in title_lower:
                    score += 2.0
                    matched_words.append(word)
                elif word in body_lower:
                    score += 1.0
                    matched_words.append(word)

            if score == 0:
                continue

            # Bonus for matching multiple distinct words (breadth)
            unique_matches = len(set(matched_words))
            if unique_matches > 1:
                score += 0.5 * (unique_matches - 1)

            # Extract a context snippet from body around first match
            snippet = ""
            for word in matched_words:
                idx = body_lower.find(word)
                if idx >= 0:
                    start = max(0, idx - 60)
                    end = min(len(body), idx + len(word) + 60)
                    raw = body[start:end].strip()
                    # Clean up to nearest word boundaries
                    if start > 0:
                        raw = "..." + raw[raw.find(" ") + 1:] if " " in raw else "..." + raw
                    if end < len(body):
                        last_space = raw.rfind(" ")
                        raw = raw[:last_space] + "..." if last_space > 0 else raw + "..."
                    snippet = raw
                    break

            result_entry: dict[str, Any] = {
                "title": title,
                "path": str(path.relative_to(self.root)),
                "type": folder_name,
                "tags": fm.get("tags", []),
                "score": round(score, 2),
                "matched_words": list(set(matched_words)),
                "snippet": snippet,
            }
            if ".trash" in path.parts:
                result_entry["archived"] = True
            # Findings are unvetted working memory — weight lower than
            # established entities so vetted knowledge ranks first.
            if folder_name == "findings":
                score *= 0.65
                result_entry["score"] = round(score, 2)
                result_entry["weighted"] = True
            scored.append((score, result_entry))

        # Sort by score descending, then by title alphabetically
        scored.sort(key=lambda x: (-x[0], x[1]["title"]))
        return [entry for _, entry in scored[:max_results]]

    # -- tags.yml registry -------------------------------------------------

    def read_tags(self) -> dict[str, list[dict[str, str]]]:
        """Return the controlled tag vocabulary from tags.yml.

        Returns {category: [{tag, description}, ...]}
        """
        tags_path = self.root / "tags.yml"
        if not tags_path.exists():
            raise FileNotFoundError("tags.yml not found in vault root")
        return yaml.safe_load(tags_path.read_text(encoding="utf-8")) or {}

    def _allowed_tags(self) -> set[str]:
        """Return the flat set of all allowed tag names."""
        allowed: set[str] = set()
        for entries in self.read_tags().values():
            for entry in entries:
                allowed.add(entry["tag"])
        return allowed

    # -- extraction schema -------------------------------------------------

    def read_extraction_schema(self) -> dict[str, Any]:
        """Return the extraction schema from extraction-schema.yml."""
        path = self.root / "extraction-schema.yml"
        if not path.exists():
            raise FileNotFoundError("extraction-schema.yml not found in vault root")
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # -- body schema -------------------------------------------------------

    def _load_body_schema(self) -> dict[str, Any]:
        """Return the parsed body schema from body-schema.yml.

        Returns {"fields": {...}, "types": {...}} defining canonical
        section structure per entity type.
        """
        path = self.root / "body-schema.yml"
        if not path.exists():
            raise FileNotFoundError("body-schema.yml not found in vault root")
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def _heading_for_field(
        self, field: str, schema: dict[str, Any]
    ) -> str | None:
        """Return the ``## Heading`` text for a field, or None for preamble."""
        field_def = schema.get("fields", {}).get(field, {})
        if field_def.get("position") == "first":
            return None  # preamble — no heading
        return field_def.get("heading", field.replace("_", " ").title())

    def _field_for_heading(
        self, heading: str, schema: dict[str, Any]
    ) -> str:
        """Return the field name for a ``## Heading``, or a slug fallback."""
        for fname, fdef in schema.get("fields", {}).items():
            if fdef.get("heading") == heading:
                return fname
        # Fallback: slugify the heading
        return heading.lower().replace(" ", "_").replace("-", "_")

    # -- match_concepts ----------------------------------------------------

    def _build_entity_index(self) -> list[dict[str, Any]]:
        """Build a lightweight index of all entities for matching.

        Returns [{title, tags, path, type_folder, first_sentence}, ...]
        """
        index: list[dict[str, Any]] = []
        for folder_name, path in self._iter_entity_files():
            text = path.read_text(encoding="utf-8")
            fm, body = _parse_frontmatter(text)
            # First non-empty, non-heading line as a content fingerprint
            first_sentence = ""
            for line in body.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    first_sentence = line[:200]
                    break
            index.append({
                "title": fm.get("title", path.stem),
                "name": path.stem,
                "tags": [t.lower() for t in fm.get("tags", [])],
                "type_folder": folder_name,
                "path": str(path.relative_to(self.root)),
                "status": fm.get("status", "unknown"),
                "first_sentence": first_sentence,
            })
        return index

    @staticmethod
    def _title_normalize(title: str) -> str:
        """Lowercase, strip non-alphanumeric for fuzzy comparison."""
        return re.sub(r"[^a-z0-9]", "", title.lower())

    def match_concepts(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Match concept candidates against existing vault entities.

        Each candidate should have at minimum: {title, tags}.
        Optional: {type, claims, relationships}.

        Returns candidates enriched with match info:
        - disposition: 'new' | 'merge' | 'ambiguous'
        - matched_entity: the existing entity path (if merge/ambiguous)
        - match_reasons: why it matched
        - tag_warnings: any tags not in the controlled vocabulary
        """
        entity_index = self._build_entity_index()
        allowed_tags = self._allowed_tags()
        results: list[dict[str, Any]] = []

        for candidate in candidates:
            c_title = candidate.get("title", "")
            c_tags = {t.lower() for t in candidate.get("tags", [])}
            c_norm = self._title_normalize(c_title)

            # Validate tags against controlled vocabulary
            tag_warnings = [t for t in c_tags if t not in allowed_tags]

            best_match: dict[str, Any] | None = None
            best_score = 0.0
            match_reasons: list[str] = []

            for entity in entity_index:
                score = 0.0
                reasons: list[str] = []
                e_norm = self._title_normalize(entity["title"])
                e_name_norm = self._title_normalize(entity["name"])

                # Exact title match (strongest signal)
                if c_norm == e_norm or c_norm == e_name_norm:
                    score += 1.0
                    reasons.append("exact_title")
                # Substring containment (one contains the other)
                elif c_norm in e_norm or e_norm in c_norm:
                    score += 0.6
                    reasons.append("title_substring")
                # Shared significant words (3+ char words)
                else:
                    c_words = {w for w in re.findall(r"[a-z]{3,}", c_norm)}
                    e_words = {w for w in re.findall(r"[a-z]{3,}", e_norm)}
                    if c_words and e_words:
                        overlap = len(c_words & e_words) / len(c_words | e_words)
                        if overlap >= 0.4:
                            score += 0.3 * overlap
                            reasons.append(f"word_overlap({overlap:.0%})")

                # Tag overlap
                e_tags = set(entity["tags"])
                if c_tags and e_tags:
                    tag_overlap = len(c_tags & e_tags) / len(c_tags | e_tags)
                    if tag_overlap > 0:
                        score += 0.3 * tag_overlap
                        reasons.append(f"tag_overlap({tag_overlap:.0%})")

                if score > best_score:
                    best_score = score
                    best_match = entity
                    match_reasons = reasons

            # Determine disposition
            if best_score >= 0.8:
                disposition = "merge"
            elif best_score >= 0.4:
                disposition = "ambiguous"
            else:
                disposition = "new"

            result: dict[str, Any] = {
                **candidate,
                "disposition": disposition,
                "match_score": round(best_score, 3),
                "match_reasons": match_reasons if best_match else [],
            }
            if tag_warnings:
                result["tag_warnings"] = tag_warnings
            if best_match and disposition != "new":
                result["matched_entity"] = {
                    "title": best_match["title"],
                    "path": best_match["path"],
                    "status": best_match["status"],
                    "first_sentence": best_match["first_sentence"],
                }

            results.append(result)

        return results

    # -- synthesize --------------------------------------------------------

    def synthesize(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Create or merge concept candidates into the vault.

        Each candidate must have been through match_concepts first and include:
        - title (str): entity title
        - disposition (str): 'new' or 'merge'
        - tags (list[str]): from controlled vocabulary
        - claims (list[str]): atomic factual statements
        - relationships (list[str]): wiki-link targets

        For 'new': creates a new entity file with frontmatter + body from claims.
        For 'merge': appends novel claims and unions tags into existing entity.
        Skips 'ambiguous' candidates — those need agent resolution first.

        Returns a result per candidate with status and any warnings.
        """
        allowed_tags = self._allowed_tags()
        results: list[dict[str, Any]] = []
        written_paths: list[Path] = []

        for candidate in candidates:
            disposition = candidate.get("disposition")
            title = candidate.get("title", "")

            if disposition == "ambiguous":
                results.append({
                    "title": title,
                    "action": "skipped",
                    "reason": "ambiguous — resolve match before synthesizing",
                })
                continue

            if not title:
                results.append({"action": "error", "reason": "missing title"})
                continue

            # Validate tags
            c_tags = candidate.get("tags", [])
            valid_tags = [t for t in c_tags if t.lower() in allowed_tags]
            invalid_tags = [t for t in c_tags if t.lower() not in allowed_tags]

            claims = candidate.get("claims", [])
            relationships = candidate.get("relationships", [])
            entity_type = candidate.get("type", "concept")

            if disposition == "new":
                result = self._synthesize_new(
                    title=title,
                    entity_type=entity_type,
                    tags=valid_tags,
                    claims=claims,
                    relationships=relationships,
                    project=candidate.get("project"),
                )
            elif disposition == "merge":
                matched = candidate.get("matched_entity", {})
                entity_path = matched.get("path", "")
                if not entity_path:
                    results.append({
                        "title": title,
                        "action": "error",
                        "reason": "merge disposition but no matched_entity path",
                    })
                    continue
                result = self._synthesize_merge(
                    entity_path=entity_path,
                    new_tags=valid_tags,
                    new_claims=claims,
                    new_relationships=relationships,
                )
            else:
                results.append({
                    "title": title,
                    "action": "error",
                    "reason": f"unknown disposition: {disposition}",
                })
                continue

            if invalid_tags:
                result["tag_warnings"] = [
                    f"tag '{t}' not in controlled vocabulary — dropped"
                    for t in invalid_tags
                ]

            # Track written file paths for batch commit
            if result.get("action") in ("created", "merged") and result.get("path"):
                written_paths.append(self.root / result["path"])

            results.append(result)

        # Batch commit all written files in a single git operation
        if written_paths:
            titles = [r.get("title", "?") for r in results if r.get("action") in ("created", "merged")]
            message = f"synthesize {len(written_paths)} entities: {', '.join(titles[:5])}"
            if len(titles) > 5:
                message += f" (+{len(titles) - 5} more)"
            git_info = self._commit_and_push_batch(written_paths, message)
            # Attach git info to the last result for visibility
            for r in results:
                if r.get("action") in ("created", "merged"):
                    r["git"] = git_info.get("git", "skipped")

        return results

    def _entity_folder_for(
        self, entity_type: str, project: str | None
    ) -> Path:
        """Return the directory where a new entity of the given type should live.

        Project-scoped types (scope: project in types.yml) go under
        ``{project_dir}/{type}/``.  Root-scoped types stay at
        ``{vault_root}/{type}/``.  The ``project`` type itself goes at
        the project directory root (no subfolder).
        """
        types_path = self.root / "types.yml"
        registry = {}
        if types_path.exists():
            registry = yaml.safe_load(types_path.read_text(encoding="utf-8")) or {}

        type_meta = registry.get(entity_type, {})
        scope = type_meta.get("scope", "root")

        if scope == "project" and entity_type != "project":
            proj_dir = self._project_dir_for(project)
            if proj_dir:
                return proj_dir / entity_type
        elif entity_type == "project":
            proj_dir = self._project_dir_for(project)
            if proj_dir:
                return proj_dir

        return self.root / entity_type

    def _synthesize_new(
        self,
        *,
        title: str,
        entity_type: str,
        tags: list[str],
        claims: list[str],
        relationships: list[str],
        project: str | None = None,
    ) -> dict[str, Any]:
        """Create a brand-new entity file from a concept candidate."""
        # Determine folder
        proj = project or self._active_project()
        type_folder = self._entity_folder_for(entity_type, proj)
        if not type_folder.is_dir():
            type_folder.mkdir(parents=True, exist_ok=True)

        file_path = type_folder / f"{title}.md"
        if file_path.exists():
            return {
                "title": title,
                "action": "error",
                "reason": f"file already exists: {file_path.relative_to(self.root)}",
            }

        # Build frontmatter
        fm: dict[str, Any] = {
            "title": title,
            "guid": str(uuid.uuid4()),
            "type": [entity_type],
            "status": "draft",
            "tags": tags,
        }
        if proj:
            fm["project"] = [proj]

        # Build body from claims
        body_lines = [f"# {title}", ""]
        if claims:
            for claim in claims:
                body_lines.append(f"- {claim}")
            body_lines.append("")

        # Add relationships as a Related section
        if relationships:
            body_lines.append("## Related")
            body_lines.append("")
            for rel in relationships:
                # Normalize: wrap in [[ ]] if not already
                if not rel.startswith("[["):
                    rel = f"[[{rel}]]"
                body_lines.append(f"- {rel}")
            body_lines.append("")

        body = "\n".join(body_lines)
        text = _serialize_frontmatter(fm, body)
        warnings = self._validate_links(text)

        file_path.write_text(text, encoding="utf-8")

        return {
            "title": title,
            "action": "created",
            "path": str(file_path.relative_to(self.root)),
            "guid": fm["guid"],
            "claims_count": len(claims),
            "warnings": warnings,
        }

    def _synthesize_merge(
        self,
        *,
        entity_path: str,
        new_tags: list[str],
        new_claims: list[str],
        new_relationships: list[str],
    ) -> dict[str, Any]:
        """Merge new claims and tags into an existing entity. Additive only."""
        path = self.root / entity_path
        if not path.exists():
            return {
                "path": entity_path,
                "action": "error",
                "reason": f"file not found: {entity_path}",
            }

        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        # --- Union tags (additive) ---
        existing_tags = [t.lower() for t in fm.get("tags", [])]
        added_tags = [t for t in new_tags if t.lower() not in existing_tags]
        if added_tags:
            fm["tags"] = fm.get("tags", []) + added_tags

        # --- Append novel claims ---
        # Check each claim against existing body to avoid duplicates.
        # Use normalized substring matching — if the core words of a claim
        # already appear in the body, consider it a duplicate.
        body_lower = body.lower()
        novel_claims: list[str] = []
        duplicate_claims: list[str] = []
        for claim in new_claims:
            # Extract significant words (4+ chars) for fuzzy matching
            claim_words = set(re.findall(r"[a-z]{4,}", claim.lower()))
            if not claim_words:
                novel_claims.append(claim)
                continue
            # If 70%+ of significant words appear in existing body, it's a dup
            matches = sum(1 for w in claim_words if w in body_lower)
            if matches / len(claim_words) >= 0.7:
                duplicate_claims.append(claim)
            else:
                novel_claims.append(claim)

        if novel_claims:
            # Append under a "## Synthesized" section
            synth_header = "\n## Synthesized\n\n"
            if "## Synthesized" in body:
                # Append to existing synthesized section
                for claim in novel_claims:
                    body += f"\n- {claim}"
            else:
                body += synth_header
                for claim in novel_claims:
                    body += f"- {claim}\n"

        # --- Add novel relationships ---
        existing_links = set(extract_wikilinks(text))
        novel_links: list[str] = []
        for rel in new_relationships:
            link_target = rel.strip("[]")
            if link_target not in existing_links:
                novel_links.append(rel)

        if novel_links:
            # Find or create Related section
            if "## Related" in body:
                # Append to existing Related section
                for rel in novel_links:
                    if not rel.startswith("[["):
                        rel = f"[[{rel}]]"
                    body += f"\n- {rel}"
            else:
                body += "\n## Related\n\n"
                for rel in novel_links:
                    if not rel.startswith("[["):
                        rel = f"[[{rel}]]"
                    body += f"- {rel}\n"

        new_text = _serialize_frontmatter(fm, body)
        warnings = self._validate_links(new_text)
        path.write_text(new_text, encoding="utf-8")

        entity_id = fm.get("id", path.stem)

        return {
            "title": fm.get("title", path.stem),
            "action": "merged",
            "path": entity_path,
            "added_tags": added_tags,
            "novel_claims": len(novel_claims),
            "duplicate_claims": len(duplicate_claims),
            "novel_links": len(novel_links),
            "warnings": warnings,
        }

    # -- get_relevant_context (Strategy 3) ---------------------------------

    def get_relevant_context(
        self,
        topic: str,
        *,
        max_entities: int = 5,
        include_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Single-call aggregation: search + query + context for a topic.

        Combines keyword search, tag-based querying, and full entity loading
        into one tool call. Returns the most relevant vault context for a
        given topic, reducing the cost from 2-3 tool calls to 1.

        Args:
            topic: Natural-language description of what the agent is working on.
            max_entities: Maximum full entities to return (default 5).
            include_types: Optional filter to specific entity types
                (e.g. ["concept", "lesson"]).

        Returns a dict with:
            - entities: full content of the top-N most relevant entities
            - related: synopses of entities linked from the top results
            - coverage_gaps: aspects of the topic with no vault coverage
        """
        # 1. Search for keyword matches
        search_results = self.search(
            topic, max_results=max_entities * 2
        )

        # 2. Filter by type if requested
        if include_types:
            lower_types = [t.lower() for t in include_types]
            search_results = [
                r for r in search_results
                if r.get("type", "").lower() in lower_types
            ] or search_results  # fall back to unfiltered if filter empties list

        # 3. Load full context for the top N entities
        top_paths = [r["path"] for r in search_results[:max_entities]]
        entities: list[dict[str, Any]] = []
        all_linked: set[str] = set()

        for path_str in top_paths:
            # Resolve by path stem
            stem = Path(path_str).stem
            try:
                ctx = self.get_context(stem)
                entities.append(ctx)
                # Track linked entities for the related section
                for linked in ctx.get("linked_entities", []):
                    linked_title = linked.get("title", "")
                    if linked_title and linked_title not in {
                        e.get("frontmatter", {}).get("title") for e in entities
                    }:
                        all_linked.add(linked_title)
            except FileNotFoundError:
                continue

        # 4. Identify coverage gaps — query words that matched nothing
        query_words = {w.lower() for w in re.findall(r"[a-zA-Z0-9]{3,}", topic)}
        covered_words: set[str] = set()
        for entity in entities:
            title_lower = entity.get("frontmatter", {}).get("title", "").lower()
            body_lower = entity.get("body", "").lower()
            for word in query_words:
                if word in title_lower or word in body_lower:
                    covered_words.add(word)
        uncovered = query_words - covered_words
        coverage_gaps = list(uncovered) if uncovered else []

        # 5. Build related synopses from linked entities not in the top results
        related_synopses: list[dict[str, Any]] = []
        for linked_title in sorted(all_linked)[:10]:
            syn = self._synopsis(linked_title)
            if syn:
                related_synopses.append(syn)

        return {
            "topic": topic,
            "entity_count": len(entities),
            "entities": entities,
            "related": related_synopses,
            "coverage_gaps": coverage_gaps,
        }

    # -- validate_action (Strategy 7) --------------------------------------

    def validate_action(
        self,
        action: str,
        rationale: str,
    ) -> dict[str, Any]:
        """Pre-action validation: check for vault conflicts before acting.

        Searches the vault for decisions, patterns, lessons, rules, and drift
        entries that may conflict with or inform the proposed action. Also
        loads all active rules and surfaces those relevant to the action.

        Args:
            action: Description of what the agent intends to do.
            rationale: Why the agent chose this approach.

        Returns:
            - status: 'proceed' | 'review' | 'conflict'
            - conflicts: entities that directly contradict the action
            - applicable_rules: active rules relevant to the action
            - supporting: entities that support the action
            - informational: entities worth reviewing
            - message: human-readable summary
        """
        combined_query = f"{action} {rationale}"

        # Search across all entity types for relevance
        all_results = self.search(combined_query, max_results=10)

        # Also load all active rules — rules are always surfaced when relevant
        all_rules = self.query(entity_type="rule", status="active")
        # Score rules against the action for relevance
        applicable_rules: list[dict[str, Any]] = []
        action_lower = combined_query.lower()
        for rule in all_rules:
            rule_title = rule.get("title", "").lower()
            rule_tags = {t.lower() for t in rule.get("tags", [])}
            # Check if rule title words appear in the action/rationale
            rule_words = set(re.findall(r"[a-z]{4,}", rule_title))
            if rule_words:
                overlap = sum(1 for w in rule_words if w in action_lower)
                if overlap / len(rule_words) >= 0.3:
                    applicable_rules.append(rule)
                    continue
            # Check tag overlap with action keywords
            action_words = set(re.findall(r"[a-z]{4,}", action_lower))
            if rule_tags & action_words:
                applicable_rules.append(rule)

        # Categorize by type
        conflicts: list[dict[str, Any]] = []
        supporting: list[dict[str, Any]] = []
        informational: list[dict[str, Any]] = []

        for result in all_results:
            entity_type = result.get("type", "")
            score = result.get("score", 0.0)

            if entity_type == "lesson" and score >= 2.0:
                # Lessons from past experience are warnings
                conflicts.append(result)
            elif entity_type == "rule" and score >= 1.5:
                # Rules are enforceable constraints — always surface
                if result not in applicable_rules:
                    applicable_rules.append(result)
            elif entity_type in ("concept", "procedure"):
                # Existing concepts/procedures may already cover this
                supporting.append(result)
            elif score >= 1.5:
                informational.append(result)

        # Determine status
        if conflicts:
            status = "conflict"
            message = (
                f"Found {len(conflicts)} potentially conflicting "
                f"lesson(s). Review before proceeding."
            )
        elif applicable_rules or supporting or informational:
            if applicable_rules and not supporting and not informational:
                status = "review"
                message = (
                    f"Found {len(applicable_rules)} applicable rule(s). "
                    f"Rules are enforceable constraints — verify compliance."
                )
            else:
                status = "review"
                parts = []
                if applicable_rules:
                    parts.append(f"{len(applicable_rules)} applicable rule(s)")
                if supporting:
                    parts.append(f"{len(supporting)} supporting")
                if informational:
                    parts.append(f"{len(informational)} informational")
                message = (
                    f"Found {', '.join(parts)} entities. "
                    f"Consider reviewing for additional context."
                )
        else:
            status = "proceed"
            message = (
                "No vault conflicts found. No existing precedent — "
                "consider persisting the rationale as a new decision entity."
            )

        return {
            "status": status,
            "message": message,
            "action": action,
            "rationale": rationale,
            "conflicts": conflicts,
            "applicable_rules": applicable_rules,
            "supporting": supporting,
            "informational": informational,
        }

    # -- findings ----------------------------------------------------------

    # Threshold in characters across all findings files before needsSynthesis
    _FINDINGS_SYNTHESIS_THRESHOLD = 8000

    def submit_findings(
        self,
        topics: list[dict[str, Any]],
        *,
        project: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit investigation findings grouped by topic.

        Each topic is a dict with:
            - topic (str): the topic name (used as filename)
            - content (str): unstructured findings text — new observations

        If a findings file for the topic already exists, the new content is
        appended (with a timestamp separator) preserving discovery order.
        If no file exists, a new findings entity is created.

        Args:
            topics: List of {topic, content} dicts.
            project: Project name (defaults to active-project).
            session_id: Optional session identifier for provenance tracking.

        Returns summary with per-topic actions and needsSynthesis flag.
        """
        proj = project or self._active_project()
        findings_dir = self.root / "findings"
        findings_dir.mkdir(exist_ok=True)

        results: list[dict[str, Any]] = []
        written_paths: list[Path] = []
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

        for entry in topics:
            topic_name = entry.get("topic", "").strip()
            content = entry.get("content", "").strip()

            if not topic_name or not content:
                results.append({
                    "topic": topic_name or "(empty)",
                    "action": "error",
                    "reason": "topic and content are both required",
                })
                continue

            file_path = findings_dir / f"{topic_name}.md"

            if file_path.exists():
                result = self._merge_findings(
                    file_path, content, timestamp, session_id
                )
            else:
                result = self._create_findings(
                    file_path,
                    topic_name=topic_name,
                    content=content,
                    project=proj,
                    timestamp=timestamp,
                    session_id=session_id,
                )

            results.append(result)
            if result.get("action") in ("created", "appended"):
                written_paths.append(file_path)

        # Batch commit
        if written_paths:
            topic_names = [r.get("topic", "?") for r in results
                           if r.get("action") in ("created", "appended")]
            message = f"findings: {', '.join(topic_names[:5])}"
            if len(topic_names) > 5:
                message += f" (+{len(topic_names) - 5} more)"
            git_info = self._commit_and_push_batch(written_paths, message)
        else:
            git_info = {"git": "skipped"}

        needs = self._needs_synthesis()

        return {
            "submitted": len([r for r in results if r["action"] in ("created", "appended")]),
            "results": results,
            "needsSynthesis": needs,
            **git_info,
        }

    def _create_findings(
        self,
        file_path: Path,
        *,
        topic_name: str,
        content: str,
        project: str | None,
        timestamp: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Create a new findings file for a topic."""
        fm: dict[str, Any] = {
            "title": topic_name,
            "guid": str(uuid.uuid4()),
            "type": ["findings"],
            "status": "collecting",
        }
        if project:
            fm["project"] = [project]

        header = f"[{timestamp}]"
        if session_id:
            header += f" (session: {session_id})"

        body_lines = [
            f"# {topic_name}",
            "",
            "## Entries",
            "",
            f"### {header}",
            "",
            content,
            "",
        ]
        body = "\n".join(body_lines)
        text = _serialize_frontmatter(fm, body)
        file_path.write_text(text, encoding="utf-8")

        return {
            "topic": topic_name,
            "action": "created",
            "path": str(file_path.relative_to(self.root)),
            "guid": fm["guid"],
        }

    def _merge_findings(
        self,
        file_path: Path,
        content: str,
        timestamp: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Append new findings to an existing findings file."""
        text = file_path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        header = f"[{timestamp}]"
        if session_id:
            header += f" (session: {session_id})"

        # Append new entry at end of body (preserving order)
        new_entry = f"\n### {header}\n\n{content}\n"
        body = body.rstrip() + "\n" + new_entry

        new_text = _serialize_frontmatter(fm, body)
        file_path.write_text(new_text, encoding="utf-8")

        return {
            "topic": fm.get("title", file_path.stem),
            "action": "appended",
            "path": str(file_path.relative_to(self.root)),
            "entry_count": body.count("### ["),
        }

    def _findings_total_size(self) -> int:
        """Return total character count across all active findings files."""
        findings_dir = self.root / "findings"
        if not findings_dir.is_dir():
            return 0
        total = 0
        for p in findings_dir.iterdir():
            if p.is_file() and p.suffix == ".md":
                text = p.read_text(encoding="utf-8")
                fm, _ = _parse_frontmatter(text)
                if fm.get("status") != "synthesized":
                    total += p.stat().st_size
        return total

    def _needs_synthesis(self) -> bool:
        """Return True if accumulated findings exceed the synthesis threshold."""
        return self._findings_total_size() >= self._FINDINGS_SYNTHESIS_THRESHOLD

    def get_findings_status(self) -> dict[str, Any]:
        """Return a summary of current findings state.

        Includes per-topic file sizes, total size, threshold,
        and whether synthesis is needed.
        """
        findings_dir = self.root / "findings"
        topics: list[dict[str, Any]] = []
        total_size = 0

        if findings_dir.is_dir():
            for p in sorted(findings_dir.iterdir()):
                if p.is_file() and p.suffix == ".md":
                    text = p.read_text(encoding="utf-8")
                    fm, body = _parse_frontmatter(text)
                    status = fm.get("status", "collecting")
                    size = len(text)
                    entry_count = body.count("### [")
                    topics.append({
                        "topic": fm.get("title", p.stem),
                        "path": str(p.relative_to(self.root)),
                        "status": status,
                        "size": size,
                        "entry_count": entry_count,
                    })
                    if status != "synthesized":
                        total_size += size

        return {
            "topics": topics,
            "topic_count": len(topics),
            "total_size": total_size,
            "threshold": self._FINDINGS_SYNTHESIS_THRESHOLD,
            "needsSynthesis": total_size >= self._FINDINGS_SYNTHESIS_THRESHOLD,
        }
