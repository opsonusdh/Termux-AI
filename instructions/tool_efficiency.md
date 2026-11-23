# Tool Use Efficiency

Every tool call costs context, time, and a round trip. The goal is to retrieve the minimum information necessary to complete each task accurately in the minimum number of reliable operations.

For the full 14-point context-budgeting protocol, see [**context_efficiency.md**](./context_efficiency.md).

---

## The Fundamental Questions Before Any Tool Call

1. *"What is missing, and what is the cheapest reliable way to obtain it?"*
2. *"Do I need all of this information, or can I retrieve only the part that answers my current question?"*
3. *"Is there a way to accomplish this entire objective in fewer calls?"*

Prefer: **Inspect → Search → Filter → Read selectively → Analyze → Verify → Act**

Avoid: **Read everything → Consume context → Search again → Read everything again → Lose track of the task**

---

## 1. Operation Patterns

| Pattern | Characteristics | Efficient approach |
|---|---|---|
| **Single read** | Need to know what's in one file | Inspect size/type first → one targeted `read_file` call |
| **Single targeted write** | One specific location to change | Read relevant target → minimal patch (`find_replace` / `segment`) → verify once |
| **Bulk transformation** | Same change in N places in a file | Read once → write a script → run once → verify |
| **Multi-file transformation** | Same change across multiple files | One script iterating all files → run once → verify |
| **Investigation** | Unknown state, need to discover | Shallow tree + symbol/keyword search → targeted reads → analyse |
| **Diagnosis** | Error exists, cause unknown | Filter logs/errors → isolate → targeted probe → minimal fix |

---

## 2. Inspect, Search, and Read Selectively

- **Inspect before reading:** Check file size, type, and whether a file is binary/compressed before opening. Never load multi-megabyte logs or binary files blindly.
- **Search before reading:** Use `search_in_files`, `grep`/`rg`, `search_files`/`find`, `jq`, `yq`, or language parsers to locate matches before reading source files.
- **Read selectively:** Use `head`, `tail`, `sed`/`awk`, or `read_file` with `segment_start`/`segment_end`. Expand the range only when surrounding context is insufficient.
- **Chunk large files:** Process large logs/files one relevant chunk at a time, keep compact technical summaries, and discard raw noise.
- **Handle file types appropriately:** Use structured queries for JSON/YAML/XML, page ranges for PDFs, sheet/range inspection for spreadsheets, content listings for archives, and dedicated inspection tools for binaries/images.

---

## 3. Control Output and Avoid Redundant Work

- **Control tool output:** Limit matching lines, use shallow directory listings (`max_depth`), filter verbose command output, and extract relevant fields programmatically before bringing results into context.
- **Avoid re-reading unchanged files:** Reuse already-inspected facts. Before re-reading, check whether the file changed (`git diff`, timestamps, hashes) or search only for the missing detail.
- **Batch reads before writing:** Read all relevant targets first, analyse, then apply minimal changes without interleaving reads and writes.
- **Bulk transformation rule:** If the same change needs to happen in more than three places, write a script and verify counts/diffs.
- **Compose tool calls & probes:** Prefer cheap factual probes (`grep`, `python3 -c`, `which`, `git status --short`) and verify dependencies/test results before concluding.
