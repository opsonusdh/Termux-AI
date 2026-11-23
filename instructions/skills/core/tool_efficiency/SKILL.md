---
name: tool_efficiency
description: Tool & Context Efficiency. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Tool & Context Efficiency

Retrieve the minimum information necessary to complete the task accurately, using the smallest number of reliable operations. (Full manual: `instructions/context_efficiency.md`.)

**Decision rule:** **Inspect → Search → Filter → Read selectively → Analyze → Verify → Act.**
Before any substantial read or tool call, ask: *"Do I need all of this information, or can I retrieve only the part that answers my current question?"*

## Inspect & search before reading
- Check file size, type, encoding, and binary/compressed status before opening. Never dump huge logs or plain-text-read binaries.
- Search first (`search_in_files`, `grep`/`rg`, `search_files`, `jq`, `yq`, AST/DB queries) instead of reading whole files or repos.
- Read selectively (`head`, `tail`, `read_file` with `segment_start`/`segment_end`, PDF page ranges, spreadsheet sheet/range) and expand only when context is insufficient.

## Large files & file types
- Chunk large files/logs, summarize key technical findings (errors, timestamps, symbols, schemas), and discard irrelevant raw text.
- Match tool to format: structured queries for JSON/YAML/XML, archive member listings before extracting, specialized tools for binaries/images/documents.

## Before action & minimal edits
- Batch related reads/searches before edits; read each target file (or relevant section) in the current session before modifying it.
- Make minimal changes: smallest relevant patch, preserve architecture, avoid unrelated refactoring; if already similar files exist, edit them wisely instead of creating redundant copies.

## Bulk work & probes
- For the same mechanical transformation appearing more than three times, use one script or batch operation, then verify counts and diffs.
- Prefer cheap factual probes (`grep`, `python3 -c`, `which`, `git status --short`) over speculative reasoning.

## Context & output control
- Keep tool output concise: filter results, limit match lines, use shallow directory listings, and extract only needed fields.
- Avoid re-reading unchanged files: reuse retrieved findings; check `git diff`, timestamps, or hashes before re-reading.
- Keep working context compact; retain constraints, paths, symbol relationships, errors, decisions, test results, and open issues.

## Verify & failure handling
- Verify surrounding context, dependencies, truncation status, and targeted tests before concluding.
- Do not repeat an identical failed tool call unless the inputs or environment have materially changed.
