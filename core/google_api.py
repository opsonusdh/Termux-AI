"""
Google REST API clients used by the Gmail / Calendar / Drive plugins.

The plugins talk to Google's HTTP APIs directly instead of pulling in
``google-api-python-client``: the project already depends on ``requests`` and
the handful of endpoints that are needed here are a few lines each.  That
keeps the install light (useful on a phone) and keeps the dependency surface
small.

Every call goes through :func:`_request`, which attaches a fresh access token
(``core.google_oauth`` refreshes it when needed) and turns Google's error
payloads into readable messages — an expired grant and a wrong scope look
very different to a user, and "HTTP 403" on its own does not.
"""
import os
import re
import sys
import json
import time
import base64
import mimetypes
import uuid
from datetime import datetime

# Path bootstrap (core first, project root second).
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_CORE_DIR)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(1, _ROOT_DIR)

import paths
from core import google_oauth

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


GMAIL_API     = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_API  = "https://www.googleapis.com/calendar/v3"
DRIVE_API     = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD  = "https://www.googleapis.com/upload/drive/v3/files"

HTTP_TIMEOUT = 30


class GoogleApiError(Exception):
    """A Google API call failed in a way worth showing to the user."""


# ── HTTP plumbing ──────────────────────────────────────────────────────────────
def _request(service: str,
             method: str,
             url: str,
             params: dict | None = None,
             json_body: dict | None = None,
             data=None,
             headers: dict | None = None,
             timeout: int = HTTP_TIMEOUT):
    """Perform an authenticated Google API request and return the response."""
    if requests is None:
        raise GoogleApiError("The 'requests' package is not installed.")

    token, error = google_oauth.get_access_token(service)
    if not token:
        raise GoogleApiError(error or f"{service} is not connected.")

    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)

    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=request_headers,
            params=params,
            json=json_body,
            data=data,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise GoogleApiError(f"Google did not answer in time ({timeout}s).") from exc
    except Exception as exc:
        raise GoogleApiError(f"Could not reach Google: {exc}") from exc

    if resp.status_code == 401:
        raise GoogleApiError(
            "Google rejected the access token (401). "
            "Reconnect the plugin with '/plugin setup <name>'.")
    if resp.status_code >= 400:
        raise GoogleApiError(_error_message(resp))
    return resp


def _error_message(resp) -> str:
    """Turn a Google error payload into one readable line."""
    detail = ""
    try:
        payload = resp.json()
        err = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(err, dict):
            detail = err.get("message") or err.get("status") or ""
            status = err.get("status")
            reason = err.get("errors", [{}])[0].get("reason", "") if isinstance(err.get("errors"), list) and err.get("errors") else ""
            if status and reason:
                detail = f"{detail} ({status}: {reason})"
    except Exception:
        detail = (resp.text or "")[:200]

    hint = ""
    if resp.status_code == 403:
        hint = (" — the granted scopes may not cover this action; "
                "reconnect with '/plugin setup <name>'.")
    elif resp.status_code == 404:
        hint = " — the item does not exist or is not shared with this account."
    return f"Google API error {resp.status_code}: {detail or 'request failed'}{hint}"


def _json(resp) -> dict:
    """Parse a JSON response body (empty dict when there is none)."""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"items": data}


