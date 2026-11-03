"""
Tests for the image-generation subsystem (core/tools.py).

Covers the two behaviours that the live NVIDIA endpoints forced us to get
right, plus the surrounding contract:

  1. Format-aware saving — NVIDIA's FLUX endpoints return JPEG while Gemini
     returns PNG.  The saved file must carry the extension that matches its
     actual bytes, otherwise Android opens a ".png"-named JPEG with the wrong
     MIME type.
  2. Transport-failure handling — a hung endpoint is a property of the
     endpoint, not of the API key, so a read timeout must move on to the next
     model instead of re-trying the same dead endpoint with the next key.
  3. Content-filter reporting — when every model rejects a prompt on safety
     grounds the error must say so plainly.

No network access and no real API keys are used here.
"""
import base64
import os
import sys

import pytest
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO_ROOT, "core")
for _p in (REPO_ROOT, CORE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import tools  # noqa: E402
from core.models import IMAGE_MODEL_SLOTS  # noqa: E402


# Minimal real-format byte prefixes.  The detector only inspects magic bytes.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + b"\x00" * 64
)
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64

NVIDIA_SLOTS = [s for s in IMAGE_MODEL_SLOTS if s.get("api") == "nvidia-genai"]


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _artifact_payload(data: bytes, reason="SUCCESS", seed=1):
    return {
        "artifacts": [{
            "base64": base64.b64encode(data).decode(),
            "finishReason": reason,
            "seed": seed,
        }]
    }


@pytest.fixture
def nvidia_keys(monkeypatch):
    """Two NVIDIA keys, so key-rotation behaviour is observable."""
    monkeypatch.setattr("llm_client.API_KEYS", {"nvidia": ["key-a", "key-b"]})
    return ["key-a", "key-b"]


# ── 1. Format detection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("data,expected", [
    (JPEG_BYTES, ".jpg"),
    (PNG_BYTES, ".png"),
    (WEBP_BYTES, ".webp"),
    (b"not an image at all", ".png"),
    (b"", ".png"),
])
def test_detect_image_extension(data, expected):
    assert tools._detect_image_extension(data) == expected


def test_write_image_file_uses_matching_extension(tmp_path):
    """A JPEG payload must land in a .jpg file, not a .png one."""
    out = tools._write_image_file(str(tmp_path / "pic"), JPEG_BYTES)
    assert out.endswith(".jpg")
    assert os.path.exists(out)
    with open(out, "rb") as fh:
        assert fh.read() == JPEG_BYTES
    assert not os.path.exists(str(tmp_path / "pic.png"))


def test_write_image_file_png_keeps_png(tmp_path):
    out = tools._write_image_file(str(tmp_path / "pic"), PNG_BYTES)
    assert out.endswith(".png")
    assert os.path.exists(out)


# ── 2. NVIDIA request contract ────────────────────────────────────────────────

def test_nvidia_body_never_sends_mode():
    """`mode` 422s on klein-4b and 500s on flux.1-dev — it must never be sent."""
    for slot in NVIDIA_SLOTS:
        body = tools._build_nvidia_image_body(slot, "a prompt")
        assert "mode" not in body


def test_nvidia_body_clamps_to_model_envelope():
    klein = tools._image_slot_by_name("black-forest-labs/flux.2-klein-4b")
    body = tools._build_nvidia_image_body(klein, "x", cfg_scale=9, steps=99)
    assert body["cfg_scale"] == 1
    assert body["steps"] == 4

    dev = tools._image_slot_by_name("black-forest-labs/flux.1-dev")
    body = tools._build_nvidia_image_body(dev, "x", cfg_scale=1, steps=1)
    assert body["cfg_scale"] > 1
    assert body["steps"] == 5


def test_nvidia_body_respects_pixel_budget():
    for slot in NVIDIA_SLOTS:
        body = tools._build_nvidia_image_body(
            slot, "x", width=1344, height=1344)
        assert body["width"] * body["height"] <= 1062400


# ── 3. Saving NVIDIA output ───────────────────────────────────────────────────

def test_nvidia_saves_jpeg_with_jpg_extension(monkeypatch, nvidia_keys, tmp_path):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        seen["auth"] = (headers or {}).get("Authorization")
        return FakeResponse(200, _artifact_payload(JPEG_BYTES, seed=99))

    monkeypatch.setattr(tools.requests, "post", fake_post)
    out_base = str(tmp_path / "scene")
    message, errors = tools._nvidia_generate_image(
        "a prompt", NVIDIA_SLOTS[:1], out_base)

    assert message is not None and "Successfully generated" in message
    assert os.path.exists(out_base + ".jpg")
    assert not os.path.exists(out_base + ".png")
    assert seen["url"].endswith(
        "/genai/black-forest-labs/flux-3-image-klein")
    assert seen["auth"].startswith("Bearer ")
    assert seen["timeout"] == tools._NVIDIA_IMAGE_TIMEOUT


