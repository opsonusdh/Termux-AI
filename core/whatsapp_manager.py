import os
import json
import time
import requests
import threading
import websocket
import subprocess
import re
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


qrcode_module = str(
    BASE_DIR
    / "Termux-WP"
    / "node_modules"
    / "qrcode-terminal"
)
if not Path(qrcode_module).exists():
    raise FileNotFoundError(f"Termux-WP is not installed or not properly configured at {BASE_DIR}")

# Define URLs for local WhatsApp Bot
BASE_URL = "http://localhost:3000"
WS_URL = "ws://localhost:3000"


class WhatsAppManager:
    def __init__(self):
        self.pending_messages = []
        self.lock = threading.Lock()

        self.contact_state = {}
        self.state_lock = threading.Lock()

        self.is_busy = False

        self.busy_instruction = (
            "You are Orion, the personal AI assistant of the user. "
            "The user is currently busy and cannot respond. "
            "Reply briefly, politely, and naturally. "
            "Do not repeat your identity unless this is the first reply in the conversation."
        )

        self.ws_thread = None
        self.running = False
        self.connection_state = "DISCONNECTED"
        self.debug = False


    def _normalize_direction(self, direction):
        return str(direction or "").strip().upper()

    def _is_outgoing_message(self, msg):
        direction = self._normalize_direction(msg.get("direction"))
        return direction in {
        "OUT", "OUTGOING", "SENT", "BOT", "REPLY", "AI", "ASSISTANT", "ORION"
        }
    
    def _normalize_context_messages(self, context):
        normalized = []
        for msg in context or []:
            body = str(msg.get("body") or msg.get("text") or "").strip()
            if not body:
                continue
            normalized.append({
                "direction": self._normalize_direction(msg.get("direction")),
                "body": body,
                "timestamp": str(msg.get("timestamp") or "").strip(),
            })
        return normalized

    def _fetch_context_window(self, sender, context, limit=20):
        normalized = self._normalize_context_messages(context)

        if sender:
            try:
                fetched = self.fetch_context(sender, limit=limit) or []
                fetched_norm = self._normalize_context_messages(fetched)
                if len(fetched_norm) > len(normalized):
                    normalized = fetched_norm
            except Exception:
                pass

        return normalized[-limit:]

    def _format_context_section(self, messages, title):
        if not messages:
            return f"{title}:\n- none"

        lines = [f"{title}:"]
        for i, msg in enumerate(messages, start=1):
            role = "USER" if not self._is_outgoing_message(msg) else "ASSISTANT"
            ts = f" [{msg['timestamp']}]" if msg.get("timestamp") else ""
            lines.append(f"{i}. {role}{ts}: {msg['body']}")
        return "\n".join(lines)

    def _sanitize_reply(self, reply_text, has_prior_outgoing):
        if not reply_text:
            return reply_text

        text = reply_text.strip()

        if has_prior_outgoing:
            patterns = [
                r"^(hi|hello|hey)[,!\s]+(i'?m|i am|this is)\s+orion[,!\s-]*",
                r"^(i'?m|i am|this is)\s+orion[,!\s-]*",
            ]
            for pattern in patterns:
                cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
                if cleaned and cleaned != text:
                    return cleaned

        return text

    def _build_auto_reply_prompt(self, sender, profile_name, text, context):
        context20 = self._fetch_context_window(sender, context, limit=20)
        primary5 = context20[-5:]
        extended = context20[:-5] if len(context20) > 5 else []

        has_prior_outgoing = any(self._is_outgoing_message(m) for m in context20)
        conversation_state = "FOLLOW_UP" if has_prior_outgoing else "FIRST_REPLY"

        system_prompt = (
            "You are a helpful personal AI assistant replying on behalf of Subhro. "
            "The user is busy right now. "
            "Reply naturally, politely, and briefly. "
            "Do not sound robotic. "
            "Do not repeat your identity unless this is the first reply in the conversation. "
            "If this is a follow-up message, continue naturally without reintroducing yourself and without greeting the sender to keep the natural conversation flow."
        )

        prompt_parts = [
            f"Contact name: {profile_name}",
            f"Contact id: {sender}",
            f"Conversation state: {conversation_state}",
            f"Has prior assistant message: {has_prior_outgoing}",
            "",
            self._format_context_section(
                primary5,
                "PRIMARY_CONTEXT (use this first, most recent 5 messages)"
            ),
        ]

        if extended:
            prompt_parts.extend([
                "",
                self._format_context_section(
                    extended,
                    "EXTENDED_CONTEXT (older messages from the last 20, use only if needed)"
                ),
            ])

        prompt_parts.extend([
            "",
            "CURRENT_MESSAGE:",
            f"USER: {text}",
        ])

        prompt = "\n".join(prompt_parts)
        return system_prompt, prompt, has_prior_outgoing
    
    
    def start(self):
        """Starts the background WebSocket listener thread."""
        if self.running:
            return
        self.running = True
        self.ws_thread = threading.Thread(target=self._run_ws_listener, daemon=True)
        self.ws_thread.start()
        print("WhatsApp integration initialized and background listener started.")

    def _run_ws_listener(self):
        """Manages the WebSocket life-cycle loop and forces active connection recovery."""
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

            # Wait 5 seconds before retrying to prevent spamming
            time.sleep(5)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            event_type = data.get("event")
            payload = data.get("payload", {})

            if event_type == "MESSAGE_RECEIVED":
                sender = payload.get("sender")
                profile_name = payload.get("profileName", "Anonymous")
                text = payload.get("text", "")
                context = payload.get("context_history", [])

                # Print real-time alert to terminal (debug only)
                if self.debug:
                    print(f"\n[WhatsApp Alert] New Message from {profile_name} ({sender})")
                    print(f'Text: "{text}"')
                    print("-" * 40)

                # Update state from current context if possible
                self._update_contact_state_from_context(sender, context)

                if self.is_busy:
                    # Run auto-reply in a separate thread to not block WS connection
                    threading.Thread(
                        target=self._handle_auto_reply,
                        args=(sender, profile_name, text, context),
                        daemon=True
                    ).start()
                else:
                    # Append to pending queue for main chat context
                    with self.lock:
                        self.pending_messages.append({
                            "sender": sender,
                            "profileName": profile_name,
                            "text": text,
                            "timestamp": datetime.now().isoformat(),
                            "context_history": context
                        })

                # Log every incoming message regardless of busy state
                try:
                    from tools import wa_log_write
                    wa_log_write("RECEIVED", profile_name, sender, text)
                except Exception:
                    pass

            elif event_type == "SYSTEM_QR_REQUIRED":
                qr_code = payload.get("qr")
                print("\n[WhatsApp Alert] Scan authorization required!")
                if qr_code:
                    print("Generating QR code on terminal... Please scan with WhatsApp:")
                    subprocess.run(
                        f'node -e "require(\'{qrcode_module}\').generate(\'{qr_code}\', {{small: true}})"',
                        shell=True
                    )

            elif event_type == "SYSTEM_STATUS":
                state = payload.get("state", "UNKNOWN")
                self.connection_state = state
                qr_code = payload.get("qr")
                if state not in ("READY", "CONNECTED"):
                    print(f"\n[WhatsApp Status] Current Client State: {state}")
                if state == "QR_REQUIRED" and qr_code:
                    print("Generating QR code on terminal... Please scan with WhatsApp:")
                    subprocess.run(
                        f'node -e "require(\'{qrcode_module}\').generate(\'{qr_code}\', {{small: true}})"',
                        shell=True
                    )

            elif event_type == "SYSTEM_READY":
                self.connection_state = "READY"
                print("\n[WhatsApp Alert] Connected and Ready!")

            elif event_type:
                if self.debug:
                    print(f"\n[WhatsApp] Unhandled event: {event_type}")

        except json.JSONDecodeError as e:
            print(f"[WhatsApp] Failed to parse message (invalid JSON): {e}")
        except Exception as e:
            print(f"[WhatsApp] Unexpected error handling message: {e}")

    def _on_error(self, ws, error):
        self.connection_state = "ERROR"
        print(f"[WhatsApp] WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self.connection_state = "DISCONNECTED"
        if self.debug:
            print(f"[WhatsApp] WebSocket closed (code={close_status_code}). Reconnecting in 5s...")

    def _normalize_direction(self, direction):
        """Convert direction values into a predictable uppercase token."""
        if direction is None:
            return ""
        return str(direction).strip().upper()

    def _is_outgoing_direction(self, direction):
        """True if a context message was sent by Orion / the bot side."""
        d = self._normalize_direction(direction)
        return d in {
            "OUT", "OUTGOING", "SENT", "BOT", "REPLY", "AI", "ASSISTANT", "ORION"
        }

    def _context_has_outgoing(self, context):
        """Detect whether the conversation already contains an outgoing message."""
        for msg in context or []:
            try:
                if self._is_outgoing_direction(msg.get("direction")):
                    return True
            except Exception:
                continue
        return False

    def _update_contact_state_from_context(self, sender, context):
        """Use the incoming context history to restore state for this contact."""
        if not sender:
            return

        has_outgoing = self._context_has_outgoing(context)

        with self.state_lock:
            state = self.contact_state.get(sender, {
                "has_introduced": False,
                "auto_reply_count": 0,
                "last_seen": None,
                "last_direction_out": False
            })

            # If context already contains outgoing history, Orion has introduced itself before
            if has_outgoing:
                state["has_introduced"] = True
                state["last_direction_out"] = True

            state["last_seen"] = datetime.now().isoformat()
            self.contact_state[sender] = state

    def _build_reply_rules(self, sender, context):
        """
        Decide whether this reply should introduce Orion or stay silent about identity.
        """
        with self.state_lock:
            state = self.contact_state.get(sender, {
                "has_introduced": False,
                "auto_reply_count": 0,
                "last_seen": None,
                "last_direction_out": False
            })

        # If the chat history already has outgoing messages, do not introduce again.
        already_introduced = state["has_introduced"] or self._context_has_outgoing(context)

        if already_introduced:
            intro_rule = (
                "Do NOT introduce yourself again. "
                "Reply naturally, as a continuation of the same conversation."
            )
        else:
            intro_rule = (
                "This is the first reply in the conversation. "
                "Introduce yourself briefly as Orion, the user's assistant."
            )

        return already_introduced, intro_rule

    def _sanitize_reply(self, reply_text, already_introduced):
        """
        Light cleanup so the model cannot keep saying 'I'm Orion' forever.
        This is a guardrail, not the main fix.
        """
        if not reply_text:
            return reply_text

        text = reply_text.strip()

        if not already_introduced:
            return text

        # Remove repeated self-intro if the model still insists on doing it.
        patterns = [
            r'^(hi|hello|hey)[,!\s]+i\'?m\s+orion[,!\s-]*',
            r'^(hi|hello|hey)[,!\s]+this is orion[,!\s-]*',
            r'^(hi|hello|hey)[,!\s]+i am orion[,!\s-]*',
            r'^i\'?m\s+orion[,!\s-]*',
            r'^this is orion[,!\s-]*',
        ]

        for pattern in patterns:
            new_text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
            if new_text != text and new_text:
                return new_text

        return text

    def _handle_auto_reply(self, sender, profile_name, text, context):
        """Generates and sends an automatic polite response when user is busy."""
        if self.debug:
            print(f"[WhatsApp Auto-Reply] Generating automatic response for {profile_name}...")

        system_prompt, prompt, has_prior_outgoing = self._build_auto_reply_prompt(
            sender, profile_name, text, context
        )

        try:
            from tools import ask_ai_simple
            reply_text = ask_ai_simple(prompt, "gemini-2.5-flash-lite", system_prompt)

            if reply_text and not reply_text.startswith("[EMPTY"):
                reply_text = self._sanitize_reply(reply_text, has_prior_outgoing)

                success = self.send_message(sender, reply_text)
                if success:
                    if self.debug:
                        print(f'[WhatsApp Auto-Reply] Sent to {profile_name}: "{reply_text}"')
                    try:
                        from tools import wa_log_write
                        wa_log_write("SENT (auto-reply)", profile_name, sender, reply_text)
                    except Exception:
                        pass
                else:
                    if self.debug:
                        print(f"[WhatsApp Auto-Reply] Send failed for {profile_name}.")
            else:
                if self.debug:
                    print("[WhatsApp Auto-Reply] Generated empty response. No reply sent.")
        except Exception as e:
            print(f"[WhatsApp Auto-Reply] Failed to generate/send reply: {e}")

    def send_message(self, to_phone, message_text):
        """Sends an outbound WhatsApp message via HTTP POST request."""
        url = f"{BASE_URL}/api/send"
        payload = {"to": to_phone, "message": message_text}
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data.get("success", False)
        except Exception as e:
            print(f"[WhatsApp Manager] Send breakdown: {e}")
            return False

    def fetch_context(self, to_phone, limit=5):
        """Fetches the last N messages for a specific number from WhatsApp cloud."""
        url = f"{BASE_URL}/api/context"
        payload = {"to": to_phone, "limit": limit}
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            if data.get("success"):
                return data.get("history", [])
        except Exception as e:
            print(f"[WhatsApp Manager] Fetch context error: {e}")
        return []

    def get_pending_messages(self, clear=True):
        """Returns and optionally clears any pending received messages."""
        with self.lock:
            messages = list(self.pending_messages)
            if clear:
                self.pending_messages.clear()
            return messages

    def set_busy(self, enabled, instruction=""):
        """Enables or disables busy mode with an optional status/instruction."""
        self.is_busy = enabled
        if instruction:
            self.busy_instruction = instruction

    def reset_contact_state(self, sender=None):
        """Reset state for one contact or for all contacts."""
        with self.state_lock:
            if sender is None:
                self.contact_state.clear()
            else:
                self.contact_state.pop(sender, None)


# Singleton instance
whatsapp_manager = WhatsAppManager()