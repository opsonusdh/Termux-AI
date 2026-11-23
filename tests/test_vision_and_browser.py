"""
Tests for the vision pipeline and the persistent browser sessions.

Three things are covered:

  1. The vision registry — which models accept an image, verified live
     against the real endpoints rather than taken from marketing copy.
  2. The image pipeline — a tool queues a picture, the picture travels to
     the model in a *user* message (Google rejects an image part inside a
     tool message), and a text-only model gets a note instead of a picture.
  3. The browser session manager — sessions survive between calls, are
     addressed by id, and are cleaned up.
  4. Mirage — when the serving model cannot see, the on-device
     vision-to-text model describes the picture as text, and the
     vision-model switch is held while Mirage is available.
  5. The Mirage endpoint — the OpenAI-compatible loopback service that
     runs the same model where the cactus package cannot load (served
     over real HTTP in-process, with the engine faked).

No network access and no real API keys are used here.
"""
import base64
import json
import io
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO_ROOT, "core")
for _p in (REPO_ROOT, CORE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import tools  # noqa: E402
from core.models import (IMAGE_MODEL_SLOTS, VISION_CAPABLE_MODELS,  # noqa: E402
                          model_supports_vision)
from core import context_manager  # noqa: E402
from core import llm_client  # noqa: E402

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
)


def _write_png(tmp_path, name="shot.png", size=64):
    path = tmp_path / name
    path.write_bytes(PNG_BYTES)
    return str(path)


# ── 1. the vision registry ───────────────────────────────────────────────────

class TestVisionRegistry:
    def test_verified_vision_models_are_listed(self):
        # Verified by sending a real PNG and checking the request was
        # accepted (workspace/audit/probe_vision_models.py).
        assert model_supports_vision("google", "gemini-3.5-flash")
        assert model_supports_vision("google", "gemini-3.1-flash-lite")
        assert model_supports_vision("google", "gemma-4-31b-it")
        assert model_supports_vision("nvidia",
                                     "nvidia/nemotron-3.5-super-vl-120b-a12b")
        assert model_supports_vision("nvidia", "minimaxai/minimax-m3.1-flash")
        # ollama is currently commented out of the registry, so its models
        # are treated as text-only again.
        assert not model_supports_vision("ollama", "gemma4:31b")

    def test_text_only_models_are_excluded(self):
        # Each of these was seen rejecting an image payload with a 400.
        assert not model_supports_vision("groq", "openai/gpt-oss-120b")
        assert not model_supports_vision(
            "nvidia", "nvidia/nemotron-3-ultra-550b-a55b")
        assert not model_supports_vision(
            "nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")
        assert not model_supports_vision("ollama", "gpt-oss:120b-cloud")
        assert not model_supports_vision("ollama", "nemotron-3-ultra:cloud")

    def test_unknown_model_is_treated_as_text_only(self):
        # An unlisted model must never be handed an image: the safe default
        # is "cannot see".
        assert not model_supports_vision("google", "some-unknown-model")
        assert not model_supports_vision("nvidia", "")
        assert not model_supports_vision("nvidia", None)

    def test_registry_only_names_known_providers(self):
        assert set(VISION_CAPABLE_MODELS) <= {"nvidia", "google", "ollama"}


# ── 2. the image pipeline ────────────────────────────────────────────────────

