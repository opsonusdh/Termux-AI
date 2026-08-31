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

For a source-changing task, do not call the first mutating tool until the required core `SKILL.md` files have been read in the current session. After any change, read `core/verification` and perform the required checks.

## Imported domain skills

The 32 supplied skills below are preserved as imported domain modules. Their useful procedures are retained, but repository compatibility is bound by `core/capabilities`, safety by `core/security_and_privacy`, and execution/verification by the core skills.


- `algorithmic-art` → `skills/imported/algorithmic-art/SKILL.md` — domain; use when trigger matches.
- `benepass-reimbursement` → `skills/imported/benepass-reimbursement/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `brand-guidelines` → `skills/imported/brand-guidelines/SKILL.md` — domain; use when trigger matches.
- `call-to-book` → `skills/imported/call-to-book/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `cancel-unsubscribe` → `skills/imported/cancel-unsubscribe/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `canvas-design` → `skills/imported/canvas-design/SKILL.md` — domain; use when trigger matches.
- `doc-coauthoring` → `skills/imported/doc-coauthoring/SKILL.md` — document/artifact; use when trigger matches.
- `docx` → `skills/imported/docx/SKILL.md` — document/artifact; use when trigger matches.
- `event-planning` → `skills/imported/event-planning/SKILL.md` — domain; use when trigger matches.
- `file-expenses` → `skills/imported/file-expenses/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `file-form` → `skills/imported/file-form/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `file-reading` → `skills/imported/file-reading/SKILL.md` — document/artifact; use when trigger matches.
- `financial-calculator` → `skills/imported/financial-calculator/SKILL.md` — domain; use when trigger matches.
- `frontend-design` → `skills/imported/frontend-design/SKILL.md` — domain; use when trigger matches.
- `grocery-shopping` → `skills/imported/grocery-shopping/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `hire-help` → `skills/imported/hire-help/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `internal-comms` → `skills/imported/internal-comms/SKILL.md` — domain; use when trigger matches.
- `learn` → `skills/imported/learn/SKILL.md` — domain; use when trigger matches.
- `mcp-builder` → `skills/imported/mcp-builder/SKILL.md` — domain; use when trigger matches.
- `meal-delivery` → `skills/imported/meal-delivery/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `pdf` → `skills/imported/pdf/SKILL.md` — document/artifact; use when trigger matches.
- `pdf-reading` → `skills/imported/pdf-reading/SKILL.md` — document/artifact; use when trigger matches.
- `pptx` → `skills/imported/pptx/SKILL.md` — document/artifact; use when trigger matches.
- `prescription-refill` → `skills/imported/prescription-refill/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `product-self-knowledge` → `skills/imported/product-self-knowledge/SKILL.md` — domain; use when trigger matches.
- `return-refund` → `skills/imported/return-refund/SKILL.md` — external workflow; workflow-only unless matching runtime capability exists.
- `setup-writing-style` → `skills/imported/setup-writing-style/SKILL.md` — domain; use when trigger matches.
- `skill-creator` → `skills/imported/skill-creator/SKILL.md` — domain; use when trigger matches.
- `slack-gif-creator` → `skills/imported/slack-gif-creator/SKILL.md` — domain; use when trigger matches.
- `theme-factory` → `skills/imported/theme-factory/SKILL.md` — domain; use when trigger matches.
- `web-artifacts-builder` → `skills/imported/web-artifacts-builder/SKILL.md` — document/artifact; use when trigger matches.
- `xlsx` → `skills/imported/xlsx/SKILL.md` — document/artifact; use when trigger matches.

## Selection rules

1. Match the task against the skill description/trigger; do not load unrelated skills.
2. Read every selected skill before execution.
3. Multiple skills are allowed; unify them into one plan and one verification chain.
4. Domain skills never override core safety, permission, evidence, or verification rules.
5. When a skill names a capability the runtime does not expose, use `core/capabilities` and adapt rather than invent.
6. Keep detailed policies in skills; keep the system prompt as a compact routing and gating layer.
