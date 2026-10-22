"""
core/shell_parse.py — structural shell parsing for the permission gate.

Why this module exists
----------------------
``core.permissions`` used to split a command into "segments" by cutting it at
every ``;``, ``&&``, ``||`` and newline.  That is fine for a flat chain, but a
*compound* command contains semicolons of its own, so the cut landed in the
middle of a block and the segment's first word was a shell keyword instead of
the command that actually runs::

    for i in 1 2 3; do rm -rf $HOME/x; done
                     ^^^^^^^^^^^^^^^ cut here -> the segment starts with "do"

    if true; then rm -rf /tmp/y; fi
                 ^^^^^^^^^^^^^ cut here -> the segment starts with "then"

The destructive command was therefore never inspected, and the whole block
slipped through the permission gate.

What this module does
---------------------
It parses a command *structurally* and returns the source text of every
**simple command** it contains — including the ones nested inside ``for``,
``while``, ``until``, ``if``, ``case``, ``{ }`` groups, subshells, function
bodies and command substitutions (``$( ... )`` and backticks).  The permission
gate can then inspect each command on its own, using the existing
per-command logic, instead of guessing from a keyword.

The parser is deliberately forgiving: a construct it does not understand is
skipped rather than aborting the parse, and any internal failure falls back to
the old naive splitting, so the gate can never end up *less* strict than it
was before.
"""

from __future__ import annotations

from dataclasses import dataclass, field


__all__ = ["Token", "simple_commands", "tokenize"]


# ── Vocabulary ───────────────────────────────────────────────────────────────

#: Keywords that open a block whose body is a command list.
_BLOCK_OPENERS = ("if", "elif", "else", "while", "until", "for", "select", "case")

#: Keywords that only ever appear in command position and never start a
#: command.  They are skipped when they turn up as a stray.
_STRUCTURAL_KEYWORDS = frozenset({
    "do", "done", "then", "fi", "else", "elif", "esac", "in", "{", "}",
    "function", "!", "time", "select", "coproc",
})

#: Redirection operators.  A redirection never ends a command — the command
#: continues with the redirection's target.
_REDIRECT_OPS = frozenset({
    ">", ">>", "<", "<<", "<<-", "<<<", ">&", "<&", "&>", "&>>", ">|",
})

#: Operators that terminate a command.
_TERMINATORS = frozenset({
    ";", "&&", "||", "|", "|&", "&", "\n", ";;", ";&", ";;&",
})

#: All operators, longest first so that the tokenizer always matches the
#: longest one (``&&`` before ``&``, ``&>>`` before ``&>`` before ``&``).
_OPERATORS = (
    ";;&", ";&", ";;",
    "&>>", "&>", "<<-", "<<<", "<<", ">>", ">|", ">&", "<&",
    "&&", "||", "|&",
    ">", "<", "&", "|", ";", "(", ")",
)

#: Characters that end a bare word.
_WORD_BREAK = set(" \t\n;&|<>()")


# ── Tokens ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Token:
    """One lexical element of a shell command.

    ``kind`` is ``"word"`` (an operand — quotes already removed) or ``"op"``
    (an operator).  ``start``/``end`` are offsets into the original source, so
    the exact original text of a command can be recovered.
    """

    kind: str
    text: str
    start: int
    end: int


# ── Tokenizer ────────────────────────────────────────────────────────────────

def _delimiter_maps(text: str) -> tuple[dict[int, int], dict[int, int]]:
    """Map every ``(`` to its matching ``)`` and every ``{`` to its ``}``.

    One quote-aware pass over the whole text.  Doing it up front matters:
    matching a delimiter by scanning forward from every opening point is
    quadratic, and a command with many nested substitutions would then make
    the permission gate quadratic in the length of the command.
    """
    parens: dict[int, int] = {}
    braces: dict[int, int] = {}
    paren_stack: list[int] = []
    brace_stack: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if quote == '"' and text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "(":
            paren_stack.append(i)
        elif ch == ")":
            if paren_stack:
                parens[paren_stack.pop()] = i
        elif ch == "{":
            brace_stack.append(i)
        elif ch == "}":
            if brace_stack:
                braces[brace_stack.pop()] = i
        i += 1
    return parens, braces


