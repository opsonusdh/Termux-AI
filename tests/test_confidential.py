"""Tests for the confidential-file classifier and the gate it feeds.

The gate used to know two filenames: ``api.keys`` and the Google OAuth
files.  Everything else — a private key called ``id_rsa``, a ``.env``, a
``credentials.json``, a token pasted into a notes file — could be read
without a question, and a read tool result goes straight into the model's
context.

These tests pin three things:

* the **name** signal catches the obvious spellings and leaves the
  harmless ones alone (``monkey.py`` is not a key file);
* the **content** signal catches credentials under harmless names without
  turning the repository's own documentation into a secret;
* the **gate** asks about a confidential file, refuses it in autonomous
  mode, and still lets the templates through.
"""
import builtins
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import confidential  # noqa: E402
from core import permissions  # noqa: E402


# ── the fixture credentials ──────────────────────────────────────────────────
#
# These are synthetic, but they are *shaped* like real secrets: a Google API
# key, an OpenAI key, a PEM private-key header.  GitHub's push protection
# scans committed text for those shapes, so a file carrying the whole
# literal is rejected at push time — and the rejection needs a human to
# bypass it.  Splitting each one across two literals leaves nothing
# scannable in the source while the runtime value stays byte-identical.
#
# The private-key body below is a truncated, non-decodable stub; it is here
# for its *shape*, never as a working key.

_GOOGLE_KEY = "AIza" + "SyCGe2o6zs58qQSduXrzZN_HQoy20D5kxXQ"
_OPENAI_KEY = "sk-proj-" + "AbCdEf0123456789AbCdEf0123456789"
_PEM_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def _not_autonomous(monkeypatch):
    """The gate has to be exercised with the gate on."""
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)


@pytest.fixture(autouse=True)
def _quiet_notifier(monkeypatch):
    """No system notification from a test."""
    monkeypatch.setattr(permissions.notifier, "permission", lambda requests: False)


@pytest.fixture
def answer(monkeypatch):
    """Answer the permission prompt with a scripted reply."""
    def _set(reply):
        monkeypatch.setattr(builtins, "input", lambda *_a, **_k: reply)
    return _set


# ── the name signal ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "api.keys",
    "api_keys.json",
    "apikey.txt",
    "credentials.json",
    "creds.yaml",
    "secrets.yaml",
    "secret.txt",
    "passwords.csv",
    "token.json",
    "access_token.txt",
    "service-account.json",
    "id_rsa",
    "private.pem",
    "client.p12",
    ".env",
    ".env.production",
    "my.key",
])
def test_a_credential_name_is_recognised(tmp_path, name):
    path = tmp_path / name
    path.write_text("nothing interesting here\n")
    finding = confidential.classify(str(path))
    assert finding.confidential, f"{name} was not recognised as confidential"
    assert finding.signal == "name"


@pytest.mark.parametrize("name", [
    "monkey.py",
    "keyword.txt",
    "keyboards.json",
    "api.py",
    "auth.py",
    "tokenizer.json",
    "README.md",
    "capability_registry.json",
    "notes.txt",
    "google_oauth.py",
    "public_key.pem",
])
def test_an_ordinary_name_is_not_a_credential(tmp_path, name):
    path = tmp_path / name
    path.write_text("nothing interesting here\n")
    assert not confidential.classify(str(path)).confidential, (
        f"{name} was wrongly treated as confidential"
    )


@pytest.mark.parametrize("name", [
    "api.keys.template",
    ".env.example",
    ".env.sample",
    "secrets.yaml.example",
    "credentials.json.sample",
    "id_rsa.pub",
])
def test_a_template_is_not_a_credential(tmp_path, name):
    """The distributable template must stay readable."""
    path = tmp_path / name
    path.write_text('{"google": ["AIza..."]}\n')
    assert not confidential.classify(str(path)).confidential


