"""Tests for the BrainVault storage layer.

Uses a temporary directory with a minimal vault structure to test
parsing, querying, search, matching, synthesis, and link checking
without any git operations.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from tbtwd_obsidian_mcp.storage import (
    BrainVault,
    _parse_frontmatter,
    _serialize_frontmatter,
    extract_wikilinks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Create a minimal vault structure for testing."""
    # brief.yml
    brief = {
        "active-project": "TestProject",
        "projects": {
            "TestProject": {
                "name": "Test Project",
                "summary": "A test project for unit tests.",
                "stack": ["Python"],
                "goals": {
                    "Test goal one": "A test goal for unit testing",
                    "Test goal two": "Another test goal",
                },
            },
        },
        "focus": "Testing the vault",
    }
    (tmp_path / "brief.yml").write_text(
        yaml.dump(brief, default_flow_style=False), encoding="utf-8"
    )

    # types.yml
    types = {
        "concept": {
            "description": "Ideas and theories.",
            "statuses": ["draft", "active", "superseded"],
            "folder": "concept",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "decision": {
            "description": "Resolved choices.",
            "statuses": ["proposed", "active", "reversed"],
            "folder": "decision",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "goal": {
            "description": "Desired outcomes.",
            "statuses": ["active", "achieved", "dropped"],
            "folder": "goal",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "system": {
            "description": "Running components.",
            "statuses": ["draft", "active", "superseded"],
            "folder": "system",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "drift": {
            "description": "Open questions.",
            "statuses": ["open", "resolved", "deferred"],
            "folder": "drift",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "pattern": {
            "description": "Recurring solutions.",
            "statuses": ["draft", "documented", "superseded"],
            "folder": "pattern",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "lesson": {
            "description": "Experience-derived insights.",
            "statuses": ["draft", "documented"],
            "folder": "lesson",
            "required-fields": ["title", "project", "tags", "status"],
        },
        "rule": {
            "description": "Enforceable constraints and standards.",
            "statuses": ["draft", "active", "superseded"],
            "folder": "rule",
            "required-fields": ["title", "project", "tags", "status"],
        },
    }
    (tmp_path / "types.yml").write_text(
        yaml.dump(types, default_flow_style=False), encoding="utf-8"
    )

    # tags.yml
    tags = {
        "domain": [
            {"tag": "architecture", "description": "Structural design."},
            {"tag": "mcp", "description": "MCP tools and transport."},
            {"tag": "storage", "description": "File I/O and persistence."},
        ],
        "scope": [
            {"tag": "core", "description": "Foundational."},
        ],
        "quality": [
            {"tag": "performance", "description": "Speed and efficiency."},
        ],
    }
    (tmp_path / "tags.yml").write_text(
        yaml.dump(tags, default_flow_style=False), encoding="utf-8"
    )

    # extraction-schema.yml
    schema = {
        "candidate-format": {
            "title": "string",
            "tags": "list[string]",
            "claims": "list[string]",
            "relationships": "list[string]",
            "type": "string (default: concept)",
        },
    }
    (tmp_path / "extraction-schema.yml").write_text(
        yaml.dump(schema, default_flow_style=False), encoding="utf-8"
    )

    # Create type folders
    for folder in ["concept", "decision", "goal", "system", "drift", "pattern", "lesson", "rule"]:
        (tmp_path / folder).mkdir()

    # Create sample entities
    _write_entity(tmp_path / "concept" / "Token Efficiency.md", {
        "title": "Token Efficiency",
        "guid": "a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5",
        "type": ["concept"],
        "status": "active",
        "tags": ["architecture", "performance", "core"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # Token Efficiency

        - L0 bootstrap costs ~300 tokens via get_brief
        - Each tool response should stay within its token budget
        - Scoped retrieval is more efficient than bulk loading

        ## Related

        - [[Architecture Overview]]
    """))

    _write_entity(tmp_path / "concept" / "Architecture Overview.md", {
        "title": "Architecture Overview",
        "guid": "b2b2b2b2-c3c3-d4d4-e5e5-f6f6f6f6f6f6",
        "type": ["concept"],
        "status": "active",
        "tags": ["core"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # Architecture Overview

        - The system is a read-through lens over file-based memory
        - Files are the source of truth, not the MCP server

        ## Related

        - [[Token Efficiency]]
        - [[Storage Layer]]
    """))

    _write_entity(tmp_path / "decision" / "YAML For Data.md", {
        "title": "YAML for data, markdown for prose",
        "guid": "c3c3c3c3-d4d4-e5e5-f6f6-a7a7a7a7a7a7",
        "type": ["decision"],
        "status": "active",
        "tags": ["architecture", "storage"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # YAML for data, markdown for prose

        ## Decision

        - Use YAML for structured data, markdown for prose content

        ## Rationale

        - YAML is 3-5x more token-efficient than markdown tables
    """))

    _write_entity(tmp_path / "goal" / "Test Goal One.md", {
        "title": "Test goal one",
        "guid": "d4d4d4d4-e5e5-f6f6-a7a7-b8b8b8b8b8b8",
        "type": ["goal"],
        "status": "active",
        "tags": ["core"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # Test goal one

        - A test goal for unit testing
    """))

    _write_entity(tmp_path / "system" / "Storage Layer.md", {
        "title": "Storage Layer",
        "guid": "e5e5e5e5-f6f6-a7a7-b8b8-c9c9c9c9c9c9",
        "type": ["system"],
        "status": "active",
        "tags": ["architecture", "storage"],
        "project": ["TestProject"],
        "serves": ["[[Test Goal One]]"],
    }, textwrap.dedent("""\
        # Storage Layer

        ## Intent

        File I/O and YAML parsing over the vault directory.

        ## Related

        - [[Architecture Overview]]
    """))

    _write_entity(tmp_path / "drift" / "Open Question.md", {
        "title": "Open Test Question",
        "guid": "f6f6f6f6-a7a7-b8b8-c9c9-d0d0d0d0d0d0",
        "type": ["drift"],
        "status": "open",
        "tags": ["architecture"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # Open Test Question

        Should we add caching to the storage layer? Performance vs. staleness trade-off.
    """))

    _write_entity(tmp_path / "lesson" / "Git Pipe Deadlock.md", {
        "title": "Git Pipe Deadlock on Windows",
        "guid": "a7a7a7a7-b8b8-c9c9-d0d0-e1e1e1e1e1e1",
        "type": ["lesson"],
        "status": "documented",
        "tags": ["performance"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # Git Pipe Deadlock on Windows

        - Git subprocesses called from stdio MCP server can deadlock on pipe buffers
        - Moving git push to a daemon thread resolved the issue
        - Always use daemon=True so the thread doesn't block server shutdown
    """))

    _write_entity(tmp_path / "rule" / "Vault Access Via MCP Only.md", {
        "title": "Vault Access Via MCP Only",
        "guid": "b8b8b8b8-c9c9-d0d0-e1e1-f2f2f2f2f2f2",
        "type": ["rule"],
        "status": "active",
        "tags": ["core", "architecture"],
        "project": ["TestProject"],
    }, textwrap.dedent("""\
        # Vault Access Via MCP Only

        All vault interaction goes through MCP server tools, never direct file access.

        ## Constraint

        - Read vault entities via MCP tools only
        - Write vault entities via MCP tools only
        - Never use direct file manipulation on vault files

        ## Related

        - [[Storage Layer]]
    """))

    return tmp_path


def _write_entity(path: Path, fm: dict, body: str) -> None:
    """Write an entity file with frontmatter + body."""
    text = _serialize_frontmatter(fm, body)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def vault(vault_dir: Path) -> BrainVault:
    """Create a BrainVault pointing at the test vault directory."""
    return BrainVault(vault_path=vault_dir)


# ---------------------------------------------------------------------------
# Unit tests: frontmatter parsing
# ---------------------------------------------------------------------------


class TestFrontmatterParsing:
    def test_parse_valid_frontmatter(self):
        text = "---\ntitle: Test\nstatus: active\n---\n# Body\n\nContent here."
        fm, body = _parse_frontmatter(text)
        assert fm["title"] == "Test"
        assert fm["status"] == "active"
        assert "# Body" in body

    def test_parse_no_frontmatter(self):
        text = "# Just a heading\n\nNo frontmatter here."
        fm, body = _parse_frontmatter(text)
        assert fm == {}
        assert "# Just a heading" in body

    def test_parse_empty_frontmatter(self):
        text = "---\n---\n# Body"
        fm, body = _parse_frontmatter(text)
        assert fm == {}
        assert "# Body" in body

    def test_serialize_roundtrip(self):
        fm = {"title": "Test Entity", "status": "active", "tags": ["core"]}
        body = "# Test Entity\n\n- A claim"
        text = _serialize_frontmatter(fm, body)
        fm2, body2 = _parse_frontmatter(text)
        assert fm2["title"] == "Test Entity"
        assert fm2["tags"] == ["core"]
        assert "# Test Entity" in body2

    def test_frontmatter_with_lists(self):
        text = "---\ntitle: Test\ntags:\n- core\n- mcp\nproject:\n- TestProject\n---\nBody"
        fm, body = _parse_frontmatter(text)
        assert fm["tags"] == ["core", "mcp"]
        assert fm["project"] == ["TestProject"]


# ---------------------------------------------------------------------------
# Unit tests: wiki-link extraction
# ---------------------------------------------------------------------------


class TestWikilinks:
    def test_extract_basic_links(self):
        text = "See [[Architecture Overview]] and [[Token Efficiency]]."
        links = extract_wikilinks(text)
        assert "Architecture Overview" in links
        assert "Token Efficiency" in links

    def test_extract_pipe_alias(self):
        text = "See [[Token Efficiency|L0 context]]."
        links = extract_wikilinks(text)
        assert "Token Efficiency|L0 context" in links

    def test_no_links(self):
        text = "No links here."
        assert extract_wikilinks(text) == []

    def test_links_in_code_blocks_ignored(self):
        text = "Normal [[Valid Link]]\n```\n[[Code Block Link]]\n```\nMore text."
        links = extract_wikilinks(text)
        assert "Valid Link" in links
        assert "Code Block Link" not in links

    def test_links_in_inline_code_ignored(self):
        text = "Normal [[Valid Link]] and `[[Inline Code Link]]` text."
        links = extract_wikilinks(text)
        assert "Valid Link" in links
        assert "Inline Code Link" not in links


# ---------------------------------------------------------------------------
# Integration tests: BrainVault reading
# ---------------------------------------------------------------------------


class TestVaultReading:
    def test_read_brief(self, vault: BrainVault):
        brief = vault.read_brief()
        assert brief["active-project"] == "TestProject"
        assert "Test goal one" in brief["projects"]["TestProject"]["goals"]
        assert "next_steps" in brief  # enriched with hints

    def test_read_types(self, vault: BrainVault):
        types = vault.read_types()
        assert "concept" in types
        assert types["concept"]["count"] == 2  # Token Efficiency, Architecture Overview
        assert types["system"]["count"] == 1  # Storage Layer
        assert types["goal"]["count"] == 1

    def test_read_entity_by_title(self, vault: BrainVault):
        entity = vault.read_entity("Token Efficiency")
        assert entity["frontmatter"]["title"] == "Token Efficiency"
        assert entity["guid"] == "a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5"
        assert "Architecture Overview" in entity["links"]

    def test_read_entity_by_frontmatter_id(self, vault: BrainVault):
        """With self-managed IDs removed, resolution falls back to guid or title."""
        entity = vault.read_entity("YAML For Data")
        assert entity["frontmatter"]["title"] == "YAML for data, markdown for prose"

    def test_read_entity_by_guid(self, vault: BrainVault):
        entity = vault.read_entity("e5e5e5e5-f6f6-a7a7-b8b8-c9c9c9c9c9c9")
        assert entity["frontmatter"]["title"] == "Storage Layer"

    def test_read_entity_not_found(self, vault: BrainVault):
        with pytest.raises(FileNotFoundError):
            vault.read_entity("Nonexistent Entity")

    def test_get_context_includes_linked(self, vault: BrainVault):
        ctx = vault.get_context("Token Efficiency")
        assert len(ctx["linked_entities"]) >= 1
        linked_titles = {e["title"] for e in ctx["linked_entities"]}
        assert "Architecture Overview" in linked_titles

    def test_get_context_by_id(self, vault: BrainVault):
        """Resolution by guid works for get_context."""
        ctx = vault.get_context("e5e5e5e5-f6f6-a7a7-b8b8-c9c9c9c9c9c9")
        assert ctx["frontmatter"]["title"] == "Storage Layer"

    def test_read_tags(self, vault: BrainVault):
        tags = vault.read_tags()
        assert "domain" in tags
        tag_names = {t["tag"] for t in tags["domain"]}
        assert "architecture" in tag_names

    def test_read_extraction_schema(self, vault: BrainVault):
        schema = vault.read_extraction_schema()
        assert "candidate-format" in schema


# ---------------------------------------------------------------------------
# Integration tests: query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_all(self, vault: BrainVault):
        results = vault.query()
        assert len(results) > 0

    def test_query_by_entity_type(self, vault: BrainVault):
        results = vault.query(entity_type="concept")
        assert all(r["relevance"] in ("active", "universal") for r in results)
        titles = {r["title"] for r in results}
        assert "Token Efficiency" in titles
        assert "Architecture Overview" in titles

    def test_query_by_tag(self, vault: BrainVault):
        results = vault.query(tag="storage")
        titles = {r["title"] for r in results}
        assert "YAML for data, markdown for prose" in titles
        assert "Storage Layer" in titles
        assert "Token Efficiency" not in titles

    def test_query_by_status(self, vault: BrainVault):
        results = vault.query(status="open")
        assert len(results) == 1
        assert results[0]["title"] == "Open Test Question"

    def test_query_relevance_sorting(self, vault: BrainVault):
        results = vault.query()
        # All entities are TestProject, so all should be "active"
        relevances = [r["relevance"] for r in results]
        assert all(r == "active" for r in relevances)

    def test_query_background_relevance(self, vault: BrainVault, vault_dir: Path):
        """Entities from other projects should be sorted as 'background'."""
        _write_entity(vault_dir / "concept" / "Other Project Thing.md", {
            "title": "Other Project Thing",
            "guid": "zzz-999",
            "type": ["concept"],
            "status": "active",
            "tags": ["core"],
            "project": ["OtherProject"],
        }, "# Other Project Thing\n\n- From another project")

        results = vault.query(entity_type="concept")
        bg = [r for r in results if r.get("relevance") == "background"]
        assert len(bg) == 1
        assert bg[0]["title"] == "Other Project Thing"


# ---------------------------------------------------------------------------
# Integration tests: search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_basic(self, vault: BrainVault):
        results = vault.search("token efficiency")
        assert len(results) > 0
        assert results[0]["title"] == "Token Efficiency"

    def test_search_title_weighted_higher(self, vault: BrainVault):
        results = vault.search("token")
        # "Token Efficiency" should score highest (title match = 2.0)
        assert results[0]["title"] == "Token Efficiency"

    def test_search_body_match(self, vault: BrainVault):
        results = vault.search("bootstrap")
        assert len(results) > 0
        # "bootstrap" appears in Token Efficiency body
        titles = {r["title"] for r in results}
        assert "Token Efficiency" in titles

    def test_search_with_type_filter(self, vault: BrainVault):
        results = vault.search("architecture", entity_type="decision")
        for r in results:
            assert r["type"] == "decision"

    def test_search_with_tag_filter(self, vault: BrainVault):
        results = vault.search("architecture", tag="storage")
        for r in results:
            assert "storage" in r["tags"]

    def test_search_empty_query(self, vault: BrainVault):
        assert vault.search("") == []

    def test_search_no_matches(self, vault: BrainVault):
        results = vault.search("xyzzy_nonexistent_term")
        assert results == []

    def test_search_max_results(self, vault: BrainVault):
        results = vault.search("architecture", max_results=2)
        assert len(results) <= 2

    def test_search_has_snippets(self, vault: BrainVault):
        results = vault.search("bootstrap")
        assert results[0]["snippet"]  # non-empty snippet


# ---------------------------------------------------------------------------
# Integration tests: link checking
# ---------------------------------------------------------------------------


class TestLinkChecking:
    def test_no_broken_links(self, vault: BrainVault):
        broken = vault.check_links()
        # The test vault has a link to [[Storage Layer]] which exists
        # and [[Architecture Overview]] which exists
        # Filter to only genuine breaks
        break_targets = {b["target"] for b in broken}
        # Storage Layer and Architecture Overview exist, Token Efficiency exists
        assert "Token Efficiency" not in break_targets
        assert "Architecture Overview" not in break_targets
        assert "Storage Layer" not in break_targets

    def test_broken_link_detected(self, vault: BrainVault, vault_dir: Path):
        """Add an entity with a broken link and verify it's detected."""
        _write_entity(vault_dir / "concept" / "Broken Links Test.md", {
            "title": "Broken Links Test",
            "guid": "yyy-888",
            "type": ["concept"],
            "status": "draft",
            "tags": ["core"],
            "project": ["TestProject"],
        }, "# Broken Links Test\n\n- See [[Totally Nonexistent Entity]]")

        broken = vault.check_links()
        broken_targets = {b["target"] for b in broken}
        assert "Totally Nonexistent Entity" in broken_targets


# ---------------------------------------------------------------------------
# Integration tests: match_concepts
# ---------------------------------------------------------------------------


class TestMatchConcepts:
    def test_exact_title_match(self, vault: BrainVault):
        candidates = [{"title": "Token Efficiency", "tags": ["performance"]}]
        results = vault.match_concepts(candidates)
        assert results[0]["disposition"] == "merge"
        assert results[0]["match_score"] >= 0.8

    def test_new_concept(self, vault: BrainVault):
        candidates = [{"title": "Completely Novel Concept", "tags": ["core"]}]
        results = vault.match_concepts(candidates)
        assert results[0]["disposition"] == "new"

    def test_ambiguous_match(self, vault: BrainVault):
        # "Token Efficiency Overview" contains "Token Efficiency" as a substring
        # which scores 0.6, plus word overlap should push into ambiguous range
        candidates = [{"title": "Token Efficiency Overview", "tags": ["architecture", "performance"]}]
        results = vault.match_concepts(candidates)
        # Substring containment (0.6) + tag overlap should push into ambiguous/merge
        assert results[0]["disposition"] in ("merge", "ambiguous")

    def test_tag_warnings(self, vault: BrainVault):
        candidates = [{"title": "Test", "tags": ["invalid_tag_xyz"]}]
        results = vault.match_concepts(candidates)
        assert "tag_warnings" in results[0]
        assert "invalid_tag_xyz" in results[0]["tag_warnings"]

    def test_no_tag_warnings_for_valid_tags(self, vault: BrainVault):
        candidates = [{"title": "Test", "tags": ["core", "architecture"]}]
        results = vault.match_concepts(candidates)
        assert "tag_warnings" not in results[0] or results[0]["tag_warnings"] == []

    def test_matched_entity_info(self, vault: BrainVault):
        candidates = [{"title": "Token Efficiency", "tags": ["performance"]}]
        results = vault.match_concepts(candidates)
        matched = results[0].get("matched_entity", {})
        assert matched.get("title") == "Token Efficiency"
        assert "path" in matched


# ---------------------------------------------------------------------------
# Integration tests: synthesize
# ---------------------------------------------------------------------------


class TestSynthesize:
    def test_synthesize_new(self, vault: BrainVault, vault_dir: Path):
        candidates = [{
            "title": "Brand New Concept",
            "disposition": "new",
            "tags": ["core", "architecture"],
            "claims": [
                "This is a brand new concept for testing",
                "It has multiple claims to verify synthesis",
            ],
            "relationships": ["Token Efficiency", "Architecture Overview"],
            "type": "concept",
        }]
        results = vault.synthesize(candidates)
        assert results[0]["action"] == "created"
        assert results[0]["claims_count"] == 2

        # Verify the file was actually created
        path = vault_dir / results[0]["path"]
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        assert fm["title"] == "Brand New Concept"
        assert "guid" in fm
        assert "brand new concept" in body.lower()
        assert "[[Token Efficiency]]" in body or "[[Architecture Overview]]" in body

    def test_synthesize_merge(self, vault: BrainVault, vault_dir: Path):
        candidates = [{
            "title": "Token Efficiency",
            "disposition": "merge",
            "tags": ["mcp"],  # new tag to add
            "claims": [
                "Synthesis merging adds novel claims to existing entities",
            ],
            "relationships": [],
            "matched_entity": {
                "path": "concept/Token Efficiency.md",
            },
        }]
        results = vault.synthesize(candidates)
        assert results[0]["action"] == "merged"
        assert results[0]["novel_claims"] == 1
        assert "mcp" in results[0]["added_tags"]

        # Verify the file was updated
        text = (vault_dir / "concept" / "Token Efficiency.md").read_text(encoding="utf-8")
        assert "Synthesis merging adds novel claims" in text
        fm, _ = _parse_frontmatter(text)
        assert "mcp" in fm["tags"]

    def test_synthesize_merge_deduplication(self, vault: BrainVault):
        candidates = [{
            "title": "Token Efficiency",
            "disposition": "merge",
            "tags": [],
            "claims": [
                # This claim's significant words overlap with existing body content
                "L0 bootstrap costs approximately 300 tokens via get_brief",
            ],
            "relationships": [],
            "matched_entity": {
                "path": "concept/Token Efficiency.md",
            },
        }]
        results = vault.synthesize(candidates)
        assert results[0]["action"] == "merged"
        assert results[0]["duplicate_claims"] >= 1

    def test_synthesize_ambiguous_skipped(self, vault: BrainVault):
        candidates = [{
            "title": "Ambiguous Thing",
            "disposition": "ambiguous",
            "tags": ["core"],
            "claims": ["A claim"],
        }]
        results = vault.synthesize(candidates)
        assert results[0]["action"] == "skipped"

    def test_synthesize_invalid_tags_dropped(self, vault: BrainVault, vault_dir: Path):
        candidates = [{
            "title": "Tag Test Entity",
            "disposition": "new",
            "tags": ["core", "totally_invalid_tag"],
            "claims": ["Testing tag validation"],
            "relationships": [],
            "type": "concept",
        }]
        results = vault.synthesize(candidates)
        assert results[0]["action"] == "created"
        assert any("totally_invalid_tag" in w for w in results[0].get("tag_warnings", []))

        # Verify invalid tag was not persisted
        text = (vault_dir / "concept" / "Tag Test Entity.md").read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(text)
        assert "totally_invalid_tag" not in fm["tags"]
        assert "core" in fm["tags"]

    def test_synthesize_duplicate_file_error(self, vault: BrainVault):
        # Token Efficiency already exists
        candidates = [{
            "title": "Token Efficiency",
            "disposition": "new",
            "tags": ["core"],
            "claims": ["Duplicate"],
            "relationships": [],
            "type": "concept",
        }]
        results = vault.synthesize(candidates)
        assert results[0]["action"] == "error"
        assert "already exists" in results[0]["reason"]


# ---------------------------------------------------------------------------
# Integration tests: get_relevant_context
# ---------------------------------------------------------------------------


class TestGetRelevantContext:
    def test_basic_aggregation(self, vault: BrainVault):
        result = vault.get_relevant_context("token efficiency performance")
        assert result["entity_count"] > 0
        assert "entities" in result
        assert "decisions" in result
        assert "drift" in result
        assert "coverage_gaps" in result

    def test_includes_related_synopses(self, vault: BrainVault):
        result = vault.get_relevant_context("architecture overview")
        # Architecture Overview links to Token Efficiency and Storage Layer
        assert "related" in result

    def test_type_filter(self, vault: BrainVault):
        result = vault.get_relevant_context(
            "architecture", include_types=["decision"]
        )
        # Should prioritize decisions
        assert result["entity_count"] > 0


# ---------------------------------------------------------------------------
# Integration tests: validate_action
# ---------------------------------------------------------------------------


class TestValidateAction:
    def test_no_conflict(self, vault: BrainVault):
        result = vault.validate_action(
            action="Add a completely novel feature with no vault precedent",
            rationale="Because it is brand new",
        )
        assert result["status"] in ("proceed", "review")

    def test_returns_structure(self, vault: BrainVault):
        result = vault.validate_action(
            action="Change YAML format for data storage",
            rationale="Exploring alternatives",
        )
        assert "status" in result
        assert "message" in result
        assert "conflicts" in result
        assert "applicable_rules" in result
        assert "supporting" in result
        assert "informational" in result

    def test_surfaces_applicable_rules(self, vault: BrainVault):
        result = vault.validate_action(
            action="Edit vault entity files directly using read_file and replace_string",
            rationale="Need to update entity body content",
        )
        assert "applicable_rules" in result
        rule_titles = [r.get("title", r.get("id", "")) for r in result["applicable_rules"]]
        assert any("Vault Access" in t for t in rule_titles)


# ---------------------------------------------------------------------------
# Integration tests: update_memory
# ---------------------------------------------------------------------------


class TestUpdateMemory:
    def test_update_status(self, vault: BrainVault, vault_dir: Path):
        result = vault.update_memory("Open Question", {"status": "resolved"})
        assert result["updated"] == "Open Question"
        assert "status" in result["fields"]

        # Verify the file was updated
        entity = vault.read_entity("Open Question")
        assert entity["frontmatter"]["status"] == "resolved"

    def test_update_tags(self, vault: BrainVault):
        result = vault.update_memory("Test Goal One", {"tags": ["core", "performance"]})
        assert result["updated"] == "Test Goal One"

        entity = vault.read_entity("Test Goal One")
        assert "performance" in entity["frontmatter"]["tags"]

    def test_update_nonexistent_entity(self, vault: BrainVault):
        with pytest.raises(FileNotFoundError):
            vault.update_memory("Nonexistent", {"status": "active"})

    def test_update_warns_on_broken_link(self, vault: BrainVault):
        result = vault.update_memory("Test Goal One", {
            "serves": ["[[Nonexistent Target]]"],
        })
        assert any("not found" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Integration tests: update_body
# ---------------------------------------------------------------------------


class TestUpdateBody:
    def test_create_new_section(self, vault: BrainVault, vault_dir: Path):
        result = vault.update_body(
            "Token Efficiency",
            section="Applicable Rules",
            content="- [[Vault Access Via MCP Only]]",
        )
        assert result["updated"] == "Token Efficiency"
        assert result["section"] == "Applicable Rules"
        assert result["action"] == "created"

        # Verify section was inserted before ## Related
        text = (vault_dir / "concept" / "Token Efficiency.md").read_text(encoding="utf-8")
        rules_pos = text.index("## Applicable Rules")
        related_pos = text.index("## Related")
        assert rules_pos < related_pos

    def test_replace_existing_section(self, vault: BrainVault, vault_dir: Path):
        # First create the section
        vault.update_body(
            "Token Efficiency",
            section="Applicable Rules",
            content="- [[Vault Access Via MCP Only]]",
        )
        # Then replace it
        result = vault.update_body(
            "Token Efficiency",
            section="Applicable Rules",
            content="- [[Vault Access Via MCP Only]]\n- [[Storage Layer]]",
        )
        assert result["action"] == "replaced"

        text = (vault_dir / "concept" / "Token Efficiency.md").read_text(encoding="utf-8")
        assert "[[Storage Layer]]" in text
        # Only one ## Applicable Rules heading should exist
        assert text.count("## Applicable Rules") == 1

    def test_create_section_no_related(self, vault: BrainVault, vault_dir: Path):
        # Test Goal One has no ## Related section
        result = vault.update_body(
            "Test Goal One",
            section="Notes",
            content="Some additional notes here.",
        )
        assert result["action"] == "created"

        text = (vault_dir / "goal" / "Test Goal One.md").read_text(encoding="utf-8")
        assert "## Notes" in text
        assert "Some additional notes here." in text

    def test_nonexistent_entity(self, vault: BrainVault):
        with pytest.raises(FileNotFoundError):
            vault.update_body("Nonexistent", section="Test", content="test")

    def test_validates_links(self, vault: BrainVault):
        result = vault.update_body(
            "Token Efficiency",
            section="Applicable Rules",
            content="- [[Nonexistent Rule Entity]]",
        )
        assert any("not found" in w for w in result["warnings"])

    def test_preserves_other_sections(self, vault: BrainVault, vault_dir: Path):
        result = vault.update_body(
            "Storage Layer",
            section="Applicable Rules",
            content="- [[Vault Access Via MCP Only]]",
        )
        assert result["action"] == "created"

        text = (vault_dir / "system" / "Storage Layer.md").read_text(encoding="utf-8")
        # Original sections should still be present
        assert "## Intent" in text
        assert "## Related" in text
        assert "## Applicable Rules" in text
        assert "File I/O and YAML parsing" in text


# ---------------------------------------------------------------------------
# Unit tests: archive_entity
# ---------------------------------------------------------------------------


class TestArchiveEntity:
    def test_archive_moves_file(self, vault: BrainVault, vault_dir: Path):
        result = vault.archive_entity("YAML For Data")
        assert result["archived"] == "YAML For Data"
        assert Path(result["from"]) == Path("decision/YAML For Data.md")
        assert Path(result["to"]) == Path(".trash/decision/YAML For Data.md")

        # Original file should be gone
        assert not (vault_dir / "decision" / "YAML For Data.md").exists()
        # Archived file should exist
        assert (vault_dir / ".trash" / "decision" / "YAML For Data.md").exists()

    def test_archived_entity_excluded_from_search(self, vault: BrainVault):
        vault.archive_entity("YAML For Data")
        results = vault.search("YAML")
        titles = [r["title"] for r in results]
        assert "YAML for data, markdown for prose" not in titles

    def test_archived_entity_excluded_from_query(self, vault: BrainVault):
        vault.archive_entity("YAML For Data")
        results = vault.query(entity_type="decision")
        ids = [r["id"] for r in results]
        assert "YAML For Data" not in ids

    def test_reports_incoming_links(self, vault: BrainVault):
        # Architecture Overview links to Storage Layer
        result = vault.archive_entity("Storage Layer")
        assert result["incoming_link_count"] > 0
        assert any("Architecture Overview" in link for link in result["incoming_links"])

    def test_nonexistent_entity(self, vault: BrainVault):
        with pytest.raises(FileNotFoundError):
            vault.archive_entity("Nonexistent Entity")

    def test_archive_creates_trash_structure(self, vault: BrainVault, vault_dir: Path):
        vault.archive_entity("Open Question")
        assert (vault_dir / ".trash" / "drift" / "Open Question.md").exists()

    def test_duplicate_archive_raises(self, vault: BrainVault):
        vault.archive_entity("YAML For Data")
        # Can't archive again — already in trash
        with pytest.raises(FileNotFoundError):
            vault.archive_entity("YAML For Data")

    def test_search_include_archived(self, vault: BrainVault):
        vault.archive_entity("YAML For Data")
        # Default: excluded
        results = vault.search("YAML")
        assert not any(r["title"] == "YAML for data, markdown for prose" for r in results)
        # With include_archived: found, marked as archived
        results = vault.search("YAML", include_archived=True)
        archived = [r for r in results if r["title"] == "YAML for data, markdown for prose"]
        assert len(archived) == 1
        assert archived[0]["archived"] is True

    def test_query_include_archived(self, vault: BrainVault):
        vault.archive_entity("YAML For Data")
        # Default: excluded
        results = vault.query(entity_type="decision")
        assert not any(r["id"] == "YAML For Data" for r in results)
        # With include_archived: found, marked as archived
        results = vault.query(entity_type="decision", include_archived=True)
        archived = [r for r in results if r["id"] == "YAML For Data"]
        assert len(archived) == 1
        assert archived[0]["archived"] is True

    def test_get_context_include_archived(self, vault: BrainVault):
        vault.archive_entity("YAML For Data")
        # Default: not found
        with pytest.raises(FileNotFoundError):
            vault.get_context("YAML For Data")
        # With include_archived: found
        ctx = vault.get_context("YAML For Data", include_archived=True)
        assert ctx["id"] == "YAML For Data"
        assert ".trash" in ctx["path"]

    def test_active_entities_not_marked_archived(self, vault: BrainVault):
        """Active entities should not have the archived flag in query/search results."""
        results = vault.query(entity_type="concept")
        for r in results:
            assert "archived" not in r
        results = vault.search("Token")
        for r in results:
            assert "archived" not in r
