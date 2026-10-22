# Changelog

All notable changes to Termux-AI will be documented in this file.

---

## [3.0.0]

### Added
- **`core/shell_runner.py`**: new module that runs shell commands with a **per-stage timeout** and guaranteed process-tree termination. Command chains (`a && b`, `a; b`, `a | b`) are split with a quote-aware, compound-aware parser, and every external stage is wrapped in GNU `timeout` so each stage gets its own budget instead of sharing one.
- **`core/shell_parse.py`**: new module that parses a shell command *structurally* and returns the source text of every **simple command** it contains — including the ones nested inside `for`/`while`/`until`/`if`/`case` blocks, `{ }` groups, subshells, function bodies and `$( )` / backtick substitutions. The permission gate uses it to inspect each command on its own instead of guessing from a keyword.
- **Chain-wide timeout ceiling**: the `shell` tool gained a `total_timeout` parameter — a ceiling for the **whole chain**, independent of how many stages it has. Without it the ceiling is derived from the per-stage budget (one budget per stage, capped), and both are now reported distinctly: a stage timeout says so, a chain ceiling says "chain-wide limit".
- **In-repo test suites**: `tests/test_shell_parse.py` and `tests/test_permissions.py` ship with the repository, so the shell parser and the permission gate are covered by the code that is actually pushed to git.
- **Agent Notifications**: New `notify` config option in `config/config.json` (default: `true`). When enabled, sends a system notification via `termux-notification` after agent tasks complete:
  - `/agent auto` loop completion
  - `/agent [prompt]` inline agent task completion
  - Persistent agent mode task completion
- **Status bar cleanup**: added the status bar. format: `EXP | AUTO | VOICE | 0 msg | OFF | 11:53:40`
- **Config validation**: Added automatic validation and migration of config keys on startup
- **Config update helper**: New `_update_config()` function for atomic config writes with fresh reads
- **Platform wrapper re-exports**: `core/tools.py` now re-exports the `tools/wrapper_*.py` functions explicitly, so every advertised tool route resolves to a real callable.
- **Sub-AI tool registry**: a single `_SUB_TOOL_FUNCS` registry now drives both the tools advertised to a delegated sub-AI and the tools it can actually execute, so a sub-AI can no longer be offered a tool whose dispatch would reject it.
- **Regression suites**: `workspace/audit/test_shell_timeout.py` (21 checks) and `workspace/audit/test_shell_semantics.py` (14 checks), both runnable under pytest and standalone.
- **In-repo test suite**: `tests/test_shell_runner.py` (25 checks, pytest) now ships with the repository, so the new shell runner is covered by the code that is actually pushed to git.

### Changed
- **Shell timeout semantics**: the `shell` tool's `timeout` is now applied per command in a chain rather than to the chain as a whole, and every process spawned by a command is terminated with it. The tool description and the dispatch default (`30` instead of `0`) were updated to match.
- **Permission gate: structural instead of textual**: the gate used to cut a command at every `;`, `&&`, `||` and newline, which tore compound blocks apart — the cut landed inside the block and the segment's command word became a shell keyword (`do`, `then`, …) instead of the command that runs. It now parses the command and checks every simple command it contains.
- **Single permission-module instance**: `core/tools.py` imported the gate as a top-level `permissions` module while three other call sites in the same file used `from core import permissions`, so the running app loaded **two** independent copies of the same file with separate module state. The import is now consistent.
- **Monotonic clocks for durations and deadlines**: `core/tools.py` (port/CDP wait loops) and `orchestration/worker.py` now measure elapsed time with `time.monotonic()` instead of `time.time()`. The wall clock can jump on Android (system suspend / time sync), which made deadlines fire early and inflated reported durations.
- **Status bar**: Simplified from `EXP | AUTO | VOICE | 0 msg | OFF | 11:53:40`
- **Config writes**: All config updates now use `_update_config()` for atomic operations and race condition prevention
- **Provider URLs**: `ask_ai_simple()` and `delegate_subtask()` read their base URL from `core/models.py` `PROVIDERS` instead of hardcoding them, so provider fixes propagate.
- **Tab completion**: now only offers commands that actually exist.

