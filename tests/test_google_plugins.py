"""Tests for the Gmail / Google Calendar / Google Drive plugins.

The original plugin manager only carried three *placeholder* entries for the
Google services: names, descriptions and a tool->plugin map, but no schemas,
no implementation and — most importantly — no way for the user to grant
access.  Turning "gmail" on therefore appeared to work while none of it
existed.

These tests pin down the behaviour that replaced it:

  * an account plugin is only usable once the account is actually connected,
  * enabling one starts the consent flow instead of silently doing nothing,
  * the OAuth layer builds a correct consent URL and never loses the refresh
    token on refresh,
  * the credential files are gated by the permission layer, and
  * every advertised Google tool has a dispatch route and an implementation.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.parse

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import paths  # noqa: E402
from core import google_oauth  # noqa: E402
from core import plugin_manager as pm_module  # noqa: E402


# ── Fixtures ───────────────────────────────────────────────────────────────────
@pytest.fixture
def google_env(tmp_path, monkeypatch):
    """Point every Google path at a throwaway directory.

    The real ``config/google_oauth.json`` and ``data/google_tokens/`` must
    never be touched by a test run.
    """
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    tokens = data_dir / "google_tokens"
    cfg_dir.mkdir(parents=True)
    tokens.mkdir(parents=True)

    monkeypatch.setattr("paths.GOOGLE_OAUTH_FILE", str(cfg_dir / "google_oauth.json"))
    monkeypatch.setattr("paths.GOOGLE_TOKENS_DIR", str(tokens))
    monkeypatch.setattr("paths.GOOGLE_PENDING_FILE", str(data_dir / "pending.json"))

    # A stray environment variable would silently override the file.
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    google_oauth._invalidate_status_cache()
    google_oauth.invalidate_diagnosis_cache()
    yield {"config_dir": cfg_dir, "tokens": tokens, "root": tmp_path}
    google_oauth._invalidate_status_cache()
    google_oauth.invalidate_diagnosis_cache()


def _connect(service="gmail", account="user@example.com"):
    """Write a token so the service counts as connected."""
    ok = google_oauth.save_token(service, {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expiry": time.time() + 3600,
        "scope": " ".join(google_oauth.service_scopes(service)),
        "account": account,
    })
    assert ok
    return account


def _split_qs(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


# ── Consent URL / PKCE ─────────────────────────────────────────────────────────

def test_auth_url_carries_scopes_and_pkce(google_env):
    google_oauth.save_client_config("client-123.apps.googleusercontent.com", "secret")
    verifier = "v" * 43
    challenge = google_oauth._code_challenge(verifier)

    url = google_oauth.build_auth_url(
        "gmail",
        redirect_uri="http://127.0.0.1:9999",
        code_challenge=challenge,
        state="state-abc",
    )
    qs = _split_qs(url)

    assert url.startswith(google_oauth.AUTH_ENDPOINT)
    assert qs["client_id"] == ["client-123.apps.googleusercontent.com"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["code_challenge"][0] == challenge
    assert qs["state"] == ["state-abc"]
    assert qs["redirect_uri"] == ["http://127.0.0.1:9999"]
    # Offline access + forced consent is what yields a refresh token.
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]

    scopes = set(qs["scope"][0].split())
    assert "https://www.googleapis.com/auth/gmail.send" in scopes
    assert "https://www.googleapis.com/auth/gmail.modify" in scopes


def test_code_challenge_is_sha256_of_verifier():
    import hashlib
    verifier = "a-known-verifier-value-" + "x" * 20
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert google_oauth._code_challenge(verifier) == expected


def test_each_service_requests_its_own_scopes(google_env):
    gmail = set(google_oauth.service_scopes("gmail"))
    calendar = set(google_oauth.service_scopes("google_calendar"))
    drive = set(google_oauth.service_scopes("google_drive"))

    assert all("gmail" in s for s in gmail)
    assert all("calendar" in s for s in calendar)
    assert all("drive" in s for s in drive)
    # No service may silently ask for another service's data.
    assert not (gmail & drive)
    assert not (calendar & drive)


# ── Token storage / refresh ────────────────────────────────────────────────────

def test_token_file_is_not_world_readable(google_env):
    _connect("gmail")
    path = google_oauth.token_path("gmail")
    assert os.path.exists(path)
    mode = os.stat(path).st_mode & 0o777
    assert mode & 0o077 == 0, f"token file is too permissive: {oct(mode)}"


def test_refresh_does_not_drop_the_refresh_token(google_env, monkeypatch):
    """Google's refresh response has no refresh_token — it must be preserved."""
    google_oauth.save_client_config("cid", "csecret")
    _connect("gmail")

    monkeypatch.setattr(google_oauth, "_post", lambda url, data: (
        200, {"access_token": "new-access", "expires_in": 3600}
    ))

    ok, payload = google_oauth._refresh_access_token("gmail")
    assert ok
    assert payload["access_token"] == "new-access"

    stored = google_oauth.load_token("gmail")
    assert stored["refresh_token"] == "refresh-token"
    assert stored["access_token"] == "new-access"
    assert stored["account"] == "user@example.com"
    assert stored["expiry"] > time.time()


def test_get_access_token_uses_the_cached_token_while_valid(google_env, monkeypatch):
    _connect("gmail")
    called = {"n": 0}
    monkeypatch.setattr(google_oauth, "_post",
                        lambda url, data: called.__setitem__("n", called["n"] + 1) or (200, {}))

    token, error = google_oauth.get_access_token("gmail")
    assert token == "access-token"
    assert error == ""
    assert called["n"] == 0, "a still-valid token must not trigger a refresh"


def test_get_access_token_refreshes_when_expired(google_env, monkeypatch):
    google_oauth.save_client_config("cid", "")
    google_oauth.save_token("gmail", {
        "access_token": "old", "refresh_token": "r", "expiry": time.time() - 10,
    })
    monkeypatch.setattr(google_oauth, "_post", lambda url, data: (
        200, {"access_token": "fresh", "expires_in": 1800}
    ))

    token, error = google_oauth.get_access_token("gmail")
    assert token == "fresh"
    assert error == ""


def test_missing_connection_reports_the_setup_command(google_env):
    token, error = google_oauth.get_access_token("google_calendar")
    assert token is None
    assert "/plugin setup google_calendar" in error


