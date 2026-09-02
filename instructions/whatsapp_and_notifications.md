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