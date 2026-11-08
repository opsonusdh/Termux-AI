"""The skill catalogue: what is in it, what is in the prompt, what is loaded.

Three properties matter:

* the prompt carries **only** the core skills — the imported ones are one
  ``skill("")`` call away, and forty-seven descriptions in every request is
  a price nobody should pay for a catalogue that is mostly irrelevant;
* a body is read from disk on **every** call, so an edited ``SKILL.md`` is
  visible on the next call and not after the next restart;
* the pruned imported skills are gone from the catalogue but their files are
  still on disk — nothing was deleted.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import skill_registry  # noqa: E402


# ── what is registered ────────────────────────────────────────────────────────

def test_the_fifteen_core_skills_are_registered():
    core = {skill.name for skill in skill_registry.core_catalog()}
    assert len(core) == 15
    for expected in ("bootstrap", "coding", "verification", "reasoning",
                     "security_and_privacy", "tool_efficiency",
                     "whatsapp_and_notifications"):
        assert expected in core


def test_the_curated_imported_skills_are_registered():
    imported = {skill.name for skill in skill_registry.imported_catalog()}
    assert imported == set(skill_registry.REGISTERED_IMPORTED_SKILLS)
    assert "docx" in imported
    assert "skill-creator" in imported


def test_the_pruned_imported_skills_are_not_advertised():
    """They are gone from the catalogue — their files stay on disk."""
    imported = {skill.name for skill in skill_registry.imported_catalog()}
    for dropped in ("algorithmic-art", "brand-guidelines", "benepass-reimbursement",
                    "meal-delivery", "web-artifacts-builder", "mcp-builder"):
        assert dropped not in imported
        # Nothing was deleted: the file is still where it was.
        assert os.path.isfile(
            os.path.join(
                REPO_ROOT, "instructions", "skills", "imported", dropped, "SKILL.md"
            )
        )


# ── what reaches the prompt ───────────────────────────────────────────────────

def test_the_prompt_block_lists_only_the_core_skills():
    block = skill_registry.prompt_block()
    assert "coding" in block
    assert "verification" in block
    for imported in skill_registry.REGISTERED_IMPORTED_SKILLS:
        assert f"- {imported} " not in block


def test_the_prompt_block_is_one_line_per_skill():
    block = skill_registry.prompt_block()
    lines = [line for line in block.splitlines() if line.strip()]
    assert len(lines) == len(skill_registry.core_catalog())
    assert all(line.startswith("- ") for line in lines)


# ── what the tool returns ─────────────────────────────────────────────────────

def test_a_skill_body_is_the_manual_without_the_frontmatter():
    body = skill_registry.body("coding")
    assert body
    assert not body.lstrip().startswith("---")
    assert "Coding Standards" in body


def test_the_frontmatter_name_is_accepted_as_an_alias():
    assert skill_registry.body("core-bootstrap") is not None


def test_an_unknown_skill_names_the_closest_matches():
    text = skill_registry.skill_text("codng")
    assert text.startswith("[ERROR]")
    assert "coding" in text


def test_an_empty_name_returns_the_catalogue():
    text = skill_registry.skill_text("")
    assert "Core skills:" in text
    assert "Imported skills:" in text
    assert "coding" in text


# ── the body is never cached ──────────────────────────────────────────────────

def test_the_body_is_re_read_on_every_call(tmp_path, monkeypatch):
    """An edited skill is visible on the next call."""
    skills = tmp_path / "skills"
    core = skills / "core"
    imported = skills / "imported"
    (core / "demo").mkdir(parents=True)
    (imported).mkdir(parents=True)

    skill_file = core / "demo" / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo\ndescription: A demo skill.\n---\n\nFirst version.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(skill_registry, "SKILLS_DIR", str(skills))
    monkeypatch.setattr(skill_registry, "CORE_SKILLS_DIR", str(core))
    monkeypatch.setattr(skill_registry, "IMPORTED_SKILLS_DIR", str(imported))
    monkeypatch.setattr(skill_registry, "REGISTERED_IMPORTED_SKILLS", ())
    monkeypatch.setattr(skill_registry, "registry", skill_registry.SkillRegistry())

    assert "First version." in skill_registry.body("demo")

    skill_file.write_text(
        "---\nname: demo\ndescription: A demo skill.\n---\n\nSecond version.\n",
        encoding="utf-8",
    )

    body = skill_registry.body("demo")
    assert "Second version." in body
    assert "First version." not in body


# ── the frontmatter parser ────────────────────────────────────────────────────

def test_a_block_description_is_read_as_one_line():
    meta, body = skill_registry._parse_frontmatter(
        "---\nname: demo\ndescription: |\n  first line\n  second line\n---\n\nBody.\n"
    )
    assert meta["name"] == "demo"
    assert "first line" in meta["description"]
    assert "second line" in meta["description"]
    assert body.strip() == "Body."


def test_a_quoted_description_is_unquoted():
    meta, _body = skill_registry._parse_frontmatter(
        '---\nname: demo\ndescription: "A quoted description."\n---\n'
    )
    assert meta["description"] == "A quoted description."


def test_a_file_without_frontmatter_is_all_body():
    meta, body = skill_registry._parse_frontmatter("Just text.\n")
    assert meta == {}
    assert body.strip() == "Just text."