def _b64url_decode(data: str) -> str:
    """Decode Gmail's URL-safe base64 payloads."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return ""


def _rfc3339(value: str) -> str:
    """Normalise a user-supplied date/time into RFC3339.

    Naive values are interpreted in the device's local timezone, because a
    user typing "tomorrow 14:00" means their own 14:00, not UTC.
    """
    value = (value or "").strip()
    if not value:
        return ""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat()


# ── Gmail ──────────────────────────────────────────────────────────────────────
def gmail_list_messages(query: str = "", max_results: int = 10,
                        label_ids: str = "") -> dict:
    """List message stubs matching a Gmail search query.

    Each entry is enriched with subject/from/date (fetched with the cheap
    ``format=metadata`` call) so callers can display a real message list
    instead of bare message IDs.
    """
    max_results = max(1, min(100, int(max_results or 10)))
    params = {"maxResults": max_results}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = label_ids
    data = _json(_request("gmail", "GET", f"{GMAIL_API}/messages", params=params))
    messages = []
    for item in data.get("messages", []) or []:
        mid = item.get("id", "")
        meta = _gmail_message_meta(mid) if mid else {}
        messages.append({
            "id": mid,
            "thread_id": item.get("threadId", ""),
            "snippet": item.get("snippet", meta.get("snippet", "")),
            "subject": meta.get("subject", ""),
            "from": meta.get("from", ""),
            "date": meta.get("date", ""),
        })
    return {
        "messages": messages,
        "estimate": data.get("resultSizeEstimate", len(messages)),
        "query": query,
    }


def _gmail_message_meta(message_id: str) -> dict:
    """Fetch headers + snippet for one message (metadata format, cheap)."""
    try:
        data = _json(_request("gmail", "GET",
                              f"{GMAIL_API}/messages/{message_id}",
                              params={"format": "metadata",
                                      "metadataFields": ["From", "To",
                                                         "Subject", "Date"]}))
    except GoogleApiError:
        return {}
    headers = {}
    for header in (data.get("payload") or {}).get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name and name not in headers:
            headers[name] = header.get("value", "")
    return {
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "snippet": data.get("snippet", ""),
    }


def gmail_get_profile() -> dict:
    """Return the connected Gmail account's profile (emailAddress, …).

    Needed so plugin status can show the actual Gmail address instead of an
    empty account field.
    """
    data = _json(_request("gmail", "GET", f"{GMAIL_API}/profile"))
    return {
        "email_address": data.get("emailAddress", ""),
        "messages_total": data.get("messagesTotal", 0),
        "threads_total": data.get("threadsTotal", 0),
        "history_id": data.get("historyId", ""),
    }


def gmail_get_message(message_id: str) -> dict:
    """Fetch one message with its headers and body text."""
    if not message_id:
        raise GoogleApiError("A message id is required.")
    data = _json(_request("gmail", "GET",
                          f"{GMAIL_API}/messages/{message_id}",
                          params={"format": "full"}))

    headers = {}
    for header in data.get("payload", {}).get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name and name not in headers:
            headers[name] = header.get("value", "")

    attachments = []
    for part in _walk_parts(data.get("payload", {})):
        filename = part.get("filename") or ""
        if filename:
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", ""),
                "size": part.get("body", {}).get("size", 0),
            })

    return {
        "id": data.get("id", message_id),
        "thread_id": data.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "labels": data.get("labelIds", []),
        "body": _body_text(data.get("payload", {})),
        "attachments": attachments,
        "snippet": data.get("snippet", ""),
    }


def _walk_parts(part: dict):
    """Yield every MIME part of a Gmail payload."""
    if not isinstance(part, dict):
        return
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def _html_to_text(html: str) -> str:
    """Crude but dependency-free HTML → plain text.

    BeautifulSoup is preferred when available; otherwise a small regex stripper
    removes tags and unclobbers the most common entities.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup  # optional, already a project dependency
        return BeautifulSoup(html, "html.parser").get_text("\n")
    except Exception:
        pass
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ")
              .replace("&amp;", "&")
              .replace("&lt;", "<")
              .replace("&gt;", ">")
              .replace("&quot;", '"')
              .replace("&#39;", "'"))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _body_text(payload: dict) -> str:
    """Extract the readable text of a Gmail message.

    ``multipart/alternative`` parts are the *same* content in several formats,
    so joining them would return every message twice.  The plain-text part is
    preferred; the HTML part is stripped to plain text when there is no plain
    text.
    """
    if not payload:
        return ""

    mime = str(payload.get("mimeType", ""))
    parts = payload.get("parts") or []
    data = (payload.get("body") or {}).get("data")

    if mime == "multipart/alternative" and parts:
        for wanted in ("text/plain", "text/html"):
            for part in parts:
                if str(part.get("mimeType", "")) == wanted:
                    text = _body_text(part)
                    if text:
                        return text

    if data and (mime.startswith("text/") or not mime):
        decoded = _b64url_decode(data)
        if mime == "text/html":
            return _html_to_text(decoded)
        return decoded

    chunks = []
    for child in parts:
        text = _body_text(child)
        if text:
            chunks.append(text)
    if chunks:
        return "\n".join(chunks)
    if data:
        return _b64url_decode(data)
    return ""