def test_failed_refresh_surfaces_the_reason(google_env, monkeypatch):
    google_oauth.save_client_config("cid", "")
    google_oauth.save_token("gmail", {
        "access_token": "old", "refresh_token": "r", "expiry": time.time() - 10,
    })
    monkeypatch.setattr(google_oauth, "_post", lambda url, data: (
        400, {"error": "invalid_grant",
              "error_description": "Token has been expired or revoked."}
    ))

    token, error = google_oauth.get_access_token("gmail")
    assert token is None
    assert "revoked" in error.lower()


def test_is_authorized_needs_something_usable(google_env):
    assert google_oauth.is_authorized("gmail") is False
    google_oauth.save_token("gmail", {"access_token": "a", "expiry": time.time() - 5})
    assert google_oauth.is_authorized("gmail") is False, "expired with no refresh token"
    google_oauth.save_token("gmail", {"access_token": "a", "refresh_token": "r"})
    assert google_oauth.is_authorized("gmail") is True


def test_revoke_removes_the_local_token(google_env, monkeypatch):
    monkeypatch.setattr(google_oauth, "requests", None)  # skip the network call
    _connect("google_drive")
    assert google_oauth.is_authorized("google_drive")
    assert google_oauth.revoke_token("google_drive") is True
    assert google_oauth.is_authorized("google_drive") is False


# ── Plugin manager ─────────────────────────────────────────────────────────────

@pytest.fixture
def manager(monkeypatch):
    """A PluginManager whose config file is a throwaway."""
    def _make(enabled=None, tmp=None):
        cfg = (tmp or os.environ["PYTEST_TMP_CFG"])
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"plugins": enabled or {}}, fh)
        monkeypatch.setattr("paths.CONFIG_FILE", cfg)
        return pm_module.PluginManager()
    return _make


@pytest.fixture(autouse=True)
def _tmp_cfg(tmp_path_factory, monkeypatch):
    """Give `manager` a config path without leaking the real one."""
    cfg = tmp_path_factory.mktemp("cfg") / "config.json"
    monkeypatch.setenv("PYTEST_TMP_CFG", str(cfg))
    monkeypatch.setattr("paths.CONFIG_FILE", str(cfg))
    yield


def test_account_plugin_tools_are_blocked_until_connected(manager, google_env):
    mgr = manager(enabled={"gmail": {"enabled": True}})
    assert mgr.is_enabled("gmail") is True
    # Enabled, but the account was never connected.
    assert mgr.is_authorized("gmail") is False
    assert mgr.is_tool_allowed("send_gmail_message") is False

    names = {t["function"]["name"] for t in mgr.filter_tools_description(
        _fake_tools())}
    assert "send_gmail_message" not in names
    assert "shell" in names, "core tools must survive the filter"


def test_account_plugin_tools_unlock_after_connecting(manager, google_env):
    mgr = manager(enabled={"gmail": {"enabled": True}})
    _connect("gmail")
    assert mgr.is_authorized("gmail") is True
    assert mgr.is_tool_allowed("send_gmail_message") is True

    names = {t["function"]["name"] for t in mgr.filter_tools_description(
        _fake_tools())}
    assert "send_gmail_message" in names


def test_disabled_plugin_blocks_even_a_connected_account(manager, google_env):
    mgr = manager(enabled={"gmail": {"enabled": False}})
    _connect("gmail")
    assert mgr.is_tool_allowed("send_gmail_message") is False


def test_local_plugin_is_always_authorized(manager):
    mgr = manager(enabled={"whatsapp": {"enabled": True}})
    assert mgr.requires_setup("whatsapp") is False
    assert mgr.is_authorized("whatsapp") is True
    assert mgr.is_tool_allowed("send_whatsapp_message") is True


def test_plugin_state_describes_the_setup_situation(manager, google_env):
    mgr = manager(enabled={"gmail": {"enabled": True}})

    state = mgr.plugin_state("gmail")
    assert state["requires_setup"] is True
    assert state["client_configured"] is False
    assert state["authorized"] is False

    google_oauth.save_client_config("cid", "secret")
    google_oauth._invalidate_status_cache()
    state = mgr.plugin_state("gmail")
    assert state["client_configured"] is True
    assert state["authorized"] is False

    _connect("gmail")
    state = mgr.plugin_state("gmail")
    assert state["authorized"] is True
    assert state["account"] == "user@example.com"


def test_google_plugins_are_declared_with_a_service():
    """Every Google plugin must be wired to a real service + scopes."""
    for name in ("gmail", "google_calendar", "google_drive"):
        entry = pm_module.DEFAULT_PLUGINS[name]
        assert entry["service"] == name
        assert name in google_oauth.GOOGLE_SERVICES
        assert google_oauth.service_scopes(name)


def test_every_mapped_google_tool_exists_in_both_registries():
    """The map must not promise a tool that no layer implements."""
    import tools as tools_module

    schema_names = {t["function"]["name"] for t in tools_module.TOOLS_DESCRIPTION}
    sub_names = set(tools_module._SUB_TOOL_FUNCS)

    google_tools = [t for t, p in pm_module.TOOL_PLUGIN_MAP.items()
                    if p in ("gmail", "google_calendar", "google_drive")]
    assert google_tools, "no Google tools are mapped at all"

    for tool in google_tools:
        assert tool in schema_names, f"{tool} has no schema"
        assert tool in sub_names, f"{tool} is not executable by a sub-AI"
        assert callable(getattr(tools_module, tool, None)), f"{tool} has no implementation"


def _fake_tools():
    return [
        {"type": "function", "function": {"name": "shell", "parameters": {}}},
        {"type": "function", "function": {"name": "send_gmail_message", "parameters": {}}},
        {"type": "function", "function": {"name": "list_calendars", "parameters": {}}},
        {"type": "function", "function": {"name": "list_drive_files", "parameters": {}}},
    ]


# ── Permission gate ────────────────────────────────────────────────────────────

def test_google_credentials_are_gated_like_api_keys(monkeypatch):
    from core import permissions
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)

    assert permissions.command_needs_permission("cat config/google_oauth.json")
    assert permissions.command_needs_permission("cat data/google_tokens/gmail.json")
    assert permissions.command_needs_permission("ls data/google_tokens")
    assert permissions.command_needs_permission(
        "python3 -c \"print(open('config/google_oauth.json').read())\"")


def test_google_credentials_stay_gated_in_autonomous_mode(monkeypatch):
    from core import permissions
    monkeypatch.setattr(permissions, "is_autonomous", lambda: True)

    assert permissions.command_needs_permission("cat config/google_oauth.json")
    assert permissions.command_needs_permission("cat data/google_tokens/gmail.json")