def _match_paren(text: str, start: int, parens: dict[int, int] | None = None) -> int:
    """Return the index just past the ``)`` matching the ``(`` at *start*."""
    if parens is not None:
        close = parens.get(start)
        if close is not None:
            return close + 1
        return len(text)

    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if quote == '"' and text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _match_brace(text: str, start: int, braces: dict[int, int] | None = None) -> int:
    """Return the index just past the ``}`` matching the ``{`` at *start*."""
    if braces is not None:
        close = braces.get(start)
        if close is not None:
            return close + 1
        return len(text)

    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if quote == '"' and text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def tokenize(text: str) -> tuple[list[Token], list[str]]:
    """Split *text* into tokens.

    Returns ``(tokens, substitutions)``.  *substitutions* holds the body of
    every ``$( ... )`` and backtick substitution found — those bodies are
    executed by the shell, so they are parsed separately.
    """
    tokens: list[Token] = []
    substitutions: list[str] = []
    parens, braces = _delimiter_maps(text)
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # -- whitespace ------------------------------------------------
        if ch in " \t":
            i += 1
            continue

        # -- newline ----------------------------------------------------
        if ch == "\n":
            tokens.append(Token("op", "\n", i, i + 1))
            i += 1
            continue

        # -- backslash escape -------------------------------------------
        if ch == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            value = text[i + 1] if i + 1 < n else "\\"
            tokens.append(Token("word", value, i, i + 2))
            i += 2
            continue

        # -- single quotes ----------------------------------------------
        if ch == "'":
            end = text.find("'", i + 1)
            end = n if end == -1 else end
            tokens.append(Token("word", text[i + 1:end], i, min(end + 1, n)))
            i = end + 1
            continue

        # -- double quotes ----------------------------------------------
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    if nxt in '"\\$`':
                        buf.append(nxt)
                        j += 2
                        continue
                    buf.append(text[j])
                    j += 1
                    continue
                buf.append(text[j])
                j += 1
            tokens.append(Token("word", "".join(buf), i, min(j + 1, n)))
            i = j + 1
            continue

        # -- $( ... ) command substitution --------------------------------
        if ch == "$" and text.startswith("$(", i):
            end = _match_paren(text, i + 1, parens)
            inner = text[i + 2:end - 1] if end - 1 > i + 2 else ""
            substitutions.append(inner)
            tokens.append(Token("word", text[i:end], i, end))
            i = end
            continue

        # -- ${ ... } parameter expansion ---------------------------------
        if ch == "$" and text.startswith("${", i):
            end = _match_brace(text, i + 1, braces)
            tokens.append(Token("word", text[i:end], i, end))
            i = end
            continue

        # -- $(( ... )) arithmetic ---------------------------------------
        if ch == "$" and text.startswith("$(", i) is False and text.startswith("((", i):
            end = text.find("))", i + 2)
            end = n if end == -1 else end + 2
            tokens.append(Token("word", text[i:end], i, end))
            i = end
            continue

        # -- backtick substitution ---------------------------------------
        if ch == "`":
            end = text.find("`", i + 1)
            if end == -1:
                tokens.append(Token("word", text[i:], i, n))
                i = n
                continue
            substitutions.append(text[i + 1:end])
            tokens.append(Token("word", text[i:end + 1], i, end + 1))
            i = end + 1
            continue

        # -- (( ... )) arithmetic ----------------------------------------
        if text.startswith("((", i):
            end = text.find("))", i + 2)
            end = n if end == -1 else end + 2
            tokens.append(Token("word", text[i:end], i, end))
            i = end
            continue

        # -- operators ----------------------------------------------------
        for op in _OPERATORS:
            if text.startswith(op, i):
                tokens.append(Token("op", op, i, i + len(op)))
                i += len(op)
                break
        else:
            # -- bare word -----------------------------------------------
            j = i
            while j < n and text[j] not in _WORD_BREAK:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            if j == i:
                # A character we do not understand — consume it so the
                # tokenizer can never loop forever.
                tokens.append(Token("word", text[i], i, i + 1))
                j = i + 1
            tokens.append(Token("word", text[i:j], i, j))
            i = j

    return tokens, substitutions


