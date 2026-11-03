"""
Google OAuth 2.0 plumbing for the Gmail / Google Calendar / Google Drive plugins.

Why this module exists
-----------------------
The plugin manager only ever knew *that* a Google plugin existed — it had no
idea how a user actually grants access to their mailbox, calendar or Drive.
This module owns the whole "connect the account" story:

  * stores the OAuth **client** credentials the user creates once in Google
    Cloud Console (``config/google_oauth.json``),
  * stores the per-service **tokens** (``data/google_tokens/<service>.json``),
  * runs the consent flow so the user clicks a URL and approves access,
  * refreshes access tokens silently, and reports the current state.

Two consent flows exist, because the right one depends on the OAuth client
type the user created in Google Cloud Console:

  * **Browser redirect with a local callback** (the one Termux-AI uses) — a
    throwaway ``127.0.0.1`` server catches Google's redirect.  This needs a
    ``Desktop app`` client: Google's loopback migration guide keeps the
    loopback redirect for desktop clients only.
  * **Device flow** — the user opens a URL on any device, types a short code
    and the app polls until the token arrives.  It needs a ``TVs and Limited
    Input devices`` client and only accepts a small allowlist of scopes that
    does not include Gmail, Calendar or Drive.

Both use PKCE, which Google now requires for installed applications.  The
usable flow is picked automatically (see :func:`diagnose_client`), and a
client of the wrong type is reported back in plain language instead of as
Google's "Access blocked" wall.

Nothing here talks to the LLM and nothing here decides policy — the plugin
manager and ``core/interface.py`` own that.  This module only performs the
OAuth dance and stores the result.
"""
import os
import sys
import json
import time
import base64
import hashlib
import re
import secrets
import socket
import threading
import subprocess

# Path bootstrap (core first, project root second) — same convention as the
# rest of the package.
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_CORE_DIR)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(1, _ROOT_DIR)

import paths

try:
    import requests
except ImportError:  # pragma: no cover - requests is a hard dependency elsewhere
    requests = None


# ── Google endpoints ───────────────────────────────────────────────────────────
AUTH_ENDPOINT       = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT      = "https://oauth2.googleapis.com/token"
DEVICE_CODE_ENDPOINT = "https://oauth2.googleapis.com/device/code"
TOKENINFO_ENDPOINT  = "https://oauth2.googleapis.com/tokeninfo"
REVOKE_ENDPOINT     = "https://oauth2.googleapis.com/revoke"

HTTP_TIMEOUT = 25          # seconds for every OAuth round trip
DEVICE_FLOW_GRACE = 30     # extra seconds beyond the device code lifetime
# How long a non-blocking 'finish' call may poll before giving up and telling
# the caller to ask again.  Bounded on purpose: a tool call must not hang.
DEVICE_FINISH_WAIT = 60
# The pre-flight check that validates a client + redirect combination must be
# quick: it runs while the user is waiting for the setup screen.
PREFLIGHT_TIMEOUT = 15

# ── Setup guide links ──────────────────────────────────────────────────────────
# Every link the user needs while creating the OAuth client.  They live in one
# place so the instructions, the error messages and the documentation cannot
# drift apart.
CLOUD_CONSOLE_OVERVIEW = "https://console.cloud.google.com/auth/overview"
CLOUD_CONSOLE_CLIENTS = "https://console.cloud.google.com/auth/clients"
DEVICE_FLOW_DOC = "https://developers.google.com/identity/protocols/oauth2/limited-input-device"
DESKTOP_APP_DOC = "https://developers.google.com/identity/protocols/oauth2/native-app"
SCOPES_DOC = "https://developers.google.com/identity/protocols/oauth2/scopes"
LOOPBACK_MIGRATION_DOC = ("https://developers.google.com/identity/protocols/"
                          "oauth2/resources/loopback-migration")

# The API library pages of every service a Google plugin can use.
_API_LIBRARY_URLS = (
    "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
    "https://console.cloud.google.com/apis/library/drive.googleapis.com",
    "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
)