class TestMultimodalSanitizer:
    def test_text_only_model_gets_a_string_with_a_note(self):
        content = [{"type": "text", "text": "look"},
                   {"type": "orion_image", "path": "/tmp/x.png",
                    "label": "shot"}]
        out = llm_client._sanitize_multimodal_content(
            content, "groq", "openai/gpt-oss-120b")
        assert isinstance(out, str)
        assert "cannot view images" in out
        assert "look" in out

    def test_vision_model_keeps_the_image_marker(self):
        content = [{"type": "text", "text": "look"},
                   {"type": "orion_image", "path": "/tmp/x.png",
                    "label": "shot"}]
        out = llm_client._sanitize_multimodal_content(
            content, "google", "gemini-3.5-flash")
        assert isinstance(out, list)
        assert any(p.get("type") == "orion_image" for p in out)

    def test_sanitizer_is_idempotent(self):
        content = [{"type": "text", "text": "look"},
                   {"type": "orion_image", "path": "/tmp/x.png",
                    "label": "shot"}]
        once = llm_client._sanitize_multimodal_content(
            content, "google", "gemini-3.5-flash")
        twice = llm_client._sanitize_multimodal_content(
            once, "google", "gemini-3.5-flash")
        assert once == twice

    def test_strip_images_removes_the_picture_and_leaves_a_note(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "orion_image", "path": "/tmp/x.png", "label": "shot"}]}]
        stripped = llm_client._strip_images(messages)
        assert all(p.get("type") != "orion_image"
                   for p in stripped[0]["content"])
        assert any("image omitted" in str(p.get("text", "")).lower()
                   for p in stripped[0]["content"])

    def test_strip_images_reports_no_change_when_there_are_no_images(self):
        messages = [{"role": "user", "content": "plain"}]
        assert llm_client._strip_images(messages) is messages


