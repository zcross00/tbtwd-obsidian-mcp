"""One-time vault migration: flat type folders → project-based directory structure.

Moves project-scoped entities (concept, goal, system) under project dirs,
reclassifies eliminated types (decision→concept, pattern→concept/template),
and archives drift/feature entities.
"""

import re
import shutil
import sys
from pathlib import Path

import yaml

VAULT = Path(r"c:\Users\zcros\development\TheBrainThatWouldntDie")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Project directory mapping (from brief.yml)
PROJECT_DIRS = {
    "TheBrainThatWouldntDie": "tbtwd",
    "Sovereign": "sovereign",
}

# Entities that should become templates instead of concepts
PATTERN_TO_TEMPLATE = {
    "Entity Body Templates",
    "Domain Model Decision Tree",
    "Unity Scene Wiring Checklist",
}

DRY_RUN = "--dry-run" in sys.argv


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if m:
        fm = yaml.safe_load(m.group(1)) or {}
        body = text[m.end():]
        return fm, body
    return {}, text


def serialize(fm: dict, body: str) -> str:
    fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False).rstrip("\n")
    return f"---\n{fm_str}\n---\n{body}"


def get_project(fm: dict) -> str | None:
    proj = fm.get("project")
    if isinstance(proj, list) and proj:
        return proj[0]
    if isinstance(proj, str):
        return proj
    return None


def get_type(fm: dict) -> str | None:
    t = fm.get("type")
    if isinstance(t, list) and t:
        return t[0]
    if isinstance(t, str):
        return t
    return None


def move_file(src: Path, dst: Path, reason: str):
    print(f"  {'[DRY] ' if DRY_RUN else ''}MOVE: {src.relative_to(VAULT)} → {dst.relative_to(VAULT)}  ({reason})")
    if not DRY_RUN:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def update_frontmatter(path: Path, updates: dict, reason: str):
    fm, body = parse_frontmatter(path)
    for k, v in updates.items():
        fm[k] = v
    print(f"  {'[DRY] ' if DRY_RUN else ''}UPDATE FM: {path.relative_to(VAULT)}  {updates}  ({reason})")
    if not DRY_RUN:
        path.write_text(serialize(fm, body), encoding="utf-8")


def archive_file(src: Path, reason: str):
    rel = src.relative_to(VAULT)
    dst = VAULT / ".trash" / rel
    print(f"  {'[DRY] ' if DRY_RUN else ''}ARCHIVE: {rel} → .trash/{rel}  ({reason})")
    if not DRY_RUN:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def delete_file(path: Path, reason: str):
    print(f"  {'[DRY] ' if DRY_RUN else ''}DELETE: {path.relative_to(VAULT)}  ({reason})")
    if not DRY_RUN:
        path.unlink()


def delete_dir(path: Path, reason: str):
    print(f"  {'[DRY] ' if DRY_RUN else ''}RMDIR: {path.relative_to(VAULT)}  ({reason})")
    if not DRY_RUN:
        path.rmdir()


