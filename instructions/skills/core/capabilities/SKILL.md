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
- **Web content (rendered)**: `browse_web` and `browser` — read a page as Markdown (headings, lists, links, tables survive; nav/footer/cookie banners are dropped), pull one element with `search`, and capture a screenshot only when asked (`screenshot=true`); a `find` action lists clickable elements by label/index.
- **WhatsApp**: status, chats, messages, send, media, groups, contacts
- **System diagnosis**: battery, weather, storage, memory, network, datetime
- **Memory**: save, retrieve, list conversation chunks
- **Image generation**: `generate_image` via Gemini
- **Sub-agent delegation**: `delegate_subtask` for parallel reasoning
- **Project management**: initialize, add/update subtasks
- **Notifications**: `termux-notification`, `termux-toast`, `termux-vibrate`

### Available via Termux API wrappers (`tools/`):
- Battery status, Wi-Fi info/scan, clipboard, contacts, SMS, location, sensors

### Plugin-gated Google services (`core/google_api.py`)

Gmail, Google Calendar and Google Drive are real connectors, but they are
**plugin-gated and account-gated**: the tools are only advertised to the
model while the plugin is enabled *and* the user has completed the OAuth
consent flow. Google API client credentials live in
`config/google_oauth.json`; refresh tokens live in `data/google_tokens/`.

Use the **`manage_plugin`** tool for all of this — do not guess at plugin
state from memory:

- `manage_plugin(action="list")` — every plugin with its enabled/connected state.
- `manage_plugin(action="status", plugin="gmail")` — one plugin.
- `manage_plugin(action="enable"|"disable", plugin="gmail")` — switch it on/off.
- `manage_plugin(action="setup", plugin="gmail")` — starts the Google
  permission request and **returns a URL the user must open**. Enabling an
  account plugin is not enough on its own; access has to be granted.
- `manage_plugin(action="finish", plugin="gmail")` — call this *after* the
  user says they approved the request, to complete the connection.
- `manage_plugin(action="disconnect", plugin="gmail")` — revoke access.

The same flow is available as a slash command: `/plugin list|status|enable|
disable|setup|disconnect`, always with the subcommand first —
`/plugin setup gmail` (the reversed `/plugin gmail setup` is accepted too).

The Google OAuth client must be an application of type **Desktop app**: a
"TVs and Limited Input devices" client cannot request the Gmail/Calendar/Drive
scopes (the device-code flow rejects them) and Google blocks the local
redirect for that client type. If the setup fails with a client-type error,
say so and point the user at <https://console.cloud.google.com/auth/clients>
instead of retrying.

If the account is not connected, the correct answer is to **show the user the
consent URL** (or, if no OAuth client exists yet, to point them at
`/plugin setup <name>`) — not to attempt the action or to claim it succeeded.
A tool call made before the account is connected returns an explicit error
naming the command to run; report that to the user instead of retrying.

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
| Full browser automation (Playwright, Puppeteer, Selenium) | ❌ Not available | `browse_web` / `browser` for rendered content (DOM → Markdown, opt-in `screenshot`, `find`/`search` actions) |
| Desktop GUI automation | ❌ Not available | Termux API wrappers for device actions |
| Cloud service connectors (AWS, GCP, Azure) | ❌ Not available | Direct HTTP via `http_request` |
| Git credentials management | ❌ Not available | SSH keys in `~/.ssh/` |
| Gmail / Calendar / Drive without OAuth consent | ❌ Not available | `manage_plugin(action="setup", plugin=...)` for the URL, or `/plugin setup <name>` |
| System-level package management | ⚠️ Requires authorization | `pkg install` via `permissions.py` |
| Background services/daemons | ⚠️ Limited | Two daemon threads max (summarizer, reflection) |