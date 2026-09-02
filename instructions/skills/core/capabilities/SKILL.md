---
name: capabilities
description: Repository-specific capability and availability map. Prevents the agent from claiming tools, connectors, browsers, external accounts, or artifact backends that are not actually available.
---

# Runtime Capability Map

Use this skill when a task depends on an external service, browser/computer control, artifact backend, or platform integration.

## Always trust the live runtime

- The actual tool surface available to Orion is authoritative.
- A domain skill may describe an ideal workflow; it does not grant capabilities.
- Before claiming a capability is available, inspect the real tool/runtime surface or run a harmless probe when appropriate.
- Never manufacture browser clicks, phone calls, calendar actions, cloud uploads, Git credentials, or service connectors.

## Repository-native capabilities

The repo provides Python-based file, shell, indexing, memory, model, rendering, context, and platform-wrapper paths. Use the concrete tools exposed by `core/tools.py` and the active runtime.

### Available in Termux-AI (via `core/tools.py`):
- **File operations**: read, write, list, search, rename, delete
- **Shell execution**: `run_code` with full Termux environment
- **Web scraping**: `web_scrape` for HTTP/HTML content
- **WhatsApp**: status, chats, messages, send, media, groups, contacts
- **System diagnosis**: battery, weather, storage, memory, network, datetime
- **Memory**: save, retrieve, list conversation chunks
- **Image generation**: `generate_image` via Gemini
- **Sub-agent delegation**: `delegate_subtask` for parallel reasoning
- **Project management**: initialize, add/update subtasks
- **Notifications**: `termux-notification`, `termux-toast`, `termux-vibrate`

### Available via Termux API wrappers (`tools/`):
- Battery status, Wi-Fi info/scan, clipboard, contacts, SMS, location, sensors

## Imported domain skills

Imported skills may mention services such as Benepass, TaskRabbit, pharmacies, Slack, Claude, Anthropic, or browser/computer-use. Treat those as domain knowledge only unless a matching runtime capability is actually present.

When unavailable:
1. Do the useful local/informational part.
2. State the exact missing capability.
3. Do not claim that the external action was completed.
4. Ask for authorization only when the action would otherwise be possible and side-effectful.

## Capabilities NOT available in Termux-AI

| Capability | Status | Alternative |
|---|---|---|
| Browser automation (Playwright, Puppeteer, Selenium) | ❌ Not available | `web_scrape` for static content |
| Desktop GUI automation | ❌ Not available | Termux API wrappers for device actions |
| Cloud service connectors (AWS, GCP, Azure) | ❌ Not available | Direct HTTP via `http_request` |
| Git credentials management | ❌ Not available | SSH keys in `~/.ssh/` |
| System-level package management | ⚠️ Requires authorization | `pkg install` via `permissions.py` |
| Background services/daemons | ⚠️ Limited | Two daemon threads max (summarizer, reflection) |