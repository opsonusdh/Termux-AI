# Orion Skill System

This directory is the authoritative instruction/skill system for Termux-AI. The system prompt is intentionally small: this index routes work, and the selected skills contain the detailed rules.

## Precedence

1. Current user request and runtime safety constraints.
2. Mandatory core skill(s) selected below.
3. Matching imported domain skill(s).
4. Inference and remembered context are never stronger than current files or commands.

A skill is active only after its current `SKILL.md` has been read with the file tool in this session. A mention of a skill path does not count as a read.

## Mandatory core routing

| Task surface | Read first | Add when relevant |
|---|---|---|
| Every task | `core/bootstrap` | `core/communication` |
| Coding/editing/refactoring/new tools | `core/coding` | `core/verification`, `core/reasoning` for diagnosis, `core/tool_efficiency` for bulk work |
| Runtime/import/tool failure | `core/troubleshooting` | `core/reasoning`, `core/verification` |
| Secrets/private data/external side effects | `core/security_and_privacy` | `core/capabilities` |
| History/context/continuation | `core/memory_and_context` | `core/reasoning` |
| Delegation/Supervisor/Worker/Critic/IPC | `core/orchestration_workflows` | `core/verification` |
| Git/release/config/template maintenance | `core/release_and_maintenance` | `core/verification`, `core/security_and_privacy` |
| Platform wrappers/packages/cross-platform behavior | `core/environment_and_tools` | `core/security_and_privacy` |
| Decision/ambiguity/autonomy | `core/decision_making` | `core/reasoning` |
| User-facing reports/style | `core/communication` | selected domain skill |
| WhatsApp/SMS/notifications | `core/whatsapp_and_notifications` | `core/security_and_privacy`, `core/capabilities` |
| External/integrated capability question | `core/capabilities` | selected domain skill |

## Hard execution gate

For a source-changing task, do not call the first mutating tool until the required core skills have been loaded in the current session — `skill("coding")` returns the manual itself, and reading the `SKILL.md` file counts too. After any change, load `core/verification` and perform the required checks.

## Imported domain skills

The imported skills are domain modules, loaded on demand through the `skill`
tool. The prompt lists only the **fifteen core skills**, because forty-seven
descriptions in every request is a price nobody should pay for a catalogue that
is mostly irrelevant.

**Nine of the original thirty-two stay in the registry** — the document formats,
the design guidance, the writing workflows and the skill-authoring manual. The
other twenty-three described procedures this runtime cannot carry out: p5.js
art, claude.ai artifacts, Anthropic brand work, expense reimbursements for
employers this assistant has never heard of, files that arrive at `/mnt/uploads`.
They are no longer advertised and cannot be called by accident.

**Their files stay on disk** — nothing was deleted. A skill that becomes
relevant again only has to be added back to `REGISTERED_IMPORTED_SKILLS` in
`core/skill_registry.py`. Repository compatibility is bound by
`core/capabilities`, safety by `core/security_and_privacy`, and
execution/verification by the core skills.


- `doc-coauthoring` → `skills/imported/doc-coauthoring/SKILL.md` — document/artifact; use when trigger matches.
- `docx` → `skills/imported/docx/SKILL.md` — document/artifact; use when trigger matches.
- `frontend-design` → `skills/imported/frontend-design/SKILL.md` — domain; use when trigger matches.
- `internal-comms` → `skills/imported/internal-comms/SKILL.md` — domain; use when trigger matches.
- `pdf` → `skills/imported/pdf/SKILL.md` — document/artifact; use when trigger matches.
- `pdf-reading` → `skills/imported/pdf-reading/SKILL.md` — document/artifact; use when trigger matches.
- `pptx` → `skills/imported/pptx/SKILL.md` — document/artifact; use when trigger matches.
- `skill-creator` → `skills/imported/skill-creator/SKILL.md` — domain; use when trigger matches.
- `xlsx` → `skills/imported/xlsx/SKILL.md` — document/artifact; use when trigger matches.

## Selection rules

1. Match the task against the skill description/trigger; do not load unrelated skills.
2. Read every selected skill before execution.
3. Multiple skills are allowed; unify them into one plan and one verification chain.
4. Domain skills never override core safety, permission, evidence, or verification rules.
5. When a skill names a capability the runtime does not expose, use `core/capabilities` and adapt rather than invent.
6. Keep detailed policies in skills; keep the system prompt as a compact routing and gating layer.
