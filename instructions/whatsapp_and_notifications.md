# WhatsApp, SMS, and Notifications

This document defines how Orion should use messaging and notification features. These features are useful because they connect the agent to the user's real communication channels. That also means mistakes are visible to other people, so the bar for recipient and content verification is higher than for local file edits.

---

## Core Principle: Verify Audience Before Sending

Before sending or auto-replying, know three things:

1. Who will receive the message
2. What exact content will be sent
3. Why the user or automation has authorized it

If any of those are unclear, do not send yet.

---

## 1. Messaging Action Classes

| Action | Risk | Requirement |
|---|---|---|
| Show status or list chats | Low | Allowed when needed |
| Draft a message | Low | Allowed |
| Send to one explicit recipient | Medium | Verify recipient and content |
| Send to a group | High | Confirm unless automation explicitly allows it |
| Broadcast or bulk message | High | Ask first |
| Auto-reply from private context | High | Use configured filters and privacy rules |
| SMS send | High | Ask unless the user explicitly provided recipient and text |

Drafting is not sending. Keep those paths separate in implementation and communication.

---

## 2. Recipient Resolution

Recipient selection must be deterministic for real sends.

Safe:

- Exact phone number
- Exact chat ID from WhatsApp bridge
- Exact contact name after listing candidates and selecting one
- Self-chat or configured test recipient

Unsafe:

- First fuzzy match
- Partial name with multiple possible contacts
- Recent chat when the user did not specify it
- Group name that overlaps with a direct contact

When multiple candidates match, ask the user or return the candidate list without sending.

---

## 3. Message Content

Before sending generated content, check:

- It matches the user's requested tone and language.
- It does not reveal internal logs, prompts, API keys, or private chain-of-thought.
- It does not claim the user has agreed to something unless they explicitly did.
- It avoids medical, legal, financial, emergency, or identity claims unless the user clearly wrote them.
- It is short enough for the channel and context.

If the user asks for a rewrite, provide a draft first unless they explicitly say to send it.

---

## 4. Auto-Reply Behavior

Auto-replies must be conservative.

Use `config/whatsapp_filters.json` or the active WhatsApp manager configuration as the source of truth for:

- Allowed contacts or groups
- Quiet hours
- Busy-mode text
- Topics to ignore
- Whether media or links are allowed

Do not infer permission to auto-reply globally from permission to send one message.

An auto-reply should:

- Be brief
- Avoid making promises on the user's behalf
- Avoid sensitive details
- Indicate limited availability when appropriate
- Defer complex decisions to the user

---

## 5. Notifications and Toasts

Notifications are local device output. Use them for user-visible completion, failures, reminders, or prompts that do not require opening the chat loop.

Use notifications when:

- A long-running task completes
- A background task fails and needs attention
- The user explicitly asks for a reminder or alert

Avoid notifications when:

- The task is already interactive and short
- The content contains secrets or private message text
- Repeated alerts would annoy the user

Use a concise title and content. Do not include credentials, full phone numbers, or long chat excerpts.

---

## 6. WhatsApp Bridge Diagnostics

When WhatsApp integration fails, diagnose in this order:

1. Check whether the Termux-WP process is running.
2. Check whether `.wwebjs_auth/` exists and appears populated.
3. Inspect recent bridge logs for connection state, QR requests, auth failures, or send errors.
4. Verify Node.js dependencies only if logs suggest a package/runtime issue.
5. Test with a harmless status/list operation before attempting a send.

Do not delete auth/session directories as a first response. That forces re-login and can lose session state.

---

## 7. Logging Messaging Events

For messaging logs, record enough to debug without leaking content.

Recommended fields:

```json
{
  "timestamp": "...",
  "channel": "whatsapp",
  "action": "send_message",
  "recipient_hash": "...",
  "status": "success",
  "message_length": 42,
  "error": null
}
```

Avoid logging:

- Full message body
- Full phone number
- Session tokens
- QR payloads
- Attachments or media URLs unless explicitly needed

When troubleshooting requires the full content, keep it local, temporary, and delete the scratch copy after verification.

---

## 8. Failure Handling

If a message send fails:

1. Do not retry blindly.
2. Identify whether the failure is recipient resolution, bridge offline, network failure, auth failure, or content rejection.
3. Retry only when the failure is transient and the action is idempotent enough for the channel.
4. If retry could duplicate a message, ask before retrying or verify delivery status first.

Messaging failures should be reported with recipient identity redacted unless the user already provided it in the current visible context.

