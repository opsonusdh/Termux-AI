"""core/skill_registry.py — the skill catalogue and the ``skill`` tool.

The skill system used to be a ritual: the model read
``instructions/skills/INDEX.md`` to find out which manual applied, then read
one to three ``SKILL.md`` files before it could touch anything.  That cost a
file read per task and put forty-seven descriptions in front of the model
every single turn.

The registry keeps the discipline and drops the ritual:

* the **prompt** carries the fifteen core skills — name and one line each,
  because those are the ones a task routes through;
* the **rest** (the imported domain skills) are reachable on demand through
  the ``skill`` tool, so they cost nothing until a task actually needs one;
* the **body** of a skill is re-read from disk on every call, so an edited
  ``SKILL.md`` is visible on the next call instead of the next restart.

The catalogue is assembled from the files themselves — every ``SKILL.md``
already carries ``name`` and ``description`` in its frontmatter — so it
cannot drift away from what is on disk.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass

import paths

SKILLS_DIR = os.path.join(paths.INSTRUCTIONS_DIR, "skills")
CORE_SKILLS_DIR = os.path.join(SKILLS_DIR, "core")
IMPORTED_SKILLS_DIR = os.path.join(SKILLS_DIR, "imported")

#: The imported skills that stay in the registry.
#:
#: The imported set was a drop-in copy of an Anthropic-ecosystem bundle:
#: p5.js art, claude.ai HTML artifacts, Anthropic brand guidelines, Slack
#: GIFs, an MCP builder, expense-reimbursement flows for employers this
#: runtime has never heard of, and procedures that assume files arrive at
#: ``/mnt/uploads``.  None of that is reachable from a Termux terminal, and
#: every one of them was a candidate the model had to guess between.
#:
#: What survives is the part that describes work this runtime can actually
#: do: the document formats, the design guidance, the writing workflows and
#: the skill-authoring manual.  The files of the other twenty-three stay on
#: disk — nothing is deleted — but they are no longer advertised, and the
#: model cannot call them by accident.
REGISTERED_IMPORTED_SKILLS: tuple[str, ...] = (
    "docx",
    "pdf",
    "pdf-reading",
    "xlsx",
    "pptx",
    "frontend-design",
    "doc-coauthoring",
    "internal-comms",
    "skill-creator",
)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$")
_DESC_RE = re.compile(r"^description:\s*(.*)$")
_BLOCK_MARKERS = {"|", ">", "|-", ">-", "|+", ">+", "|"}


@dataclass(frozen=True)
class SkillSummary:
    """One skill as the catalogue sees it."""

    name: str
    description: str
    path: str          # relative to the project root
    group: str         # "core" | "imported"

    def catalog_line(self) -> str:
        """One line for the prompt block."""
        description = " ".join(self.description.split())
        return f"- {self.name} — {description}"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into (frontmatter mapping, body).

    The frontmatter is the small YAML subset the skills actually use: a
    ``key: value`` line, or a ``key: |`` block whose value is the indented
    lines below it.  A full YAML parser is not needed for that, and not
    having one keeps the runtime free of a dependency it does not need.
    """
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}, text or ""

    meta: dict = {}
    lines = match.group(1).splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if value in _BLOCK_MARKERS:
            block: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt.strip())
                index += 1
            meta[key] = " ".join(part for part in block if part).strip()
        else:
            meta[key] = _strip_quotes(value)

    return meta, (text[match.end():] or "")


