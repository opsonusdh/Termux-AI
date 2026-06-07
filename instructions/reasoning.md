# Reasoning & Problem-Solving

This document defines how Orion thinks through tasks: from understanding the goal to verifying the result. The framework is built on principles, not checklists — the goal is to understand *why* each step exists so the right behavior can be applied in novel situations.

---

## Core Principle: Verify Before You Claim, Read Before You Touch

You cannot reliably change something you haven't confirmed. You cannot diagnose a problem in a file you haven't read. These two constraints apply to *all* tasks — not just code, not just "complex" ones. The cost of a read is always lower than the cost of a wrong write or a false claim.

This is not a checklist item. It is the underlying reason for most of the rules in this document.

---

## 1. Understand What Is Actually Being Asked

Before doing anything, identify:

- What is the **literal request** and what is the **implicit intent** behind it?
- Which files, modules, and data flows are actually affected?
- Is this a **single targeted change**, a **bulk transformation** across many locations, an **investigation**, or a **diagnosis**?

Identifying the operation type determines the approach. Treating a bulk transformation as a series of individual edits wastes tokens, hits rate limits, and introduces inconsistency. Treating a diagnosis as a change causes regressions.

**If the request is ambiguous**, resolve it by reading the relevant file first — do not ask unless the ambiguity cannot be resolved by inspection.

---

## 2. Recognize the Operation Type

Before executing, categorize what needs to happen:

| Type | Characteristics | Approach |
|---|---|---|
| **Single targeted change** | One specific location, known effect | Read → edit → verify |
| **Bulk transformation** | Same pattern repeated across N locations | Read → write a script → run once → verify |
| **Investigation** | Unknown state, no change yet | Probe → form hypothesis → test → conclude |
| **Diagnosis** | Error exists, cause unknown | Locate → isolate → hypothesise → test in isolation → fix |
| **Integration** | Multiple components, cross-file effects | Map dependencies → plan sequence → execute in order → integration test |

**The bulk transformation rule:** If you find yourself planning to make the same edit in more than three places, stop. Write a script that does all of them in one pass. This is faster, more consistent, and does not hit rate limits. Example: adding a trace print after every `log_write` in a file is a regex substitution, not fifteen individual edits.

---

## 3. State What You Know and What You Don't

Before executing:

- State explicitly which files you have **read in this session** and which you are relying on **memory or assumption** for.
- If you have not read a file, you cannot claim to know its current contents. Do not claim it. Read it.
- If the codebase has likely changed since your last read of a file, re-read it.

This is calibrated confidence. "I read this file earlier in this session and it contained X" is a valid claim. "It probably contains X based on how the codebase is usually structured" is an assumption — label it as one or verify it.

---

## 4. Plan Before Executing

For any task with more than one step:

1. Write out the steps before starting.
2. Identify dependencies between steps (you cannot test an import path until the file containing it exists).
3. Identify what could go wrong at each step.
4. Create `workspace/reasoning_tmp.txt` as your working scratch space.

```markdown
# Task: [Objective]

## Operation type: bulk transformation / targeted change / diagnosis

## Files to read first:
- core/tools.py (not read yet)
- core/llm_client.py (read earlier this session)

## Steps:
- [ ] Read core/tools.py fully
- [ ] Identify all log_write calls in WA functions
- [ ] Write patch script
- [ ] Run patch script
- [ ] Syntax check
- [ ] Verify with grep

## Assumptions I am making:
- GRAY and RESET are already imported at the top of tools.py
```

The planning step is not overhead. It is the most efficient path because it prevents backtracking.

---

## 5. Investigate Before Modifying

When the current state of a file or system is uncertain:

- **Read the actual file.** Use `read_file` or `run_code` to inspect current contents. Do not infer from memory.
- **Run targeted probes.** Short bash or Python one-liners are cheap and accurate. Use them.
- **Check what actually exists** before reporting unavailability. `which <command>` and `pkg search <package>` are free.
- **No hallucination.** If you have not seen it in the actual source code of this session, do not state that it exists or has a specific value.

---

## 6. Execute Step by Step

- Work through the plan in dependency order.
- Keep changes isolated — avoid touching multiple unrelated modules in a single step.
- After each file edit, re-read the affected section to confirm it applied correctly.
- If a step reveals that earlier assumptions were wrong, stop and revise the plan rather than continuing on a wrong foundation.

---

## 7. Verify the Result

Every change requires verification. The verification must match the scope of the change:

- **Syntax check** after every file edit: `python3 -c "import ast; ast.parse(open('file.py').read())"`
- **Import check** after structural changes: `python3 -c "import module; print('OK')"`
- **Behavioral check** after logic changes: run a minimal test that exercises the changed code path
- **Output confirmation** after bulk transformations: `grep` or `python3` to count that N changes actually landed

Verification is not optional. "I made the change" is not the same as "the change is correct." See `verification.md` for full protocol.

---

## 8. Report What Matters

After completing a task:

- State **what was done** and **why**.
- Show **proof**: command output, assertion results, grep counts, syntax check output.
- Do not pad with apologies, unsolicited warnings, or generic caveats.
- If something went wrong or an assumption turned out to be false, state it plainly.

See `communication.md` for full communication standards.

---

## 9. Scientific Troubleshooting Protocol

When an error occurs, work through this sequence. Never guess:

1. **Locate** — read the full error, find the exact file, line, and exception type.
2. **Isolate** — categorize the cause: logic error, wrong path, permission block, type mismatch, import collision, or concurrency issue.
3. **Hypothesise** — form one or two specific hypotheses. Write them down. Example: *"The `import tools` is hitting `tools/__init__.py` instead of `core/tools.py` because root is first in sys.path."*
4. **Test in isolation** — write a minimal reproducer in `workspace/` to confirm or refute the hypothesis before touching production code.
5. **Fix, verify, and clean up** — port the fix to production, run verification, delete the test script.

---

## 10. Common Pitfalls in This Codebase

| Symptom | Likely Cause | Fix |
|---|---|---|
| `import tools` gets `tools/__init__.py` | Root before `core/` in sys.path | Ensure `core/` is `sys.path[0]` |
| `retrieve_chunk()` returns error for valid chunk | int/string type mismatch on chunk ID | Check ID type being passed |
| Tool calls missing from raw chunk | `set_tool_context()` not called before `close_chunk()` | Verify `llm_client.py` calls it |
| Active context grows every turn | `history.append()` in chat loop | Remove — `build_history()` serves history |
| Agent recovery picks wrong task | `active_task_id` check missing | Recovery priority: `active_task_id` → `cursor` → first pending |
| State corrupt after restart | `_next_id` not recovered | On load: `max(int parent IDs) + 1` |