### Fixed
- **The permission gate was blind to compound blocks**: `for i in 1 2 3; do rm -rf $HOME/x; done`, `if true; then rm -rf /tmp/y; fi`, `while read l; do rm -rf $l; done`, `case $x in a) rm -rf /tmp/c ;; esac`, `{ rm -rf /tmp/g; }` and `( rm -rf /tmp/s )` all bypassed the gate, because the segment's command word was the block keyword. The destructive command inside the block is now inspected, and the gate reports the offending command rather than the keyword.
- **Four more permission bypasses of the same class**: a leading `NAME=value` assignment hid the command (`FOO=1 rm -rf /tmp/env`); wrapper commands ran another command behind the gate (`command rm -rf …`, `env FOO=1 rm -rf …`, `xargs rm -rf …`, `nohup rm -rf …`, `timeout 30 rm -rf …`); a command substitution executed unchecked (`echo $(rm -rf /tmp/x)`, `` echo `rm -rf /tmp/x` ``); and the right-hand side of a pipe or a background job was never inspected (`ls | tee /tmp/out`, `sleep 1 & rm -rf /tmp/x`).
- **`$VAR` in a path was never resolved**: `rm -rf $HOME/x` looked like a relative path inside the project root and was waved through; paths are now expanded before the location check.
- **`api.keys` could be smuggled past the gate**: the reference was only looked for in the individual segments, so `for f in config/api.keys; do cat $f; done` slipped through — the word list is not a command of its own. The reference is now checked against the whole command, in every mode.
- **Inline interpreter code was only pattern-matched**: `bash -c "rm /tmp/x"` passed because the pattern list only looked for the recursive form; the inline code is now parsed and each command in it is checked.
- **The parser could be used as a denial-of-service vector**: every nesting level re-parsed the text inside it, so a deeply nested command substitution was quadratic in the length of the command (7s for 3 200 levels). Delimiters are now matched in a single pass and the substitution depth is limited — 246ms for the same input.
- **Orphaned shell processes**: on timeout the old `shell()` called `Popen.kill()`, which signalled only the `bash` process — every process it spawned survived, reparented to init, burning battery and still writing files. The whole tree (process group + a `/proc` session sweep) is now terminated.
- **Shell tool hanging past its timeout**: the old `shell()` called `communicate()` after the kill, which blocks until every pipe holder exits, so a grandchild could keep the tool call alive for minutes. Output is now read on background threads and the call always returns on time.
- **A stage timeout could be silently swallowed**: GNU `timeout` exits with 124, but the chain's exit status hid it — `sleep 300 || echo recovered`, `sleep 300 ; echo after` and `sleep 300 | cat` all finished with status 0, so the tool reported success for a command it had actually killed. A DEBUG trap now observes every command's status (and `${PIPESTATUS[0]}` for the left stage of a pipeline), so a timeout is reported no matter which operator follows it.
- **Orchestration worker could hang forever**: `orchestration/worker.py` ran tasks with `subprocess.run()` and no timeout; a stuck command blocked the orchestrator indefinitely. Tasks are now bounded by `TASK_TIMEOUT` and report a `timeout` status.
- **`sleep_mode()` crashed on every call** with `NameError: BASE_DIR` (it used an undefined name instead of `paths.ROOT`).
- **11 advertised tools always raised `NameError`**: `get_contacts`, `get_contact_by_id`, `list_sensors`, `get_sensor_data`, `set_wallpaper`, `take_photo`, `record_video`, `record_audio`, `pick_file`, `share_file` and `share_text` were in the tool schema and the dispatch table but were never importable.
- **Phantom `tools_core` module**: five call sites imported a module that never existed, and the bare `except ImportError` swallowed it — silently disabling progressive chunk summarisation, all WhatsApp auto-replies and the WhatsApp log.
- **HTTP 400 exhausted the whole key pool**: an invalid request was retried against every key and marked healthy keys as exhausted. It now skips the model immediately.
- **`generate_image()` always failed**: 4 of the 6 model IDs returned 404 (hiding the real error behind a bogus "model not found"), and the response parser only handled one of the two response shapes the API returns.
- **Piped stdin crashed the CLI** with `termios.error`; a corrupt `config.json` killed the chat loop every turn; `list_sensors` returned garbage; the WhatsApp log stored sender id and name in swapped fields.
- Config persistence race conditions between different modules

---

## [1.0.0] - 2024-XX-XX

### Added
- Initial release of Termux-AI (Orion)
- Multi-provider LLM fallback (Google Gemini, OpenRouter, Groq, NVIDIA)
- Tool use: `run_code`, `read_file`, `write_file`, `web_scrape`, `save_memory`, `retrieve_memory`, `index_files`, `intermediate_print`, `sleep_mode`
- Enhanced tool suite: `search_in_files`, `list_directory`, `search_files`, `rename_file`, `delete_file`, `http_request`, `get_datetime`
- Chunk-based context memory with progressive compression
- Persistent two-tier RAG memory system
- Agentic execution layer (Supervisor → Worker → Critic)
- Orchestration framework (multi-process task delegation)
- Self-correction with automatic retry
- Voice I/O (STT via Termux-STT, TTS via edge-tts)
- WhatsApp integration (via Termux-WP)
- Autonomous mode (opt-in)
- Advanced LLM client with model slots, error handling, reasoning budget management

---

## Template for Future Releases

## [X.Y.Z] - YYYY-MM-DD

### Added
- Feature descriptions

### Changed
- Changes to existing functionality

### Deprecated
- Soon-to-be removed features

### Removed
- Removed features

### Fixed
- Bug fixes

### Security
- Security improvements