def test_generate_image_end_to_end_writes_jpeg(monkeypatch, nvidia_keys, tmp_path):
    """The public tool must produce a .jpg for JPEG provider output."""
    monkeypatch.setattr(
        tools.requests, "post",
        lambda *a, **k: FakeResponse(200, _artifact_payload(JPEG_BYTES)))
    monkeypatch.setattr(tools.paths, "WORKSPACE_DIR", str(tmp_path))

    result = tools.generate_image(
        "a prompt", provider="nvidia",
        model="black-forest-labs/flux.2-klein-4b",
        filename="unit_img",
    )
    assert "Successfully generated" in result
    written = os.path.join(str(tmp_path), "images", "unit_img.jpg")
    assert os.path.exists(written)
    with open(written, "rb") as fh:
        assert fh.read() == JPEG_BYTES


# ── 4. Transport failures must not re-try the same dead endpoint ─────────────

def test_transport_failure_moves_to_next_model_not_next_key(
        monkeypatch, nvidia_keys, tmp_path):
    """A hung endpoint is not a key problem: one attempt per model, not per key."""
    calls = []

    def boom(url, headers=None, json=None, timeout=None):
        calls.append(url)
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(tools.requests, "post", boom)
    message, errors = tools._nvidia_generate_image(
        "a prompt", NVIDIA_SLOTS, str(tmp_path / "scene"))

    # Not every catalogue entry is reachable from the hosted API: the
    # self-hosted NIMs 404 and the image-editing models need an input
    # image, so they are deliberately not attempted.
    attemptable = [s for s in NVIDIA_SLOTS
                   if s.get("hosted_available", True)
                   and not s.get("requires_image")]

    assert message is None
    assert len(calls) == len(attemptable), (
        f"expected one attempt per reachable model "
        f"({len(attemptable)}), got {len(calls)}")
    assert len(set(calls)) == len(attemptable)
    # Every model that was actually reached failed at the transport level.
    transport_errors = [e for e in errors if "request failed" in e]
    assert len(transport_errors) == len(attemptable), errors


def test_http_error_does_rotate_keys(monkeypatch, nvidia_keys, tmp_path):
    """An HTTP-level failure IS key-specific, so the next key must be tried."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((headers or {}).get("Authorization"))
        return FakeResponse(429, None, "rate limited")

    monkeypatch.setattr(tools.requests, "post", fake_post)
    message, errors = tools._nvidia_generate_image(
        "a prompt", NVIDIA_SLOTS[:1], str(tmp_path / "scene"))

    assert message is None
    assert len(calls) == 2, f"expected both keys to be tried, got {calls}"
    assert calls[0] != calls[1]
    assert any("HTTP 429" in e for e in errors)


# ── 5. Content-filter reporting ───────────────────────────────────────────────

def test_content_filtered_reports_clear_cause(monkeypatch, nvidia_keys, tmp_path):
    """When every model filters the prompt, the error must name the reason."""
    monkeypatch.setattr(
        tools.requests, "post",
        lambda *a, **k: FakeResponse(200, _artifact_payload(
            b"", reason="CONTENT_FILTERED")))

    message, errors = tools._nvidia_generate_image(
        "a prompt", NVIDIA_SLOTS, str(tmp_path / "scene"))

    assert message is None
    assert errors, "expected an error list"
    assert any("safety filter" in e for e in errors), errors
    assert any("CONTENT_FILTERED" in e for e in errors), errors


def test_content_filtered_not_raised_when_other_error_present(
        monkeypatch, nvidia_keys, tmp_path):
    """A mixed failure set must not be reported as a pure filter rejection."""
    def fake_post(url, headers=None, json=None, timeout=None):
        if "flux.1-dev" in url:
            return FakeResponse(500, None, "internal error")
        return FakeResponse(200, _artifact_payload(b"", reason="CONTENT_FILTERED"))

    monkeypatch.setattr(tools.requests, "post", fake_post)
    message, errors = tools._nvidia_generate_image(
        "a prompt", NVIDIA_SLOTS, str(tmp_path / "scene"))

    assert message is None
    assert not any("safety filter" in e for e in errors), errors
    assert any("HTTP 500" in e for e in errors), errors


# ── 6. Artifact extraction ────────────────────────────────────────────────────

def test_extract_artifacts_skips_failed_entries():
    payload = {"artifacts": [
        {"base64": "", "finishReason": "CONTENT_FILTERED"},
        {"base64": base64.b64encode(JPEG_BYTES).decode(),
         "finishReason": "SUCCESS", "seed": 12},
    ]}
    data, reason, seed = tools._extract_nvidia_artifacts(payload)
    assert data == JPEG_BYTES
    assert reason == "SUCCESS"
    assert seed == 12


def test_extract_artifacts_rejects_malformed_payload():
    data, _, _ = tools._extract_nvidia_artifacts("not a dict")
    assert data is None
    data, _, _ = tools._extract_nvidia_artifacts({"artifacts": []})
    assert data is None