def test_disable_autonomous_flag_overrides_config(monkeypatch, tmp_path):
    """A user who wants the gate back must not have to edit config.json."""
    from core import permissions

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"autonomous": True}))
    flag = tmp_path / "disable_autonomous"
    monkeypatch.setattr(permissions, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(permissions, "DISABLE_AUTONOMOUS_FILE", str(flag))

    assert permissions.is_autonomous() is True
    flag.write_text("1")
    assert permissions.is_autonomous() is False


def test_unrelated_paths_are_not_gated(monkeypatch):
    from core import permissions
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    assert not permissions.command_needs_permission("cat README.md")
    assert not permissions.command_needs_permission("ls workspace/")


# ── API layer ──────────────────────────────────────────────────────────────────

def _stub_request(monkeypatch, responses):
    """Replace google_api._request with a scripted stub."""
    from core import google_api
    calls = []

    def _fake(service, method, url, params=None, json_body=None, data=None,
              headers=None, timeout=None):
        calls.append({"service": service, "method": method, "url": url,
                      "params": params or {}, "json": json_body})
        key = f"{method} {url}"
        if key not in responses:
            raise AssertionError(f"unexpected call: {key}")
        return responses[key]

    monkeypatch.setattr(google_api, "_request", _fake)
    return calls


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text
        self.content = b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_gmail_send_builds_a_decodable_raw_message(monkeypatch):
    from core import google_api
    calls = _stub_request(monkeypatch, {
        f"POST {google_api.GMAIL_API}/messages/send": _Resp({"id": "m1", "threadId": "t1"}),
    })

    result = google_api.gmail_send_message("a@b.c", "Hello", "Body text")
    assert result["id"] == "m1"

    raw = calls[0]["json"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "To: a@b.c" in decoded
    assert "Subject: Hello" in decoded
    assert "Body text" in decoded
    assert 'Content-Type: text/plain; charset="UTF-8"' in decoded


def test_calendar_event_times_are_normalised_to_rfc3339(monkeypatch):
    from core import google_api
    calls = _stub_request(monkeypatch, {
        f"POST {google_api.CALENDAR_API}/calendars/primary/events":
            _Resp({"id": "e1", "summary": "Standup", "htmlLink": "http://x"}),
    })

    google_api.calendar_create_event(
        summary="Standup",
        start="2026-03-04T15:00:00",
        end="2026-03-04T15:30:00",
    )
    sent = calls[0]["json"]
    # A naive local time must gain an offset instead of being sent verbatim
    # (which Google would read as UTC and shift the event).
    assert sent["start"]["dateTime"].startswith("2026-03-04T15:00:00")
    assert len(sent["start"]["dateTime"]) > len("2026-03-04T15:00:00")


def test_calendar_create_requires_a_title_and_times():
    from core import google_api
    with pytest.raises(google_api.GoogleApiError):
        google_api.calendar_create_event(summary="", start="a", end="b")
    with pytest.raises(google_api.GoogleApiError):
        google_api.calendar_create_event(summary="x", start="", end="")


def test_drive_query_and_fields_are_forwarded(monkeypatch):
    from core import google_api
    calls = _stub_request(monkeypatch, {
        f"GET {google_api.DRIVE_API}/files":
            _Resp({"files": [{"id": "f1", "name": "notes.txt"}]}),
    })

    data = google_api.drive_list_files(query="name contains 'notes'")
    assert data["files"][0]["name"] == "notes.txt"
    assert calls[0]["params"]["q"] == "name contains 'notes'"
    assert "nextPageToken" in calls[0]["params"]["fields"]


def test_drive_upload_rejects_a_missing_file(tmp_path):
    from core import google_api
    with pytest.raises(google_api.GoogleApiError):
        google_api.drive_upload_file(str(tmp_path / "nope.bin"))


def test_google_error_payload_becomes_a_readable_message():
    from core import google_api
    resp = _Resp({"error": {"code": 403, "message": "Insufficient Permission",
                            "status": "PERMISSION_DENIED",
                            "errors": [{"reason": "insufficientPermissions"}]}},
                 status=403)
    message = google_api._error_message(resp)
    assert "403" in message
    assert "Insufficient Permission" in message
    assert "/plugin" in message, "the user must be told how to fix it"


def test_missing_connection_raises_before_any_http_call(google_env, monkeypatch):
    from core import google_api
    called = {"n": 0}
    monkeypatch.setattr(google_api.requests, "request",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(google_api.GoogleApiError) as exc:
        google_api.gmail_list_labels()
    assert "/plugin setup gmail" in str(exc.value)
    assert called["n"] == 0


def test_gmail_body_text_prefers_plain_text():
    from core import google_api
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain",
             "body": {"data": base64.urlsafe_b64encode(b"hello").decode()}},
            {"mimeType": "text/html",
             "body": {"data": base64.urlsafe_b64encode(b"<b>hello</b>").decode()}},
        ],
    }
    assert google_api._body_text(payload) == "hello"


def test_gmail_list_enriches_subject_from_date(monkeypatch):
    """messages.list entries should carry subject/from/date, not bare ids."""
    from core import google_api
    calls = []

    def _fake(service, method, url, params=None, json_body=None, data=None,
              headers=None, timeout=None):
        calls.append((method, url, params or {}))
        key = f"{method} {url}"
        if key.endswith("/messages"):
            return _Resp({"messages": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t2"},
            ], "resultSizeEstimate": 2})
        if key.endswith("/messages/m1"):
            return _Resp({"payload": {"headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "alice@example.com"},
                {"name": "Date", "value": "Mon, 01 Jan 2026 10:00:00 +0000"},
            ]}, "snippet": "hello world"})
        if key.endswith("/messages/m2"):
            return _Resp({"payload": {"headers": []}, "snippet": ""})
        raise AssertionError(f"unexpected call: {key}")

    monkeypatch.setattr(google_api, "_request", _fake)
    data = google_api.gmail_list_messages()
    assert len(data["messages"]) == 2
    m1 = data["messages"][0]
    assert m1["id"] == "m1"
    assert m1["subject"] == "Hello"
    assert m1["from"] == "alice@example.com"
    assert m1["snippet"] == "hello world"
    m2 = data["messages"][1]
    assert m2["subject"] == ""
    assert m2["from"] == ""


def test_html_to_text_strips_tags():
    from core import google_api
    out = google_api._html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in out and "world" in out
    out = google_api._html_to_text("<div>a</div><div>b</div>")
    assert "a" in out and "b" in out
    assert google_api._html_to_text("plain text") == "plain text"
    assert google_api._html_to_text("") == ""
    assert google_api._html_to_text("x &amp; y") == "x & y"


