"""core/confidential.py — decide whether a file holds something secret.

The permission gate used to know about exactly two things: ``api.keys`` and
the Google OAuth files.  Everything else was fair game, so a private key
called ``id_rsa``, a ``.env`` file, a ``credentials.json`` or a token pasted
into a notes file could be read — and once it is in a tool result, it is in
the model's context and on its way to a provider.

Two independent signals decide the question.

**The name.**  ``api.keys``, ``credentials.json``, ``.env``, ``secrets.yaml``,
``id_rsa``, ``service-account.pem`` — the name is a declaration.  So is the
directory: anything inside ``.ssh/`` or a folder called ``credentials/``.

**The content.**  A harmless name can hide anything: ``notes.txt`` with a
provider token in it, ``config.json`` with a ``password`` field.  The content
is inspected for *credential-shaped* material — a private-key block, a
recognised provider token format, a JWT, an ``Authorization: Bearer`` header,
a secret-shaped name bound to a real-looking value, or a high-entropy blob
that is nothing but key material.

The content signal is deliberately **not** a keyword search for "secret" or
"key".  Half this repository talks about credentials — this file, the
security manual, the permission tests — and a keyword search would gate the
very documents that explain the gate.  What is detected is the *shape* of a
credential, not the *words* around it.

Two rules keep the detector honest:

* **Nothing here ever returns the matched value.**  A finding is a label and
  a position, never the secret itself.  The detector has to see the content
  to judge it; the content stays in this process.
* **A finding is a reason to ask, not a verdict.**  The caller decides what
  to do with it.  The permission gate asks the user.

The verdicts are cached by ``(path, size, mtime)``, because the gate asks
about the same files over and over and a content scan must not become the
slow part of a tool call.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

import paths


# ── the name signal ────────────────────────────────────────────────────────────

#: Tokens that mean "this file is a secret" on their own.
_NAME_STRONG = frozenset({
    "cred", "creds", "credential", "credentials",
    "secret", "secrets",
    "password", "passwords", "passwd", "passphrase", "passphrases",
    "token", "tokens",
    "apikey", "apikeys",
    "keystore", "keychain", "keyring",
})

#: Tokens that mean "this file is a secret" only next to a partner.
_NAME_WEAK = frozenset({"key", "keys", "api", "auth", "oauth"})

#: What turns a weak "key"/"keys" into a strong signal.
_KEY_PARTNERS = frozenset({
    "api", "secret", "private", "access", "signing", "encryption",
    "ssh", "tls", "ssl", "service", "account", "master", "auth",
    "lic", "license", "activation", "encrypt", "decrypt",
    "consumer", "client", "provider", "database",
})

#: What turns a weak "api" into a strong signal.
_API_PARTNERS = frozenset({
    "key", "keys", "secret", "token", "cred", "creds",
    "credential", "credentials", "password", "passwd", "pwd",
})

#: What turns a weak "auth" into a strong signal.
_AUTH_PARTNERS = frozenset({
    "token", "secret", "key", "keys", "cred", "creds",
    "credential", "credentials", "json", "file", "info",
})

#: What turns a weak "oauth" into a strong signal.  The module that *handles*
#: OAuth is not a credential; the file that *holds* one is.
_OAUTH_PARTNERS = frozenset({
    "json", "token", "secret", "key", "keys", "cred", "creds",
    "credential", "credentials", "info",
})

#: Exact file names that are credentials whatever they contain.
_SECRET_NAMES = frozenset({
    ".env", ".netrc", "_netrc", ".htpasswd", ".pgpass",
    ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})

#: Extensions that carry key material.
_SECRET_EXTENSIONS = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".kdbx",
    ".asc", ".gpg", ".pgp",
})

#: Suffixes that mark a file as a *template* — a sample, not a secret.
_SAFE_SUFFIXES = (".template", ".example", ".sample", ".dist", ".tpl", ".pub")

#: Directories whose contents are credentials whatever they are called.
_SECRET_DIRS = frozenset({
    ".ssh", ".gnupg", ".aws", ".gcloud", ".kube", ".docker",
    ".wwebjs_auth", ".secrets", ".credentials",
})

#: Directory names that are credentials on their own.  A weak word inside a
#: longer directory name is not enough — a folder called ``key_provider``
#: is a project — but a folder called ``keys`` is a key store.
_SECRET_DIR_NAMES = frozenset({"key", "keys"})

#: Files the security policy protects by name, independent of their content.
_POLICY_PROTECTED = {
    paths.MEMORY_FILE: "long-lived personal memory",
    paths.STATE_FILE: "persistent application state",
    paths.CHUNKS_FILE: "the full conversation transcript",
}


def _tokens(name: str) -> list[str]:
    """Split a file or directory name into lower-case word tokens.

    ``OPENAI_API_KEY`` → ``['openai', 'api', 'key']``; ``apiKey`` →
    ``['api', 'key']``; ``api.keys`` → ``['api', 'keys']``.  Splitting on
    word boundaries is what keeps ``monkey`` from being a ``key``.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or ""))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _name_verdict(tokens: list[str], allow_weak: bool = True) -> str | None:
    """The secret-ish word in *tokens*, if there is one.

    Strong words speak for themselves.  Weak words ("key", "api", "auth")
    need a partner: ``api_keys`` is a secret, ``keyboard`` and ``api.py``
    are not.

    *allow_weak* is turned off for directory names, where a weak word is
    far too easy to hit by accident — a folder called
    ``key_provider_app`` is a project, not a credential.
    """
    for token in tokens:
        if token in _NAME_STRONG:
            return token

    if not allow_weak:
        return None

    for weak in ("key", "keys", "api", "auth", "oauth"):
        if weak not in tokens:
            continue
        if weak in ("key", "keys"):
            partners = _KEY_PARTNERS
        elif weak == "api":
            partners = _API_PARTNERS
        elif weak == "oauth":
            partners = _OAUTH_PARTNERS
        else:
            partners = _AUTH_PARTNERS
        for token in tokens:
            if token != weak and token in partners:
                return f"{weak}+{token}"
    return None


