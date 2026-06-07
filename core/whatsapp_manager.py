import os
import json
import time
import re
import requests
import threading
import websocket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

qrcode_module = str(
    BASE_DIR / "Termux-WP" / "node_modules" / "qrcode-terminal"
)
if not Path(qrcode_module).exists():
    raise FileNotFoundError(
        f"Termux-WP is not installed or not properly configured at {BASE_DIR}"
    )

BASE_URL = "http://localhost:3000"
WS_URL   = "ws://localhost:3000"

GRAY  = "\033[90m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


class WhatsAppManager:
    def __init__(self):
        self.pending_messages = []
        self.lock             = threading.Lock()

        self.contact_state = {}
        self.state_lock    = threading.Lock()

        self.is_busy = False
        self.busy_instruction = ""  # Personal context about the user (name, occupation, etc.)
        self.user_profile = ""
        self.ws_thread        = None
        self.running          = False
        self.connection_state = "DISCONNECTED"
        self.debug            = False
        self._ready_event     = threading.Event()
        self._seen_msg_ids    = set()
        self._seen_msg_lock   = threading.Lock()
        self._active_senders  = set()  # senders with an auto-reply in progress
        self._active_lock     = threading.Lock()

        # Human-first hold — after the owner manually replies to someone, Orion waits
        # before auto-replying to that person's next message.
        # How it works:
        #   - Orion marks each auto-reply it sends so the WS echo can be identified
        #   - If MESSAGE_SENT arrives and is NOT an Orion echo → owner sent manually
        #   - On the next incoming message from that contact → defer auto-reply
        self._orion_echo_expected = set()   # JIDs where we expect our own send echo
        self._hold_until          = {}      # {jid: datetime} — defer auto-reply until this time
        self._deferred_timers     = {}      # {jid: threading.Timer}
        self._deferred_lock       = threading.Lock()
        self.reply_hold_seconds   = 300     # default 5 min; set via set_reply_hold()

        # Filtering and control system config
        self.filters_path = BASE_DIR / "config" / "whatsapp_filters.json"
        self.ignore_all_groups = False
        self.ignored_contacts = []
        self.ignored_groups = []
        self.muted_threads = {}  # {jid: {"reason", "since", "until"}} — timed or permanent
        self.exclude_all_groups_except = []
        self.per_contact = {}    # {jid: {"last_auto_reply_at", "cooldown_until"}} — persisted
        self.my_number = ""
        self.busy_since = None   # ISO timestamp when busy mode was last enabled
        self.load_filters()

    #  Filters and Control helpers

    def load_filters(self):
        try:
            if self.filters_path.exists():
                with open(self.filters_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.ignore_all_groups = data.get("ignore_all_groups", False)
                    self.ignored_contacts = data.get("ignored_contacts", [])
                    self.ignored_groups = data.get("ignored_groups", [])
                    self.muted_threads = data.get("muted_threads", {})
                    self.exclude_all_groups_except = data.get("exclude_all_groups_except", [])
                    self.per_contact = data.get("per_contact", {})
                    self.is_busy = data.get("is_busy", False)
                    self.busy_since = data.get("busy_since", None)
                    self.busy_instruction = data.get("busy_instruction", "")
                    self.user_profile = data.get("user_profile", "")
                    self.reply_hold_seconds = int(data.get("reply_hold_seconds", 300))
            else:
                self.exclude_all_groups_except = []
                self.save_filters()
        except Exception as e:
            print(f"[WhatsApp] Error loading filters: {e}")

    def save_filters(self):
        try:
            self.filters_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filters_path, "w", encoding="utf-8") as f:
                json.dump({
                    "ignore_all_groups": self.ignore_all_groups,
                    "ignored_contacts": self.ignored_contacts,
                    "ignored_groups": self.ignored_groups,
                    "muted_threads": self.muted_threads,
                    "exclude_all_groups_except": getattr(self, "exclude_all_groups_except", []),
                    "per_contact": self.per_contact,
                    "is_busy": self.is_busy,
                    "busy_since": self.busy_since,
                    "busy_instruction": self.busy_instruction,
                    "user_profile": self.user_profile,
                    "reply_hold_seconds": self.reply_hold_seconds,
                }, f, indent=4)
        except Exception as e:
            print(f"[WhatsApp] Error saving filters: {e}")

    def _is_ignored(self, sender, profile_name, is_group):
        if not sender:
            return True

        # Check exclude_all_groups_except whitelist
        if is_group and getattr(self, "exclude_all_groups_except", []):
            sender_clean = sender.split("@")[0]
            whitelisted = False
            for g in self.exclude_all_groups_except:
                g_lower = g.lower().strip()
                if (sender and g_lower in sender.lower()) or (sender_clean and g_lower in sender_clean.lower()) or (profile_name and g_lower in profile_name.lower()):
                    whitelisted = True
                    break
            if not whitelisted:
                return True
            
        # Check ignore_all_groups
        if is_group and self.ignore_all_groups:
            return True

        sender_clean = sender.split("@")[0]

        # Check ignored_groups
        if is_group:
            if sender in self.ignored_groups or sender_clean in self.ignored_groups or profile_name in self.ignored_groups:
                return True
        else:
            # Check ignored_contacts
            if sender in self.ignored_contacts or sender_clean in self.ignored_contacts or profile_name in self.ignored_contacts:
                return True
                
        return False
    def _is_mentioned_in_group(self, text):
        """
        Fallback mention check used only when bot.js mentionedMe flag is False.

        Two body formats exist depending on WA/wwebjs version:
          • @919876543210 some_text          ← number in body (older)
          • @\u2068Display Name\u2069 text   ← LRM-wrapped display name (newer)

        We detect both. The number check uses self.my_number (session-resolved, no
        hardcoding). The LRM check catches any @⁨...⁩ pattern — a sufficient signal
        that SOMEONE was @mentioned; the caller (busy-mode gate) is already inside a
        whitelisted group, so a false positive here is acceptable.
        """
        if not text:
            return False

        # Format 1: @phonenumber in body — needs self.my_number resolved from session
        if self.my_number:
            clean_num = self.my_number.split("@")[0]
            if f"@{clean_num}" in text:
                return True

        # Format 2: @⁨Name⁩ — WhatsApp wraps display names with U+2068 / U+2069
        # Presence of this pattern means someone was @mentioned via the autocomplete picker
        if "\u2068" in text and "@" in text:
            return True

        return False

    def _is_stop_command(self, text):
        if not text:
            return False
        text_lower = text.lower().strip()
        stop_phrases = [
            "stop replying", "dont reply", "don't reply", "stop", "chup", "chup kor",
            "chup korbi", "shut up", "mute", "stop bot", "stop ai", "silence"
        ]
        for phrase in stop_phrases:
            if phrase in text_lower:
                return True
        return False

    def _is_explicit_summon(self, text):
        if not text:
            return False
        text_lower = text.lower()
        if "orion" in text_lower:
            return True
        if "@orion" in text_lower:
            return True
        if re.search(r'\bai\b', text_lower):
            return True
        return False

    def _last_ai_message_was_leave_msg(self, context):
        normalized = self._normalize_context_messages(context)
        # Find the most recent outgoing message from Orion
        for msg in reversed(normalized):
            if self._is_outgoing_message(msg):
                body_lower = msg.get("body", "").lower()
                if "leave a message" in body_lower or "leave a note" in body_lower or "get back to you" in body_lower:
                    return True
                break  # only check the last outgoing message
        return False

    def _is_muted_and_not_summoned(self, sender, text, context):
        entry = self.muted_threads.get(sender)
        is_opt_out = entry is not None

        if is_opt_out:
            # Check if timed mute has expired (backward-compatible: old entries have no "until")
            until = entry.get("until") if isinstance(entry, dict) else None
            if until:
                try:
                    if datetime.now() >= datetime.fromisoformat(until):
                        self.muted_threads.pop(sender, None)
                        self.save_filters()
                        is_opt_out = False
                        print(f"[WhatsApp] Timed mute for {sender} expired — auto-lifted.")
                except (ValueError, TypeError):
                    pass  # malformed timestamp: treat as permanent

        is_after_leave_msg = self._last_ai_message_was_leave_msg(context)

        if is_opt_out or is_after_leave_msg:
            if self._is_explicit_summon(text):
                if is_opt_out:
                    self.muted_threads.pop(sender, None)
                    self.save_filters()
                return False  # explicitly summoned — always reply
            return True  # muted and not summoned

        return False

    def _is_in_cooldown(self, sender):
        """
        60-second burst guard — only prevents duplicate auto-replies if two messages
        arrive at almost the same moment. Does NOT block conversation follow-ups.
        The LLM prompt already handles variation through conversation history.
        """
        entry = self.per_contact.get(sender, {})
        last_reply_at = entry.get("last_auto_reply_at")
        if not last_reply_at:
            return False
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_reply_at)).total_seconds()
            return False #elapsed < 60 | PERSONAL: KEEPING FALSE FOR INSTANT REPLIES
        except (ValueError, TypeError):
            return False

    #  Public Filter Configuration API

    def ignore_contact(self, contact):
        """Add contact JID, phone number or name to the ignored list."""
        if contact and contact not in self.ignored_contacts:
            self.ignored_contacts.append(contact)
            self.save_filters()
            return True
        return False

    def unignore_contact(self, contact):
        """Remove contact JID, phone number or name from the ignored list."""
        if contact and contact in self.ignored_contacts:
            self.ignored_contacts.remove(contact)
            self.save_filters()
            return True
        return False

    def ignore_group(self, group):
        """Add group JID or name to the ignored list."""
        if group and group not in self.ignored_groups:
            self.ignored_groups.append(group)
            self.save_filters()
            return True
        return False

    def unignore_group(self, group):
        """Remove group JID or name from the ignored list."""
        if group and group in self.ignored_groups:
            self.ignored_groups.remove(group)
            self.save_filters()
            return True
        return False

    def set_ignore_all_groups(self, enabled):
        """Set whether to ignore all groups entirely."""
        self.ignore_all_groups = bool(enabled)
        self.save_filters()

    def set_exclude_all_groups_except(self, groups_list):
        """Set a whitelist of groups to include. If non-empty, all other groups are excluded."""
        self.exclude_all_groups_except = groups_list if isinstance(groups_list, list) else ([groups_list] if groups_list else [])
        self.save_filters()
    #  Direction helpers

    def _normalize_direction(self, direction):
        return str(direction or "").strip().upper()

    def _is_outgoing_message(self, msg):
        direction = self._normalize_direction(msg.get("direction"))
        return direction in {
            "OUTBOUND", "OUT", "OUTGOING", "SENT",
            "BOT", "REPLY", "AI", "ASSISTANT", "ORION"
        }

    def _normalize_context_messages(self, context):
        normalized = []
        for msg in context or []:
            body = str(msg.get("body") or msg.get("text") or "").strip()
            if not body:
                continue
            normalized.append({
                "direction" : self._normalize_direction(msg.get("direction")),
                "body"      : body,
                "timestamp" : str(msg.get("timestamp") or "").strip(),
            })
        return normalized

    #  Context helpers

    def _fetch_context_window(self, sender, context, limit=20, skip_http_fetch=False):
        normalized = self._normalize_context_messages(context)
        
        if sender and not skip_http_fetch:
            try:
                fetched      = self.fetch_context(sender, limit=limit) or []
                fetched_norm = self._normalize_context_messages(fetched)
                if len(fetched_norm) > len(normalized):
                    normalized = fetched_norm
            except Exception:
                pass
        return normalized[-limit:]

    def _build_context_str(self, context):
        lines = []
        for msg in self._normalize_context_messages(context):
            role = "Orion" if self._is_outgoing_message(msg) else "Them"
            lines.append(f"{role}: {msg['body']}")
        return "\n".join(lines) if lines else ""

    def _format_context_section(self, messages, title):
        if not messages:
            return f"{title}:\n- none"
        lines = [f"{title}:"]
        for i, msg in enumerate(messages, start=1):
            role = "ASSISTANT" if self._is_outgoing_message(msg) else "USER"
            ts   = f" [{msg['timestamp']}]" if msg.get("timestamp") else ""
            lines.append(f"{i}. {role}{ts}: {msg['body']}")
        return "\n".join(lines)

    #  Contact state

    def _has_introduced(self, sender, context):
        for msg in self._normalize_context_messages(context):
            if self._is_outgoing_message(msg):
                return True
        with self.state_lock:
            return self.contact_state.get(sender, {}).get("has_introduced", False)

    def _update_contact_state_from_context(self, sender, context):
        if not sender:
            return
        has_outgoing = any(
            self._is_outgoing_message(m)
            for m in self._normalize_context_messages(context)
        )
        with self.state_lock:
            state = self.contact_state.get(sender, {
                "has_introduced"     : False,
                "auto_reply_count"   : 0,
                "last_seen"          : None,
                "last_direction_out" : False,
            })
            if has_outgoing:
                state["has_introduced"]     = True
                state["last_direction_out"] = True
            state["last_seen"] = datetime.now().isoformat()
            self.contact_state[sender] = state

    def reset_contact_state(self, sender=None):
        with self.state_lock:
            if sender is None:
                self.contact_state.clear()
            else:
                self.contact_state.pop(sender, None)

    #  Auto-reply helpers

    def _sanitize_reply(self, reply_text, already_introduced):
        if not reply_text:
            return reply_text
        text = reply_text.strip()
        if not already_introduced:
            return text
        patterns = [
            r"^(hi|hello|hey)[,!\s]+i'?m\s+orion[,!\s-]*",
            r"^(hi|hello|hey)[,!\s]+this is orion[,!\s-]*",
            r"^(hi|hello|hey)[,!\s]+i am orion[,!\s-]*",
            r"^i'?m\s+orion[,!\s-]*",
            r"^this is orion[,!\s-]*",
        ]
        for pattern in patterns:
            cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
            if cleaned and cleaned != text:
                return cleaned
        return text

    #  Human-first hold logic

    def _should_defer_reply(self, sender):
        """
        Returns True if the hold window is still active for this sender —
        meaning the owner manually replied to them recently and Orion should wait.
        """
        if self.reply_hold_seconds <= 0:
            return False
        with self._deferred_lock:
            hold_until = self._hold_until.get(sender)
        if not hold_until:
            return False
        return datetime.now() < hold_until

    def _schedule_deferred_reply(self, sender, profile_name, text, context, media, msg_id):
        """
        Queue an auto-reply to fire when the hold window expires.
        Each new message from the contact replaces the previous queued reply
        so only the latest message gets replied to.
        """
        with self._deferred_lock:
            old = self._deferred_timers.pop(sender, None)
            if old:
                old.cancel()

            hold_until = self._hold_until.get(sender)
            if not hold_until:
                return
            wait = max(1, (hold_until - datetime.now()).total_seconds())

            timer = threading.Timer(
                wait,
                self._fire_deferred_reply,
                args=(sender, profile_name, text, context, media, msg_id),
            )
            timer.daemon = True
            timer.start()
            self._deferred_timers[sender] = timer

        mins, secs = int(wait // 60), int(wait % 60)
        wait_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        print(f"[WhatsApp] Reply to {profile_name} deferred — firing in {wait_str} if owner stays silent.")

    def _fire_deferred_reply(self, sender, profile_name, text, context, media, msg_id):
        """
        Called when the hold window expires. Sends auto-reply only if:
          • busy mode is still on
          • hold window has genuinely expired (owner didn't send again)
        """
        with self._deferred_lock:
            self._deferred_timers.pop(sender, None)
            hold_until = self._hold_until.get(sender)

        if not self.is_busy:
            return

        # Safety net: if hold was extended since we scheduled, don't fire
        if hold_until and datetime.now() < hold_until:
            if self.debug:
                print(f"{GRAY}[WhatsApp] Deferred reply to {profile_name} cancelled — hold extended.{RESET}")
            return

        print(f"[WhatsApp] Hold expired — firing deferred auto-reply to {profile_name}.")
        threading.Thread(
            target=self._handle_auto_reply,
            args=(sender, profile_name, text, context, media, msg_id),
            daemon=True,
        ).start()

    def _build_auto_reply_prompt(self, sender, profile_name, text, context, media=None):
        context20 = self._fetch_context_window(sender, context, limit=20, skip_http_fetch=True)
        primary5  = context20[-5:]
        extended  = context20[:-5] if len(context20) > 5 else []

        already_introduced = self._has_introduced(sender, context20)
        conversation_state = "FOLLOW_UP" if already_introduced else "FIRST_REPLY"

        now     = datetime.now()
        now_str = now.strftime("%A, %d %B %Y at %H:%M")

        # How long has the user been in busy mode?
        busy_duration_str = ""
        if self.busy_since:
            try:
                delta = now - datetime.fromisoformat(self.busy_since)
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                if h > 0:
                    busy_duration_str = f"about {h}h {m}m" if m else f"about {h}h"
                else:
                    busy_duration_str = f"about {m} minutes"
            except (ValueError, TypeError):
                pass

        # How recently did we last auto-reply to this person?
        pc = self.per_contact.get(sender, {})
        last_reply_at = pc.get("last_auto_reply_at")
        reply_timing_str = "This is the first auto-reply to this person."
        if last_reply_at:
            try:
                last_dt = datetime.fromisoformat(last_reply_at)
                delta   = now - last_dt
                mins    = int(delta.total_seconds() / 60)
                hours   = mins // 60
                if hours > 24:
                    reply_timing_str = f"You last auto-replied to this person {hours // 24}d ago."
                elif hours > 0:
                    reply_timing_str = f"You last auto-replied to this person {hours}h {mins % 60}m ago."
                else:
                    reply_timing_str = f"You last auto-replied to this person {mins} minutes ago."
            except (ValueError, TypeError):
                pass

        # Build media description
        media_desc = None
        if media and media.get("type") not in (None, "text", "chat"):
            mtype   = media.get("type", "media")
            caption = media.get("caption") or ""
            if mtype == "image":
                media_desc = f"[They sent an image{': ' + caption if caption else ''}]"
            elif mtype == "video":
                if media.get("isGif"):
                    media_desc = f"[They sent a GIF{': ' + caption if caption else ''}]"
                else:
                    dur = media.get("duration")
                    media_desc = f"[They sent a video{' (' + str(dur) + 's)' if dur else ''}{': ' + caption if caption else ''}]"
            elif mtype == "ptt":
                dur = media.get("duration")
                media_desc = f"[They sent a voice note{' (' + str(dur) + 's)' if dur else ''}]"
            elif mtype == "audio":
                media_desc = "[They sent an audio file]"
            elif mtype == "sticker":
                media_desc = "[They sent an animated sticker]" if media.get("isAnimated") else "[They sent a sticker]"
            elif mtype == "document":
                fname = media.get("filename", "")
                media_desc = f"[They sent a document{': ' + fname if fname else ''}]"
            elif mtype == "location":
                lat = media.get("latitude", "?")
                lon = media.get("longitude", "?")
                media_desc = f"[They shared a location ({lat}, {lon})]"
            elif mtype == "vcard":
                media_desc = "[They shared a contact card]"
            elif mtype == "revoked":
                media_desc = "[They deleted a message]"
            else:
                media_desc = f"[They sent a {mtype}]"

        if already_introduced:
            intro_rule = (
                "You have already introduced yourself earlier in this conversation. "
                "Do NOT say your name again. Do NOT greet them with their name. "
                "Just continue the conversation naturally, like a person mid-chat would."
            )
        else:
            intro_rule = (
                "This is your first message to this person. "
                "Introduce yourself once as Orion, the user's assistant. "
                "Keep it brief — one line max."
            )

        media_rule = ""
        if media_desc:
            media_rule = (
                f"\n10. The contact sent media (not text): {media_desc}. "
                "Acknowledge it naturally. Do NOT pretend you can see or describe the actual content."
            )

        busy_context = f"The user has been in busy mode for {busy_duration_str}." if busy_duration_str else ""
        reply_history = reply_timing_str

        user_context_parts = []
        if self.user_profile:
            user_context_parts.append(f"User Profile:\n{self.user_profile}")
        if self.busy_instruction:
            user_context_parts.append(f"Busy Instruction:\n{self.busy_instruction}")
        context_section = ("Context about the user:\n" + "\n\n".join(user_context_parts) + "\n\n") if user_context_parts else ""

        system_prompt = (
            f"You are Orion, a personal AI assistant managing WhatsApp messages on behalf of a user who is currently busy.\n"
            f"Current date and time: {now_str}.\n"
            f"{busy_context}\n"
            f"Reply history with this contact: {reply_history}\n\n"
            f"{context_section}"
            "CORE RULES:\n"
            "1. The user is busy and cannot respond personally. Handle the conversation for them.\n"
            "2. Write like a real person texting, not an AI. Short, natural sentences.\n"
            "3. Vary your replies — never send the same 'I'm busy' line twice in a row. The history shows what you've already said.\n"
            "4. If you replied recently (< 30 min), only reply again if they said something new and substantive.\n"
            "5. Tell the contact the user is busy and ask them to leave a message — but only once per conversation, not repeatedly.\n"
            "6. Never start with a greeting after the first reply.\n"
            "7. If asked how long the user will be busy, use the busy duration above if available, otherwise say you don't know exactly.\n"
            "8. Keep replies to 1-2 sentences unless the question genuinely needs more.\n"
            "9. Be time-aware: use the current time naturally (e.g. 'good morning', 'good evening', 'it's late') when it fits.\n"
            f"10. {intro_rule}"
            f"{media_rule}"
        )

        current_msg_line = media_desc if (media_desc and not text.strip()) else f"USER: {text}"

        prompt_parts = [
            f"Contact name: {profile_name}",
            f"Conversation state: {conversation_state}",
            "",
            self._format_context_section(primary5, "PRIMARY_CONTEXT (most recent 5 messages)"),
        ]
        if extended:
            prompt_parts.extend([
                "",
                self._format_context_section(extended, "EXTENDED_CONTEXT (older messages, use only if needed)"),
            ])
        prompt_parts.extend([
            "",
            "CURRENT_MESSAGE:",
            current_msg_line,
            "",
            "Write your reply now. Output only the reply text, nothing else.",
        ])

        return system_prompt, "\n".join(prompt_parts), already_introduced

    #  WebSocket lifecycle

    def start(self):
        if self.running:
            return
        self.running = True
        print("WhatsApp integration initialized and background listener started.")
        self.ws_thread = threading.Thread(target=self._run_ws_listener, daemon=True)
        self.ws_thread.start()
        self._ready_event.wait(timeout=3)

    def _run_ws_listener(self):
        while self.running:
            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                pass
            time.sleep(5)

    def _on_message(self, ws, message):
        try:
            data       = json.loads(message)
            event_type = data.get("event")
            payload    = data.get("payload", {})

            if event_type == "MESSAGE_RECEIVED":
                sender       = payload.get("sender")
                profile_name = payload.get("profileName", "Anonymous")
                text         = payload.get("text") or ""
                context      = payload.get("context_history", [])
                msg_id       = payload.get("messageId")
                is_group     = payload.get("isGroup", sender.endswith("@g.us") if sender else False)
                chat_name    = payload.get("chatName") or profile_name
                group_sender = payload.get("groupSender")   # actual sender JID inside a group
                # bot.js pre-computes whether the owner was @mentioned — trust it directly
                mentioned_me = payload.get("mentionedMe", False)
                # Media metadata — never contains the binary, just type/caption/mimetype etc.
                media        = payload.get("media", {"type": "text", "hasMedia": False})

                # Deduplicate: drop if we already processed this message ID
                if msg_id:
                    with self._seen_msg_lock:
                        if msg_id in self._seen_msg_ids:
                            return
                        self._seen_msg_ids.add(msg_id)
                        # Keep the set bounded — drop oldest if over 200
                        if len(self._seen_msg_ids) > 200:
                            self._seen_msg_ids.pop()

                if self.debug:
                    print(f"{GRAY}[WhatsApp] Message from {profile_name} ({sender}): \"{text}\"{RESET}")

                # 1. Filter out completely ignored contacts/groups
                if self._is_ignored(sender, chat_name if is_group else profile_name, is_group):
                    if self.debug:
                        print(f"{GRAY}[WhatsApp] Ignored message from {profile_name} ({sender}) by filter.{RESET}")
                    return

                self._update_contact_state_from_context(sender, context)

                if self.is_busy:
                    # 2. Filter group messages — only reply when bot.js confirmed an @mention,
                    #    OR fall back to text-based check if mentionedMe flag wasn't sent
                    if is_group and not mentioned_me and not self._is_mentioned_in_group(text):
                        if self.debug:
                            print(f"{GRAY}[WhatsApp] Group msg skipped — not mentioned."
                                  f" mentionedMe={mentioned_me}"
                                  f" text_fallback={self._is_mentioned_in_group(text)}"
                                  f" text='{(text or '')[:60]}'{RESET}")
                        return

                    # 3. Filter muted threads (including opt-out or post-leave-message state)
                    if self._is_muted_and_not_summoned(sender, text, context):
                        if self.debug:
                            print(f"{GRAY}[WhatsApp] Skipped auto-reply: thread {profile_name} is muted/leave-msg state and not summoned.{RESET}")
                        return

                    # 4. Check if they tell us to stop replying → timed 24h mute (not permanent)
                    if self._is_stop_command(text):
                        now = datetime.now()
                        self.muted_threads[sender] = {
                            "reason": "opt_out",
                            "since" : now.isoformat(),
                            "until" : (now + timedelta(hours=24)).isoformat(),
                        }
                        self.save_filters()
                        ack_msg = "Got it! I will stop replying now. Let me know if you need any assistance later (just ask for Orion)."
                        self.send_message(sender, ack_msg)
                        try:
                            from tools import wa_log_write
                            wa_log_write("SENT (auto-reply)", profile_name, sender, ack_msg)
                        except Exception:
                            pass
                        return

                    # 5. Human-first hold — if owner messaged this contact recently,
                    #    defer the auto-reply until the hold window expires.
                    if self._should_defer_reply(sender):
                        self._schedule_deferred_reply(sender, profile_name, text, context, media, msg_id)
                    else:
                        threading.Thread(
                            target=self._handle_auto_reply,
                            args=(sender, profile_name, text, context, media, msg_id),
                            daemon=True,
                        ).start()
                else:
                    with self.lock:
                        self.pending_messages.append({
                            "sender"         : sender,
                            "profileName"    : profile_name,
                            "chatName"       : chat_name,
                            "groupSender"    : group_sender,
                            "isGroup"        : is_group,
                            "text"           : text,
                            "media"          : media,
                            "timestamp"      : datetime.now().isoformat(),
                            "context_history": context,
                        })

                try:
                    from tools import wa_log_write
                    log_text = text or f"[{media.get('type', 'media')}]"
                    wa_log_write("RECEIVED", profile_name, sender, log_text)
                except Exception:
                    pass

            elif event_type == "MESSAGE_SENT":
                to_jid = payload.get("to")
                if not to_jid:
                    pass
                else:
                    with self._deferred_lock:
                        is_orion_echo = to_jid in self._orion_echo_expected
                        if is_orion_echo:
                            # This is the WS echo of our own auto-reply — ignore it
                            self._orion_echo_expected.discard(to_jid)
                        else:
                            # Owner sent manually — activate hold window for this contact
                            self._hold_until[to_jid] = datetime.now() + timedelta(seconds=self.reply_hold_seconds)
                            # Cancel any queued deferred reply (owner is handling it)
                            timer = self._deferred_timers.pop(to_jid, None)
                            if timer:
                                timer.cancel()
                                print(f"[WhatsApp] Deferred reply to {to_jid} cancelled — owner replied manually.")
                            mins = self.reply_hold_seconds // 60
                            print(f"[WhatsApp] Owner sent to {to_jid} — hold active for {mins}m.")

            elif event_type == "SYSTEM_QR_REQUIRED":
                qr_code = payload.get("qr")
                print(f"\n{YELLOW}[WhatsApp] QR scan required. Please scan with WhatsApp:{RESET}")
                if qr_code:
                    subprocess.run(
                        ["node", "-e",
                         f"require('{qrcode_module}').generate(process.env.QR_CODE, {{small: true}})"],
                        env={**os.environ, "QR_CODE": qr_code},
                    )

            elif event_type == "SYSTEM_STATUS":
                state   = payload.get("state", "UNKNOWN")
                qr_code = payload.get("qr")
                self.connection_state = state
                if "myNumber" in payload:
                    self.my_number = payload.get("myNumber", "")
                if state in ("READY", "CONNECTED"):
                    self._ready_event.set()
                else:
                    print(f"[WhatsApp] Status: {state}")
                if state == "QR_REQUIRED" and qr_code:
                    print(f"{YELLOW}[WhatsApp] QR scan required. Please scan with WhatsApp:{RESET}")
                    subprocess.run(
                        ["node", "-e",
                         f"require('{qrcode_module}').generate(process.env.QR_CODE, {{small: true}})"],
                        env={**os.environ, "QR_CODE": qr_code},
                    )

            elif event_type == "SYSTEM_READY":
                self.connection_state = "READY"
                if "myNumber" in payload:
                    self.my_number = payload.get("myNumber", "")
                self._ready_event.set()
                print(f"[WhatsApp] Connected and ready. My number: {self.my_number}")

            elif event_type:
                if self.debug:
                    print(f"{GRAY}[WhatsApp] Unhandled event: {event_type}{RESET}")

        except json.JSONDecodeError as e:
            print(f"{RED}[WhatsApp] Bad JSON from server: {e}{RESET}")
        except Exception as e:
            print(f"{RED}[WhatsApp] Error handling message: {e}{RESET}")

    def _on_error(self, ws, error):
        self.connection_state = "ERROR"
        if self.debug:
            print(f"{RED}[WhatsApp] WebSocket error: {error}{RESET}")

    def _on_close(self, ws, close_status_code, close_msg):
        self.connection_state = "DISCONNECTED"
        if self.debug:
            print(f"{YELLOW}[WhatsApp] Connection closed (code={close_status_code}). Reconnecting in 5s...{RESET}")

    #  Auto-reply

    def _handle_auto_reply(self, sender, profile_name, text, context, media=None, msg_id=None):
        # Drop duplicate concurrent calls for the same sender
        with self._active_lock:
            if sender in self._active_senders:
                if self.debug:
                    print(f"{GRAY}[WhatsApp] Auto-reply already in progress for {profile_name}, skipping.{RESET}")
                return
            self._active_senders.add(sender)

        try:
            self._do_auto_reply(sender, profile_name, text, context, media, msg_id)
        finally:
            with self._active_lock:
                self._active_senders.discard(sender)

    def _do_auto_reply(self, sender, profile_name, text, context, media=None, msg_id=None):
        # Skip if within 30-min cooldown (they already know we're busy, no need to repeat)
        # Explicit summon bypasses this — handled before reaching here via _is_explicit_summon
        if self._is_in_cooldown(sender):
            if self.debug:
                print(f"{GRAY}[WhatsApp] Auto-reply skipped: {profile_name} is in cooldown.{RESET}")
            return

        if self.debug:
            print(f"{GRAY}[WhatsApp] Auto-reply generating for {profile_name}...{RESET}")

        # Show typing indicator while LLM generates (15s safety timeout)
        self.set_typing(sender, duration_ms=15000)

        system_prompt, prompt, already_introduced = self._build_auto_reply_prompt(
            sender, profile_name, text, context, media
        )

        try:
            from tools import ask_ai_simple
            reply_text = ask_ai_simple(prompt, "gemini-2.5-flash-lite", system_prompt)

            if reply_text and not reply_text.startswith("[EMPTY"):
                reply_text = self._sanitize_reply(reply_text, already_introduced)
                # Send as a quoted reply to the triggering message when we have its ID
                success = self.send_message(sender, reply_text, quoted_msg_id=msg_id)

                if success:
                    # Mark the chat as read on our phone
                    self.set_seen(sender)

                    if self.debug:
                        print(f"{GRAY}[WhatsApp] Auto-reply sent to {profile_name}: \"{reply_text}\"{RESET}")

                    now = datetime.now()
                    with self.state_lock:
                        state = self.contact_state.get(sender, {
                            "has_introduced"     : False,
                            "auto_reply_count"   : 0,
                            "last_seen"          : None,
                            "last_direction_out" : False,
                        })
                        state["has_introduced"]     = True
                        state["last_direction_out"] = True
                        state["auto_reply_count"]   = state.get("auto_reply_count", 0) + 1
                        self.contact_state[sender]  = state

                        pc = self.per_contact.get(sender, {})
                        pc["last_auto_reply_at"] = now.isoformat()
                        self.per_contact[sender] = pc

                    self.save_filters()

                    try:
                        from tools import wa_log_write
                        wa_log_write("SENT (auto-reply)", profile_name, sender, reply_text)
                    except Exception:
                        pass
                else:
                    if self.debug:
                        print(f"{GRAY}[WhatsApp] Auto-reply send failed for {profile_name}.{RESET}")
            else:
                if self.debug:
                    print(f"{GRAY}[WhatsApp] Empty response generated. No reply sent.{RESET}")

        except Exception as e:
            print(f"[WhatsApp] Auto-reply error: {e}")

    #  HTTP API

    def send_message(self, to_phone, message_text, quoted_msg_id=None):
        url     = f"{BASE_URL}/api/send"
        payload = {"to": to_phone, "message": message_text}
        if quoted_msg_id:
            payload["quotedMessageId"] = quoted_msg_id
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            ok = response.json().get("success", False)
            if ok:
                # Mark this JID so the MESSAGE_SENT WS echo is identified as ours
                # and not mistaken for a manual send by the owner.
                jid = to_phone if "@" in to_phone else to_phone.replace("+", "").replace(" ", "") + "@c.us"
                with self._deferred_lock:
                    self._orion_echo_expected.add(jid)
            return ok
        except Exception as e:
            print(f"{RED}[WhatsApp] Send error: {e}{RESET}")
            return False

    def set_typing(self, to_jid, duration_ms=15000):
        """Show 'typing...' indicator in the chat. Auto-clears after duration_ms."""
        try:
            requests.post(f"{BASE_URL}/api/typing",
                          json={"to": to_jid, "duration": duration_ms}, timeout=5)
        except Exception:
            pass

    def set_seen(self, to_jid):
        """Mark the chat as read — clears unread badge on the phone."""
        try:
            requests.post(f"{BASE_URL}/api/seen", json={"to": to_jid}, timeout=5)
        except Exception:
            pass

    def react(self, message_id, emoji):
        """React to a specific message with an emoji."""
        try:
            r = requests.post(f"{BASE_URL}/api/react",
                              json={"messageId": message_id, "emoji": emoji}, timeout=10)
            return r.json().get("success", False)
        except Exception as e:
            print(f"{RED}[WhatsApp] React error: {e}{RESET}")
            return False

    def get_contact_info(self, jid):
        """Fetch profile info (name, number, about, profile pic URL) for a contact JID."""
        try:
            r = requests.get(f"{BASE_URL}/api/contact/{jid}", timeout=15)
            r.raise_for_status()
            return r.json().get("contact", {})
        except Exception as e:
            print(f"{RED}[WhatsApp] Contact info error: {e}{RESET}")
            return {}

    def get_group_participants(self, jid):
        """Return (participants_list, group_name) for a group JID."""
        try:
            r = requests.get(f"{BASE_URL}/api/group/{jid}/participants", timeout=15)
            r.raise_for_status()
            data = r.json()
            return data.get("participants", []), data.get("groupName", "")
        except Exception as e:
            print(f"{RED}[WhatsApp] Group participants error: {e}{RESET}")
            return [], ""

    def download_media(self, message_id):
        """Download media from a message. Returns dict with mimetype/filename/data(base64)."""
        try:
            r = requests.post(f"{BASE_URL}/api/media",
                              json={"messageId": message_id}, timeout=60)
            r.raise_for_status()
            data = r.json()
            if data.get("success"):
                return data
        except Exception as e:
            print(f"{RED}[WhatsApp] Media download error: {e}{RESET}")
        return {}

    def search_chat(self, jid, query, limit=20):
        """Search for messages containing query in a specific chat."""
        try:
            r = requests.post(f"{BASE_URL}/api/search",
                              json={"to": jid, "query": query, "limit": limit}, timeout=20)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            print(f"{RED}[WhatsApp] Search error: {e}{RESET}")
            return []

    def archive_chat(self, jid, archive=True):
        """Archive or unarchive a chat."""
        try:
            r = requests.post(f"{BASE_URL}/api/archive",
                              json={"to": jid, "archive": archive}, timeout=10)
            return r.json().get("success", False)
        except Exception as e:
            print(f"{RED}[WhatsApp] Archive error: {e}{RESET}")
            return False

    def schedule_message(self, to_phone, message, send_at_iso):
        """Schedule a message to send at a specific ISO datetime. Returns (ok, info_str)."""
        try:
            target_dt     = datetime.fromisoformat(send_at_iso)
            delay_seconds = (target_dt - datetime.now()).total_seconds()
            if delay_seconds <= 0:
                return False, "Scheduled time is in the past."
            timer = threading.Timer(delay_seconds, self._fire_scheduled, args=[to_phone, message])
            timer.daemon = True
            timer.start()
            with self.lock:
                if not hasattr(self, "_scheduled"):
                    self._scheduled = []
                self._scheduled.append({"to": to_phone, "message": message, "send_at": send_at_iso})
            mins = int(delay_seconds / 60)
            until_str = target_dt.strftime("%H:%M on %d %b")
            return True, f"Scheduled in {mins}m (fires at {until_str})."
        except Exception as e:
            return False, str(e)

    def _fire_scheduled(self, to_phone, message):
        success = self.send_message(to_phone, message)
        print(f"[WhatsApp] Scheduled message {'sent' if success else 'FAILED'} → {to_phone}")
        with self.lock:
            if hasattr(self, "_scheduled"):
                self._scheduled = [s for s in self._scheduled
                                   if not (s["to"] == to_phone and s["message"] == message)]

    def fetch_context(self, to_phone, limit=5):
        url     = f"{BASE_URL}/api/context"
        payload = {"to": to_phone, "limit": limit}
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            if data.get("success"):
                return data.get("history", [])
        except Exception as e:
            print(f"{RED}[WhatsApp] Fetch context error: {e}{RESET}")
        return []

    def get_chats(self, filter_type="all"):
        """Fetch the full chat/group list. filter_type: 'all' | 'dm' | 'group'."""
        try:
            r = requests.get(f"{BASE_URL}/api/chats", timeout=15)
            r.raise_for_status()
            chats = r.json().get("chats", [])
        except Exception as e:
            print(f"{RED}[WhatsApp] get_chats error: {e}{RESET}")
            return []
        if filter_type == "dm":    return [c for c in chats if c.get("type") == "dm"]
        if filter_type == "group": return [c for c in chats if c.get("type") == "group"]
        return chats

    #  Public API

    def get_pending_messages(self, clear=True):
        with self.lock:
            messages = list(self.pending_messages)
            if clear:
                self.pending_messages.clear()
            return messages

    def set_busy(self, enabled, instruction=""):
        self.is_busy = enabled
        self.busy_since = datetime.now().isoformat() if enabled else None
        if instruction:
            self.busy_instruction = instruction
        if not enabled:
            with self._deferred_lock:
                for timer in self._deferred_timers.values():
                    timer.cancel()
                self._deferred_timers.clear()
                self._hold_until.clear()
                self._orion_echo_expected.clear()
        self.save_filters()

    def silence_contact(self, jid, hours=24):
        """
        Manually silence auto-replies to a JID for N hours.
        Pass hours=0 to lift an existing silence immediately.
        """
        if hours == 0:
            removed = jid in self.muted_threads
            self.muted_threads.pop(jid, None)
            self.save_filters()
            return removed, f"Silence lifted for {jid}."
        now   = datetime.now()
        until = now + timedelta(hours=hours)
        self.muted_threads[jid] = {
            "reason": "manual",
            "since" : now.isoformat(),
            "until" : until.isoformat(),
        }
        self.save_filters()
        return True, f"Silenced for {hours}h (until {until.strftime('%H:%M on %d %b')})."

    def set_user_profile(self, profile):
        self.user_profile = profile
        self.save_filters()

    def set_reply_hold(self, seconds):
        """
        Set how long (in seconds) to wait after the owner's last message before
        auto-replying. Common values: 60, 120, 300, 600.
        Pass 0 to disable the hold entirely (always reply immediately).
        """
        self.reply_hold_seconds = max(0, int(seconds))
        self.save_filters()
        mins = self.reply_hold_seconds // 60
        return f"Reply hold set to {self.reply_hold_seconds}s ({mins}m)." if mins else f"Reply hold set to {self.reply_hold_seconds}s."


# Singleton instance
whatsapp_manager = WhatsAppManager()
