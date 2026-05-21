# Reasoning & Analytical Problem-Solving Instructions

This document outlines the cognitive and operational framework for executing tasks, solving problems, and resolving errors autonomously. Every execution path must follow this disciplined approach to ensure safety, precision, and architectural consistency.

---

## 1. The Reasoning Lifecycle

For every task, follow this sequential reasoning process:

```
[ Clarify Intent ] ──> [ Decompose ] ──> [ Track (reasoning_tmp.txt) ]
                                                        │
[ Report ] <── [ Verify Result ] <── [ Execute ] <── [ Investigate ]
```

### Step 1: Clarify Intent
- **Deconstruct Request:** Analyze what the user is asking. Identify the implicit business requirements, not just the literal instruction.
- **Identify Context:** Determine which system components, files, and modules are affected.
- **Minimize Friction:** If the request is implicit or logical, proceed autonomously. Do not ask for permissions unless it involves security-sensitive elements or destructive modifications outside `~/ai_root`.

### Step 2: Decompose & Plan
- Break the task into discrete, atomic, and testable milestones.
- Identify dependencies between tasks (e.g., we cannot refactor an orchestrator import path until we define the new centralized path mapping).

### Step 3: Track Execution Progress
- Create or update the dynamic tracking file: `~/ai_root/workspace/reasoning_tmp.txt`.
- Define a strict markdown to-do list (`- [ ] Task Name`).
- As each task is resolved, update the state of the task list (`- [x] Task Name`) immediately.

### Step 4: Programmatic Investigation
- **Inspect First:** Never assume file structures or system configs. Read the files, inspect directories, and verify variables.
- **Run Probes:** Use shell commands or python one-liners to check for package installations, library compatibility, and permission sets in the current Termux context.
- **Eliminate Hallucinations:** You must not hallucinate commands, APIs, database tables, or folder paths. If you have not seen it, search or check.

### Step 5: Iterative Execution
- Work through the to-do list step-by-step.
- Keep changes isolated. Avoid modifying multiple directories or modules simultaneously unless they are tied to a single atomic upgrade step.

### Step 6: Empirical Verification
- Verify results programmatically.
- Execute test suites, assert outcomes, check return codes, and print output proof.
- Confirm clean resource termination (no orphan processes or locked files).

### Step 7: Clear & Structured Reporting
- Report results cleanly and directly.
- Include execution logs or command-line outputs as definitive proof.
- Explain "what was done" and "why it works". Avoid generic introductions, unnecessary warnings, or apologies.

---

## 2. Dynamic State Tracking (`reasoning_tmp.txt`)

The `reasoning_tmp.txt` file acts as the volatile memory of the active operation. It must represent the exact micro-state of your current thinking and execution.

### Schema of `reasoning_tmp.txt`:
```markdown
# Current Task: [Detailed Objective]

## Active To-Do List:
- [x] Finished preliminary task (e.g., inspect paths)
- [/] In-progress subtask (e.g., refactor paths.py)
- [ ] Remaining subtask (e.g., write validation test)
- [ ] Final verification

## Discoveries & State Notes:
- Log of active variables, paths, or shell results discovered.
- Crucial constraints identified during active execution.
```

---

## 3. Scientific Troubleshooting & Self-Correction

When an error or failure is encountered, do not guess. Work through this diagnostic protocol:

1. **Locate the Failure Point:** Read stdout/stderr logs and extract the stack trace. Identify the exact file, line number, and function where the crash occurred.
2. **Isolate the Cause:** Determine if it is a logical error, path resolution failure, permission block, concurrency deadlock, or package mismatch.
3. **Generate Hypotheses:** Brainstorm potential fixes (e.g., "The FIFO hangs because there is no reader on the thread; changing it to multiprocessing.Queue should resolve this").
4. **Test in Isolation:** Write a minimal, reproducible test script (e.g., `test_ipc_simple.py`) in the `workspace` directory to validate the hypothesis before writing any production modifications.
5. **Implement & Document:** Once the isolated test passes, port the fix to production modules, clean up the test scripts, and log the incident in `~/ai_root/logs/reflection_log.jsonl`.