def _read_skill_file(
    directory: str,
    name: str,
) -> tuple[SkillSummary, str, dict] | None:
    """Load one skill from *directory*; None when it is not there."""
    skill_dir = os.path.join(directory, name)
    skill_file = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_file):
        return None
    try:
        with open(skill_file, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None

    meta, body = _parse_frontmatter(text)
    group = "core" if os.path.abspath(directory) == os.path.abspath(CORE_SKILLS_DIR) else "imported"
    summary = SkillSummary(
        name=name,
        description=str(meta.get("description", "")).strip(),
        path=os.path.relpath(skill_file, paths.ROOT),
        group=group,
    )
    return summary, body, meta


class SkillRegistry:
    """The catalogue of the skills this runtime offers."""

    def __init__(self) -> None:
        self._catalog: list[SkillSummary] | None = None
        self._stamp: tuple = ()

    # ── discovery ──────────────────────────────────────────────────────

    def _dir_stamp(self) -> tuple:
        stamp = []
        for directory in (CORE_SKILLS_DIR, IMPORTED_SKILLS_DIR):
            try:
                stamp.append((directory, os.path.getmtime(directory)))
            except OSError:
                stamp.append((directory, 0.0))
        return tuple(stamp)

    def _scan(self) -> list[SkillSummary]:
        found: list[SkillSummary] = []

        try:
            core_names = sorted(
                entry for entry in os.listdir(CORE_SKILLS_DIR)
                if os.path.isfile(os.path.join(CORE_SKILLS_DIR, entry, "SKILL.md"))
            )
        except OSError:
            core_names = []

        for name in core_names:
            loaded = _read_skill_file(CORE_SKILLS_DIR, name)
            if loaded:
                found.append(loaded[0])

        for name in REGISTERED_IMPORTED_SKILLS:
            loaded = _read_skill_file(IMPORTED_SKILLS_DIR, name)
            if loaded:
                found.append(loaded[0])

        return found

    def catalog(self) -> list[SkillSummary]:
        """Every registered skill — core first, then the imported ones."""
        stamp = self._dir_stamp()
        if self._catalog is None or stamp != self._stamp:
            self._catalog = self._scan()
            self._stamp = stamp
        return list(self._catalog)

    def core_catalog(self) -> list[SkillSummary]:
        """The core skills — the ones the prompt lists."""
        return [skill for skill in self.catalog() if skill.group == "core"]

    def imported_catalog(self) -> list[SkillSummary]:
        """The imported skills that stay in the registry."""
        return [skill for skill in self.catalog() if skill.group == "imported"]

    def invalidate(self) -> None:
        """Forget the cached catalogue; the next call rescans."""
        self._catalog = None
        self._stamp = ()

    # ── lookup ─────────────────────────────────────────────────────────

    def find(self, name: str) -> SkillSummary | None:
        """The skill called *name*, or None.

        Both the directory name (``coding``) and the frontmatter name
        (``core-bootstrap``) are accepted, and so is the qualified form
        (``core/coding``).
        """
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None
        wanted = wanted.rstrip("/")
        short = wanted.split("/")[-1]

        for skill in self.catalog():
            if skill.name.lower() in (wanted, short):
                return skill

        # The frontmatter name is an alias: "core-bootstrap" finds the
        # bootstrap skill, whose directory is "bootstrap".
        for skill in self.catalog():
            loaded = _read_skill_file(
                CORE_SKILLS_DIR if skill.group == "core" else IMPORTED_SKILLS_DIR,
                skill.name,
            )
            if not loaded:
                continue
            _summary, _body, meta = loaded
            if str(meta.get("name", "")).strip().lower() == wanted:
                return skill
        return None

    def body(self, name: str) -> str | None:
        """The text of the skill called *name*, frontmatter removed.

        The body is read from disk on every call: a skill that was edited
        a minute ago must be visible now, not after the next restart.
        """
        skill = self.find(name)
        if skill is None:
            return None
        directory = (
            CORE_SKILLS_DIR if skill.group == "core" else IMPORTED_SKILLS_DIR
        )
        loaded = _read_skill_file(directory, skill.name)
        if loaded is None:
            return None
        return loaded[1].strip()

    # ── rendering ──────────────────────────────────────────────────────

    def prompt_block(self) -> str:
        """The compact catalogue block that goes into the prompt.

        Only the core skills are listed.  The imported ones are one
        ``skill("")`` call away, and forty-seven lines of descriptions in
        every request is a price nobody should pay for a catalogue that is
        mostly irrelevant.
        """
        lines = [skill.catalog_line() for skill in self.core_catalog()]
        return "\n".join(lines)

    def catalog_text(self) -> str:
        """The full catalogue, as text — what ``skill("")`` returns."""
        sections = []
        for group, title in (("core", "Core skills"), ("imported", "Imported skills")):
            skills = [s for s in self.catalog() if s.group == group]
            if not skills:
                continue
            body = "\n".join(skill.catalog_line() for skill in skills)
            sections.append(f"{title}:\n{body}")
        return "\n\n".join(sections)


#: The process-wide registry.  One catalogue, one place to invalidate.
registry = SkillRegistry()


def catalog() -> list[SkillSummary]:
    """Every registered skill."""
    return registry.catalog()


def core_catalog() -> list[SkillSummary]:
    """The core skills."""
    return registry.core_catalog()


def imported_catalog() -> list[SkillSummary]:
    """The imported skills that stay in the registry."""
    return registry.imported_catalog()


def body(name: str) -> str | None:
    """The text of one skill, or None when there is no such skill."""
    return registry.body(name)


def prompt_block() -> str:
    """The compact core-skill catalogue for the prompt."""
    return registry.prompt_block()


def catalog_text() -> str:
    """The full catalogue, as text."""
    return registry.catalog_text()


def skill_text(name: str) -> str:
    """What the ``skill`` tool returns.

    An empty name (or ``"catalog"`` / ``"list"``) returns the catalogue;
    a known name returns the skill body; an unknown name returns an error
    that names the closest matches instead of a bare failure.
    """
    wanted = str(name or "").strip()
    if not wanted or wanted.lower() in ("catalog", "list", "?"):
        return (
            "Registered skills (call skill(name) to load one):\n\n"
            + catalog_text()
        )

    text = body(wanted)
    if text is not None:
        return text

    known = [skill.name for skill in catalog()]
    close = difflib.get_close_matches(wanted.lower(), known, n=5, cutoff=0.4)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return (
        f"[ERROR] No skill named '{wanted}'.{hint}\n"
        f"Known skills: {', '.join(known)}"
    )
