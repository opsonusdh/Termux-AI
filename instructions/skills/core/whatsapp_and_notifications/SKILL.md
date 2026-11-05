---
name: whatsapp_and_notifications
description: WhatsApp, SMS, and Notifications. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# WhatsApp, SMS, and Notifications

Treat messaging as externally visible side effects.

## Before action
Verify the recipient/contact, message content, and intended action. Read relevant instruction and safety material before messaging tasks.

## User control
A send, delete, mute, block, or account-affecting action requires clear user intent unless it is an already-authorized automation.

## Privacy
Do not expose full phone numbers, message histories, authentication data, QR payloads, or unrelated private content in logs or reports.

## Failures
Inspect the actual bridge status and error. Do not claim delivery, connection, or availability without verification.

## Termux Notifications
Use `termux-notification` for persistent notifications, `termux-toast` for brief messages, `termux-vibrate` for haptic feedback.

---

## The WhatsApp Plugin Is Quiet by Design

The gateway talks. The chat must not.

- **The pairing QR is a live secret.** It is *captured*, never printed, and drawn only
  when the user asks for it (`/plugin setup whatsapp`, or
  `manage_plugin(action="setup", plugin="whatsapp")`). Whoever scans it gets read/write
  access to the account, and the gateway rotates it every few seconds while the
  WebSocket replays the current status.
- **`setup` is the pairing path.** When the session is **not** linked, the gateway is
  started and the code is shown once. When it **is** already linked, a pairing code
  only means a re-link — so the gateway is stopped and the plugin switches itself off,
  and the user is told so. Never decide during `INITIALIZING`: the HTTP port opens
  long before the WhatsApp engine has booted, so the pairing state is only known after
  `READY` or `QR_REQUIRED`.
- **No status chatter.** The "connected, my number" banner and the per-state
  "Status: …" lines are gone. The number is still kept for the group-mention check;
  `/plugin status whatsapp` reports the pairing state when the user asks.
- **Enabling a plugin starts it.** `/plugin enable whatsapp` starts the Termux-WP node
  gateway; a gateway that dies is restarted with a growing backoff; an orphaned
  Chromium holding the WhatsApp profile is cleared before the next start. If the
  runtime did not come up, say so — do not claim the tools work.
- **Never use `pkill -f bot.js`** on this device: the pattern matches the running
  shell itself. Scan `/proc/*/cmdline` for the gateway process instead.

---

## Runtime Notifications

`notify: true` in `config/config.json` pushes a system notification whenever the
runtime stops and waits for a human — the permission gate, `confirm` and `ask_user`
each get their own title, and the agent-task-completed notice is a fourth. The flag is
read from the file at every prompt, so `/notify false` takes effect on the very next
question. The notifier never prints and never raises: a missing Termux:API package
means the notification is skipped, not that the question is.