def gmail_send_message(to: str, subject: str, body: str,
                       cc: str = "", bcc: str = "") -> dict:
    """Send a plain-text email."""
    if not to:
        raise GoogleApiError("A recipient ('to') is required.")

    headers = [f"To: {to}"]
    if cc:
        headers.append(f"Cc: {cc}")
    if bcc:
        headers.append(f"Bcc: {bcc}")
    headers.append(f"Subject: {subject or ''}")
    headers.append('Content-Type: text/plain; charset="UTF-8"')
    headers.append("")
    headers.append(body or "")

    raw = base64.urlsafe_b64encode("\r\n".join(headers).encode("utf-8")).decode()
    data = _json(_request("gmail", "POST", f"{GMAIL_API}/messages/send",
                          json_body={"raw": raw}))
    return {
        "id": data.get("id", ""),
        "thread_id": data.get("threadId", ""),
        "to": to,
        "subject": subject or "",
    }


def gmail_list_labels() -> dict:
    """List the mailbox labels."""
    data = _json(_request("gmail", "GET", f"{GMAIL_API}/labels"))
    labels = []
    for item in data.get("labels", []) or []:
        labels.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "type": item.get("type", ""),
            "messages_total": item.get("messagesTotal", 0),
            "unread": item.get("messagesUnread", 0),
        })
    return {"labels": labels}


def gmail_create_label(name: str) -> dict:
    """Create a mailbox label."""
    if not name:
        raise GoogleApiError("A label name is required.")
    data = _json(_request("gmail", "POST", f"{GMAIL_API}/labels",
                          json_body={
                              "name": name,
                              "labelListVisibility": "labelShow",
                              "messageListVisibility": "show",
                          }))
    return {"id": data.get("id", ""), "name": data.get("name", name)}


def gmail_delete_message(message_id: str, permanent: bool = False) -> dict:
    """Move a message to the trash, or delete it permanently."""
    if not message_id:
        raise GoogleApiError("A message id is required.")
    if permanent:
        _request("gmail", "DELETE", f"{GMAIL_API}/messages/{message_id}")
        return {"id": message_id, "action": "deleted permanently"}
    _request("gmail", "POST", f"{GMAIL_API}/messages/{message_id}/trash")
    return {"id": message_id, "action": "moved to trash"}


# ── Google Calendar ────────────────────────────────────────────────────────────
def calendar_list_calendars() -> dict:
    """List the calendars the account can see."""
    data = _json(_request("google_calendar", "GET",
                          f"{CALENDAR_API}/users/me/calendarList"))
    calendars = []
    for item in data.get("items", []) or []:
        calendars.append({
            "id": item.get("id", ""),
            "summary": item.get("summary", ""),
            "primary": item.get("primary", False),
            "access_role": item.get("accessRole", ""),
        })
    return {"calendars": calendars}


