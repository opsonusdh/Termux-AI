# Tool Use Efficiency

Every tool call costs time and a round trip. The goal is to accomplish each task in the minimum number of tool calls that produces a correct, verified result. Unnecessary calls are not just inefficient — they create rate limit pressure and introduce more opportunities for error.

---

## The Fundamental Question Before Any Tool Call

*"Is there a way to accomplish this entire objective in fewer calls?"*

Ask this before every sequence of reads, writes, and executes. If the answer is yes, take that path.

---

## 1. Identify the Operation Pattern First

Before touching any tool, identify what class of operation this is:

| Pattern | Characteristics | Efficient approach |
|---|---|---|
| **Single read** | Need to know what's in one file | One `read_file` call |
| **Single targeted write** | One specific location to change | Read once → write once → verify once |
| **Bulk transformation** | Same change in N places in a file | Read once → write a script → run once → grep count |
| **Multi-file transformation** | Same change across multiple files | One script iterating all files → run once → verify |
| **Investigation** | Unknown state, need to discover | Batch all reads first, then analyse, then act |
| **Diagnosis** | Error exists, cause unknown | Read error → isolate → targeted probe → fix |

Recognizing the pattern before starting determines the tool strategy.

---

## 2. Bulk Transformation Rule

If the same change needs to happen in more than three places: **write a script**.

Do not make individual edits. Individual edits for a bulk transformation:
- Consume N read + N write round trips
- Hit rate limits (as shown by the 15-edit, 8+ rate-limit scenario)
- Risk inconsistency if the pattern varies slightly in some locations
- Are harder to verify than counting grep matches

**The correct approach:**

```python
# Example: add a trace print after every log_write in WhatsApp functions
python3 - << 'EOF'
import re

with open('./core/tools.py', 'r') as f:
    src = f.read()

# Add trace print after every log_write in a WA function
patched = re.sub(
    r'(    log_write\(f"\[([^\]]+)\][^)]*"\)\n)',
    lambda m: m.group(1) + f'    print(f"{{GRAY}}[WhatsApp] {m.group(2).replace("_", " ")}...{{RESET}}\\n"',
    src
)

with open('./core/tools.py', 'w') as f:
    f.write(patched)

# Verify
count = patched.count('[WhatsApp]')
print(f'Done. {count} trace prints inserted.')
EOF
```

**Then verify:**
```bash
grep -c 'print(f"{GRAY}\[WhatsApp\]' ./core/tools.py
```

One read, one write, one verify. Not fifteen of each.

---

## 3. Batch Reads Before Writing

When you need to understand multiple files before making changes:
- Read all of them first
- Do all analysis
- Then write

Do not interleave reads and writes. Reading file A, then writing to B based on A, then needing to re-read A because B introduced a dependency — this is a symptom of planning during execution rather than before it.

---

## 4. Use the Right Tool for Each Job

| Job | Right tool | Wrong tool |
|---|---|---|
| Check if a binary exists | `which <cmd>` (bash) | Attempting to run it and catching the error |
| Check package availability | `pip list \| grep X` | Assuming it's installed |
| Find a pattern across a file | `grep -n 'pattern' file` | Reading the whole file and searching manually |
| Count occurrences | `grep -c 'pattern' file` | Reading, splitting, and counting in Python |
| Verify N edits landed | `grep -c` | Re-reading the whole file visually |
| Multi-file search | `grep -rn 'pattern' ./core/` | Opening each file individually |
| Check return code | `echo $?` after the command | Running it again |

---

## 5. Read Once, Use Fully

When you read a file, extract everything you need from that read:
- Which functions are present
- Import structure
- The specific section you need to modify
- Any adjacent code that your change might affect

Do not read a file, extract one thing, and then read it again for a second thing. One read per file per task.

---

## 6. Compose Tool Calls

When a task requires a sequence of bash operations, run them together in one `run_code` call:

```bash
# Wrong: three separate tool calls
run_code("python3 -c \"import ast; ...\"")
run_code("grep -c 'pattern' file.py")
run_code("node --check bot.js")

# Right: one tool call
run_code("""
python3 -c "import ast; ast.parse(open('core/tools.py').read()); print('tools.py OK')"
grep -c 'print.*GRAY.*WhatsApp' core/tools.py
node --check Termux-WP/bot.js && echo "bot.js OK"
""")
```

---

## 7. Write Verification Into the Script

When writing a transformation script, include its own verification at the end:

```python
# Transform
patched = re.sub(pattern, replacement, src)

# Write
with open(path, 'w') as f:
    f.write(patched)

# Verify inline — no separate tool call needed
import ast
ast.parse(patched)  # raises if syntax broken
count = patched.count(expected_pattern)
print(f'OK — {count} changes applied, syntax clean.')
```

This turns three tool calls (write, syntax check, count verify) into one.

---

## 8. When NOT to Optimize

Not every situation calls for a script. Individual edits are correct when:
- Fewer than three locations need changing
- The changes are meaningfully different at each location (not the same pattern)
- The file is small enough that reading it once is sufficient for complete understanding

Premature optimization into scripts when individual edits are cleaner adds unnecessary abstraction. Recognize the operation type first, then choose the tool strategy.