class TestMirageEyes:
    """Mirage turns a picture into text when the serving model cannot see."""

    @staticmethod
    def _fake(describe):
        return types.SimpleNamespace(describe=describe)

    def test_text_only_model_gets_the_mirage_description(self, monkeypatch,
                                                         tmp_path):
        calls = []

        def describe(image, **kw):
            calls.append(image)
            return {"description": "a red square on white"}

        monkeypatch.setattr(llm_client, "_get_mirage",
                            lambda: self._fake(describe))
        content = [{"type": "text", "text": "look"},
                   {"type": "orion_image", "path": _write_png(tmp_path),
                    "label": "shot"}]
        out = llm_client._sanitize_multimodal_content(
            content, "groq", "openai/gpt-oss-120b")
        assert isinstance(out, str)
        assert "a red square on white" in out
        assert "Mirage" in out
        assert "shot" in out
        # the local path reached the describer
        assert calls and os.path.isfile(calls[0])

    def test_a_failed_description_still_leaves_the_note(self, monkeypatch):
        def describe(image, **kw):
            raise RuntimeError("engine crashed")

        monkeypatch.setattr(llm_client, "_get_mirage",
                            lambda: self._fake(describe))
        content = [{"type": "orion_image", "path": "/tmp/x.png",
                    "label": "shot"}]
        out = llm_client._sanitize_multimodal_content(
            content, "groq", "openai/gpt-oss-120b")
        assert isinstance(out, str)
        assert "cannot view images" in out

    def test_mirage_unavailable_keeps_the_old_note(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_get_mirage", lambda: None)
        content = [{"type": "orion_image", "path": "/tmp/x.png",
                    "label": "shot"}]
        out = llm_client._sanitize_multimodal_content(
            content, "groq", "openai/gpt-oss-120b")
        assert isinstance(out, str)
        assert "cannot view images" in out

    def test_data_uri_is_decoded_into_bytes(self, monkeypatch):
        seen = {}

        def describe(image, **kw):
            seen["image"] = image
            return {"text": "ocr says hello"}

        monkeypatch.setattr(llm_client, "_get_mirage",
                            lambda: self._fake(describe))
        uri = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
        content = [{"type": "image_url", "image_url": {"url": uri},
                    "label": "paste"}]
        out = llm_client._sanitize_multimodal_content(
            content, "groq", "openai/gpt-oss-120b")
        assert seen["image"] == PNG_BYTES
        assert "ocr says hello" in out

    def test_a_remote_url_is_not_described(self, monkeypatch):
        def describe(image, **kw):
            raise AssertionError("a remote URL must not reach the describer")

        monkeypatch.setattr(llm_client, "_get_mirage",
                            lambda: self._fake(describe))
        content = [{"type": "image_url",
                    "image_url": {"url": "https://example.com/x.png"},
                    "label": "web"}]
        out = llm_client._sanitize_multimodal_content(
            content, "groq", "openai/gpt-oss-120b")
        assert isinstance(out, str)
        assert "cannot view images" in out

    def test_vision_switch_holds_when_mirage_serves(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_get_mirage", lambda: object())
        atts = [{"path": "/tmp/x.png", "explicit": True}]
        assert not llm_client._wants_vision_switch(
            atts, "groq", "openai/gpt-oss-120b", False)

    def test_vision_switch_still_happens_without_mirage(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_get_mirage", lambda: None)
        atts = [{"path": "/tmp/x.png", "explicit": True}]
        assert llm_client._wants_vision_switch(
            atts, "groq", "openai/gpt-oss-120b", False)

    def test_switch_rules_are_unchanged_otherwise(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_get_mirage", lambda: None)
        atts = [{"path": "/tmp/x.png", "explicit": True}]
        # a model that sees images is never switched away from
        assert not llm_client._wants_vision_switch(
            atts, "google", "gemini-3.5-flash", False)
        # an already-switched turn never switches again
        assert not llm_client._wants_vision_switch(
            atts, "groq", "openai/gpt-oss-120b", True)
        # an unasked-for picture never changes the model
        implicit = [{"path": "/tmp/x.png", "explicit": False}]
        assert not llm_client._wants_vision_switch(
            implicit, "groq", "openai/gpt-oss-120b", False)


class TestMirageEndpoint:
    """The local OpenAI-compatible endpoint that carries Mirage on Termux."""

    @staticmethod
    def _png_bytes():
        from PIL import Image
        image = Image.new("RGB", (8, 8), (200, 30, 30))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    @classmethod
    def _data_uri(cls):
        return ("data:image/png;base64,"
                + base64.b64encode(cls._png_bytes()).decode())

    @staticmethod
    def _server(engine_result=None, engine_error=None):
        """A real loopback HTTP server whose engine is a stub."""
        import threading
        from http.server import ThreadingHTTPServer
        import mirage_server

        def describe(rgb, width, height, options):
            if engine_error is not None:
                raise mirage_server.BridgeError(engine_error)
            return engine_result if engine_result is not None else {}

        state = mirage_server._ServerState(
            types.SimpleNamespace(describe=describe))
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     mirage_server._make_handler(state))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_health_and_model_list(self):
        import urllib.request
        server, base = self._server()
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                health = json.loads(r.read().decode())
            assert health["status"] == "ok"
            with urllib.request.urlopen(base + "/v1/models", timeout=5) as r:
                models = json.loads(r.read().decode())
            assert models["object"] == "list"
            assert models["data"][0]["id"] == "mirage"
        finally:
            server.shutdown()

    def test_openai_completion_shape_with_a_data_uri(self):
        import urllib.request
        engine = {"scene": {"background": "red"},
                  "objects": [{"name": "yellow rectangle",
                               "color": "yellow", "shape": "rectangular",
                               "position": "center"}]}
        server, base = self._server(engine_result=engine)
        try:
            body = json.dumps({
                "model": "mirage",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": self._data_uri()}},
                ]}],
            }).encode()
            request = urllib.request.Request(
                base + "/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as r:
                payload = json.loads(r.read().decode())
            assert payload["object"] == "chat.completion"
            message = payload["choices"][0]["message"]
            assert message["role"] == "assistant"
            assert "yellow rectangle" in message["content"]
            assert payload["mirage_raw"] == engine
            assert payload["usage"]["completion_tokens"] >= 1
        finally:
            server.shutdown()

    def test_stream_returns_sse_chunks(self):
        import urllib.request
        server, base = self._server(engine_result={"text": "hi"})
        try:
            body = json.dumps({
                "model": "mirage", "stream": True,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": self._data_uri()}},
                ]}],
            }).encode()
            request = urllib.request.Request(
                base + "/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as r:
                assert r.headers.get("Content-Type") == "text/event-stream"
                raw = r.read().decode()
            assert raw.count("data: ") == 4          # role, content, stop, [DONE]
            assert raw.rstrip().endswith("[DONE]")
        finally:
            server.shutdown()

    def test_a_request_without_an_image_is_an_openai_error(self):
        import urllib.error
        import urllib.request
        server, base = self._server()
        try:
            body = json.dumps({
                "model": "mirage",
                "messages": [{"role": "user", "content": "plain text"}],
            }).encode()
            request = urllib.request.Request(
                base + "/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(request, timeout=10)
            assert excinfo.value.code == 400
            payload = json.loads(excinfo.value.read().decode())
            assert payload["error"]["type"] == "invalid_request_error"
        finally:
            server.shutdown()

    def test_an_engine_failure_is_a_502(self):
        import urllib.error
        import urllib.request
        server, base = self._server(engine_error="bridge died")
        try:
            body = json.dumps({
                "model": "mirage",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": self._data_uri()}},
                ]}],
            }).encode()
            request = urllib.request.Request(
                base + "/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(request, timeout=10)
            assert excinfo.value.code == 502
        finally:
            server.shutdown()

    def test_prose_from_engine_result(self):
        import mirage_server
        prose = mirage_server.prose_from_result({
            "scene": {"background": "red", "style": "minimalist"},
            "objects": [{"name": "yellow rectangle", "color": "yellow",
                         "shape": "rectangular", "position": "center"}],
            "text": [{"text": "EXIT"}],
        })
        assert "red" in prose and "yellow rectangle" in prose
        assert "EXIT" in prose

    def test_decode_image_accepts_bytes_and_data_uri(self):
        import mirage_server
        rgb, width, height = mirage_server.decode_image(self._data_uri())
        assert len(rgb) == width * height * 3
        rgb2, w2, h2 = mirage_server.decode_image(self._png_bytes())
        assert (w2, h2) == (width, height)

    def test_ensure_endpoint_reuses_a_running_server(self, monkeypatch):
        import mirage_server
        monkeypatch.setattr(mirage_server, "_health_ok",
                            lambda port, timeout=0.5: True)

        def fail(*args, **kwargs):
            raise AssertionError("must not spawn a server")

        monkeypatch.setattr(mirage_server.subprocess, "Popen", fail)
        client = mirage_server.ensure_endpoint()
        assert isinstance(client, mirage_server.MirageEndpointClient)

    def test_ensure_endpoint_returns_none_without_node(self, monkeypatch):
        import mirage_server
        monkeypatch.setattr(mirage_server, "_health_ok",
                            lambda port, timeout=0.5: False)
        monkeypatch.setattr(mirage_server.shutil, "which", lambda name: None)
        assert mirage_server.ensure_endpoint() is None

    def test_endpoint_client_returns_the_package_shape(self):
        import mirage_server
        server, base = self._server(
            engine_result={"scene": {"background": "red"}})
        try:
            client = mirage_server.MirageEndpointClient(base)
            result = client.describe(self._data_uri())
            assert result["description"]            # the prose key llm_client reads
            assert result["raw"]["scene"] == {"background": "red"}
        finally:
            server.shutdown()


class TestWirePreparation:
    def _history(self, path):
        return [
            {"role": "user", "content": "take a screenshot"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "browser", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
            {"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "orion_image", "path": path, "label": "screenshot"}]},
        ]

    def test_vision_model_gets_a_data_uri(self, tmp_path):
        path = _write_png(tmp_path)
        wire = llm_client._prepare_messages_for_wire(
            self._history(path), "google", "gemini-3.5-flash")
        parts = [p for m in wire for p in (m.get("content") or [])
                 if isinstance(p, dict) and p.get("type") == "image_url"]
        assert len(parts) == 1
        assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_text_only_model_gets_no_image_part(self, tmp_path):
        path = _write_png(tmp_path)
        wire = llm_client._prepare_messages_for_wire(
            self._history(path), "groq", "openai/gpt-oss-120b")
        parts = [p for m in wire for p in (m.get("content") or [])
                 if isinstance(p, dict) and p.get("type") == "image_url"]
        assert parts == []
        assert any("cannot view images" in str(p.get("text", "")).lower()
                   for m in wire for p in (m.get("content") or [])
                   if isinstance(p, dict))

    def test_the_number_of_images_is_capped(self, tmp_path):
        path = _write_png(tmp_path)
        history = []
        for i in range(6):
            history.append({"role": "user", "content": [
                {"type": "text", "text": f"shot {i}"},
                {"type": "orion_image", "path": path, "label": f"s{i}"}]})
        wire = llm_client._prepare_messages_for_wire(
            history, "google", "gemini-3.5-flash", max_images=2)
        parts = [p for m in wire for p in (m.get("content") or [])
                 if isinstance(p, dict) and p.get("type") == "image_url"]
        assert len(parts) == 2

    def test_the_most_recent_images_are_the_ones_kept(self, tmp_path):
        path = _write_png(tmp_path)
        history = [{"role": "user", "content": [
            {"type": "text", "text": "old"},
            {"type": "orion_image", "path": path, "label": "old"}]},
            {"role": "user", "content": [
                {"type": "text", "text": "new"},
                {"type": "orion_image", "path": path, "label": "new"}]}]
        wire = llm_client._prepare_messages_for_wire(
            history, "google", "gemini-3.5-flash", max_images=1)
        old_parts = wire[0]["content"]
        new_parts = wire[1]["content"]
        # The older picture was dropped, the recent one was inlined.
        assert not any(p.get("type") == "image_url" for p in old_parts)
        assert any(p.get("type") == "image_url" for p in new_parts)
        assert any("old" in str(p.get("text", "")) for p in old_parts)

    def test_the_original_messages_are_not_mutated(self, tmp_path):
        path = _write_png(tmp_path)
        history = self._history(path)
        llm_client._prepare_messages_for_wire(
            history, "google", "gemini-3.5-flash")
        assert history[3]["content"][1]["type"] == "orion_image"


class TestAttachmentQueue:
    def test_queue_and_pop(self, tmp_path):
        path = _write_png(tmp_path)
        tools.pop_image_attachments()
        tools.queue_image_attachment(path, "a screenshot")
        pending = tools.pop_image_attachments()
        assert len(pending) == 1
        assert pending[0]["path"] == os.path.abspath(path)
        assert pending[0]["label"] == "a screenshot"
        assert tools.pop_image_attachments() == []

    def test_a_missing_file_is_ignored(self):
        tools.pop_image_attachments()
        assert tools.queue_image_attachment("/no/such/file.png") == ""
        assert tools.pop_image_attachments() == []

    def test_data_uri_rejects_an_oversized_file(self, tmp_path):
        path = tmp_path / "big.bin"
        path.write_bytes(b"\x00" * 5000)
        assert tools.image_to_data_uri(str(path), max_bytes=1000) is None

    def test_data_uri_detects_the_mime_type(self, tmp_path):
        jpg = tmp_path / "x.jpg"
        jpg.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)
        uri = tools.image_to_data_uri(str(jpg))
        assert uri.startswith("data:image/jpeg;base64,")

    def test_image_message_is_a_user_message(self):
        # Google rejects an image part inside a tool message, so the picture
        # has to travel in a user message after the tool result.
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "done"}]
        llm_client._append_image_message(
            messages, [{"path": "/tmp/x.png", "label": "shot"}])
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"][1]["type"] == "orion_image"

    def test_no_attachments_means_no_extra_message(self):
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "done"}]
        llm_client._append_image_message(messages, [])
        assert len(messages) == 1


