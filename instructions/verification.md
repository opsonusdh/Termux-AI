# Verification Protocol

Verification is not a final step — it is part of the work. A change that has not been verified has not been completed.

---

## The Core Principle

"I made the change" is not the same as "the change is correct." These are two different things, and confusing them is the most common source of regressions. Every modification requires proof that it did what was intended and did not break anything adjacent.

---

## 1. Match Verification to Scope

The depth of verification must match the scope of the change:

| Change type | Minimum verification |
|---|---|
| Single line edit | Syntax check + re-read the affected section |
| New function added | Syntax check + call the function and assert the return value |
| Bulk transformation (N locations) | Syntax check + grep/count to confirm exactly N changes landed |
| Structural refactor (imports, paths, module boundaries) | Syntax check + import check + integration test covering the full call chain |
| New LLM-callable tool | Syntax check + dispatch test through `_dispatch_tool()` + assert return type |
| Cross-file dependency change | Integration test covering every file that imports the changed symbol |

Doing less than the minimum for the scope is incomplete verification.

---

## 2. Syntax Check (Required After Every File Edit)

Run this immediately after every write. Do not proceed to the next step until it passes:

```python
# Python files
python3 -c "import ast; ast.parse(open('core/tools.py').read()); print('OK')"

# JavaScript files
node --check Termux-WP/bot.js && echo "OK"

# JSON files
python3 -c "import json; json.load(open('config/config.json')); print('OK')"
```

A syntax error discovered after three more edits is much harder to locate than one discovered immediately.

---

## 3. Import Check (Required After Structural Changes)

After adding imports, moving modules, or changing `sys.path` setup:

```python
python3 -c "
import sys
sys.path.insert(0, 'core')
sys.path.insert(1, '.')
import tools
import context_manager
from agent import state_manager
print('All imports OK')
"
```

---

## 4. Behavioral Verification (Required After Logic Changes)

After changing how a function works, write a minimal test that exercises the actual code path and asserts a specific value:

```python
python3 -c "
import sys
sys.path.insert(0, 'core')
sys.path.insert(1, '.')
import context_manager as cm

cm.open_chunk('test')
cm.set_tool_context([])
cid = cm.close_chunk('reply')
assert cid == 1, f'Expected 1, got {cid}'

history = cm.build_history()
assert len(history) > 0, 'History should not be empty'
print('OK')
"
```

**Assert specific values.** `assert result is not None` is not a behavioral test — it only proves the function returned. `assert result == expected_value` proves it returned the right thing.

---

## 5. Bulk Transformation Verification

After running a script that makes the same change in N places, verify that exactly N changes were made:

```bash
# Count how many times the new pattern appears
grep -c 'print(f"{GRAY}\[WhatsApp\]' core/tools.py

# Count how many WA functions exist (should match)
grep -c '^def.*whatsapp' core/tools.py
```

If the counts don't match, the script had a gap. Find it before moving on.

---

## 6. Integration Testing

Integration tests verify the full call chain, not just the changed component in isolation.

Requirements for a valid integration test:
1. Starts from the public entry point (e.g., `_dispatch_tool()`, not the internal function directly)
2. Covers at least one complete round trip (input → processing → output)
3. Asserts the final output value, not intermediate state
4. Tests at least one edge case (empty input, missing file, zero results)

```python
# Example: full tool dispatch integration test
python3 -c "
import sys, json
sys.path.insert(0, 'core')
sys.path.insert(1, '.')

# Simulate how llm_client calls _dispatch_tool
from llm_client import _dispatch_tool

tool_call = {
    'function': {
        'name': 'get_whatsapp_status',
        'arguments': '{}'
    }
}

result = _dispatch_tool(tool_call)
assert isinstance(result, str), f'Expected str, got {type(result)}'
assert len(result) > 0, 'Result should not be empty'
print('Integration test OK:', result[:60])
"
```

---

## 7. Re-Read After Writing

After every file edit, re-read the section you changed and confirm:
- The edit appears exactly as intended
- The surrounding context is intact (no accidental deletions or duplications)
- Indentation and syntax are correct

This takes seconds and catches the most common class of write errors.

---

## 8. What "Done" Means

A task is done when:

1. ✓ The change is in the file (verified by re-read)
2. ✓ Syntax is clean (verified by syntax check)
3. ✓ Imports resolve correctly (verified by import check if structural)
4. ✓ The behavior is correct (verified by behavioral or integration test)
5. ✓ N changes landed when N were intended (verified by grep count if bulk)
6. ✓ No adjacent functionality was broken (verified by running related tests)

Not when you have made the edit. When all of the above are true.
