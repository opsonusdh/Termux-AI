# Changelog

All notable changes to Termux-AI will be documented in this file.

---

## [3.0.0]

### Added
- **Agent Notifications**: New `notify` config option in `config/config.json` (default: `true`). When enabled, sends a system notification via `termux-notification` after agent tasks complete:
  - `/agent auto` loop completion
  - `/agent [prompt]` inline agent task completion
  - Persistent agent mode task completion
- **Status bar cleanup**: added the status bar. format: `EXP | AUTO | VOICE | 0 msg | OFF | 11:53:40`
- **Config validation**: Added automatic validation and migration of config keys on startup
- **Config update helper**: New `_update_config()` function for atomic config writes with fresh reads

### Changed
- **Status bar**: Simplified from `EXP | AUTO | VOICE | 0 msg | OFF | 11:53:40`
- **Config writes**: All config updates now use `_update_config()` for atomic operations and race condition prevention

### Fixed
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