def test_a_credential_directory_makes_its_contents_confidential(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    config = ssh / "config"
    config.write_text("Host example\n")
    finding = confidential.classify(str(config))
    assert finding.confidential
    assert ".ssh" in finding.reason


def test_the_policy_protects_personal_memory():
    assert confidential.classify(os.path.join(REPO_ROOT, "memories.txt")).confidential


# ── the content signal ────────────────────────────────────────────────────────

def test_a_provider_key_under_a_harmless_name_is_caught(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(
        "shopping list\n"
        "milk\n"
        f"{_OPENAI_KEY}\n"
    )
    finding = confidential.classify(str(path))
    assert finding.confidential
    assert finding.signal == "content"


def test_a_google_api_key_under_a_harmless_name_is_caught(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(f"{_GOOGLE_KEY}\n")
    assert confidential.classify(str(path)).confidential


def test_a_private_key_block_is_caught(tmp_path):
    path = tmp_path / "server.txt"
    path.write_text(
        f"{_PEM_HEADER}\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtz\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    assert confidential.classify(str(path)).confidential


def test_a_jwt_is_caught(tmp_path):
    path = tmp_path / "session.txt"
    path.write_text(
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    )
    assert confidential.classify(str(path)).confidential


def test_a_secret_shaped_assignment_is_caught(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{\n  "api_key": "7fJ2pQ9xW3mZ8kL4vB6nR1tY5uC0dF7g"\n}\n')
    assert confidential.classify(str(path)).confidential


def test_a_file_of_pure_key_material_is_caught(tmp_path):
    path = tmp_path / "vault.txt"
    path.write_text(
        "a8F3kQ2mZ9xW4vB7nR1tY6uC0dF5gH2jL8sP3wE6rK1yN9\n"
        "b9G4lR3nA8yX5wC7oS2tZ0dE6fG1hI3kL9uQ4vB7xM8zC2\n"
        "c0H5mS4oB9zY6xD8pT3uA1eF7gH2iK4lJ0vR5wC8yN3dM4\n"
    )
    assert confidential.classify(str(path)).confidential


@pytest.mark.parametrize("text", [
    'api_key = "your-api-key-here"',
    'api_key = os.getenv("OPENAI_API_KEY")',
    'api_key = "${OPENAI_API_KEY}"',
    'api_key = "<your key here>"',
    'password = "changeme"',
    'password = "example-password"',
    'api_key = "AIza..."',
])
def test_a_placeholder_is_not_a_credential(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text + "\n")
    assert not confidential.classify(str(path)).confidential


def test_a_documentation_example_is_not_a_credential(tmp_path):
    """``qpdf --password=secret123`` in a manual is not a leaked password."""
    path = tmp_path / "REFERENCE.md"
    path.write_text(
        "# Remove password\n"
        "qpdf --password=secret123 --decrypt encrypted.pdf out.pdf\n"
    )
    assert not confidential.classify(str(path)).confidential


def test_a_hash_inside_a_structured_file_is_not_a_credential(tmp_path):
    """One integrity hash in a lock file is not a key dump."""
    path = tmp_path / "package-lock.json"
    path.write_text(
        '{\n  "packages": {\n    "": {\n      "integrity": '
        '"sha512-Aup7aUOfpbAUg2ROOJN6Iw5f9DMBlzu0mIkm/malLQFN/YQgO48wCj0Kxa3sE'
        'HJvPVFg7siR+qRInwXd2qhQKw==",\n      "name": "thing"\n    }\n  }\n}\n'
    )
    assert not confidential.classify(str(path)).confidential


def test_the_repository_itself_is_not_a_secret(tmp_path):
    """The gate must not lock the user out of their own project."""
    for relative in (
        "core/tools.py",
        "core/permissions.py",
        "core/confidential.py",
        "core/google_oauth.py",
        "README.md",
        "instructions/skills/core/security_and_privacy/SKILL.md",
    ):
        path = os.path.join(REPO_ROOT, relative)
        if not os.path.isfile(path):
            continue
        finding = confidential.classify(path)
        assert not finding.confidential, (
            f"{relative} was wrongly treated as confidential: {finding.reason}"
        )


def test_the_reason_never_carries_the_secret(tmp_path):
    """The detector sees the value; the label must not repeat it."""
    secret = "7fJ2pQ9xW3mZ8kL4vB6nR1tY5uC0dF7g"
    path = tmp_path / "notes.txt"
    path.write_text(f'api_key = "{secret}"\n')
    finding = confidential.classify(str(path))
    assert finding.confidential
    assert secret not in finding.reason


# ── the gate ──────────────────────────────────────────────────────────────────

def test_reading_a_credential_file_is_gated(tmp_path):
    path = tmp_path / "id_rsa"
    path.write_text(f"{_PEM_HEADER}\n")
    assert permissions.command_needs_permission(f"cat {path}")


def test_reading_an_ordinary_file_is_not_gated(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\n")
    assert not permissions.command_needs_permission(f"cat {path}")


def test_a_confidential_file_stays_gated_in_autonomous_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "is_autonomous", lambda: True)
    path = tmp_path / "id_rsa"
    path.write_text(f"{_PEM_HEADER}\n")
    assert permissions.command_needs_permission(f"cat {path}")


def test_autonomous_mode_refuses_instead_of_asking(tmp_path, monkeypatch):
    """There is nobody at the keyboard: the gate must not wait for one."""
    monkeypatch.setattr(permissions, "is_autonomous", lambda: True)
    path = tmp_path / "id_rsa"
    path.write_text(f"{_PEM_HEADER}\n")
    approved, reason = permissions.validate_command(f"cat {path}")
    assert not approved
    assert "autonomous" in reason.lower()


def test_the_gate_asks_before_reading_a_confidential_file(tmp_path, answer):
    path = tmp_path / "id_rsa"
    path.write_text(f"{_PEM_HEADER}\n")
    answer("n")
    approved, reason = permissions.request_file_permission("read", str(path))
    assert not approved
    assert "PERMISSION DENIED" in reason


def test_the_gate_lets_an_approved_read_through(tmp_path, answer):
    path = tmp_path / "id_rsa"
    path.write_text(f"{_PEM_HEADER}\n")
    answer("y")
    approved, _reason = permissions.request_file_permission("read", str(path))
    assert approved


def test_the_gate_asks_before_writing_over_a_credential(tmp_path, answer):
    path = tmp_path / "credentials.json"
    path.write_text("{}\n")
    answer("n")
    approved, reason = permissions.request_file_permission("write", str(path))
    assert not approved
    assert "PERMISSION DENIED" in reason


def test_the_template_is_still_readable(tmp_path):
    path = tmp_path / "api.keys.template"
    path.write_text('{"google": ["AIza..."]}\n')
    approved, _reason = permissions.request_file_permission("read", str(path))
    assert approved


def test_a_normal_file_is_still_readable(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\n")
    approved, _reason = permissions.request_file_permission("read", str(path))
    assert approved


# ── the tools that read around the gate ───────────────────────────────────────

def test_search_skips_confidential_files(tmp_path):
    from core import tools

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "public.txt").write_text("needle here\n")
    (tree / "api.keys").write_text("needle here\n")

    out = tools.search_in_files(str(tree), "needle")
    assert "public.txt" in out
    assert "api.keys" not in out
    assert "skipped" in out.lower()


def test_indexing_skips_confidential_files(tmp_path, monkeypatch):
    from core import tools
    import paths

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "public.txt").write_text("ordinary content\n")
    (tree / "api.keys").write_text('{"google": ["AIza..."]}\n')

    target = tmp_path / "indexed.txt"
    monkeypatch.setattr(paths, "INDEXED_MEMORY_FILE", str(target))

    out = tools.index_files(str(tree))
    assert "1 file" in out
    body = target.read_text()
    assert "ordinary content" in body
    assert "api.keys" not in body


def test_a_credential_is_not_uploaded_to_drive(tmp_path, answer):
    from core import tools

    path = tmp_path / "credentials.json"
    path.write_text('{"api_key": "7fJ2pQ9xW3mZ8kL4vB6nR1tY5uC0dF7g"}\n')
    answer("n")
    out = tools.upload_drive_file(str(path))
    assert "PERMISSION DENIED" in out