def test_gmail_get_profile(monkeypatch):
    from core import google_api
    monkeypatch.setattr(google_api, "_request",
                        lambda *a, **k: _Resp({"emailAddress": "user@example.com",
                                               "messagesTotal": 42}))
    data = google_api.gmail_get_profile()
    assert data["email_address"] == "user@example.com"
    assert data["messages_total"] == 42


def test_calendar_default_window_when_no_bounds(monkeypatch):
    """Without timeMin/timeMax the request should bound the window around now."""
    from core import google_api
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    captured = {}

    def _fake(service, method, url, params=None, json_body=None, data=None,
              headers=None, timeout=None):
        captured.update(params or {})
        return _Resp({"items": [], "nextPageToken": ""})

    monkeypatch.setattr(google_api, "_request", _fake)
    google_api.calendar_list_events()

    # timeMin/timeMax must both be present and roughly now±window.
    assert "timeMin" in captured and "timeMax" in captured
    assert "singleEvents" in captured and captured["singleEvents"] == "true"
    assert captured["orderBy"] == "startTime"
    lo = datetime.fromisoformat(captured["timeMin"])
    hi = datetime.fromisoformat(captured["timeMax"])
    assert lo <= now + timedelta(days=1), lo
    assert hi >= now - timedelta(days=1), hi
    # 30 days back, 90 days forward.
    assert abs((now - lo).days - 30) <= 2
    assert abs((hi - now).days - 90) <= 2


def test_calendar_pagination_and_cancelled(monkeypatch):
    from core import google_api
    captured = {}

    def _fake(service, method, url, params=None, json_body=None, data=None,
              headers=None, timeout=None):
        captured.update(params or {})
        return _Resp({"items": [{"id": "e1", "summary": "Old",
                                 "status": "cancelled",
                                 "start": {"dateTime": "2026-01-01T10:00:00+05:30"},
                                 "end": {"dateTime": "2026-01-01T11:00:00+05:30"},
                                 "attendees": [{"email": "a@b.c"}]}],
                      "nextPageToken": "PAGE-2"})

    monkeypatch.setattr(google_api, "_request", _fake)
    data = google_api.calendar_list_events(include_cancelled=True,
                                           page_token="PAGE-1",
                                           max_results=50)
    assert captured.get("showDeleted") == "true"
    assert captured.get("pageToken") == "PAGE-1"
    assert captured.get("maxResults") == 50
    assert data["next_page_token"] == "PAGE-2"
    ev = data["events"][0]
    assert ev["status"] == "cancelled"
    assert ev["attendees"] == ["a@b.c"]


# ── Tools ──────────────────────────────────────────────────────────────────────

def test_missing_connection_is_reported_by_the_tool(google_env):
    import tools as tools_module
    out = tools_module.get_gmail_messages()
    assert out.startswith("[ERROR]")
    assert "/plugin setup gmail" in out


def test_gmail_listing_formats_snippets(google_env, monkeypatch):
    from core import google_api
    import tools as tools_module
    monkeypatch.setattr(google_api, "gmail_list_messages", lambda **k: {
        "messages": [{"id": "m1", "snippet": "hello there"}],
        "estimate": 1, "query": "",
    })
    out = tools_module.get_gmail_messages()
    assert "m1" in out and "hello there" in out


def test_calendar_listing_shows_ids_for_follow_up_calls(google_env, monkeypatch):
    from core import google_api
    import tools as tools_module
    monkeypatch.setattr(google_api, "calendar_list_events", lambda **k: {
        "calendar_id": "primary",
        "events": [{"id": "e1", "summary": "Standup",
                    "start": "2026-03-04T15:00:00", "end": "2026-03-04T15:30:00"}],
    })
    out = tools_module.get_calendar_events()
    assert "Standup" in out
    assert "id=e1" in out


def test_read_gmail_message_returns_body(google_env, monkeypatch):
    """read_gmail_message must surface headers + body, not just an id."""
    from core import google_api
    import tools as tools_module
    monkeypatch.setattr(google_api, "gmail_get_message",
                        lambda mid: {"id": mid, "thread_id": "t",
                                     "from": "alice@example.com",
                                     "to": "me@example.com",
                                     "subject": "Invoice",
                                     "date": "Mon, 01 Jan 2026 10:00:00 +0000",
                                     "labels": ["INBOX"],
                                     "body": "Please pay the invoice.",
                                     "attachments": [],
                                     "snippet": "Please pay"})
    out = tools_module.read_gmail_message("m1")
    assert "alice@example.com" in out
    assert "Invoice" in out
    assert "Please pay the invoice." in out


def test_read_gmail_message_metadata_format(google_env, monkeypatch):
    from core import google_api
    import tools as tools_module
    monkeypatch.setattr(google_api, "_gmail_message_meta",
                        lambda mid: {"from": "alice@example.com",
                                     "to": "me@example.com",
                                     "subject": "Hi",
                                     "date": "Mon, 01 Jan 2026 10:00:00 +0000",
                                     "snippet": "hello there"})
    out = tools_module.read_gmail_message("m1", format="metadata")
    assert "alice@example.com" in out
    assert "Hi" in out
    assert "hello there" in out


def test_get_gmail_profile_shows_address(google_env, monkeypatch):
    from core import google_api
    import tools as tools_module
    monkeypatch.setattr(google_api, "gmail_get_profile",
                        lambda: {"email_address": "user@example.com",
                                 "messages_total": 7, "threads_total": 5,
                                 "history_id": "123"})
    out = tools_module.get_gmail_profile()
    assert "user@example.com" in out
    assert "7" in out


def test_new_gmail_tools_have_schemas_and_routes():
    import tools as tools_module
    import core.llm_client as lc

    schema_names = {t["function"]["name"] for t in tools_module.TOOLS_DESCRIPTION}
    assert "read_gmail_message" in schema_names
    assert "get_gmail_profile" in schema_names

    source = open(lc.__file__, encoding="utf-8").read()
    assert '"read_gmail_message"' in source
    assert '"get_gmail_profile"' in source

    assert callable(getattr(tools_module, "read_gmail_message", None))
    assert callable(getattr(tools_module, "get_gmail_profile", None))


# ── /plugin command flow ───────────────────────────────────────────────────────

