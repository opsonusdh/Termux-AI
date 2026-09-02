# Tool Use Efficiency

Every tool call costs time and a round trip. The goal is to accomplish each task in the minimum number of tool calls that produces a correct, verified result. Unnecessary calls are not just inefficient — they create rate limit pressure and introduce more opportunities for error.

---

## The Fundamental Question Before Any Tool Call

*"Is there a way to accomplish this entire objective in fewer calls?"*

Ask this before every sequence of reads, writes, and executes. If the answer is yes, take that path.

---

## 1. Operation Patterns

| Pattern | Characteristics | Efficient approach |
|---|---|---|
| **Single read** | Need to know what's in one file | One `read_file` call |
| **Single targeted write** | One specific location to change | Read once → write once → verify once |
| **Bulk transformation** | Same change in N places in a file | Read once → write a script → run once → verify |
| **Multi-file transformation** | Same change across multiple files | One script iterating all files → run once → verify |
| **Investigation** | Unknown state, need to discover | Batch all reads first, then analyse, then act |
| **Diagnosis** | Error exists, cause unknown | Read error → isolate → targeted probe → fix |

---

## 2. Systematic Discovery & Codebase Exploration

- **Targeted Content Search (`grep`):** Locate definitions and usages cleanly.
- **File Content Retrieval (`read_file`):** Read files when complete content or specific segments are needed.
- **Deep Codebase Understanding (`index_files`):** Use for RAG queries across larger codebases.

---

## 3. Bulk Transformation Rule

If the same change needs to happen in more than three places: **write a script**.

Do not make individual edits. A script handles all changes in one pass cleanly and consistently.

---

## 4. Batch Reads Before Writing

When you need to understand multiple files before making changes:
- Read all of them first
- Do all analysis
- Then write

Do not interleave reads and writes.

---

## 5. Compose Tool Calls

When a task requires a sequence of operations, run them together in one tool call where appropriate.