def _name_reason(path: str) -> str | None:
    """Why *path*'s name says it holds a secret — or None."""
    base = os.path.basename(path)
    low = base.lower()
    if not base or low.endswith(_SAFE_SUFFIXES):
        return None

    tokens = _tokens(base)

    # A file that calls itself public is public.  The content check still
    # applies to it: a "public" key file holding a private key block is
    # still a private key.
    if "public" not in tokens:
        if low in _SECRET_NAMES or low.startswith(".env."):
            return f"the file name '{base}' is a known credential file"

        ext = os.path.splitext(low)[1]
        if ext in _SECRET_EXTENSIONS:
            return f"the file name '{base}' carries the key-file extension '{ext}'"

        hit = _name_verdict(tokens)
        if hit:
            return f"the file name '{base}' contains '{hit}'"

    # The directory can be the declaration: ~/.ssh/config, secrets/notes.txt.
    parent = os.path.basename(os.path.dirname(path))
    if parent:
        if parent.lower() in _SECRET_DIRS:
            return f"the file sits in the credential directory '{parent}/'"
        hit = _name_verdict(_tokens(parent), allow_weak=False)
        if hit:
            return f"the file sits in the directory '{parent}/' ('{hit}')"
        if parent.lower() in _SECRET_DIR_NAMES:
            return f"the file sits in the directory '{parent}/'"

    # The browser profiles the browse tool keeps carry cookies and session
    # tokens under a name that says nothing.
    if _in_browser_profile(path):
        return "it sits in a browser profile (cookies and session tokens)"
    return None


def _in_browser_profile(path: str) -> bool:
    """True when *path* is inside the browser profiles under ``workspace/``."""
    try:
        root = os.path.abspath(paths.WORKSPACE_DIR)
        target = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    marker = os.path.join(root, ".browser")
    return target.startswith(marker + os.sep)


# ── the content signal ─────────────────────────────────────────────────────────

#: How much of a file is inspected.  A credential is at the top of the file
#: in every realistic case; the tail of a 50 MB log is not where the key is.
_CONTENT_SCAN_BYTES = 256 * 1024

#: Files bigger than this are not scanned at all.
_CONTENT_MAX_FILE_BYTES = 8 * 1024 * 1024