def _isolate_interface(monkeypatch, tmp_path):
    """Point interface's config at a throwaway and reset the manager."""
    import core.interface as iface
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"plugins": {}}))
    monkeypatch.setattr(iface, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr("paths.CONFIG_FILE", str(cfg))
    monkeypatch.setattr(pm_module, "plugin_manager", pm_module.PluginManager())
    monkeypatch.setattr(iface, "plugin_manager", pm_module.plugin_manager)
    return iface


def test_plugin_list_shows_the_setup_state(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    iface._show_all_plugins()
    out = capsys.readouterr().out
    assert "gmail" in out
    assert "NEEDS SETUP" in out or "NO OAUTH CLIENT" in out


def test_plugin_status_reports_not_connected(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin status gmail")
    out = capsys.readouterr().out
    assert "NOT CONNECTED" in out or "NO OAUTH CLIENT" in out
    assert "/plugin setup gmail" in out


def test_plugin_status_reports_the_connected_account(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    _connect("gmail")
    iface._handle_plugin_command("/plugin status gmail")
    out = capsys.readouterr().out
    assert "CONNECTED" in out
    assert "user@example.com" in out


def test_disconnect_revokes(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    _connect("google_drive")
    monkeypatch.setattr(google_oauth, "requests", None)
    assert google_oauth.is_authorized("google_drive")

    iface._handle_plugin_command("/plugin disconnect google_drive")
    out = capsys.readouterr().out
    assert "disconnected" in out
    assert google_oauth.is_authorized("google_drive") is False


def test_enable_starts_the_consent_flow(monkeypatch, tmp_path, google_env, capsys):
    """The whole point of the report: turning the plugin on must ask for access."""
    iface = _isolate_interface(monkeypatch, tmp_path)
    started = {"called": 0}

    def _fake_setup(plugin_name):
        started["called"] += 1
        # Pretend the user approved.
        _connect("gmail")
        print("SETUP-FLOW-STARTED")
        return True

    monkeypatch.setattr(iface, "_run_plugin_setup", _fake_setup)
    iface._handle_plugin_command("/plugin enable gmail")
    out = capsys.readouterr().out

    assert started["called"] == 1
    assert "SETUP-FLOW-STARTED" in out
    assert iface.plugin_manager.is_enabled("gmail") is True


def test_enable_skips_setup_when_already_connected(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    _connect("gmail")
    started = {"called": 0}
    monkeypatch.setattr(iface, "_run_plugin_setup",
                        lambda name: started.__setitem__("called", started["called"] + 1))

    iface._handle_plugin_command("/plugin enable gmail")
    out = capsys.readouterr().out
    assert started["called"] == 0
    assert "enabled" in out


def test_enable_local_plugin_does_not_ask_for_an_account(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(iface, "_run_plugin_setup",
                        lambda name: called.__setitem__("n", called["n"] + 1))
    iface._handle_plugin_command("/plugin enable whatsapp")
    assert called["n"] == 0


def test_setup_prompts_for_client_credentials(monkeypatch, tmp_path, google_env, capsys):
    """With no OAuth client yet, setup must explain how to create one."""
    iface = _isolate_interface(monkeypatch, tmp_path)
    answers = iter(["client-abc.apps.googleusercontent.com", "shh"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))

    seen = {}

    def _fake_run_setup(service, client_id="", client_secret="", **kwargs):
        seen["service"] = service
        seen["client_id"] = client_id
        if not client_id:
            return {"ok": False, "stage": "need_client", "message": "", "account": ""}
        return {"ok": True, "stage": "connected", "message": "", "account": "u@e.com"}

    monkeypatch.setattr(google_oauth, "run_setup", _fake_run_setup)
    ok = iface._run_plugin_setup("gmail")

    assert ok is True
    assert seen["service"] == "gmail"
    assert seen["client_id"] == "client-abc.apps.googleusercontent.com"
    out = capsys.readouterr().out
    assert "console.cloud.google.com" in out


def test_setup_is_cancelled_when_no_client_id_is_given(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    monkeypatch.setattr(google_oauth, "run_setup", lambda service, **k: {
        "ok": False, "stage": "need_client", "message": "", "account": ""})

    assert iface._run_plugin_setup("gmail") is False
    out = capsys.readouterr().out
    assert "cancelled" in out.lower()


def test_unknown_setup_target_is_rejected(monkeypatch, tmp_path, google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin setup bogus")
    out = capsys.readouterr().out
    assert "Unknown plugin" in out


# ── Dispatch ───────────────────────────────────────────────────────────────────

def test_dispatch_explains_the_missing_connection(monkeypatch, google_env):
    import core.llm_client as lc
    from core import plugin_manager as pm

    monkeypatch.setattr(pm.plugin_manager, "_plugins", {
        "gmail": {"enabled": True, "description": "", "service": "gmail",
                  "setup_hint": ""},
    })
    out = lc._dispatch_tool({
        "function": {"name": "send_gmail_message",
                     "arguments": json.dumps({"to": "a@b.c", "body": "hi"})}
    })
    assert out.startswith("[ERROR]")
    assert "'/plugin setup gmail'" in out


def test_dispatch_says_disabled_for_a_disabled_plugin(monkeypatch, google_env):
    import core.llm_client as lc
    from core import plugin_manager as pm

    monkeypatch.setattr(pm.plugin_manager, "_plugins", {
        "gmail": {"enabled": False, "description": "", "service": "gmail",
                  "setup_hint": ""},
    })
    out = lc._dispatch_tool({
        "function": {"name": "list_calendars", "arguments": "{}"}
    })
    assert out.startswith("[ERROR]")
    assert "disabled" in out


def test_dispatch_of_sub_ai_routes_google_tools(monkeypatch, google_env):
    import tools as tools_module
    called = {}

    def _spy(**kw):
        called["kw"] = kw
        return "ok"

    # _SUB_TOOL_FUNCS captured the original function at import time, so the
    # dispatched callable has to be patched there, not just on the module.
    monkeypatch.setitem(tools_module._SUB_TOOL_FUNCS, "list_drive_files", _spy)

    out = tools_module._dispatch_sub_tool("list_drive_files",
                                          json.dumps({"page_size": 5}))
    assert out == "ok"
    assert called["kw"]["page_size"] == 5


# ── Deferred (non-blocking) consent flow ───────────────────────────────────────
#
# '/plugin <name> setup' deliberately blocks until the user clicks approve.
# A tool call cannot: the model has to be able to hand back the URL now and
# collect the result later.  These tests pin that split down.

@pytest.fixture
def fake_google(monkeypatch):
    """A stand-in for Google's device + token endpoints."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    state = {"polls": 0, "approve_after": 0}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = parse_qs(self.rfile.read(length).decode())
            path = urlparse(self.path).path
            port = self.server.server_address[1]

            if path.endswith("/device/code"):
                payload = {
                    "device_code": "DEVICE-CODE",
                    "user_code": "WXYZ-1234",
                    "verification_url": f"http://127.0.0.1:{port}/device",
                    "verification_uri_complete": f"http://127.0.0.1:{port}/device?code=WXYZ-1234",
                    "expires_in": 1800,
                    "interval": 1,
                }
            elif path.endswith("/token"):
                grant = (body.get("grant_type") or [""])[0]
                if grant == "urn:ietf:params:oauth:grant-type:device_code":
                    state["polls"] += 1
                    if state["polls"] <= state["approve_after"]:
                        payload = {"error": "authorization_pending"}
                    else:
                        payload = {"access_token": "access", "refresh_token": "refresh",
                                   "token_type": "Bearer", "expires_in": 3600}
                else:
                    payload = {"access_token": "refreshed", "expires_in": 3600}
            else:
                payload = {"error": "unexpected_path"}

            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            raw = json.dumps({
                "email": "connected@example.com",
                "scope": "https://www.googleapis.com/auth/gmail.modify",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    monkeypatch.setattr(google_oauth, "DEVICE_CODE_ENDPOINT", f"http://127.0.0.1:{port}/device/code")
    monkeypatch.setattr(google_oauth, "TOKEN_ENDPOINT", f"http://127.0.0.1:{port}/token")
    monkeypatch.setattr(google_oauth, "TOKENINFO_ENDPOINT", f"http://127.0.0.1:{port}/tokeninfo")
    # Keep the bounded wait short so the suite stays fast.
    monkeypatch.setattr(google_oauth, "DEVICE_FINISH_WAIT", 1)
    # The pre-flight check talks to the real Google — never inside a test.
    monkeypatch.setattr(google_oauth, "check_consent_url",
                        lambda url: {"ok": True, "detail": ""})

    yield state
    google_oauth.clear_loopback_capture("gmail")
    server.shutdown()
    server.server_close()


def test_begin_device_consent_returns_a_url_without_waiting(google_env, fake_google):
    google_oauth.save_client_config("client-id", "client-secret")
    info, error = google_oauth.begin_device_consent("gmail")

    assert error == ""
    assert info["url"].startswith("http")
    assert info["user_code"] == "WXYZ-1234"
    # The pending state has to survive the call — that is what makes the
    # second half possible from a later tool call.
    assert google_oauth.load_pending_device("gmail")["device_code"] == "DEVICE-CODE"


def test_begin_device_consent_without_a_client_is_actionable(google_env):
    info, error = google_oauth.begin_device_consent("gmail")
    assert info == {}
    assert "OAuth client" in error


def test_finish_device_consent_reports_pending_instead_of_hanging(google_env, fake_google):
    google_oauth.save_client_config("client-id", "client-secret")
    fake_google["approve_after"] = 10 ** 6

    google_oauth.begin_device_consent("gmail")
    result = google_oauth.finish_device_consent("gmail")

    assert result["ok"] is False
    assert result["stage"] == "pending"
    assert "Not approved yet" in result["message"]
    # An unfinished attempt must not throw away the pending authorization.
    assert google_oauth.load_pending_device("gmail")


def test_finish_device_consent_connects_once_approved(google_env, fake_google):
    google_oauth.save_client_config("client-id", "client-secret")
    fake_google["approve_after"] = 0

    google_oauth.begin_device_consent("gmail")
    result = google_oauth.finish_device_consent("gmail")

    assert result["ok"] is True
    assert result["stage"] == "connected"
    assert result["account"] == "connected@example.com"
    assert google_oauth.is_authorized("gmail")
    assert google_oauth.load_pending_device("gmail") == {}


def test_finish_device_consent_without_a_pending_request(google_env):
    result = google_oauth.finish_device_consent("gmail")
    assert result["ok"] is False
    assert result["stage"] == "expired"


def test_device_consent_reports_a_declined_request(google_env, fake_google, monkeypatch):
    google_oauth.save_client_config("client-id", "client-secret")
    monkeypatch.setattr(google_oauth, "poll_device_flow",
                        lambda service, code: ("denied", {"error": "access_denied"}))

    google_oauth.begin_device_consent("gmail")
    result = google_oauth.finish_device_consent("gmail")

    assert result["ok"] is False
    assert result["stage"] == "denied"


def test_expired_pending_state_is_not_reused(google_env, fake_google):
    google_oauth.save_client_config("client-id", "client-secret")
    google_oauth.begin_device_consent("gmail")

    with open(paths.GOOGLE_PENDING_FILE) as fh:
        data = json.load(fh)
    data["expires_at"] = time.time() - 10
    with open(paths.GOOGLE_PENDING_FILE, "w") as fh:
        json.dump(data, fh)

    assert google_oauth.load_pending_device("gmail") == {}
    assert google_oauth.finish_device_consent("gmail")["stage"] == "expired"


# ── manage_plugin tool ─────────────────────────────────────────────────────────

@pytest.fixture
def plugins(monkeypatch, tmp_path, google_env):
    """An isolated plugin manager + interface, so no test writes config.json.

    ``core/tools.py`` looks the manager up on the module at call time, so
    replacing the global instance here is enough to keep the tool isolated.
    """
    import tools as tools_module
    iface = _isolate_interface(monkeypatch, tmp_path)
    return {"iface": iface, "tools": tools_module, "manager": iface.plugin_manager}


def test_manage_plugin_lists_every_plugin(plugins):
    out = plugins["tools"].manage_plugin("list")
    for name in ("whatsapp", "gmail", "google_calendar", "google_drive"):
        assert name in out


def test_manage_plugin_status_explains_an_unconnected_account(plugins):
    out = plugins["tools"].manage_plugin("status", "gmail")
    assert "NOT connected" in out


def test_manage_plugin_enable_returns_the_permission_url(plugins, fake_google):
    """The user's report: enabling a Google plugin must ask for permission."""
    google_oauth.save_client_config("client-id", "client-secret")

    out = plugins["tools"].manage_plugin("enable", "gmail")

    assert "needs permission" in out
    assert google_oauth.AUTH_ENDPOINT in out
    # The browser redirect needs a local callback the server can catch.
    assert "127.0.0.1" in out
    assert "code_challenge" in out
    assert plugins["manager"].is_enabled("gmail") is True


def test_manage_plugin_enable_without_a_client_says_what_to_run(plugins):
    out = plugins["tools"].manage_plugin("enable", "gmail")
    assert "[ERROR]" in out
    assert "/plugin setup gmail" in out


def _consent_url(text: str) -> str:
    """Pull the Google consent URL out of a tool answer."""
    match = re.search(r"(https://accounts\.google\.com/\S+)", text)
    assert match, f"no consent URL in: {text}"
    return match.group(1)


def _approve_loopback(url: str) -> None:
    """Simulate the browser sending Google's redirect to the local server."""
    import urllib.request

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    redirect = query["redirect_uri"][0]
    state = (query.get("state") or [""])[0]
    target = f"{redirect}/?code=TEST-CODE&state={state}"
    with urllib.request.urlopen(target, timeout=10) as response:
        response.read()


def test_manage_plugin_finish_completes_the_connection(plugins, fake_google):
    google_oauth.save_client_config("client-id", "client-secret")

    out = plugins["tools"].manage_plugin("setup", "gmail")
    _approve_loopback(_consent_url(out))

    out = plugins["tools"].manage_plugin("finish", "gmail")

    assert "now connected" in out
    assert "connected@example.com" in out


def test_manage_plugin_finish_accepts_a_pasted_redirect_url(plugins, fake_google):
    """Approved on another device: the copied URL is a complete code."""
    google_oauth.save_client_config("client-id", "client-secret")

    out = plugins["tools"].manage_plugin("setup", "gmail")
    url = _consent_url(out)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    redirect = query["redirect_uri"][0]

    out = plugins["tools"].manage_plugin(
        "finish", "gmail", code=f"{redirect}/?code=PASTED-CODE")

    assert "now connected" in out
    assert "connected@example.com" in out


def test_manage_plugin_finish_reports_pending_while_unapproved(plugins, fake_google):
    google_oauth.save_client_config("client-id", "client-secret")
    plugins["tools"].manage_plugin("setup", "gmail")

    out = plugins["tools"].manage_plugin("finish", "gmail")

    assert "not" in out.lower() or "pending" in out.lower()
    assert "now connected" not in out


def test_manage_plugin_setup_on_a_local_plugin_runs_the_runtime_setup(plugins):
    """A local plugin has no account handshake — 'setup' is its runtime path.

    For WhatsApp that path is the pairing step (see
    ``tests/test_whatsapp_quiet.py``).  Here the runtime is stubbed, so the
    test only asserts that the call is routed to it.
    """
    out = plugins["tools"].manage_plugin("setup", "whatsapp")
    assert "runtime stubbed during tests" in out


def test_manage_plugin_rejects_unknown_names_and_actions(plugins):
    tool = plugins["tools"].manage_plugin
    assert "[ERROR]" in tool("status", "not-a-plugin")
    assert "[ERROR]" in tool("explode", "gmail")
    assert "[ERROR]" in tool("status", "")


def test_manage_plugin_disable_turns_the_plugin_off(plugins):
    out = plugins["tools"].manage_plugin("disable", "gmail")
    assert "disabled" in out
    assert plugins["manager"].is_enabled("gmail") is False


def test_manage_plugin_disconnect_revokes_access(plugins, monkeypatch):
    _connect("gmail")
    monkeypatch.setattr(google_oauth, "requests", None)
    out = plugins["tools"].manage_plugin("disconnect", "gmail")
    assert "disconnected" in out
    assert google_oauth.is_authorized("gmail") is False


def test_manage_plugin_is_never_blocked_by_the_plugin_gate(plugins):
    """It is a core tool: it has to work precisely when a plugin is off."""
    plugins["manager"].disable("gmail")
    assert plugins["manager"].is_tool_allowed("manage_plugin") is True


def test_manage_plugin_schema_and_dispatch_exist():
    """The tool must be advertised and routable, not just implemented."""
    import tools as tools_module

    names = [t["function"]["name"] for t in tools_module.TOOLS_DESCRIPTION]
    assert "manage_plugin" in names

    import core.llm_client as llm_client
    source = open(llm_client.__file__, encoding="utf-8").read()
    assert '"manage_plugin"' in source


def test_manage_plugin_is_not_offered_to_a_delegated_sub_ai():
    """Enabling plugins mutates shared config — a main-agent decision."""
    import tools as tools_module
    assert "manage_plugin" not in tools_module._SUB_TOOL_FUNCS


# ── Startup notice ─────────────────────────────────────────────────────────────

def test_startup_notice_finds_enabled_but_unconnected_plugins(plugins):
    plugins["manager"].enable("gmail")
    pending = plugins["iface"]._pending_plugin_setups()
    assert any(s["name"] == "gmail" for s in pending)


def test_startup_notice_ignores_a_connected_plugin(plugins):
    plugins["manager"].enable("gmail")
    _connect("gmail")
    assert plugins["iface"]._pending_plugin_setups() == []


def test_startup_notice_ignores_a_disabled_plugin(plugins):
    assert plugins["iface"]._pending_plugin_setups() == []


def test_startup_notice_prints_the_command_without_blocking(plugins, monkeypatch, capsys):
    """A piped session must be told what to run, and must never be prompted."""
    iface = plugins["iface"]
    plugins["manager"].enable("gmail")
    monkeypatch.setattr(iface.sys.stdin, "isatty", lambda: False, raising=False)

    started = {"n": 0}
    monkeypatch.setattr(iface, "_run_plugin_setup",
                        lambda name: started.__setitem__("n", started["n"] + 1))

    iface._notify_plugin_setups()
    out = capsys.readouterr().out

    assert "not connected" in out
    assert "/plugin setup gmail" in out
    assert started["n"] == 0  # never auto-runs in a non-interactive session


# ── Rendered output must not leak escape placeholders ──────────────────────────

def test_plugin_list_does_not_print_a_raw_reset_placeholder(plugins, capsys):
    """A '{RESET}' that falls outside its f-string is printed literally."""
    plugins["iface"]._show_all_plugins()
    assert "{RESET}" not in capsys.readouterr().out


def test_plugin_status_does_not_print_a_raw_reset_placeholder(plugins, capsys):
    plugins["iface"]._handle_plugin_command("/plugin status gmail")
    assert "{RESET}" not in capsys.readouterr().out


def test_connected_banner_does_not_print_a_raw_reset_placeholder(plugins, capsys):
    """The success line is built by concatenation — an easy place to drop the f."""
    import tools as tools_module

    google_oauth.save_token("gmail", {
        "access_token": "a", "refresh_token": "r",
        "expiry": time.time() + 3600, "account": "someone@example.com",
    })
    plugins["iface"]._show_all_plugins()
    out = capsys.readouterr().out
    assert "CONNECTED" in out
    assert "{RESET}" not in out


# ── The wrong OAuth client type (the "Access blocked" dead end) ───────────────
#
# Google's own documentation is the reason this section exists:
#
#   * the device-code flow only supports a small allowlist of scopes, and
#     gmail.modify / calendar.events / drive are not on it — the endpoint
#     answers "Invalid device flow scope: ...";
#   * the loopback redirect is kept for "Desktop app" clients only ("the
#     loopback IP address flow ... will continue to be supported on desktop
#     apps"), so Google answers a TV client's local redirect with "Localhost
#     URI is not allowed for 'NATIVE_DEVICE' client type" and shows the user
#     "Access blocked: <name>'s request is invalid".

SETUP_URLS = (
    "https://console.cloud.google.com/auth/overview",
    "https://console.cloud.google.com/auth/clients",
    "https://developers.google.com/identity/protocols/oauth2/limited-input-device",
    "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
    "https://console.cloud.google.com/apis/library/drive.googleapis.com",
    "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
    "https://developers.google.com/identity/protocols/oauth2/scopes",
)


def test_setup_instructions_name_the_desktop_app_client():
    """The old instructions told the user to pick the TV client — that is the bug."""
    text = google_oauth.client_setup_instructions("gmail")
    assert "Desktop app" in text
    assert "Do NOT pick" in text
    assert "TVs and Limited Input devices" in text


def test_setup_instructions_carry_every_setup_url():
    text = google_oauth.client_setup_instructions("gmail")
    for url in SETUP_URLS:
        assert url in text, f"missing {url}"


def test_setup_instructions_are_coloured():
    text = google_oauth.client_setup_instructions("gmail")
    assert "\033[" in text, "the setup instructions must be colourised"


def test_setup_instructions_exist_for_every_service():
    for service in ("gmail", "google_calendar", "google_drive"):
        text = google_oauth.client_setup_instructions(service)
        assert "Desktop app" in text
        for url in SETUP_URLS:
            assert url in text, f"{service} is missing {url}"


def test_friendly_error_names_the_client_type_problem():
    detail = ("invalid_request rMLocalhost URI is not allowed for "
              "'NATIVE_DEVICE' client type")
    message = google_oauth.friendly_error(detail, "gmail")
    assert "Desktop app" in message
    assert "TVs and Limited Input devices" in message
    assert google_oauth.CLOUD_CONSOLE_CLIENTS in message


def test_friendly_error_explains_the_device_scope_problem():
    detail = ("Invalid device flow scope: "
              "https://www.googleapis.com/auth/gmail.modify")
    message = google_oauth.friendly_error(detail, "gmail")
    assert "Desktop app" in message
    assert "device-code flow" in message


def test_friendly_error_leaves_unknown_problems_alone():
    assert google_oauth.friendly_error("something else entirely") == \
        "something else entirely"


def test_diagnose_client_recognises_a_tv_client(google_env, monkeypatch):
    google_oauth.save_client_config("cid", "secret")
    google_oauth.invalidate_diagnosis_cache()
    monkeypatch.setattr(google_oauth, "_post", lambda url, data: (
        400, {"error": "invalid_scope",
              "error_description": "Invalid device flow scope: gmail.modify"}))
    assert google_oauth.diagnose_client("gmail")["kind"] == "tv_client"


def test_diagnose_client_recognises_a_desktop_client(google_env, monkeypatch):
    google_oauth.save_client_config("cid", "secret")
    google_oauth.invalidate_diagnosis_cache()
    monkeypatch.setattr(google_oauth, "_post", lambda url, data: (
        401, {"error": "invalid_client",
              "error_description": "The OAuth client type is incorrect."}))
    assert google_oauth.diagnose_client("gmail")["kind"] == "desktop"


def test_diagnose_client_uses_the_device_code_when_it_works(google_env, monkeypatch):
    google_oauth.save_client_config("cid", "secret")
    google_oauth.invalidate_diagnosis_cache()
    monkeypatch.setattr(google_oauth, "_post", lambda url, data: (
        200, {"device_code": "D", "user_code": "U"}))
    result = google_oauth.diagnose_client("gmail")
    assert result["kind"] == "device_ok"
    assert result["device"]["device_code"] == "D"


def test_diagnose_client_without_a_client_is_explicit(google_env):
    assert google_oauth.diagnose_client("gmail")["kind"] == "no_client"


def test_run_setup_stops_when_google_blocks_the_client(google_env, monkeypatch):
    """A TV client must not send the user to a link that cannot work."""
    google_oauth.save_client_config("cid", "secret")
    google_oauth.invalidate_diagnosis_cache()
    monkeypatch.setattr(google_oauth, "diagnose_client", lambda service: {
        "kind": "tv_client", "detail": "Invalid device flow scope", "device": None})
    monkeypatch.setattr(google_oauth, "check_consent_url", lambda url: {
        "ok": False,
        "detail": ("invalid_request rMLocalhost URI is not allowed for "
                   "'NATIVE_DEVICE' client type")})

    result = google_oauth.run_setup("gmail", open_browser=False, timeout=1)

    assert result["stage"] == "blocked"
    assert "Desktop app" in result["message"]


def test_plugin_hints_use_the_documented_command_order():
    for name in ("gmail", "google_calendar", "google_drive"):
        hint = pm_module.DEFAULT_PLUGINS[name]["setup_hint"]
        assert f"/plugin setup {name}" in hint


def test_plugin_command_accepts_both_argument_orders(monkeypatch, tmp_path,
                                                     google_env, capsys):
    """'/plugin setup gmail' is documented; '/plugin gmail setup' must work too."""
    iface = _isolate_interface(monkeypatch, tmp_path)

    iface._handle_plugin_command("/plugin status gmail")
    documented = capsys.readouterr().out
    iface._handle_plugin_command("/plugin gmail status")
    natural = capsys.readouterr().out

    assert "/plugin setup gmail" in documented
    assert "/plugin setup gmail" in natural
    assert "NOT CONNECTED" in documented or "NO OAUTH CLIENT" in documented
    assert "NOT CONNECTED" in natural or "NO OAUTH CLIENT" in natural


def test_plugin_command_still_rejects_an_unknown_subcommand(monkeypatch, tmp_path,
                                                            google_env, capsys):
    iface = _isolate_interface(monkeypatch, tmp_path)
    iface._handle_plugin_command("/plugin frobnicate")
    out = capsys.readouterr().out
    assert "Unknown subcommand" in out
