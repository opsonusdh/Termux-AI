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