def create_project_entity(proj_key: str, proj_dir: Path, brief_data: dict):
    """Create the project root entity file."""
    proj_meta = brief_data["projects"][proj_key]
    name = proj_meta["name"]
    file_path = proj_dir / f"{name}.md"
    if file_path.exists():
        print(f"  SKIP: {file_path.relative_to(VAULT)} already exists")
        return

    import uuid
    fm = {
        "title": name,
        "guid": str(uuid.uuid4()),
        "type": ["project"],
        "status": "active",
        "tags": ["core"],
        "project": [proj_key],
    }
    summary = proj_meta.get("summary", "")
    stack = proj_meta.get("stack", [])
    goals = proj_meta.get("goals", {})

    body = f"# {name}\n\n{summary}\n"
    if stack:
        body += f"\n## Stack\n\n{', '.join(stack)}\n"
    if goals:
        body += "\n## Goals\n\n"
        for goal_name, goal_desc in goals.items():
            body += f"- [[{goal_name}]] — {goal_desc}\n"

    content = serialize(fm, body)
    print(f"  {'[DRY] ' if DRY_RUN else ''}CREATE: {file_path.relative_to(VAULT)}")
    if not DRY_RUN:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"=== Vault Migration ({mode}) ===\n")

    # Load brief
    brief = yaml.safe_load((VAULT / "brief.yml").read_text(encoding="utf-8"))

    # ── Phase 1: Create project directories and entity files ──────────────
    print("Phase 1: Create project directories and entity files")
    for proj_key, dir_name in PROJECT_DIRS.items():
        proj_dir = VAULT / dir_name
        for sub in ("concept", "goal", "system", "template"):
            target = proj_dir / sub
            if not target.exists():
                print(f"  {'[DRY] ' if DRY_RUN else ''}MKDIR: {target.relative_to(VAULT)}")
                if not DRY_RUN:
                    target.mkdir(parents=True, exist_ok=True)
        create_project_entity(proj_key, proj_dir, brief)

    # ── Phase 2: Move project-scoped entities ─────────────────────────────
    print("\nPhase 2: Move project-scoped entities (concept, goal, system)")
    for type_name in ("concept", "goal", "system"):
        type_dir = VAULT / type_name
        if not type_dir.is_dir():
            continue
        for md in sorted(type_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            fm, _ = parse_frontmatter(md)
            proj = get_project(fm)
            if proj and proj in PROJECT_DIRS:
                dir_name = PROJECT_DIRS[proj]
                dst = VAULT / dir_name / type_name / md.name
                move_file(md, dst, f"project={proj}")
            else:
                print(f"  WARNING: {md.relative_to(VAULT)} has no project assignment!")

    # ── Phase 3: Reclassify eliminated types ──────────────────────────────
    print("\nPhase 3: Reclassify decisions → concepts")
    decision_dir = VAULT / "decision"
    if decision_dir.is_dir():
        for md in sorted(decision_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            fm, _ = parse_frontmatter(md)
            proj = get_project(fm)
            dir_name = PROJECT_DIRS.get(proj, "tbtwd")
            dst = VAULT / dir_name / "concept" / md.name
            move_file(md, dst, "decision→concept")
            if not DRY_RUN:
                update_frontmatter(dst, {"type": ["concept"]}, "reclassify decision→concept")

    print("\nPhase 3b: Reclassify patterns → concepts/templates")
    pattern_dir = VAULT / "pattern"
    if pattern_dir.is_dir():
        for md in sorted(pattern_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            fm, _ = parse_frontmatter(md)
            title = fm.get("title", md.stem)
            proj = get_project(fm)
            dir_name = PROJECT_DIRS.get(proj, "tbtwd")

            if title in PATTERN_TO_TEMPLATE:
                new_type = "template"
            else:
                new_type = "concept"

            dst = VAULT / dir_name / new_type / md.name
            move_file(md, dst, f"pattern→{new_type}")
            if not DRY_RUN:
                update_frontmatter(dst, {"type": [new_type]}, f"reclassify pattern→{new_type}")

    print("\nPhase 3c: Archive drift entities")
    drift_dir = VAULT / "drift"
    if drift_dir.is_dir():
        for md in sorted(drift_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            archive_file(md, "drift type eliminated")

    print("\nPhase 3d: Archive feature entities")
    feature_dir = VAULT / "feature"
    if feature_dir.is_dir():
        for md in sorted(feature_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            archive_file(md, "feature type eliminated")

    # ── Phase 4: Clean up ─────────────────────────────────────────────────
    print("\nPhase 4: Clean up _index.md files and empty directories")
    for type_name in ("concept", "decision", "drift", "feature", "goal", "pattern", "system"):
        type_dir = VAULT / type_name
        if not type_dir.is_dir():
            continue
        idx = type_dir / "_index.md"
        if idx.exists():
            delete_file(idx, "obsolete index file")
        # Check if directory is now empty
        remaining = list(type_dir.iterdir())
        if not remaining:
            delete_dir(type_dir, "empty after migration")
        else:
            remaining_names = [f.name for f in remaining]
            print(f"  NOTE: {type_dir.relative_to(VAULT)} still has: {remaining_names}")

    # Also clean _index.md from root-scoped type folders
    for type_name in ("procedure", "rule", "lesson"):
        idx = VAULT / type_name / "_index.md"
        if idx.exists():
            delete_file(idx, "obsolete index file")

    print("\n=== Migration complete ===")
    if DRY_RUN:
        print("(No files were modified. Run without --dry-run to execute.)")


if __name__ == "__main__":
    main()