class TestHistoryPersistence:
    def test_a_multimodal_tool_result_survives_into_the_history(self):
        content = [{"type": "text", "text": "look"},
                   {"type": "orion_image", "path": "/tmp/x.png",
                    "label": "shot"}]
        out = context_manager._msg_to_history(
            {"role": "tool", "tool_call_id": "c1", "content": content})
        assert isinstance(out[0]["content"], list)
        assert out[0]["content"][1]["type"] == "orion_image"

    def test_a_plain_tool_result_is_unchanged(self):
        out = context_manager._msg_to_history(
            {"role": "tool", "tool_call_id": "c1", "content": "done"})
        assert out[0]["content"] == "done"


# ── 3. the browser sessions ──────────────────────────────────────────────────

class TestBrowserToolContract:
    def test_the_browser_tool_is_advertised(self):
        names = [t["function"]["name"] for t in tools.TOOLS_DESCRIPTION]
        assert "browser" in names
        assert "browse_web" in names

    def test_the_action_types_are_advertised(self):
        entry = next(t for t in tools.TOOLS_DESCRIPTION
                     if t["function"]["name"] == "browser")
        actions = entry["function"]["parameters"]["properties"]["action"]
        assert set(actions["enum"]) == {
            "open", "act", "navigate", "screenshot", "state", "close", "list"}

    def test_the_dispatch_route_exists(self):
        # The route table is built inside _dispatch_tool; the tool has to be
        # reachable by name.  action='list' touches nothing.
        tools._browser_close_all()
        out = llm_client._dispatch_tool({
            "function": {"name": "browser",
                         "arguments": '{"action": "list"}'}})
        assert "No browser sessions" in out

    def test_closing_an_unknown_session_reports_the_error(self):
        out = tools.browser(action="close", browser_id="does-not-exist")
        assert out.startswith("[ERROR]")

    def test_acting_on_an_unknown_session_reports_the_error(self):
        out = tools.browser(action="act", browser_id="does-not-exist",
                            actions=[{"type": "click", "selector": "#x"}])
        assert out.startswith("[ERROR]")

    def test_an_unknown_action_is_rejected(self):
        out = tools.browser(action="teleport")
        assert out.startswith("[ERROR]")

    def test_opening_without_a_url_is_rejected(self):
        out = tools.browser(action="open", url="")
        assert out.startswith("[ERROR]")

    def test_listing_with_no_sessions(self):
        tools._browser_close_all()
        assert "No browser sessions" in tools.browser(action="list")

    def test_the_session_registry_is_empty_after_close_all(self):
        tools._browser_close_all()
        assert tools._BROWSER_SESSIONS == {}