# ── Parser ───────────────────────────────────────────────────────────────────

@dataclass
class _Parser:
    text: str
    tokens: list[Token] = field(default_factory=list)
    pos: int = 0
    commands: list[str] = field(default_factory=list)

    # -- helpers -------------------------------------------------------

    def _peek(self) -> Token | None:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _expect_word(self, word: str) -> None:
        tok = self._peek()
        if tok is not None and tok.kind == "word" and tok.text == word:
            self.pos += 1

    # -- list parsing ---------------------------------------------------

    def _parse_list(self, stop: set[str], stop_at_sep: bool = False) -> None:
        """Parse simple commands until one of *stop* is reached.

        *stop* holds keywords that end the list; they are **not** consumed, so
        the caller can decide what to do with them.  With *stop_at_sep* the
        list also ends at a ``;`` or newline (used for the condition of an
        ``if``/``while``).
        """
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]

            if tok.kind == "op":
                if tok.text == "(":
                    # Subshell — its contents are commands too.
                    self.pos += 1
                    self._parse_list(set())
                    continue
                if tok.text == ")":
                    self.pos += 1
                    return
                if tok.text in (";;", ";&", ";;&"):
                    # End of a ``case`` arm.
                    return
                if tok.text in _REDIRECT_OPS:
                    # A redirection that is not attached to a command (e.g.
                    # the ``< file`` of a ``while`` loop).  Skip it *and* its
                    # target, so the target is not mistaken for a command.
                    self.pos += 1
                    nxt = self._peek()
                    if nxt is not None and nxt.kind == "word":
                        self.pos += 1
                    continue
                if stop_at_sep and tok.text in (";", "\n"):
                    self.pos += 1
                    return
                self.pos += 1
                continue

            word = tok.text

            if word in stop:
                return

            if word == "if":
                self.pos += 1
                self._parse_list({"then"}, stop_at_sep=True)
                self._expect_word("then")
                self._parse_list({"fi", "elif", "else"})
                continue

            if word == "elif":
                self.pos += 1
                self._parse_list({"then"}, stop_at_sep=True)
                self._expect_word("then")
                self._parse_list({"fi", "elif", "else"})
                continue

            if word == "else":
                self.pos += 1
                self._parse_list({"fi"})
                continue

            if word in ("while", "until"):
                self.pos += 1
                self._parse_list({"do"}, stop_at_sep=True)
                self._expect_word("do")
                self._parse_list({"done"})
                continue

            if word in ("for", "select"):
                self.pos += 1
                self._skip_for_header()
                self._expect_word("do")
                self._parse_list({"done"})
                continue

            if word == "case":
                self.pos += 1
                self._parse_case()
                continue

            if word == "do":
                self.pos += 1
                self._parse_list({"done"})
                continue

            if word == "then":
                self.pos += 1
                self._parse_list({"fi", "elif", "else"})
                continue

            if word == "{":
                # Brace group.
                self.pos += 1
                self._parse_list({"}"})
                self._expect_word("}")
                continue

            if word in ("!", "time", "function", "in", "coproc"):
                # A prefix: the real command follows.
                self.pos += 1
                continue

            if word in ("done", "fi", "esac", "}"):
                # A stray closer — skip it and carry on.
                self.pos += 1
                continue

            self._parse_simple_command()

    def _skip_for_header(self) -> None:
        """Skip the header of a ``for``/``select`` loop."""
        tok = self._peek()
        if tok is None:
            return
        if tok.kind == "word":
            # Either the loop variable or a whole `(( ... ))` expression.
            self.pos += 1

        tok = self._peek()
        if tok is not None and tok.kind == "word" and tok.text == "in":
            self.pos += 1
            # The word list runs until `;`, a newline or `do`.
            while self.pos < len(self.tokens):
                t = self.tokens[self.pos]
                if t.kind == "op":
                    if t.text in (";", "\n"):
                        self.pos += 1
                    break
                if t.kind == "word" and t.text == "do":
                    break
                self.pos += 1
            return

        # No `in` — the header ends at the separator.
        tok = self._peek()
        if tok is not None and tok.kind == "op" and tok.text in (";", "\n"):
            self.pos += 1

    def _parse_case(self) -> None:
        """Parse a ``case`` statement."""
        tok = self._peek()
        if tok is not None and tok.kind == "word":
            self.pos += 1  # the subject

        tok = self._peek()
        if tok is not None and tok.kind == "word" and tok.text == "in":
            self.pos += 1

        while self.pos < len(self.tokens):
            # The pattern list ends at `)`.
            closed = False
            while self.pos < len(self.tokens):
                t = self.tokens[self.pos]
                if t.kind == "op" and t.text == ")":
                    self.pos += 1
                    closed = True
                    break
                if t.kind == "word" and t.text == "esac":
                    self.pos += 1
                    return
                self.pos += 1
            if not closed:
                return

            # The arm's command list.
            self._parse_list({"esac"})

            tok = self._peek()
            if tok is None:
                return
            if tok.kind == "op" and tok.text in (";;", ";&", ";;&"):
                self.pos += 1
                continue
            if tok.kind == "word" and tok.text == "esac":
                self.pos += 1
            return

    def _parse_simple_command(self) -> None:
        """Parse one simple command and record its source text."""
        first = self.tokens[self.pos]
        start = first.start
        end = first.end
        self.pos += 1

        while self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            if t.kind == "op":
                if t.text in _REDIRECT_OPS:
                    # A redirection does not end the command; its target is
                    # the next word.
                    end = t.end
                    self.pos += 1
                    nxt = self._peek()
                    if nxt is not None and nxt.kind == "word":
                        end = nxt.end
                        self.pos += 1
                    continue
                break
            end = t.end
            self.pos += 1

        text = self.text[start:end].strip()
        if text:
            self.commands.append(text)


