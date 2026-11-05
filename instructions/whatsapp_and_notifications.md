# WhatsApp, SMS, and Notifications

This document defines how Orion should use messaging and notification features. These features connect the agent to the user's communication channels.

---

## 1. Core Principle: Verify Audience and Content Before Sending

Before sending or auto-replying, know three things:

1. Who will receive the message
2. What exact content will be sent
3. Why the user or automation has authorized it

If any of those are unclear, do not send yet.

---

## 2. Action Classes and Risk Mitigation

| Action | Risk | Requirement |
|---|---|---|
| Show status or list chats | Low | Allowed when needed |
| Draft a message | Low | Allowed |
| Send to one explicit recipient | Medium | Verify recipient and content |
| Send to a group | High | Confirm unless automation explicitly allows it |
| Broadcast or bulk message | High | Ask first |
| Auto-reply from private context | High | Use configured filters and privacy rules |
| SMS send | High | Ask unless the user explicitly provided recipient and text |

---

## 3. Recipient Resolution and Validation

Recipient selection must be deterministic and validated before any real send.

Safe:
- Exact phone number (validated)
- Exact chat ID from WhatsApp bridge
- Self-chat or configured test recipient

Unsafe:
- First fuzzy match
- Partial name with multiple possible contacts
- Recent chat when the user did not specify it

---

## 4. Message Content and Safety Screening

Before sending generated content, check:
- It matches the user's requested tone and language.
- It does not reveal internal logs, prompts, API keys, or private chain-of-thought.
- It avoids embarrassing, reputation-damaging, or socially risky content.

---

## 5. Notifications and Toasts

Use notifications for user-visible completion, failures, reminders, or alerts.
Use a concise title and content without leaking sensitive secrets.

Use `termux-notification` for persistent notifications, `termux-toast` for brief messages, `termux-vibrate` for haptic feedback.

---

## 6. The WhatsApp Plugin Is Quiet by Design

The gateway talks. The chat must not.

- **The pairing QR is a live secret.** It is *captured*, never printed, and drawn only
  when the user asks for it (`/plugin setup whatsapp`). Whoever scans it gets
  read/write access to the account, and the gateway rotates it every few seconds.
- **`setup` is the pairing path.** Not linked → start the gateway and show the code
  once. Already linked → a code only means a re-link, so stop the gateway, switch the
  plugin off and say so. Never decide during `INITIALIZING`: the HTTP port opens before
  the WhatsApp engine has booted, so wait for `READY` or `QR_REQUIRED`.
- **No status chatter.** The "connected, my number" banner and the per-state
  "Status: …" lines are gone. `/plugin status whatsapp` reports the pairing state on
  request.
- **Enabling a plugin starts it.** The gateway is launched for you, a dead gateway is
  restarted with a growing backoff, and an orphaned Chromium holding the WhatsApp
  profile is cleared before the next start. If the runtime did not come up, say so.
- **Never use `pkill -f bot.js`** on this device — the pattern matches the running
  shell itself. Scan `/proc/*/cmdline` for the gateway process instead.