class TestBrowserHelpers:
    def test_element_extraction_includes_form_values(self):
        # A form control's current value is not part of its text content,
        # so reading back a field the model typed into has to ask for it
        # explicitly.
        src = open(os.path.join(CORE, "tools.py"), encoding="utf-8").read()
        assert "el.value" in src
        assert "[value: " in src

    def test_the_action_types_include_scroll_press_and_select(self):
        src = open(os.path.join(CORE, "tools.py"), encoding="utf-8").read()
        for action in ("scroll", "press", "select"):
            assert f'action_type == "{action}"' in src

    def test_the_browser_profile_lives_in_the_workspace(self):
        # /tmp is not writable on Termux and does not exist on Windows.
        assert os.path.join("workspace", ".browser") in tools._BROWSER_DIR
        assert os.path.join("workspace", "screenshots") in tools._SCREENSHOT_DIR

    def test_old_screenshots_are_pruned(self, tmp_path, monkeypatch):
        # Screenshots must not accumulate forever on a phone.
        shots = tmp_path / "shots"
        shots.mkdir()
        monkeypatch.setattr(tools, "_SCREENSHOT_DIR", str(shots))
        monkeypatch.setattr(tools, "_SCREENSHOT_KEEP", 3)
        for i in range(6):
            path = shots / f"shot_{i}.jpg"
            path.write_bytes(b"\xff\xd8\xff")
            os.utime(path, (1000 + i, 1000 + i))

        tools._prune_screenshots()
        left = sorted(p.name for p in shots.iterdir())
        assert left == ["shot_3.jpg", "shot_4.jpg", "shot_5.jpg"]

    def test_pruning_is_a_no_op_below_the_limit(self, tmp_path, monkeypatch):
        shots = tmp_path / "shots"
        shots.mkdir()
        monkeypatch.setattr(tools, "_SCREENSHOT_DIR", str(shots))
        monkeypatch.setattr(tools, "_SCREENSHOT_KEEP", 10)
        (shots / "only.jpg").write_bytes(b"\xff\xd8\xff")
        tools._prune_screenshots()
        assert (shots / "only.jpg").exists()

    def test_pruning_a_missing_directory_does_not_raise(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(tools, "_SCREENSHOT_DIR",
                            str(tmp_path / "does-not-exist"))
        tools._prune_screenshots()  # must not raise


# ── 4. the extended NVIDIA image registry ────────────────────────────────────

class TestNvidiaImageRegistry:
    ALL_CATALOGUE_MODELS = [
        "black-forest-labs/flux-3-image-klein",
        "black-forest-labs/flux.2-klein-4b",
        "black-forest-labs/flux.1-dev",
        "black-forest-labs/flux.1-schnell",
        "black-forest-labs/flux.1-kontext-dev",
        "qwen/qwen-image",
        "qwen/qwen-image-edit",
        "nvidia/qwen-image-edit-nvpcb-ovsl2sl",
        "stabilityai/stable-diffusion-3.5-large",
    ]

    def test_every_catalogue_model_is_registered(self):
        names = [s["name"] for s in IMAGE_MODEL_SLOTS]
        for model in self.ALL_CATALOGUE_MODELS:
            assert model in names, model

    def test_the_self_hosted_models_are_flagged(self):
        # These 404 from ai.api.nvidia.com — they are self-hosted NIMs.
        for model in ("qwen/qwen-image", "qwen/qwen-image-edit",
                      "nvidia/qwen-image-edit-nvpcb-ovsl2sl",
                      "stabilityai/stable-diffusion-3.5-large"):
            slot = next(s for s in IMAGE_MODEL_SLOTS if s["name"] == model)
            assert slot.get("hosted_available") is False, model
            assert slot.get("hosted_note"), model

    def test_the_hosted_models_are_flagged(self):
        for model in ("black-forest-labs/flux-3-image-klein",
                      "black-forest-labs/flux.2-klein-4b",
                      "black-forest-labs/flux.1-dev",
                      "black-forest-labs/flux.1-schnell",
                      "black-forest-labs/flux.1-kontext-dev"):
            slot = next(s for s in IMAGE_MODEL_SLOTS if s["name"] == model)
            assert slot.get("hosted_available") is not False, model

    def test_every_model_has_its_own_size_grid(self):
        for slot in IMAGE_MODEL_SLOTS:
            if slot.get("provider_id") != "nvidia-images":
                continue
            enum = slot.get("size_enum")
            assert enum, slot["name"]
            assert 1024 in enum, slot["name"]
            assert all(v > 0 for v in enum), slot["name"]

    def test_the_flux1_grid_is_the_narrow_one(self):
        slot = next(s for s in IMAGE_MODEL_SLOTS
                    if s["name"] == "black-forest-labs/flux.1-dev")
        assert slot["size_enum"] == (768, 832, 896, 960, 1024, 1088,
                                     1152, 1216, 1280, 1344)

    def test_the_klein_grid_is_the_wide_one(self):
        slot = next(s for s in IMAGE_MODEL_SLOTS
                    if s["name"] == "black-forest-labs/flux.2-klein-4b")
        assert slot["size_enum"][0] == 512
        assert slot["size_enum"][-1] == 1568

    def test_the_image_editing_model_requires_an_image(self):
        slot = next(s for s in IMAGE_MODEL_SLOTS
                    if s["name"] == "black-forest-labs/flux.1-kontext-dev")
        assert slot.get("requires_image") is True


class TestNvidiaImageBody:
    def _slot(self, name):
        return next(s for s in IMAGE_MODEL_SLOTS if s["name"] == name)

    def test_the_size_is_snapped_onto_the_models_own_grid(self):
        slot = self._slot("black-forest-labs/flux.1-dev")
        body = tools._build_nvidia_image_body(slot, "x", width=1000, height=1000)
        assert body["width"] in slot["size_enum"]
        assert body["height"] in slot["size_enum"]

    def test_the_flux1_grid_rejects_the_klein_only_sizes(self):
        slot = self._slot("black-forest-labs/flux.1-dev")
        body = tools._build_nvidia_image_body(slot, "x", width=512, height=512)
        assert body["width"] == 768  # 512 is not in the FLUX.1 grid

    def test_the_klein_grid_accepts_512(self):
        slot = self._slot("black-forest-labs/flux.2-klein-4b")
        body = tools._build_nvidia_image_body(slot, "x", width=512, height=512)
        assert body["width"] == 512

    def test_the_pixel_budget_is_enforced(self):
        slot = self._slot("black-forest-labs/flux-3-image-klein")
        body = tools._build_nvidia_image_body(slot, "x", width=1568, height=1568)
        assert body["width"] * body["height"] <= 1062400

    def test_the_mode_key_is_never_sent(self):
        # flux.2-klein-4b and flux.1-kontext-dev reject it as an extra
        # field; flux-3-image-klein defaults to text-to-image anyway.
        for slot in IMAGE_MODEL_SLOTS:
            if slot.get("provider_id") != "nvidia-images":
                continue
            body = tools._build_nvidia_image_body(slot, "x")
            assert "mode" not in body, slot["name"]

    def test_negative_prompt_only_where_it_is_supported(self):
        supported = self._slot("black-forest-labs/flux-3-image-klein")
        body = tools._build_nvidia_image_body(supported, "x",
                                              negative_prompt="blurry")
        assert body["negative_prompt"] == "blurry"

        unsupported = self._slot("black-forest-labs/flux.2-klein-4b")
        body2 = tools._build_nvidia_image_body(unsupported, "x",
                                               negative_prompt="blurry")
        assert "negative_prompt" not in body2

    def test_cfg_scale_is_clamped_per_model(self):
        klein = self._slot("black-forest-labs/flux.2-klein-4b")
        body = tools._build_nvidia_image_body(klein, "x", cfg_scale=3.5)
        assert body["cfg_scale"] <= 1.0

        dev = self._slot("black-forest-labs/flux.1-dev")
        body2 = tools._build_nvidia_image_body(dev, "x", cfg_scale=1.0)
        assert body2["cfg_scale"] > 1.0

    def test_steps_are_clamped_per_model(self):
        klein = self._slot("black-forest-labs/flux.2-klein-4b")
        body = tools._build_nvidia_image_body(klein, "x", steps=30)
        assert body["steps"] <= 4


class TestNvidiaImageHealth:
    def test_a_failed_endpoint_is_skipped_afterwards(self):
        tools._NVIDIA_IMAGE_HEALTH.clear()
        assert tools._nvidia_image_is_healthy("black-forest-labs/flux.1-dev")
        tools._nvidia_image_mark_failure("black-forest-labs/flux.1-dev")
        assert not tools._nvidia_image_is_healthy("black-forest-labs/flux.1-dev")
        tools._nvidia_image_mark_success("black-forest-labs/flux.1-dev")
        assert tools._nvidia_image_is_healthy("black-forest-labs/flux.1-dev")
        tools._NVIDIA_IMAGE_HEALTH.clear()

    def test_the_cooldown_grows_with_repeated_failures(self):
        tools._NVIDIA_IMAGE_HEALTH.clear()
        name = "black-forest-labs/flux.1-schnell"
        tools._nvidia_image_mark_failure(name)
        first = tools._NVIDIA_IMAGE_HEALTH[name]["fails"]
        tools._nvidia_image_mark_failure(name)
        second = tools._NVIDIA_IMAGE_HEALTH[name]["fails"]
        assert second > first
        tools._NVIDIA_IMAGE_HEALTH.clear()

    def test_the_timeout_is_bounded(self):
        # A hung endpoint must not stall the whole rotation for minutes.
        assert tools._NVIDIA_IMAGE_TIMEOUT <= 90


# ── 5. the tool wiring ───────────────────────────────────────────────────────

class TestToolWiring:
    def test_the_browser_route_is_registered(self):
        assert "browser" in tools._SUB_TOOL_FUNCS
        assert tools._SUB_TOOL_FUNCS["browser"] is tools.browser

    def test_the_generated_image_is_attached_for_the_model(self):
        src = open(os.path.join(CORE, "tools.py"), encoding="utf-8").read()
        assert "_attach_generated_image" in src
        assert "queue_image_attachment" in src

    def test_the_generate_image_schema_lists_the_nvidia_models(self):
        entry = next(t for t in tools.TOOLS_DESCRIPTION
                     if t["function"]["name"] == "generate_image")
        description = entry["function"]["description"]
        assert "flux-3-image-klein" in description
        assert "flux.1-schnell" in description
        assert "flux.1-kontext-dev" in description
        assert "qwen/qwen-image" in description

    def test_the_generate_image_schema_has_the_new_arguments(self):
        entry = next(t for t in tools.TOOLS_DESCRIPTION
                     if t["function"]["name"] == "generate_image")
        props = entry["function"]["parameters"]["properties"]
        assert "negative_prompt" in props
        assert "screenshot" not in props  # that belongs to browser/browse_web

    def test_the_browse_web_schema_has_the_screenshot_argument(self):
        entry = next(t for t in tools.TOOLS_DESCRIPTION
                     if t["function"]["name"] == "browse_web")
        props = entry["function"]["parameters"]["properties"]
        assert "screenshot" in props
