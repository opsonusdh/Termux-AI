import os
import json
import time
import re
import requests
import threading
import websocket
import subprocess
from datetime import datetime
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
RESET = "\033[0m"


class WhatsAppManager:
    def __init__(self):
        self.pending_messages = []
        self.lock             = threading.Lock()

        self.contact_state = {}
        self.state_lock    = threading.Lock()

        self.is_busy = False
        self.busy_instruction = (
            "You are Orion, the personal AI assistant of the user. "
            "The user is currently busy and cannot respond. "
            "Reply briefly, politely, and naturally. "
            "Do not repeat your identity unless this is the first reply in the conversation."
        )

        self.ws_thread        = None
        self.running          = False
        self.connection_state = "DISCONNECTED"
        self.debug            = False
        self._ready_event     = threading.Event()
        self._seen_msg_ids    = set()
        self._seen_msg_lock   = threading.Lock()
        self._active_senders  = set()  # senders with an auto-reply in progress
        self._active_lock     = threading.Lock()

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

    def _fetch_context_window(self, sender, context, limit=20):
        normalized = self._normalize_context_messages(context)
        if sender:
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

    def _build_auto_reply_prompt(self, sender, profile_name, text, context):
        context20 = self._fetch_context_window(sender, context, limit=20)
        primary5  = context20[-5:]
        extended  = context20[:-5] if len(context20) > 5 else []

        already_introduced = self._has_introduced(sender, context20)
        conversation_state = "FOLLOW_UP" if already_introduced else "FIRST_REPLY"

        if already_introduced:
            intro_rule = (
                "You have already introduced yourself earlier in this conversation. "
                "Do NOT say your name again. Do NOT greet them with their name. "
                "Do NOT say 'Hi [name]' or 'Hello [name]'. "
                "Just continue the conversation naturally, like a person mid-chat would."
            )
        else:
            intro_rule = (
                "This is your first message to this person. "
                "Introduce yourself once as Orion, the user's assistant. "
                "Keep it brief — one line max. "
                "Do NOT repeat the introduction in the same message."
            )

        system_prompt = (
            "You are Orion, a personal AI assistant managing WhatsApp messages for a busy user.\n\n"
            "CORE RULES — follow these strictly:\n"
            "1. Write like a real person texting, not an AI assistant. Short, natural sentences.\n"
            "2. Never start a reply with the contact's name (e.g. never write 'Hi Sumana,' or 'Hello Sumana,').\n"
            "3. Never repeat yourself across messages. Read the conversation history and vary your response.\n"
            "4. If they ask a real question, answer it. Do not ignore it and just say the user is busy.\n"
            "5. If they ask how long the user will be busy, say you don't know exactly but you'll pass the message on.\n"
            "6. Keep replies to 1-2 sentences unless the question genuinely needs more.\n"
            f"7. {intro_rule}\n\n"
            f"User instruction: {self.busy_instruction}"
        )

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
            f"USER: {text}",
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
                text         = payload.get("text", "")
                context      = payload.get("context_history", [])
                msg_id       = payload.get("messageId")

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

                self._update_contact_state_from_context(sender, context)

                if self.is_busy:
                    threading.Thread(
                        target=self._handle_auto_reply,
                        args=(sender, profile_name, text, context),
                        daemon=True,
                    ).start()
                else:
                    with self.lock:
                        self.pending_messages.append({
                            "sender"         : sender,
                            "profileName"    : profile_name,
                            "text"           : text,
                            "timestamp"      : datetime.now().isoformat(),
                            "context_history": context,
                        })

                try:
                    from tools import wa_log_write
                    wa_log_write("RECEIVED", profile_name, sender, text)
                except Exception:
                    pass

            elif event_type == "SYSTEM_QR_REQUIRED":
                qr_code = payload.get("qr")
                print("\n[WhatsApp] QR scan required. Please scan with WhatsApp:")
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
                self._ready_event.set()
                if state not in ("READY", "CONNECTED"):
                    print(f"[WhatsApp] Status: {state}")
                if state == "QR_REQUIRED" and qr_code:
                    print("[WhatsApp] QR scan required. Please scan with WhatsApp:")
                    subprocess.run(
                        ["node", "-e",
                         f"require('{qrcode_module}').generate(process.env.QR_CODE, {{small: true}})"],
                        env={**os.environ, "QR_CODE": qr_code},
                    )

            elif event_type == "SYSTEM_READY":
                self.connection_state = "READY"
                self._ready_event.set()
                print("[WhatsApp] Connected and ready.")

            elif event_type:
                if self.debug:
                    print(f"{GRAY}[WhatsApp] Unhandled event: {event_type}{RESET}")

        except json.JSONDecodeError as e:
            print(f"[WhatsApp] Bad JSON from server: {e}")
        except Exception as e:
            print(f"[WhatsApp] Error handling message: {e}")

    def _on_error(self, ws, error):
        self.connection_state = "ERROR"
        if self.debug:
            print(f"{GRAY}[WhatsApp] WebSocket error: {error}{RESET}")

    def _on_close(self, ws, close_status_code, close_msg):
        self.connection_state = "DISCONNECTED"
        if self.debug:
            print(f"{GRAY}[WhatsApp] Connection closed (code={close_status_code}). Reconnecting in 5s...{RESET}")

    #  Auto-reply

    def _handle_auto_reply(self, sender, profile_name, text, context):
        # Drop duplicate concurrent calls for the same sender
        with self._active_lock:
            if sender in self._active_senders:
                if self.debug:
                    print(f"{GRAY}[WhatsApp] Auto-reply already in progress for {profile_name}, skipping.{RESET}")
                return
            self._active_senders.add(sender)

        try:
            self._do_auto_reply(sender, profile_name, text, context)
        finally:
            with self._active_lock:
                self._active_senders.discard(sender)

    def _do_auto_reply(self, sender, profile_name, text, context):
        if self.debug:
            print(f"{GRAY}[WhatsApp] Auto-reply generating for {profile_name}...{RESET}")

        system_prompt, prompt, already_introduced = self._build_auto_reply_prompt(
            sender, profile_name, text, context
        )

        try:
            from tools import ask_ai_simple
            reply_text = ask_ai_simple(prompt, "gemini-2.5-flash-lite", system_prompt)

            if reply_text and not reply_text.startswith("[EMPTY"):
                reply_text = self._sanitize_reply(reply_text, already_introduced)
                success    = self.send_message(sender, reply_text)

                if success:
                    if self.debug:
                        print(f"{GRAY}[WhatsApp] Auto-reply sent to {profile_name}: \"{reply_text}\"{RESET}")
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

    def send_message(self, to_phone, message_text):
        url     = f"{BASE_URL}/api/send"
        payload = {"to": to_phone, "message": message_text}
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return response.json().get("success", False)
        except Exception as e:
            print(f"[WhatsApp] Send error: {e}")
            return False

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
            print(f"[WhatsApp] Fetch context error: {e}")
        return []

    #  Public API

    def get_pending_messages(self, clear=True):
        with self.lock:
            messages = list(self.pending_messages)
            if clear:
                self.pending_messages.clear()
            return messages

    def set_busy(self, enabled, instruction=""):
        self.is_busy = enabled
        if instruction:
            self.busy_instruction = instruction


# Singleton instance
whatsapp_manager = WhatsAppManager()
