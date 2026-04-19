"""Storage layer — file I/O, YAML/frontmatter parsing, and link resolution over the Brain vault."""

from __future__ import annotations

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

    def __init__(
        self,
        *,
        repo_url: str | None = None,
        vault_path: str | Path | None = None,
    ) -> None:
        self._repo_url = repo_url
        self._remote_verified = False
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
        """Stage a file, commit, and push to main. Returns git status info.

        Push runs in a background thread so the tool response is not blocked
        by network latency.
        """
        rel = str(path.relative_to(self.root))
        info: dict[str, Any] = {"git": "skipped"}

        if not self._repo_url:
            return info

        # Ensure remote is configured before push
        self._ensure_remote(self._repo_url)

        # Stage
        result = self._git("add", rel)
        if result.returncode != 0:
            info["git"] = "error"
            info["git_error"] = f"git add failed: {result.stderr.strip()}"
            return info

        # Commit
        result = self._git("commit", "-m", message)
        if result.returncode != 0:
            combined = (result.stdout + result.stderr).lower()
            if "nothing to commit" in combined or "nothing added to commit" in combined:
                info["git"] = "no_changes"
                return info
            info["git"] = "error"
            info["git_error"] = f"git commit failed: {result.stderr.strip() or result.stdout.strip()}"
            return info

        # Push in background — don't block the tool response on network I/O
        def _bg_push() -> None:
            try:
                result = self._git("push", "origin", "main")
                if result.returncode != 0:
                    log.warning("background push failed: %s", result.stderr.strip())
                else:
                    log.info("background push succeeded")
            except Exception:
                log.exception("background push error")

        threading.Thread(target=_bg_push, daemon=True).start()
        info["git"] = "committed_push_pending"
        return info

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

    # -- types.yml registry ------------------------------------------------

    def read_types(self) -> dict[str, Any]:
        """Return the parsed type registry from types.yml.

        Each key is a type name (e.g. 'goal') with description, icon, and
        a count of entities currently in that folder.
        """
        types_path = self.root / "types.yml"
        if not types_path.exists():
            raise FileNotFoundError("types.yml not found in vault root")
        registry = yaml.safe_load(types_path.read_text(encoding="utf-8")) or {}

        # Enrich with entity counts
        for type_name, meta in registry.items():
            folder = self.root / type_name
            if folder.is_dir():
                meta["count"] = sum(
                    1 for p in folder.iterdir()
                    if p.is_file() and p.suffix == ".md" and p.name != "_index.md"
                )
            else:
                meta["count"] = 0
        return registry

    # -- type folders & entity files ----------------------------------------

    def _type_folders(self) -> list[str]:
        """Return the names of all type-based subfolders (e.g. goal, system)."""
        return [
            d.name
            for d in self.root.iterdir()
            if d.is_dir() and d.name not in _IGNORED_ROOTS and not d.name.startswith(".")
        ]

    def _iter_entity_files(self) -> list[tuple[str, Path]]:
        """Return (type_folder, path) for every .md entity file in type folders."""
        results: list[tuple[str, Path]] = []
        for folder_name in sorted(self._type_folders()):
            folder = self.root / folder_name
            for p in sorted(folder.iterdir()):
                if p.is_file() and p.suffix == ".md" and p.name != "_index.md":
                    results.append((folder_name, p))
        return results

    def _resolve_entity_path(self, identifier: str) -> Path | None:
        """Resolve an entity by filename stem (e.g. 'Storage Layer'),
        by type/filename path (e.g. 'system/Storage Layer'),
        by frontmatter id (e.g. 'S-1'), or by guid.
        """
        # Direct path match (e.g. "system/Storage Layer" or "system/Storage Layer.md")
        for ext in ("", ".md"):
            p = self.root / f"{identifier}{ext}"
            if p.exists() and p.is_file():
                return p

        # Match by filename stem across all type folders
        for _, path in self._iter_entity_files():
            if path.stem == identifier:
                return path

        # Match by frontmatter id or guid
        for _, path in self._iter_entity_files():
            text = path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)
            if fm.get("id") == identifier or fm.get("guid") == identifier:
                return path

        return None

    def read_entity(self, identifier: str) -> dict[str, Any]:
        """Read an entity file and return {guid, id, frontmatter, body, links, path}."""
        path = self._resolve_entity_path(identifier)
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
        """Scan frontmatter across all concept files. Return matching synopses.

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

            entry: dict[str, Any] = {
                "guid": fm.get("guid"),
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
            results.append(result)

        return results

    def _synthesize_new(
        self,
        *,
        title: str,
        entity_type: str,
        tags: list[str],
        claims: list[str],
        relationships: list[str],
    ) -> dict[str, Any]:
        """Create a brand-new entity file from a concept candidate."""
        # Determine folder
        type_folder = self.root / entity_type
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
        active = self._active_project()
        fm: dict[str, Any] = {
            "title": title,
            "guid": str(uuid.uuid4()),
            "type": [entity_type],
            "status": entity_type if entity_type in ("concept", "drift") else "draft",
            "tags": tags,
        }
        if active:
            fm["project"] = [active]

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

        # Commit
        git_info = self._commit_and_push(
            file_path, f"synthesize new: {title}"
        )

        return {
            "title": title,
            "action": "created",
            "path": str(file_path.relative_to(self.root)),
            "guid": fm["guid"],
            "claims_count": len(claims),
            "warnings": warnings,
            **git_info,
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
        git_info = self._commit_and_push(
            path, f"synthesize merge: {entity_id}"
        )

        return {
            "title": fm.get("title", path.stem),
            "action": "merged",
            "path": entity_path,
            "added_tags": added_tags,
            "novel_claims": len(novel_claims),
            "duplicate_claims": len(duplicate_claims),
            "novel_links": len(novel_links),
            "warnings": warnings,
            **git_info,
        }
