"""core/tool_pipeline.py — the shared vocabulary of a tool call.

This module owns the two facts every tool call has to agree on:

* **is this result a failure?**  The runtime used to answer that question
  by sniffing the first characters of the result string, inline, in the
  middle of the tool loop — and the sniffing was done in one place only by
  accident.  A tool that legitimately returns data starting with
  ``[ERROR]`` was counted as a failure; a tool that fails with a message
  that does not start with one of the known markers was not counted at all.
  The check now lives here, in one tested function.
* **is this the same call as that one?**  The old signature was the raw
  argument text, so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` were
  different calls and the loop guard never noticed the repetition.  The
  signature is now the canonical JSON of the arguments, keys sorted.

The rest of the pipeline — admission, approval, guards, scopes — is a later
step.  What is here now is the part every later step will use anyway.
"""
from __future__ import annotations

import json

#: The prefixes a tool result may start with when it reports a failure,
#: mapped to the kind of failure.  The order matters: the first match
#: wins, and ``[TOOL ERROR]`` has to be checked before ``[ERROR]`` would
#: ever match it (it does not — the strings differ — but the list is kept
#: in one place so the next marker has an obvious home).
ERROR_PREFIXES: dict[str, str] = {
    "[ERROR]": "error",
    "[TOOL ERROR]": "tool_error",
    "[PERMISSION DENIED]": "permission_denied",
    "[TIMEOUT]": "timeout",
}


class ToolError:
    """An explicit failure a route can return instead of a magic string.

    The routes still return plain strings — the contract is load-bearing
    and several tests depend on it — so this class is opt-in and unused
    for now.  It exists so a route can say *what kind* of failure it was
    instead of having the failure guessed from a prefix.
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return self.message


def looks_like_error(content) -> str | None:
    """The kind of failure *content* reports, or None when it is not one.

    The check is deliberately strict: the marker has to be the very first
    thing in the result.  A tool that returns a paragraph *containing*
    "[ERROR]" is not a failure, and neither is a result that merely ends
    with one.
    """
    if isinstance(content, ToolError):
        return content.kind
    if not isinstance(content, str):
        return None
    for prefix, kind in ERROR_PREFIXES.items():
        if content.startswith(prefix):
            return kind
    return None


def normalize_call_signature(tool_name: str, args) -> str:
    """An order-independent signature for one tool call.

    ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` produce the same
    signature, so the loop guard sees the repetition it is supposed to
    see.  Arguments that are not valid JSON are kept verbatim: two
    malformed calls are still two calls that should be compared.
    """
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except (TypeError, ValueError):
            args = {"__raw_args__": args}
    if not isinstance(args, dict):
        args = {"__args__": args}
    try:
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(sorted(args.items(), key=lambda item: str(item[0])))
    return f"{tool_name}:{payload}"
