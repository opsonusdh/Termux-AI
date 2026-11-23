"""Regression tests ensuring URLs, paths, slugs, and identifiers are not
falsely redacted as ``[REDACTED:key material]`` while real key blobs remain
redacted.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import confidential  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "https://artificialanalysis.ai/models/gemini-4-hydrogen",
        "https://blog.google/technology/ai/gemini-4-hydrogen-Model/",
        "https://www.minimax.io/news/minimax-announces-first-half-2026-financial-results",
        "https://search.brave.com/search?q=Gemini+4+Hydrogen+MiniMax+M3.1+Flash+GLM+5.2+Flash",
        "https://www.bing.com/search?q=site:artificialanalysis.ai+Gemini+4+OR+MiniMax+M3.1+OR+GLM+5.2",
        "workspace/.browser/session_49501_25712/Default/WebStorage/12345",
        "workspace/.browser/session_49501_25712/Default/Spellcheck Dictionary/cache/index-dir",
        "skills/imported/pptx/ooxml/schemas/ISO-IEC29500-4_2016/pml-presentation.xsd",
        "skill_registry.REGISTERED_IMPORTED_SKILLS",
        "ws://127.0.0.1:36893/devtools/browser/9e8f7a6b-5c4d-3e2f-1a0b-9c8d7e6f5a4b",
    ],
)
def test_non_secret_urls_paths_and_identifiers_are_not_redacted(text):
    assert confidential.redact_credentials(text) == text


@pytest.mark.parametrize(
    "blob",
    [
        "aB3+cD6/eF9+gH2/iJ5+kL8/mN1+oP4/qR7=sT0/uV2wX5yZ8=",
        "1a2b3c4d5e6f7890.AbCdEfGhIjKlMnOpQrStUvWxYz123456",
        "AbCdEfGhIjKlMnOpQrStUv/WxYz0123456789AbCd",
    ],
)
def test_unlabelled_key_material_is_still_redacted(blob):
    assert confidential.redact_credentials(blob) == "[REDACTED:key material]"


def test_remote_devtools_websocket_still_redacted():
    url = "ws://10.0.0.5:9222/devtools/browser/9e8f7a6b-5c4d-3e2f-1a0b-9c8d7e6f5a4b"
    assert "[REDACTED:key material]" in confidential.redact_credentials(url)