#: Recognised credential shapes.  Each entry is (label, pattern).
_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a PEM private-key block",
     re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("an OpenAI/Anthropic-style API key",
     re.compile(r"\bsk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{20,}\b")),
    ("an OpenRouter API key",
     re.compile(r"\bsk-or-v1-[A-Za-z0-9]{20,}\b")),
    ("a Google API key",
     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Google OAuth client secret",
     re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b")),
    ("a Groq API key",
     re.compile(r"\bgsk_[A-Za-z0-9]{32,}\b")),
    ("an NVIDIA API key",
     re.compile(r"\bnvapi-[A-Za-z0-9_-]{24,}\b")),
    ("a Hugging Face token",
     re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("a GitHub token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|"
                r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("an AWS access key id",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token",
     re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("a Stripe key",
     re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("a SendGrid key",
     re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")),
    ("a Telegram bot token",
     re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("a Discord bot token",
     re.compile(r"\b[MNO][A-Za-z0-9_-]{23}\.[A-Za-z0-9_-]{6}\."
                r"[A-Za-z0-9_-]{27}\b")),
    ("a Twilio account SID",
     re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("a JSON Web Token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
                r"[A-Za-z0-9_-]{5,}\b")),
    ("a bearer token",
     re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?bearer\s+"
                r"[A-Za-z0-9._~+/-]{20,}")),
    ("credentials embedded in a URL",
     re.compile(r"\bhttps?://[^\s/:@]+:[^\s/@]{6,}@[^\s/]+")),
)

#: A secret-shaped *name* bound to a value.  The name may carry a prefix
#: ("OPENAI_API_KEY"), because that is how environment variables are written.
_SECRET_NAME_RE = (
    r"(?:[A-Za-z0-9_]+[_-])*"
    r"(?:api[_-]?keys?|api[_-]?secrets?|apikeys?|secret[_-]?keys?|secrets?"
    r"|access[_-]?tokens?|refresh[_-]?tokens?|auth[_-]?tokens?"
    r"|bearer[_-]?tokens?|client[_-]?secrets?|private[_-]?keys?"
    r"|encryption[_-]?keys?|signing[_-]?keys?|session[_-]?keys?"
    r"|passwords?|passwd|pwd|passphrases?|credentials?)"
)

_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9-])" + _SECRET_NAME_RE +
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9+/=_.\-~!@#$%^&*]{6,})"
)

#: Values that are obviously not a secret.
_PLACEHOLDER_MARKERS = (
    "example", "dummy", "fake", "placeholder", "changeme", "change_me",
    "your_", "your-", "xxxx", "todo", "fixme", "redacted", "notreal",
    "sample", "insert_", "replace", "test_key", "testkey", "demo_key",
    "demokey", "here",
)

#: Words that start a demonstration value ("secret123", "admin2024").
_DEMO_WORDS = (
    "secret", "password", "passwd", "admin", "test", "demo", "example",
    "sample", "dummy", "fake", "letmein", "qwerty", "abc", "my", "user",
    "guest", "login", "master", "change", "temp", "foo", "bar", "xxx",
)

#: A long random-looking run of characters — key material with no label.
_GIBBERISH_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")

#: A blob that says what it is.  An integrity hash in a lock file is random
#: and long, but it is not a credential.
_HASH_LABEL_RE = re.compile(
    r"^(?:sha1|sha256|sha384|sha512|md5|checksum|digest)[-:][A-Za-z0-9+/=_-]+$",
    re.IGNORECASE,
)

#: Shannon entropy, in bits per character, above which a run of characters is
#: more random than language.
_GIBBERISH_MIN_ENTROPY = 3.4

#: How much of a file has to be key material before the file *is* key
#: material.  A hash in a lock file is not a credential; a file that is
#: nothing but hashes is.
_GIBBERISH_SHARE = 0.35

#: Extensions whose files are plausible containers of key material.
_CONTAINER_EXTENSIONS = frozenset({
    ".env", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
    ".txt", ".keys", ".properties", ".dat", ".blob", ".pem", ".key",
})


def _is_placeholder(value: str) -> bool:
    """True when *value* is a stand-in rather than a real secret."""
    text = str(value or "").strip().strip("\"'")
    if len(text) < 6:
        return True
    low = text.lower()
    if low in ("none", "null", "true", "false", "undefined", "nil"):
        return True
    if any(marker in low for marker in _PLACEHOLDER_MARKERS):
        return True
    if text.startswith(("${", "{{", "%(", "<", "$(", "%", "$")):
        return True
    if any(ch in text for ch in "(){}[]"):
        return True
    # "AIza..." — the ellipsis is how a value is redacted in a template.
    if "..." in text or "…" in text:
        return True
    if re.fullmatch(r"[0-9.]+", text):
        return True
    # A constant or an attribute reference, not a literal.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) and (
        text.isupper() or "_" in text
    ):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+", text):
        return True
    # Dictionary words, separators and digits only: "access-1",
    # "mypassword", "secret123".  A random key mixes case or symbols; a
    # human-chosen weak password is exactly this shape, and it is also what
    # every test fixture and every documentation example looks like.
    if re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", low):
        return True
    # A demonstration value: a demo word followed by digits.
    for word in _DEMO_WORDS:
        if low.startswith(word):
            rest = low[len(word):]
            if rest and re.fullmatch(r"[0-9!@#$%^&*_.-]+", rest):
                return True
    return False


def _entropy(text: str) -> float:
    """Shannon entropy of *text*, in bits per character."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _looks_like_key_material(token: str) -> bool:
    """True when *token* is long, random and mixed enough to be a key."""
    if len(token) < 40:
        return False
    classes = 0
    if any(c.islower() for c in token):
        classes += 1
    if any(c.isupper() for c in token):
        classes += 1
    if any(c.isdigit() for c in token):
        classes += 1
    if any(not c.isalnum() for c in token):
        classes += 1
    if classes < 3:
        return False
    return _entropy(token) >= _GIBBERISH_MIN_ENTROPY


def _content_reason(path: str) -> str | None:
    """Why *path*'s content looks like a credential — or None.

    The file is read here, in this process, and only a label leaves this
    function.  The value never travels any further.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0 or size > _CONTENT_MAX_FILE_BYTES:
        return None

    try:
        with open(path, "rb") as handle:
            raw = handle.read(_CONTENT_SCAN_BYTES)
    except OSError:
        return None

    # Binary files are not text files with secrets in them; they are images,
    # archives and databases.
    if b"\x00" in raw[:4096]:
        return None

    text = raw.decode("utf-8", errors="replace")

    for label, pattern in _CONTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            return f"its content holds {label} (line {line})"

    match = _ASSIGNMENT_RE.search(text)
    if match and not _is_placeholder(match.group(1)):
        line = text.count("\n", 0, match.start()) + 1
        return (f"its content binds a secret-shaped name to a real value "
                f"(line {line})")

    # Key material with no label at all.  The test is not "does this file
    # contain a long random-looking run" — every lock file and every browser
    # profile contains one — but "is this file *made of* key material".
    extension = os.path.splitext(path.lower())[1]
    container = (extension in _CONTAINER_EXTENSIONS or not extension)
    if container and size <= 128 * 1024:
        share, blobs = _blob_share(text)
        if blobs and share >= _GIBBERISH_SHARE:
            return (f"its content is mostly high-entropy blobs — key material "
                    f"with no label ({blobs} blob(s), {share:.0%} of the file)")
    return None


def _blob_share(text: str) -> tuple[float, int]:
    """How much of *text* is made of key-material-sized runs.

    Returns ``(share, count)``.  A lock file with a handful of hashes has a
    low share; a file that is nothing but keys has a high one.
    """
    blobs = 0
    blob_chars = 0
    for match in _GIBBERISH_RE.finditer(text):
        token = match.group(0)
        if _HASH_LABEL_RE.match(token):
            continue
        if not _looks_like_key_material(token):
            continue
        blobs += 1
        blob_chars += len(token)
    total = sum(1 for char in text if not char.isspace())
    if not total:
        return 0.0, 0
    return blob_chars / total, blobs


# ── the verdict ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Finding:
    """What the classifier found.  Never carries the secret itself."""

    confidential: bool
    reason: str
    signal: str       # "policy" | "name" | "content"

    def __bool__(self) -> bool:
        return self.confidential


_NOTHING = Finding(False, "", "")

#: Cache of verdicts, keyed by (path, size, mtime).  The gate asks about the
#: same files over and over; the content scan must not be the slow part.
_CACHE: dict[tuple[str, int, float], Finding] = {}
_CACHE_LIMIT = 512


def _stamp(path: str) -> tuple[str, int, float]:
    try:
        info = os.stat(path)
        return (path, info.st_size, info.st_mtime)
    except OSError:
        return (path, -1, 0.0)


def _remember(key: tuple[str, int, float], finding: Finding) -> Finding:
    if len(_CACHE) >= _CACHE_LIMIT:
        for stale in list(_CACHE)[:_CACHE_LIMIT // 2]:
            _CACHE.pop(stale, None)
    _CACHE[key] = finding
    return finding


def _policy_reason(path: str) -> str | None:
    """The security policy's own list of protected files."""
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = os.path.abspath(path)
    for protected, what in _POLICY_PROTECTED.items():
        try:
            if os.path.realpath(protected) == resolved:
                return f"it is {what}, which the security policy protects"
        except OSError:
            continue
    return None


def classify(path: str, *, content: bool = True, base: str | None = None) -> Finding:
    """Decide whether *path* is confidential.

    *content* may be turned off for callers that only care about the name
    (a directory listing, a command that never opens the file).

    *base* is the directory a relative path is resolved against — the
    project root by default, because that is the working directory every
    shell command runs in.
    """
    raw = str(path or "").strip()
    if not raw:
        return _NOTHING

    expanded = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(expanded):
        expanded = os.path.join(base or paths.ROOT, expanded)
    try:
        absolute = os.path.abspath(expanded)
    except (OSError, ValueError):
        return _NOTHING

    reason = _policy_reason(absolute)
    if reason:
        return Finding(True, reason, "policy")

    reason = _name_reason(absolute)
    if reason:
        return Finding(True, reason, "name")

    if not content or not os.path.isfile(absolute):
        return _NOTHING

    key = _stamp(absolute)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    reason = _content_reason(absolute)
    finding = Finding(bool(reason), reason or "", "content" if reason else "")
    return _remember(key, finding)


def is_confidential(path: str, *, content: bool = True) -> bool:
    """True when *path* is a confidential file."""
    return classify(path, content=content).confidential


def reason(path: str, *, content: bool = True) -> str:
    """Why *path* is confidential — an empty string when it is not."""
    return classify(path, content=content).reason


def clear_cache() -> None:
    """Forget every cached verdict (used by the tests)."""
    _CACHE.clear()
