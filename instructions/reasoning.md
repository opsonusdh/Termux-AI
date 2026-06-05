# Reasoning & Analytical Problem-Solving

This document defines the cognitive and operational framework Orion uses to execute tasks, resolve errors, and maintain architectural consistency under Termux constraints.

---

## 1. The Reasoning Lifecycle

For every non-trivial task, follow this sequential process:

```
[ Clarify Intent ] → [ Decompose ] → [ Track (workspace/reasoning_tmp.txt) ]
                                                    │
[ Report ] ← [ Verify Result ] ← [ Execute ] ← [ Investigate ]
```

### Step 1 — Clarify Intent
- Deconstruct the request: identify the implicit requirement, not just the literal instruction.
- Determine which modules, files, and data flows are affected.
- If the request is clear and non-destructive, proceed autonomously. Ask only for explicit authorization on destructive changes outside `~/Termux-AI`.

### Step 2 — Decompose & Plan
- Break the goal into discrete, atomic, testable milestones.
- Identify inter-task dependencies before starting (e.g., a new import path cannot be used until `paths.py` is updated).

### Step 3 — Track Execution
- Create or update `~/Termux-AI/workspace/reasoning_tmp.txt`.
- Use a strict markdown checklist:

```markdown
# Current Task: [Objective]

## To-Do:
- [x] Inspect current state of target files
- [/] Refactor paths.py constants
- [ ] Update importing modules
- [ ] Write verification test

## Discoveries:
- paths.CHUNKS_FILE resolves to logs/chunks.jsonl
- context_manager._next_id is recovered from max(parent_ids)+1 on startup
```

### Step 4 — Programmatic Investigation
- **Read first.** Never assume file contents, directory layout, or variable values. Use `read_file`, `run_code`, or `index_files` to inspect before modifying.
- **Run probes.** Use Python one-liners or short bash commands to check package availability, path resolution, and permission states.
- **No hallucination.** If you have not seen it in the actual source, do not claim it exists.

### Step 5 — Iterative Execution
- Work through the checklist step by step.
- Keep changes isolated — avoid touching multiple unrelated modules in a single step.
- After each change, re-read the affected file to confirm the edit applied correctly.

### Step 6 — Empirical Verification
- Run syntax checks (`python3 -c "import ast; ast.parse(...)"`) after every file edit.
- Run integration tests that assert the expected behaviour end-to-end.
- Check return codes, print proof of correctness, and confirm no orphaned processes.

### Step 7 — Structured Report
- State what was done, why it was done, and what was verified.
- Include command output or assertion results as proof.
- Do not pad with apologies, generic warnings, or unnecessary caveats.

---

## 2. State Tracking File (`workspace/reasoning_tmp.txt`)

This file is Orion's working memory for the active operation. It is gitignored and expendable — rewrite it freely.

```markdown
# Current Task: Refactor context_manager to use _chunk_index

## To-Do:
- [x] Read current context_manager.py
- [x] Identify _chunks linear scan in retrieve_chunk()
- [/] Replace with _chunk_index dict
- [ ] Update _load() to rebuild _chunk_index on startup
- [ ] Verify retrieve_chunk("3.1") returns correct subchunk
- [ ] Syntax check

## Discoveries:
- _chunks was a list — O(n) scan; now replaced with _chunk_index dict
- Subchunk IDs are strings e.g. "3.1"; parent IDs are ints
- _next_id must be recovered from max(int keys in _chunk_index) + 1
```

---

## 3. Scientific Troubleshooting Protocol

When an error occurs, work through this sequence — never guess:

1. **Locate** — read stderr and extract the exact file, line, and exception type.
2. **Isolate** — determine the cause category: logic error, wrong path, permission block, type mismatch, import collision, or concurrency issue.
3. **Hypothesise** — form one or two specific hypotheses (e.g. "The `import tools` hits `tools/` package instead of `core/tools.py` because root is first in sys.path").
4. **Test in isolation** — write a minimal reproducer in `workspace/` to confirm or refute the hypothesis before touching production code.
5. **Fix and document** — port the fix to production, remove the test script, and note the incident in `~/Termux-AI/logs/reflection.jsonl` if it caused an execution failure.

---

## 4. Common Pitfalls in This Codebase

| Symptom | Likely Cause | Fix |
|---|---|---|
| `import tools` gets `tools/__init__.py` | `sys.path` has root before `core/` | Ensure `core/` is `sys.path[0]` |
| `retrieve_chunk()` returns `{"error": ...}` for a valid chunk | Passing int where string expected (or vice versa) | `retrieve_chunk` accepts both — check the ID type being passed |
| Tool calls missing from raw chunk | `set_tool_context()` not called before `close_chunk()` | Verify `llm_client.py` calls `_cm.set_tool_context(messages[base_len:])` |
| Active context grows every turn | `history.append()` still present in chat loop | Remove chat-turn appends; `build_history()` serves full history |
| Agent recovery picks wrong task | `active_task_id` check missing | Recovery priority: `active_task_id` → `cursor` → first pending |
| State corrupt after restart | `_next_id` not recovered | On load: `_next_id = max(int parent IDs) + 1` |

---

## 5. Module Boundary Rules

These rules are absolute — violating them causes regressions that are hard to trace:

- **`ask_ai()` is untouchable.** No state injection, no wrapping, no return-type changes. Agent context belongs exclusively in `run_agent_step()`.
- **No `history.append()` for chat turns.** The session `history` list is for pre-loop one-time injections only. Conversation history comes from `context_manager.build_history()`.
- **Summarization only runs post-reply.** `maybe_summarize_async()` must be called after `close_chunk()`, never during tool execution or model inference.
- **`state_manager` is the only writer of `data/state.json`.** No other module may write to it directly.
- **`paths.py` is the only source of file paths.** Never construct a path with string literals or `os.path.join(os.getcwd(), ...)`.