# ── Public API ───────────────────────────────────────────────────────────────

def _naive_split(text: str) -> list[str]:
    """The old, non-structural split — used as a safety net."""
    parts: list[str] = []
    buf: list[str] = []
    quote = None
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            escape = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            buf.append(ch)
            quote = ch
            i += 1
            continue
        if ch in ("\n", ";"):
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 1
            continue
        if ch == "&" and i + 1 < len(text) and text[i + 1] == "&":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 2
            continue
        if ch == "|" and i + 1 < len(text) and text[i + 1] == "|":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 2
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


#: How deep command substitutions may be nested before the parser stops
#: descending.  Every level re-parses the text inside it, so without a limit a
#: deeply nested command would make the permission gate quadratic in the
#: length of the command.  Past the limit the text is still split, only
#: without the structural analysis.
MAX_SUBSTITUTION_DEPTH = 16


def simple_commands(text: str, _depth: int = 0) -> list[str]:
    """Return the source text of every simple command in *text*.

    The list is ordered by position.  Commands nested inside compound blocks
    (``for``/``while``/``until``/``if``/``case``/``{ }``/subshells) and inside
    command substitutions are included, because the shell executes them too.

    On an internal error the old naive split is returned, so the result is
    never *less* strict than a plain textual split.
    """
    if not text or not text.strip():
        return []

    try:
        tokens, substitutions = tokenize(text)
        parser = _Parser(text=text, tokens=tokens)
        parser._parse_list(set())

        found = list(parser.commands)

        # Command substitutions execute their body, so parse those as well.
        for body in substitutions:
            if body and body.strip():
                if _depth < MAX_SUBSTITUTION_DEPTH:
                    found.extend(simple_commands(body, _depth + 1))
                else:
                    found.extend(_naive_split(body))

        if found:
            return found
        return _naive_split(text)
    except Exception:
        return _naive_split(text)