def calendar_list_events(calendar_id: str = "primary",
                         time_min: str = "", time_max: str = "",
                         max_results: int = 25, query: str = "",
                         include_cancelled: bool = False,
                         page_token: str = "") -> dict:
    """List events of a calendar.

    When neither ``time_min`` nor ``time_max`` is given the window defaults to
    thirty days in the past through ninety days ahead (local time), so a call
    with no bounds returns the events *around now* instead of the oldest
    events on the calendar.  Results are paginated via ``next_page_token``;
    pass it back to fetch the next page.  Set ``include_cancelled=True`` to
    also surface events with ``status == cancelled`` (hidden by default).
    """
    from datetime import datetime, timedelta

    max_results = max(1, min(2500, int(max_results or 25)))
    params = {
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if time_min:
        params["timeMin"] = _rfc3339(time_min)
    if time_max:
        params["timeMax"] = _rfc3339(time_max)
    if not time_min and not time_max:
        now = datetime.now().astimezone()
        params["timeMin"] = _rfc3339((now - timedelta(days=30)).isoformat())
        params["timeMax"] = _rfc3339((now + timedelta(days=90)).isoformat())
    if query:
        params["q"] = query
    if include_cancelled:
        params["showDeleted"] = "true"
    if page_token:
        params["pageToken"] = page_token

    data = _json(_request("google_calendar", "GET",
                          f"{CALENDAR_API}/calendars/{calendar_id}/events",
                          params=params))
    events = []
    for item in data.get("items", []) or []:
        start = item.get("start") or {}
        end = item.get("end") or {}
        all_day = bool(start.get("date") and not start.get("dateTime"))
        events.append({
            "id": item.get("id", ""),
            "summary": item.get("summary", "(no title)"),
            "status": item.get("status", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "start_tz": start.get("timeZone", ""),
            "end_tz": end.get("timeZone", ""),
            "all_day": all_day,
            "location": item.get("location", ""),
            "description": item.get("description", ""),
            "html_link": item.get("htmlLink", ""),
            "attendees": [a.get("email") for a in (item.get("attendees") or [])
                          if a.get("email")],
        })
    return {
        "calendar_id": calendar_id,
        "events": events,
        "next_page_token": data.get("nextPageToken", ""),
        "time_min": params.get("timeMin", ""),
        "time_max": params.get("timeMax", ""),
    }


def _event_body(summary: str, start: str, end: str, description: str,
                location: str, attendees: list | None) -> dict:
    """Build the JSON body of a Calendar event."""
    payload: dict = {}
    if summary:
        payload["summary"] = summary
    if start:
        payload["start"] = {"dateTime": _rfc3339(start)}
    if end:
        payload["end"] = {"dateTime": _rfc3339(end)}
    if description:
        payload["description"] = description
    if location:
        payload["location"] = location
    if attendees:
        payload["attendees"] = [{"email": a} for a in attendees if a]
    return payload


def calendar_create_event(calendar_id: str = "primary", summary: str = "",
                          start: str = "", end: str = "",
                          description: str = "", location: str = "",
                          attendees: list | None = None) -> dict:
    """Create a calendar event."""
    if not summary:
        raise GoogleApiError("An event title ('summary') is required.")
    if not start or not end:
        raise GoogleApiError("Both 'start' and 'end' are required.")

    payload = _event_body(summary, start, end, description, location, attendees)
    data = _json(_request("google_calendar", "POST",
                          f"{CALENDAR_API}/calendars/{calendar_id}/events",
                          json_body=payload))
    return {
        "id": data.get("id", ""),
        "summary": data.get("summary", summary),
        "html_link": data.get("htmlLink", ""),
        "start": (data.get("start") or {}).get("dateTime", ""),
        "end": (data.get("end") or {}).get("dateTime", ""),
    }


def calendar_update_event(event_id: str, calendar_id: str = "primary",
                          summary: str = "", start: str = "", end: str = "",
                          description: str = "", location: str = "",
                          attendees: list | None = None) -> dict:
    """Update an existing calendar event (only the given fields change)."""
    if not event_id:
        raise GoogleApiError("An event id is required.")

    payload = _event_body(summary, start, end, description, location, attendees)
    if not payload:
        raise GoogleApiError("Nothing to update — provide at least one field.")

    data = _json(_request("google_calendar", "PATCH",
                          f"{CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                          json_body=payload))
    return {
        "id": data.get("id", event_id),
        "summary": data.get("summary", ""),
        "html_link": data.get("htmlLink", ""),
    }


def calendar_delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    """Delete a calendar event."""
    if not event_id:
        raise GoogleApiError("An event id is required.")
    _request("google_calendar", "DELETE",
             f"{CALENDAR_API}/calendars/{calendar_id}/events/{event_id}")
    return {"id": event_id, "action": "deleted"}


# ── Google Drive ──────────────────────────────────────────────────────────────
def drive_list_files(query: str = "", page_size: int = 25,
                     order_by: str = "modifiedTime desc") -> dict:
    """List Drive files."""
    page_size = max(1, min(1000, int(page_size or 25)))
    fields = ("files(id,name,mimeType,size,modifiedTime,webViewLink,"
              "owners(emailAddress)),nextPageToken")
    params = {
        "pageSize": page_size,
        "fields": fields,
        "orderBy": order_by,
    }
    if query:
        params["q"] = query

    data = _json(_request("google_drive", "GET", f"{DRIVE_API}/files",
                          params=params))
    files = []
    for item in data.get("files", []) or []:
        owners = [o.get("emailAddress", "")
                  for o in (item.get("owners") or [])]
        files.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "mime_type": item.get("mimeType", ""),
            "size": item.get("size", ""),
            "modified": item.get("modifiedTime", ""),
            "owner": owners[0] if owners else "",
            "link": item.get("webViewLink", ""),
        })
    return {"files": files, "next_page_token": data.get("nextPageToken", "")}


def drive_upload_file(file_path: str, name: str = "",
                      parent_folder_id: str = "") -> dict:
    """Upload a local file to Drive."""
    path = os.path.expanduser(file_path or "")
    if not path:
        raise GoogleApiError("A file path is required.")
    if not os.path.isabs(path):
        path = os.path.join(paths.ROOT, path)
    if not os.path.isfile(path):
        raise GoogleApiError(f"File not found: {file_path}")

    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError as exc:
        raise GoogleApiError(f"Could not read {file_path}: {exc}") from exc

    metadata = {"name": name or os.path.basename(path)}
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    boundary = f"termux-ai-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
    params = {"uploadType": "multipart",
              "fields": "id,name,mimeType,size,webViewLink"}
    data = _json(_request("google_drive", "POST", DRIVE_UPLOAD,
                          params=params, data=body, headers=headers,
                          timeout=180))
    return {
        "id": data.get("id", ""),
        "name": data.get("name", metadata["name"]),
        "mime_type": data.get("mimeType", mime),
        "size": data.get("size", str(len(content))),
        "link": data.get("webViewLink", ""),
    }


def drive_download_file(file_id: str, output_path: str = "") -> dict:
    """Download a Drive file to the device."""
    if not file_id:
        raise GoogleApiError("A file id is required.")

    resp = _request("google_drive", "GET",
                    f"{DRIVE_API}/files/{file_id}",
                    params={"alt": "media"})

    if output_path:
        target = os.path.expanduser(output_path)
    else:
        downloads = os.path.join(paths.WORKSPACE_DIR, "downloads")
        os.makedirs(downloads, exist_ok=True)
        name = file_id
        try:
            meta = _json(_request("google_drive", "GET",
                                  f"{DRIVE_API}/files/{file_id}",
                                  params={"fields": "name"}))
            name = meta.get("name") or file_id
        except GoogleApiError:
            pass
        target = os.path.join(downloads, os.path.basename(name) or file_id)

    if not os.path.isabs(target):
        target = os.path.join(paths.ROOT, target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        with open(target, "wb") as fh:
            fh.write(resp.content)
    except OSError as exc:
        raise GoogleApiError(f"Could not write {target}: {exc}") from exc

    return {"id": file_id, "path": target, "size": len(resp.content)}


def drive_delete_file(file_id: str) -> dict:
    """Delete a Drive file."""
    if not file_id:
        raise GoogleApiError("A file id is required.")
    _request("google_drive", "DELETE", f"{DRIVE_API}/files/{file_id}")
    return {"id": file_id, "action": "deleted"}


def drive_create_folder(name: str, parent_folder_id: str = "") -> dict:
    """Create a Drive folder."""
    if not name:
        raise GoogleApiError("A folder name is required.")
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]
    data = _json(_request("google_drive", "POST", f"{DRIVE_API}/files",
                          json_body=metadata))
    return {"id": data.get("id", ""), "name": data.get("name", name)}


def drive_share_file(file_id: str, email: str, role: str = "reader",
                     permission_type: str = "user") -> dict:
    """Share a Drive file with somebody."""
    if not file_id:
        raise GoogleApiError("A file id is required.")
    if not email:
        raise GoogleApiError("The email address to share with is required.")

    payload = {
        "type": permission_type,
        "role": role,
    }
    if permission_type == "user":
        payload["emailAddress"] = email

    data = _json(_request("google_drive", "POST",
                          f"{DRIVE_API}/files/{file_id}/permissions",
                          json_body=payload))
    return {
        "id": data.get("id", ""),
        "file_id": file_id,
        "email": email,
        "role": role,
        "type": permission_type,
    }