# ── Service registry ───────────────────────────────────────────────────────────
# One entry per plugin that talks to a Google API.  ``scopes`` is what the
# consent screen will ask the user to approve, so it is deliberately kept in
# one place: the setup flow, the status output and the plugin manager all
# read it from here.
GOOGLE_SERVICES: dict[str, dict] = {
    "gmail": {
        "label": "Gmail",
        "plugin": "gmail",
        "apis": ["Gmail API"],
        "api_url": "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    },
    "google_calendar": {
        "label": "Google Calendar",
        "plugin": "google_calendar",
        "apis": ["Google Calendar API"],
        "api_url": ("https://console.cloud.google.com/apis/library/"
                    "calendar-json.googleapis.com"),
        "scopes": [
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    },
    "google_drive": {
        "label": "Google Drive",
        "plugin": "google_drive",
        "apis": ["Google Drive API"],
        "api_url": "https://console.cloud.google.com/apis/library/drive.googleapis.com",
        "scopes": [
            "https://www.googleapis.com/auth/drive",
        ],
    },
}


class OAuthError(Exception):
    """Raised when an OAuth step fails in a way the caller should show."""


# ── Small filesystem helpers ───────────────────────────────────────────────────
def _read_json(path: str) -> dict:
    """Read a JSON file, returning ``{}`` when it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[OAuth] Could not read {path}: {exc}")
        return {}


def _write_json(path: str, data: dict, mode: int = 0o600) -> bool:
    """Write JSON atomically with restrictive permissions.

    Credentials and refresh tokens must never be world-readable, and the
    write has to be atomic: a half-written token file would leave the plugin
    in a state where it looks authorised but every API call fails.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return True
    except (OSError, TypeError, ValueError) as exc:
        print(f"[OAuth] Could not write {path}: {exc}")
        return False


def _post(url: str, data: dict) -> tuple[int, dict]:
    """POST a form-encoded request and return ``(status_code, payload)``."""
    if requests is None:
        raise OAuthError("The 'requests' package is not installed.")
    try:
        resp = requests.post(url, data=data, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        raise OAuthError(f"Could not reach Google ({url}): {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": "invalid_response",
                   "error_description": resp.text[:200]}
    if not isinstance(payload, dict):
        payload = {"error": "invalid_response",
                   "error_description": str(payload)[:200]}
    return resp.status_code, payload


# ── Client credentials ─────────────────────────────────────────────────────────
def load_client_config() -> dict:
    """Return the OAuth client id/secret.

    Environment variables win over the on-disk file so a user can keep the
    secret out of the project entirely (``GOOGLE_CLIENT_ID`` /
    ``GOOGLE_CLIENT_SECRET``).
    """
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()

    data = _read_json(paths.GOOGLE_OAUTH_FILE)
    if not client_id:
        client_id = str(data.get("client_id") or "").strip()
    if not client_secret:
        client_secret = str(data.get("client_secret") or "").strip()

    return {"client_id": client_id, "client_secret": client_secret}


def save_client_config(client_id: str, client_secret: str) -> bool:
    """Persist the OAuth client credentials (0600)."""
    payload = {
        "client_id": (client_id or "").strip(),
        "client_secret": (client_secret or "").strip(),
        "saved_at": time.time(),
    }
    return _write_json(paths.GOOGLE_OAUTH_FILE, payload)


def client_configured() -> bool:
    """True when an OAuth client id is available."""
    return bool(load_client_config().get("client_id"))


def service_scopes(service: str) -> list[str]:
    """Scopes requested for a service."""
    entry = GOOGLE_SERVICES.get(service, {})
    return list(entry.get("scopes", []))


def service_label(service: str) -> str:
    """Human-readable name of a service."""
    entry = GOOGLE_SERVICES.get(service, {})
    return entry.get("label") or service


# ── Token storage ──────────────────────────────────────────────────────────────
def token_path(service: str) -> str:
    """Path of the token file for a service."""
    return os.path.join(paths.GOOGLE_TOKENS_DIR, f"{service}.json")


def load_token(service: str) -> dict:
    """Load the stored token bundle for a service."""
    return _read_json(token_path(service))


def save_token(service: str, token: dict) -> bool:
    """Store a token bundle, preserving the refresh token when omitted.

    Google's refresh response does *not* repeat the refresh token.  Dropping
    it would silently turn a working connection into a one-shot one, so an
    existing refresh token is carried over.
    """
    existing = load_token(service)
    refresh = str(token.get("refresh_token") or "").strip()
    if not refresh:
        refresh = str(existing.get("refresh_token") or "").strip()

    payload = {
        "access_token": str(token.get("access_token") or ""),
        "refresh_token": refresh,
        "token_type": str(token.get("token_type") or "Bearer"),
        "scope": str(token.get("scope") or existing.get("scope") or ""),
        "expiry": float(token.get("expiry") or 0.0),
        "account": str(token.get("account") or existing.get("account") or ""),
        "granted_at": time.time(),
    }
    ok = _write_json(token_path(service), payload)
    if ok:
        _invalidate_status_cache()
    return ok


def clear_token(service: str) -> bool:
    """Delete the stored token for a service."""
    path = token_path(service)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        print(f"[OAuth] Could not remove token file {path}: {exc}")
        return False
    _invalidate_status_cache()
    return True


def is_authorized(service: str) -> bool:
    """True when a usable token is stored for the service."""
    token = load_token(service)
    if token.get("refresh_token"):
        return True
    access = token.get("access_token")
    expiry = float(token.get("expiry") or 0.0)
    return bool(access and (expiry == 0 or expiry > time.time()))


def revoke_token(service: str) -> bool:
    """Revoke the stored grant (best effort) and delete the local token."""
    token = load_token(service)
    access = str(token.get("refresh_token") or token.get("access_token") or "")
    if access and requests is not None:
        try:
            requests.post(REVOKE_ENDPOINT, data={"token": access},
                          timeout=HTTP_TIMEOUT)
        except Exception as exc:
            # Revocation is best effort: the local token is removed either
            # way, and the user can always revoke from the Google account page.
            print(f"[OAuth] Server-side revoke failed (ignored): {exc}")
    return clear_token(service)


# ── Access tokens ──────────────────────────────────────────────────────────────
def _refresh_access_token(service: str) -> tuple[bool, dict]:
    """Exchange the refresh token for a fresh access token."""
    token = load_token(service)
    refresh = str(token.get("refresh_token") or "")
    if not refresh:
        return False, {"error": "no_refresh_token",
                       "error_description": "No refresh token stored."}

    cfg = load_client_config()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": cfg.get("client_id", ""),
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    status, payload = _post(TOKEN_ENDPOINT, data)
    if status >= 400 or "access_token" not in payload:
        return False, payload

    payload["expiry"] = time.time() + float(payload.get("expires_in", 3600))
    if not payload.get("account"):
        payload["account"] = token.get("account", "")
    save_token(service, payload)
    return True, payload


def get_access_token(service: str) -> tuple[str | None, str]:
    """Return ``(access_token, error)`` for a service.

    The access token is refreshed transparently when it is about to expire,
    so callers never have to think about token lifetime.
    """
    token = load_token(service)
    access = str(token.get("access_token") or "")
    expiry = float(token.get("expiry") or 0.0)

    if access and expiry > time.time() + 60:
        return access, ""

    if token.get("refresh_token"):
        ok, payload = _refresh_access_token(service)
        if ok:
            return str(payload.get("access_token") or ""), ""
        detail = (payload.get("error_description")
                  or payload.get("error") or "token refresh failed")
        return None, f"Could not refresh the Google access token: {detail}"

    if access:
        # Expired but no refresh token — try it anyway; some scopes keep
        # working until the server actually rejects the call.
        return access, ""

    plugin = GOOGLE_SERVICES.get(service, {}).get("plugin", service)
    return None, (f"{service_label(service)} is not connected. "
                  f"Run '/plugin setup {plugin}'.")


# ── Account info ───────────────────────────────────────────────────────────────
def fetch_account_info(access_token: str) -> dict:
    """Look up the signed-in Google account for an access token."""
    if not access_token or requests is None:
        return {}
    try:
        resp = requests.get(TOKENINFO_ENDPOINT,
                            params={"access_token": access_token},
                            timeout=HTTP_TIMEOUT)
        data = resp.json()
    except Exception as exc:
        print(f"[OAuth] tokeninfo lookup failed: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "email": data.get("email", ""),
        "scope": data.get("scope", ""),
        "expires_in": data.get("expires_in"),
    }


# ── Connected-account email resolution ─────────────────────────────────────────
# tokeninfo's ``email`` field came back empty in practice, so we resolve the
# real account address from each service's own endpoint (Gmail profile,
# primary calendar id, Drive about).  Resolved once and persisted to the token
# file so plugin status can show the Gmail ID.
def resolve_account_email(service: str) -> str:
    """Best-effort lookup of the signed-in Google account's email for a service.

    Returns ``""`` when it cannot be determined (no token, network failure,
    or an endpoint that does not expose the address).
    """
    token, _ = get_access_token(service)
    if not token or requests is None:
        return ""
    try:
        if service == "gmail":
            resp = requests.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code < 400:
                return str((resp.json() or {}).get("emailAddress") or "")
        elif service == "google_calendar":
            resp = requests.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code < 400:
                for item in (resp.json() or {}).get("items", []) or []:
                    if item.get("primary"):
                        return str(item.get("id") or "")
        elif service == "google_drive":
            resp = requests.get(
                "https://www.googleapis.com/drive/v3/about",
                params={"fields": "user(emailAddress)"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code < 400:
                return str(((resp.json() or {}).get("user") or {}).get("emailAddress") or "")
    except Exception:
        pass
    return ""


# ── Status ─────────────────────────────────────────────────────────────────────
# The plugin manager asks for the status of every Google plugin on every LLM
# request (to decide whether to advertise the tools).  Reading three small
# JSON files per request is wasteful, so the answer is cached for a few
# seconds and invalidated whenever a token is written or removed.
_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_STATUS_CACHE_TTL = 5.0


def _invalidate_status_cache() -> None:
    _STATUS_CACHE.clear()


def auth_status(service: str) -> dict:
    """Current connection state for a service."""
    now = time.time()
    cached = _STATUS_CACHE.get(service)
    if cached and (now - cached[0]) < _STATUS_CACHE_TTL:
        return cached[1]

    cfg = load_client_config()
    token = load_token(service)
    expiry = float(token.get("expiry") or 0.0)
    account = str(token.get("account") or "")
    # Resolve the connected account email once when we have a usable token but
    # no email stored yet, so plugin status can show the actual Gmail ID.
    if not account and is_authorized(service):
        try:
            resolved = resolve_account_email(service)
            if resolved:
                account = resolved
                token["account"] = account
                save_token(service, token)
        except Exception:
            pass

    status = {
        "service": service,
        "label": service_label(service),
        "client_configured": bool(cfg.get("client_id")),
        "authorized": is_authorized(service),
        "account": account,
        "scopes": [s for s in str(token.get("scope") or "").split() if s],
        "required_scopes": service_scopes(service),
        "expiry": expiry,
        "expired": bool(expiry and expiry < now),
    }
    _STATUS_CACHE[service] = (now, status)
    return status


# ── PKCE ───────────────────────────────────────────────────────────────────────
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _new_code_verifier() -> str:
    return _b64url(secrets.token_bytes(64))


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


# ── Authorization URL ──────────────────────────────────────────────────────────
def build_auth_url(service: str,
                   redirect_uri: str = "",
                   code_challenge: str = "",
                   state: str = "") -> str:
    """Build the Google consent URL the user has to open.

    ``prompt=consent`` is deliberate: it guarantees a fresh consent screen
    and therefore a fresh refresh token, which is what makes the plugin work
    again after the user revoked access.
    """
    from urllib.parse import urlencode

    cfg = load_client_config()
    params = {
        "client_id": cfg.get("client_id", ""),
        "response_type": "code",
        "scope": " ".join(service_scopes(service)),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if state:
        params["state"] = state

    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


# ── Pending authorization state (for pasted codes) ─────────────────────────────
def save_pending_auth(service: str, redirect_uri: str, code_verifier: str) -> None:
    """Remember the redirect URI + PKCE verifier of an in-flight consent."""
    _write_json(paths.GOOGLE_PENDING_FILE, {
        "service": service,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "created_at": time.time(),
    })


def load_pending_auth(service: str = "") -> dict:
    """Load the pending authorization state (optionally for one service)."""
    data = _read_json(paths.GOOGLE_PENDING_FILE)
    if service and data.get("service") != service:
        return {}
    return data


def clear_pending_auth() -> None:
    """Forget the pending authorization state."""
    try:
        if os.path.exists(paths.GOOGLE_PENDING_FILE):
            os.remove(paths.GOOGLE_PENDING_FILE)
    except OSError as exc:
        print(f"[OAuth] Could not clear pending auth state: {exc}")


# ── Device flow ────────────────────────────────────────────────────────────────
def start_device_flow(service: str) -> tuple[dict | None, str]:
    """Ask Google for a device code.

    Returns ``(payload, error)``.  On success the payload carries
    ``device_code``, ``user_code``, ``verification_url`` and
    ``verification_uri_complete``.
    """
    cfg = load_client_config()
    if not cfg.get("client_id"):
        return None, ("No Google OAuth client configured yet. "
                      "Run the setup again and enter your client id.")

    data = {
        "client_id": cfg["client_id"],
        "scope": " ".join(service_scopes(service)),
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    status, payload = _post(DEVICE_CODE_ENDPOINT, data)
    if status >= 400 or "device_code" not in payload:
        detail = (payload.get("error_description")
                  or payload.get("error")
                  or f"HTTP {status}")
        return None, detail
    return payload, ""


def poll_device_flow(service: str, device_code: str) -> tuple[str, dict]:
    """Poll the token endpoint once for a device flow.

    Returns ``(status, payload)`` where status is one of
    ``ok`` / ``pending`` / ``denied`` / ``expired`` / ``error``.
    """
    cfg = load_client_config()
    data = {
        "client_id": cfg.get("client_id", ""),
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    status, payload = _post(TOKEN_ENDPOINT, data)
    if status < 400 and "access_token" in payload:
        return "ok", payload

    error = str(payload.get("error") or "")
    if error == "authorization_pending":
        return "pending", payload
    if error == "slow_down":
        return "pending", payload
    if error == "access_denied":
        return "denied", payload
    if error == "expired_token":
        return "expired", payload
    return "error", payload


# ── Deferred device flow (URL first, approval later) ───────────────────────────
# ``run_setup`` blocks until the user approves, which is right for the
# interactive ``/plugin setup <name>`` command.  A tool call cannot block for
# minutes, so the consent flow is also exposed in two halves:
# ``begin_loopback_consent`` hands back the URL to show the user and keeps the
# local callback server running, ``finish_loopback_consent`` collects the
# result once they say they approved.
def save_pending_device(service: str, device: dict) -> bool:
    """Remember an in-flight device authorization so it can be finished later."""
    return _write_json(paths.GOOGLE_PENDING_FILE, {
        "service": service,
        "flow": "device",
        "device_code": str(device.get("device_code") or ""),
        "user_code": str(device.get("user_code") or ""),
        "verification_url": str(device.get("verification_uri_complete")
                                or device.get("verification_url") or ""),
        "interval": int(device.get("interval", 5) or 5),
        "expires_at": time.time() + int(device.get("expires_in", 1800) or 1800),
        "created_at": time.time(),
    })


def load_pending_device(service: str) -> dict:
    """Return a still-valid pending device authorization, else ``{}``."""
    data = load_pending_auth(service)
    if data.get("flow") != "device" or not data.get("device_code"):
        return {}
    if float(data.get("expires_at") or 0.0) < time.time():
        return {}
    return data


def begin_device_consent(service: str) -> tuple[dict, str]:
    """Start the device flow without waiting for approval.

    Returns ``(info, error)`` where info carries ``url``, ``user_code``,
    ``interval`` and ``expires_in``.  The pending state is stored so
    :func:`finish_device_consent` can complete it from a later call.
    """
    cfg = load_client_config()
    if not cfg.get("client_id"):
        return {}, ("No Google OAuth client is configured yet. Run "
                    "'/plugin setup <name>' once to enter your client id.")

    device, error = start_device_flow(service)
    if not device:
        return {}, error
    if not save_pending_device(service, device):
        print("[OAuth] Warning: could not persist the pending device authorization.")

    return {
        "url": str(device.get("verification_uri_complete")
                   or device.get("verification_url") or ""),
        "user_code": str(device.get("user_code") or ""),
        "interval": max(5, int(device.get("interval", 5) or 5)),
        "expires_in": int(device.get("expires_in", 1800) or 1800),
    }, ""


def finish_device_consent(service: str, timeout: int | None = None) -> dict:
    """Poll a previously started device authorization until it resolves.

    ``timeout`` bounds the whole wait so a tool call cannot hang: when the
    user has not approved yet the result stage is ``pending`` and the caller
    simply asks again.  Uses ``time.monotonic`` because CLOCK_REALTIME jumps
    on Android and would otherwise cut the wait short.
    """
    if timeout is None:
        timeout = DEVICE_FINISH_WAIT

    pending = load_pending_device(service)
    if not pending:
        return {"ok": False, "stage": "expired", "account": "",
                "message": ("No pending Google authorization for "
                            f"'{service_label(service)}'. Start the setup again.")}

    interval = max(5, int(pending.get("interval", 5) or 5))
    deadline = time.monotonic() + max(0, timeout)

    while time.monotonic() < deadline:
        state, payload = poll_device_flow(service, pending.get("device_code", ""))

        if state == "ok":
            ok, account = _finish_token(service, payload)
            clear_pending_auth()
            return {"ok": ok, "stage": "connected" if ok else "error",
                    "account": account,
                    "message": ("Authorization granted." if ok
                                else "the token was received but could not be stored")}
        if state == "denied":
            clear_pending_auth()
            return {"ok": False, "stage": "denied", "account": "",
                    "message": "The permission request was declined."}
        if state == "expired":
            clear_pending_auth()
            return {"ok": False, "stage": "expired", "account": "",
                    "message": "The request expired before it was approved."}
        if state == "error":
            detail = (payload.get("error_description")
                      or payload.get("error") or "unknown error")
            clear_pending_auth()
            return {"ok": False, "stage": "error", "account": "",
                    "message": f"Google refused the request: {detail}"}
        if payload.get("error") == "slow_down":
            interval += 5

        # Never sleep past the deadline — the caller is waiting.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    return {"ok": False, "stage": "pending", "account": "",
            "message": ("Not approved yet. Open the link, approve access for "
                        f"{service_label(service)}, then ask me to finish the setup.")}


def exchange_code(service: str,
                  code: str,
                  redirect_uri: str = "",
                  code_verifier: str = "") -> tuple[bool, dict]:
    """Exchange an authorization code for tokens."""
    cfg = load_client_config()
    data = {
        "client_id": cfg.get("client_id", ""),
        "code": (code or "").strip(),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]
    if code_verifier:
        data["code_verifier"] = code_verifier

    status, payload = _post(TOKEN_ENDPOINT, data)
    if status >= 400 or "access_token" not in payload:
        return False, payload
    return True, payload


def complete_code_flow(service: str, code_or_url: str) -> tuple[bool, str]:
    """Finish a consent flow with a code (or a full redirect URL) from the user.

    The redirect URI and PKCE verifier of the pending request are reused, so
    a code that was obtained on another device still validates.
    """
    pending = load_pending_auth(service)
    redirect_uri = pending.get("redirect_uri", "")
    verifier = pending.get("code_verifier", "")

    value = (code_or_url or "").strip()
    if not value:
        return False, "No authorization code was given."

    if "code=" in value:
        from urllib.parse import parse_qs, urlparse
        query = urlparse(value).query or value
        code = (parse_qs(query).get("code") or [""])[0]
        if not code:
            return False, "No 'code=' parameter found in that URL."
    else:
        code = value

    ok, payload = exchange_code(service, code, redirect_uri, verifier)
    if not ok:
        detail = (payload.get("error_description")
                  or payload.get("error")
                  or "the code was rejected")
        return False, detail

    payload["expiry"] = time.time() + float(payload.get("expires_in", 3600))
    info = fetch_account_info(str(payload.get("access_token") or ""))
    payload["account"] = info.get("email", "")
    if info.get("scope"):
        payload["scope"] = info["scope"]
    save_token(service, payload)
    clear_pending_auth()
    return True, str(payload.get("account") or "")


# ── Loopback flow ──────────────────────────────────────────────────────────────
# The local callback server has to outlive a single tool call: the model hands
# the user a URL, the user approves it a minute later, and only then does the
# model call "finish".  The running captures therefore live in a module-level
# registry instead of on the stack.
_LOOPBACK_CAPTURES: dict[str, dict] = {}

# How long a loopback capture stays usable.
LOOPBACK_TTL = 900


def _free_port() -> int:
    """Pick a free TCP port on 127.0.0.1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def start_loopback_capture(service: str) -> dict:
    """Start a loopback consent capture.

    Returns a dict with the consent ``url``, the ``redirect_uri``, the PKCE
    ``code_verifier`` and a ``capture`` handle.  The caller shows the URL to
    the user and then calls :func:`wait_for_loopback` — splitting it this way
    keeps the "please open this link" message from appearing after the wait
    has already started.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    verifier = _new_code_verifier()
    state = secrets.token_urlsafe(16)

    captured: dict[str, str] = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            query = parse_qs(urlparse(self.path).query)
            code = (query.get("code") or [""])[0]
            error = (query.get("error") or [""])[0]
            if error:
                captured["error"] = error
            elif code:
                captured["code"] = code
                captured["state"] = (query.get("state") or [""])[0]
            body = (
                "<html><body style='font-family:sans-serif;padding:40px'>"
                "<h2>Authorization received</h2>"
                "<p>You can close this tab and go back to Termux-AI.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_args):  # silence the default stderr logging
            return

    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        return {"url": "", "redirect_uri": "", "code_verifier": "",
                "capture": None,
                "error": f"Could not start the local callback server: {exc}"}

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    auth_url = build_auth_url(service, redirect_uri=redirect_uri,
                              code_challenge=_code_challenge(verifier),
                              state=state)
    save_pending_auth(service, redirect_uri, verifier)

    return {
        "url": auth_url,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "capture": {"done": done, "captured": captured, "server": server,
                    "state": state},
        "error": "",
    }


def wait_for_loopback(capture: dict, timeout: int = 300,
                      shutdown: bool = True) -> tuple[str, str]:
    """Wait for Google's redirect to arrive.

    Returns ``(code, error)`` — the code is empty when the user did not
    finish the consent in time.  With ``shutdown=False`` the callback server
    keeps running, so a later call can still collect the code.
    """
    if not capture:
        return "", "no callback server was started"
    done = capture["done"]
    captured = capture["captured"]
    try:
        done.wait(timeout)
    finally:
        if shutdown:
            try:
                capture["server"].shutdown()
                capture["server"].server_close()
            except Exception as exc:
                print(f"[OAuth] Callback server shutdown issue: {exc}")

    code = captured.get("code", "")
    if not code:
        error = captured.get("error") or "no authorization code was received"
        return "", error
    if captured.get("state") and captured["state"] != capture.get("state", ""):
        return "", "state mismatch in the redirect"
    return code, ""


def loopback_capture(service: str) -> dict:
    """The live loopback capture of a service ({} when there is none)."""
    capture = _LOOPBACK_CAPTURES.get(service)
    if not capture:
        return {}
    if float(capture.get("expires_at") or 0.0) < time.time():
        clear_loopback_capture(service)
        return {}
    return capture


def clear_loopback_capture(service: str) -> None:
    """Shut down the local callback server of a service."""
    capture = _LOOPBACK_CAPTURES.pop(service, None)
    if not capture:
        return
    server = (capture.get("capture") or {}).get("server")
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except Exception as exc:
        print(f"[OAuth] Callback server shutdown issue: {exc}")


def begin_loopback_consent(service: str) -> tuple[dict, str]:
    """Start the browser-redirect flow without waiting for the approval.

    Returns ``(info, error)``.  The local callback server keeps running, so a
    later :func:`finish_loopback_consent` can collect the authorization code.
    """
    cfg = load_client_config()
    if not cfg.get("client_id"):
        return {}, ("No Google OAuth client is configured yet. Create one with "
                    "application type 'Desktop app' at "
                    f"{CLOUD_CONSOLE_CLIENTS}, then run the setup again.")

    capture = start_loopback_capture(service)
    if capture.get("error"):
        return {}, capture["error"]

    check = check_consent_url(capture.get("url", ""))
    if not check.get("ok"):
        detail = friendly_error(str(check.get("detail") or ""), service)
        server = (capture.get("capture") or {}).get("server")
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:
                print(f"[OAuth] Callback server shutdown issue: {exc}")
        clear_pending_auth()
        return {}, detail

    _LOOPBACK_CAPTURES[service] = {
        "url": capture.get("url", ""),
        "redirect_uri": capture.get("redirect_uri", ""),
        "code_verifier": capture.get("code_verifier", ""),
        "capture": capture.get("capture"),
        "expires_at": time.time() + LOOPBACK_TTL,
    }
    return {
        "url": capture.get("url", ""),
        "redirect_uri": capture.get("redirect_uri", ""),
        "expires_in": LOOPBACK_TTL,
    }, ""


def finish_loopback_consent(service: str, timeout: int | None = None) -> dict:
    """Collect the authorization code of a running loopback flow."""
    if timeout is None:
        timeout = DEVICE_FINISH_WAIT

    capture = loopback_capture(service)
    if not capture:
        return {"ok": False, "stage": "expired", "account": "",
                "message": ("There is no pending Google authorization. "
                            "Start the setup again.")}

    code, error = wait_for_loopback(capture.get("capture"), timeout=timeout,
                                    shutdown=False)
    if not code:
        return {"ok": False, "stage": "pending", "account": "",
                "message": ("The browser has not sent the approval back yet. "
                            "Open the link on this device, or — if you approved "
                            "on another device — paste the copied redirect URL "
                            "with the finish action.")}

    ok, payload = exchange_code(service, code,
                                redirect_uri=capture.get("redirect_uri", ""),
                                code_verifier=capture.get("code_verifier", ""))
    clear_loopback_capture(service)
    clear_pending_auth()
    if not ok:
        detail = (payload.get("error_description") or payload.get("error")
                  or "the code was rejected")
        return {"ok": False, "stage": "error", "account": "",
                "message": friendly_error(str(detail), service)}

    stored, account = _finish_token(service, payload)
    if not stored:
        return {"ok": False, "stage": "error", "account": "",
                "message": "the token was received but could not be stored"}
    return {"ok": True, "stage": "connected", "account": account,
            "message": "Authorization granted."}


# ── Opening URLs ───────────────────────────────────────────────────────────────
def open_url(url: str) -> bool:
    """Try to open a URL in the user's browser.

    On Termux this hands the URL to ``termux-open-url``; on a desktop it
    falls back to ``xdg-open`` / ``open``; on Windows to ``os.startfile``.
    Returns True when something was launched.
    """
    if not url:
        return False

    if sys.platform == "win32":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            print(f"[OAuth] Could not open the browser: {exc}")
            return False

    candidates = [["termux-open-url", url]]
    if shutil_which("termux-open-url") is None:
        # Not on Termux — desktop fallbacks.
        candidates = [["xdg-open", url], ["open", url]]

    for cmd in candidates:
        if shutil_which(cmd[0]) is None:
            continue
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            print(f"[OAuth] Could not open the browser: {exc}")
            continue
    return False


def shutil_which(name: str) -> str | None:
    """``shutil.which`` under a name that does not clash with the module."""
    import shutil
    return shutil.which(name)


# ── Client diagnosis ───────────────────────────────────────────────────────────
# Google rejects a mismatched OAuth client *before* the user ever sees a
# consent screen, and its wording is hostile:
#
#   "Invalid device flow scope: https://www.googleapis.com/auth/gmail.modify"
#   "Localhost URI is not allowed for 'NATIVE_DEVICE' client type"
#   "Access blocked: Termux-AI's request is invalid"
#
# All three have one root cause: the client was created with the wrong
# application type.  A "TVs and Limited Input devices" client can only use the
# device-code flow, and that flow supports a small allowlist of scopes that
# does not include Gmail, Calendar or Drive; Google's loopback migration guide
# also states that the loopback redirect "will continue to be supported on
# desktop apps" only.  A "Desktop app" client does the opposite: it supports
# the local redirect Termux-AI needs and can request any scope.
#
# ``diagnose_client`` turns Google's answer into that distinction, and
# ``friendly_error`` turns the raw wording into something the user can act on.

_DIAGNOSIS_CACHE: dict[str, tuple[float, dict]] = {}
_DIAGNOSIS_TTL = 300.0


def diagnose_client(service: str) -> dict:
    """Work out whether the configured OAuth client can run our flow.

    Returns a dict with a ``kind`` key:

      * ``no_client`` — nothing is configured yet.
      * ``device_ok`` — the device-code flow works with these scopes (the
        payload of the device code is returned in ``device``).
      * ``tv_client`` — Google accepted the client for the device endpoint but
        rejected the scopes, which only happens for a "TVs and Limited Input
        devices" client.  That type cannot use the local redirect either.
      * ``desktop`` — Google rejected the client for the device endpoint
        because it is not a TV client, i.e. it is the type we want.
      * ``unknown`` — Google said something we do not recognise.
    """
    cfg = load_client_config()
    if not cfg.get("client_id"):
        return {"kind": "no_client",
                "detail": "No Google OAuth client is configured yet."}

    cached = _DIAGNOSIS_CACHE.get(service)
    if cached and (time.time() - cached[0]) < _DIAGNOSIS_TTL:
        return cached[1]

    data = {"client_id": cfg["client_id"], "scope": " ".join(service_scopes(service))}
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]

    result: dict = {"kind": "unknown", "detail": "", "device": None}
    try:
        status, payload = _post(DEVICE_CODE_ENDPOINT, data)
    except OAuthError as exc:
        result["detail"] = str(exc)
        _DIAGNOSIS_CACHE[service] = (time.time(), result)
        return result

    if status < 400 and "device_code" in payload:
        result = {"kind": "device_ok", "detail": "", "device": payload}
    else:
        error = str(payload.get("error") or "")
        detail = str(payload.get("error_description") or error or f"HTTP {status}")
        low = detail.lower()
        if error == "invalid_scope" or "device flow scope" in low:
            result = {"kind": "tv_client", "detail": detail}
        elif error == "invalid_client" or "client type" in low:
            result = {"kind": "desktop", "detail": detail}
        else:
            result = {"kind": "unknown", "detail": detail}

    _DIAGNOSIS_CACHE[service] = (time.time(), result)
    return result


def invalidate_diagnosis_cache() -> None:
    """Forget the cached client diagnosis (called when the client changes)."""
    _DIAGNOSIS_CACHE.clear()


def friendly_error(detail: str, service: str = "") -> str:
    """Turn Google's raw OAuth wording into something the user can act on."""
    text = (detail or "").strip()
    low = text.lower()
    label = service_label(service) if service else "Google"

    if "invalid device flow scope" in low or "invalid_scope" in low:
        return (f"Google's device-code flow cannot request {label} access "
                f"(it only supports a small list of scopes). Termux-AI needs a "
                f"'Desktop app' OAuth client — create one at "
                f"{CLOUD_CONSOLE_CLIENTS} and run the setup again.")

    if ("localhost uri is not allowed" in low or "native_device" in low
            or "use desktop app not tv client" in low or "access blocked" in low):
        return (f"Your OAuth client is a 'TVs and Limited Input devices' "
                f"client. Google blocks the local redirect for that type, and "
                f"the device flow cannot request {label} access either. "
                f"Create a new client with application type 'Desktop app' at "
                f"{CLOUD_CONSOLE_CLIENTS}, then run the setup again.")

    if "redirect_uri_mismatch" in low:
        return (f"The redirect URI is not registered for this client. "
                f"Desktop app clients accept a local redirect automatically — "
                f"check the client id at {CLOUD_CONSOLE_CLIENTS}.")

    if "invalid_client" in low or "client was not found" in low:
        return ("Google does not know this client id. Copy the Client ID "
                f"again from {CLOUD_CONSOLE_CLIENTS}.")

    if "access_denied" in low:
        return "The permission request was declined."

    if "unverified app" in low or "not verified" in low:
        return ("The app is unverified. Add your Google account as a test user "
                f"on the consent screen ({CLOUD_CONSOLE_OVERVIEW}) and try "
                "again.")

    return text


def _decode_auth_error(url: str) -> str:
    """Decode the ``authError`` blob Google puts on its error redirect."""
    import base64
    from urllib.parse import parse_qs, urlparse

    try:
        raw = (parse_qs(urlparse(url).query).get("authError") or [""])[0]
    except Exception:
        raw = ""
    if not raw:
        return "Google rejected the consent request."
    try:
        decoded = base64.urlsafe_b64decode(
            raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    except Exception:
        return raw
    # The payload is "<error_code><separator><message>"; the separator is a
    # protobuf length prefix that means nothing to a human reader.
    decoded = re.sub(r"^([a-z_]+)[A-Za-z]{2}(?=[A-Z])", r"\1: ", decoded)
    return re.sub(r"\s+", " ", decoded).strip()


def check_consent_url(url: str) -> dict:
    """Ask Google whether the consent URL is usable before the user opens it.

    This is the pre-flight check: it catches the "Access blocked" wall before
    the user is sent to a link that cannot work.  A network hiccup must never
    block the setup, so anything unexpected counts as "fine".
    """
    if not url or requests is None:
        return {"ok": True, "detail": ""}
    try:
        resp = requests.get(url, timeout=PREFLIGHT_TIMEOUT, allow_redirects=True)
    except Exception as exc:
        print(f"[OAuth] Pre-flight check skipped ({exc})")
        return {"ok": True, "detail": ""}
    final = str(getattr(resp, "url", "") or "")
    if "/signin/oauth/error" not in final:
        return {"ok": True, "detail": ""}
    return {"ok": False, "detail": _decode_auth_error(final)}


# ── The setup flow ─────────────────────────────────────────────────────────────
def client_setup_instructions(service: str) -> str:
    """Step-by-step, colourised instructions for creating the OAuth client.

    Every step carries the URL it happens on.  The single most common
    question about this flow is "which page do I open?", and a wrong answer
    costs the user a failed consent attempt.
    """
    from core.renderer import BLUE, BOLD, CYAN, GRAY, GREEN, RESET, YELLOW

    entry = GOOGLE_SERVICES.get(service, {})
    label = entry.get("label") or service_label(service)
    apis = entry.get("apis") or ["the API you need"]

    api_lines = []
    for api in apis:
        api_lines.append(f"        {GREEN}•{RESET} {api}")
    api_lines.append(f"        {GRAY}Open the API library for it:{RESET}")
    api_lines.append(f"          {BLUE}{entry.get('api_url','')}{RESET}")

    # Every API library page is listed, so the guide also works for the other
    # services — but the one that belongs to this service is not repeated.
    own_url = str(entry.get("api_url") or "")
    others = [url for url in _API_LIBRARY_URLS if url != own_url]
    if others:
        api_lines.append(f"        {GRAY}The other API library pages, if you"
                         f" connect more services:{RESET}")
        for url in others:
            api_lines.append(f"          {GRAY}{url}{RESET}")

    lines = [
        "",
        f"{BLUE}╭─ {label} setup — create a Google OAuth client "
        f"(about 2 minutes) ─╮{RESET}",
        "",
        f"  {YELLOW}⚠ IMPORTANT:{RESET} application type must be "
        f"{BOLD}Desktop app{RESET}.",
        f"  {GRAY}Do NOT pick 'TVs and Limited Input devices': Google blocks the"
        f" local{RESET}",
        f"  {GRAY}redirect for that type and it cannot request {label} access."
        f"{RESET}",
        "",
        f"  {CYAN}1{RESET} {BOLD}OAuth consent screen{RESET} (Branding / "
        f"Audience):",
        f"      {BLUE}{CLOUD_CONSOLE_OVERVIEW}{RESET}",
        f"      {GRAY}• choose 'External'{RESET}",
        f"      {GRAY}• fill in the app name and a support email{RESET}",
        f"      {GRAY}• add your own Google account as a test user — test users"
        f" can approve{RESET}",
        f"      {GRAY}  sensitive scopes without waiting for a review{RESET}",
        "",
        f"  {CYAN}2{RESET} {BOLD}Enable the API{RESET} in the API Library:",
        *api_lines,
        "",
        f"  {CYAN}3{RESET} {BOLD}Create the OAuth client{RESET}:",
        f"      {BLUE}{CLOUD_CONSOLE_CLIENTS}{RESET}",
        f"      {GRAY}• 'Create credentials' → 'OAuth client ID'{RESET}",
        f"      {GREEN}• Application type: {BOLD}Desktop app{RESET}",
        f"      {GRAY}  (a Desktop app client accepts the local redirect that"
        f" Termux-AI uses){RESET}",
        f"      {GRAY}• give it a name, e.g. 'Termux-AI', and create it{RESET}",
        "",
        f"  {CYAN}4{RESET} {BOLD}Copy the credentials{RESET} from the client page:",
        f"      {BLUE}{CLOUD_CONSOLE_CLIENTS}{RESET}",
        f"      {GRAY}• Client ID (ends with .apps.googleusercontent.com){RESET}",
        f"      {GRAY}• Client secret{RESET}",
        "",
        f"  {GRAY}Reference:{RESET}",
        f"      {GRAY}• Desktop app flow (what we use):{RESET} "
        f"{BLUE}{DESKTOP_APP_DOC}{RESET}",
        f"      {GRAY}• Device flow for TVs (NOT usable for {label} "
        f"scopes):{RESET}{RESET}",
        f"        {BLUE}{DEVICE_FLOW_DOC}{RESET}",
        f"      {GRAY}• All available scopes:{RESET} {BLUE}{SCOPES_DOC}{RESET}",
        f"      {GRAY}• Why the local redirect works only for Desktop "
        f"clients:{RESET}{RESET}",
        f"        {BLUE}{LOOPBACK_MIGRATION_DOC}{RESET}",
        "",
        f"  {YELLOW}→{RESET} Paste the Client ID and Client secret below, or "
        f"export{GREEN}",
        f"    {GREEN}GOOGLE_CLIENT_ID{RESET} / {GREEN}GOOGLE_CLIENT_SECRET{RESET}"
        f" to keep them out of the project directory.",
        "",
    ]
    return "\n".join(lines)


def _print_consent_header(service: str, url: str, user_code: str,
                          expires_in: int, loopback: bool = False) -> None:
    """Show the user the URL they have to open."""
    from core.renderer import BLUE, BOLD, CYAN, GRAY, RESET, YELLOW

    minutes = max(1, expires_in // 60)
    print(f"\n{BLUE}┌─ {service_label(service)} setup — Google OAuth ─────────────┐{RESET}")
    if loopback:
        print(f"  Open this URL in the browser on {BOLD}this device{RESET} —")
        print(f"  the approval is sent straight back to Termux-AI:")
    else:
        print(f"  Open this URL on any device (this phone, a laptop, anything):")
    print(f"  {CYAN}{url}{RESET}")
    if user_code:
        print(f"  Code to enter: {YELLOW}{user_code}{RESET}")
    print(f"  Sign in with the Google account you want to connect and approve the")
    print(f"  requested permissions. The link expires in ~{minutes} minutes.")
    if loopback:
        print(f"{GRAY}  Opened it somewhere else? Copy the address-bar URL after")
        print(f"  approving and paste it here — that works too.{RESET}")
    print(f"{GRAY}  (waiting for the approval… press Ctrl+C to abort){RESET}")


def _finish_token(service: str, payload: dict) -> tuple[bool, str]:
    """Store a freshly issued token and look up the account behind it."""
    payload["expiry"] = time.time() + float(payload.get("expires_in", 3600))
    info = fetch_account_info(str(payload.get("access_token") or ""))
    if info.get("email"):
        payload["account"] = info["email"]
    if info.get("scope"):
        payload["scope"] = info["scope"]
    if not save_token(service, payload):
        return False, "the token was received but could not be stored"
    return True, str(payload.get("account") or "")


"""Replacement body for ``run_setup`` in core/google_oauth.py.

Kept in the workspace so the edit is a mechanical splice rather than a
hand-typed patch that can drift from the file it replaces.
"""


def run_setup(service: str,
              client_id: str = "",
              client_secret: str = "",
              open_browser: bool = True,
              timeout: int = 300) -> dict:
    """Connect a Google service to the user's account.

    Returns a result dict with ``ok``, ``stage``, ``message`` and ``account``.
    ``stage`` is one of ``connected`` / ``already_connected`` / ``need_client``
    / ``denied`` / ``expired`` / ``cancelled`` / ``no_code`` / ``blocked``
    / ``error``.

    ``no_code`` means the browser never sent the approval back (it was opened
    on another device) — the caller can then offer to accept a pasted
    redirect URL.
    """
    from core.renderer import GRAY, RED, RESET, YELLOW

    result = {"ok": False, "stage": "", "message": "", "account": ""}

    if client_id:
        save_client_config(client_id, client_secret)
        invalidate_diagnosis_cache()

    cfg = load_client_config()
    if not cfg.get("client_id"):
        result["stage"] = "need_client"
        result["message"] = "No Google OAuth client is configured."
        return result

    status = auth_status(service)
    if status.get("authorized") and status.get("account"):
        result.update({
            "ok": True,
            "stage": "already_connected",
            "message": f"{service_label(service)} is already connected.",
            "account": status.get("account", ""),
        })
        return result

    # ── Which flow can this client actually use? ─────────────────────────────
    # The device-code flow only exists for "TVs and Limited Input devices"
    # clients and only accepts a small allowlist of scopes — Gmail, Calendar
    # and Drive are not on it.  A "Desktop app" client uses the browser
    # redirect with a local callback instead, which is what we do below.
    diagnosis = diagnose_client(service)
    kind = diagnosis.get("kind")

    if kind == "device_ok":
        device = diagnosis.get("device") or {}
        url = (device.get("verification_uri_complete")
               or device.get("verification_url") or "")
        user_code = device.get("user_code", "")
        interval = max(5, int(device.get("interval", 5)))
        expires_in = int(device.get("expires_in", 1800))
        _print_consent_header(service, url, user_code, expires_in)
        if open_browser and url:
            open_url(url)

        # Monotonic clock: CLOCK_REALTIME can jump forward on Android
        # (suspend / time sync), which would end the wait immediately.
        deadline = time.monotonic() + expires_in + DEVICE_FLOW_GRACE
        try:
            while time.monotonic() < deadline:
                time.sleep(interval)
                state, payload = poll_device_flow(service, device.get("device_code", ""))
                if state == "ok":
                    ok, account = _finish_token(service, payload)
                    result.update({
                        "ok": ok,
                        "stage": "connected" if ok else "error",
                        "message": ("Authorization granted."
                                    if ok else "the token could not be stored"),
                        "account": account,
                    })
                    return result
                if state == "denied":
                    result.update({"stage": "denied",
                                   "message": "The permission request was declined."})
                    return result
                if state == "expired":
                    result.update({"stage": "expired",
                                   "message": "The request expired before it was approved."})
                    return result
                if state == "error":
                    detail = (payload.get("error_description")
                              or payload.get("error") or "unknown error")
                    result.update({"stage": "error",
                                   "message": friendly_error(str(detail), service)})
                    return result
                if payload.get("error") == "slow_down":
                    interval += 5
        except KeyboardInterrupt:
            result.update({"stage": "cancelled",
                           "message": "Setup cancelled."})
            return result

        result.update({"stage": "expired",
                       "message": "The request expired before it was approved."})
        return result

    # ── Browser redirect with a local callback (the flow that works) ─────────
    if kind == "tv_client":
        print(f"{YELLOW}[!] Your OAuth client is a 'TVs and Limited Input "
              f"devices' client.{RESET}")
        print(f"{GRAY}    Google blocks the local redirect for that type, and "
              f"the device flow cannot request {service_label(service)} "
              f"access.{RESET}")
        print(f"{GRAY}    Fix: create a client with application type "
              f"'Desktop app' at {CLOUD_CONSOLE_CLIENTS}{RESET}")

    capture = start_loopback_capture(service)
    if capture.get("error"):
        result.update({"stage": "error", "message": capture["error"]})
        return result

    # Pre-flight: ask Google whether this client + redirect combination is
    # allowed.  This is what turns the "Access blocked" dead end into a
    # precise instruction *before* the user opens a link that cannot work.
    check = check_consent_url(capture.get("url", ""))
    if not check.get("ok"):
        detail = friendly_error(str(check.get("detail") or ""), service)
        server = (capture.get("capture") or {}).get("server")
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:
                print(f"[OAuth] Callback server shutdown issue: {exc}")
        clear_pending_auth()
        print(f"{RED}[x]{RESET} {detail}")
        result.update({"stage": "blocked", "message": detail})
        return result

    _print_consent_header(service, capture.get("url", ""), "", timeout,
                           loopback=True)
    if open_browser and capture.get("url"):
        open_url(capture.get("url"))

    try:
        code, wait_error = wait_for_loopback(capture["capture"], timeout=timeout)
    except KeyboardInterrupt:
        result.update({"stage": "cancelled", "message": "Setup cancelled."})
        return result

    if not code:
        result.update({"stage": "no_code",
                       "message": (wait_error
                                   or "no authorization code was received")})
        return result

    ok, payload = exchange_code(service, code,
                                redirect_uri=capture.get("redirect_uri", ""),
                                code_verifier=capture.get("code_verifier", ""))
    if not ok:
        detail = (payload.get("error_description")
                  or payload.get("error") or "the code was rejected")
        result.update({"stage": "error",
                       "message": friendly_error(str(detail), service)})
        return result

    stored, account = _finish_token(service, payload)
    clear_pending_auth()
    result.update({
        "ok": stored,
        "stage": "connected" if stored else "error",
        "message": ("Authorization granted."
                    if stored else "the token was received but could not be stored"),
        "account": account,
    })
    return result
