"""Turn a web page's DOM into Markdown that a model can actually read.

The browser tools used to hand the model the page's ``innerText``: one long
run of lines with no headings, no lists, no links and no idea of which text
belonged to which part of the page.  Reading that is slow, and answering
"what was in the weather box?" from it is guesswork.

This module converts the *DOM* into Markdown instead: headings become
headings, lists become lists, links stay clickable, tables stay tables, and
the boilerplate a reader never sees (navigation bars, cookie banners, ad
slots, hidden panels) is dropped before the conversion starts.

It also renders a single element's subtree as an outline.  That is what the
``search`` argument of the browser tools uses: hand it a class name, an id
or a tag name and it reports that element together with everything inside
it -- "temperature: 20 C", "sky state: Clear", "AQI: 42" -- as Markdown.

Nothing here needs a browser: the input is HTML, the output is Markdown, so
the whole converter is unit-testable offline.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


# ── what is thrown away before the conversion starts ──────────────────────────

# Tags whose *content* is never readable text.  ``input``/``textarea``/
# ``select`` are deliberately absent: a form control's current value is
# information the model asked for.
DROP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "applet", "map", "area", "audio", "video", "source",
    "track", "param", "link", "meta", "base", "head", "title",
}

# Structural furniture that a reader of the page never really looks at.
BOILERPLATE_TAGS = {"nav", "footer", "aside"}

# ARIA roles that mark the same kind of furniture.
BOILERPLATE_ROLES = {"navigation", "banner", "contentinfo", "complementary"}

# Class/id fragments that mark noise.  The boundaries matter: "downloads"
# contains "ad" but must not be mistaken for an advertisement.
_NOISE_RE = re.compile(
    r"(^|[-_ ])(ads?|advert\w*|cookie\w*|consent|gdpr|newsletter|"
    r"subscribe|social|share|sharing|breadcrumb\w*|skip[-_]?link|popup|"
    r"modal|overlay|paywall|promo|sponsored|tag[-_]?cloud|disqus|navbar|"
    r"masthead|site[-_]?header|site[-_]?footer)([-_ ]|$)",
    re.IGNORECASE,
)

_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(\.0+)?\b",
    re.IGNORECASE,
)

# Tags that break the text into blocks.  Everything else is inline.
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "details", "dialog",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li",
    "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "ul",
}

# Containers that hold content without saying anything about its shape.
GENERIC_CONTAINER_TAGS = {
    "div", "section", "article", "main", "span", "center", "font", "body",
    "fieldset", "figure", "details", "summary",
}

# Children that already carry their own structure; a container holding one
# of them is not a box of loose values.
STRUCTURAL_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "pre",
    "blockquote", "dl", "hr", "form", "article", "section",
}

# Tags the ``search`` argument understands as "a tag name".
KNOWN_TAGS = {
    "a", "abbr", "article", "aside", "b", "button", "canvas", "caption",
    "code", "dd", "div", "dl", "dt", "em", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "html", "i", "iframe", "img", "input", "label", "li",
    "main", "nav", "ol", "option", "p", "pre", "section", "select", "span",
    "strong", "table", "tbody", "td", "textarea", "tfoot", "th", "thead",
    "tr", "ul", "video",
}

# Class fragments that mark an element as the *label* of the value next to
# it, or as the value itself.
LABEL_CLASS_TOKENS = {
    "label", "labels", "title", "name", "key", "caption", "term", "heading",
    "labeltext", "fieldname", "fieldlabel", "formlabel", "rowlabel",
    "titletext", "headline",
}

VALUE_CLASS_TOKENS = {
    "value", "val", "data", "content", "result", "answer", "fieldvalue",
    "cellvalue", "valuetext", "reading", "measurement",
}

# Class tokens that say nothing about *what* the element holds.
UNINFORMATIVE_CLASS_TOKENS = {
    "active", "selected", "hidden", "first", "last", "odd", "even", "row",
    "col", "column", "item", "items", "list", "box", "clearfix", "container",
    "wrapper", "inner", "outer", "content", "text", "main", "body", "page",
    "true", "false", "default", "primary", "secondary", "visible", "show",
    "hide", "open", "closed", "large", "small", "medium", "big",
}

_WORDY_CLASS_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")


# ── small helpers ────────────────────────────────────────────────────────────

def _attr(el: Tag, name: str, default: str = "") -> str:
    """Read an attribute safely.

    A decomposed BeautifulSoup element has ``attrs = None``; walking a tree
    while removing nodes therefore has to tolerate that.
    """
    try:
        value = el.get(name, default)
    except Exception:
        return default
    if value is None:
        return default
    return str(value)


def _classes(el: Tag) -> list[str]:
    """The element's class tokens, lowercased.

    BeautifulSoup keeps ``class`` as a *list* (it is a multi-valued
    attribute), so the raw value has to be read without being stringified
    first -- otherwise "weather data" turns into the single nonsense token
    "['weather', 'data']".
    """
    try:
        raw = el.get("class") or []
    except Exception:
        return []
    if isinstance(raw, str):
        raw = raw.split()
    return [str(c).strip().lower() for c in raw if str(c).strip()]


def _is_hidden(el: Tag) -> bool:
    """True when the element is not visible to a reader."""
    if el.has_attr("hidden") if getattr(el, "attrs", None) else False:
        return True
    if _attr(el, "aria-hidden").lower() == "true":
        return True
    style = _attr(el, "style")
    if style and _HIDDEN_STYLE_RE.search(style):
        return True
    if el.name == "input" and _attr(el, "type").lower() == "hidden":
        return True
    return False


def _is_noise(el: Tag) -> bool:
    """True when the element is page furniture rather than content."""
    if el.name in BOILERPLATE_TAGS:
        return True
    if _attr(el, "role").lower() in BOILERPLATE_ROLES:
        return True
    ident = " ".join(_classes(el) + ([_attr(el, "id")] if _attr(el, "id") else []))
    if ident and _NOISE_RE.search(ident):
        return True
    return False


def _clean(el: Tag, drop_boilerplate: bool = True) -> None:
    """Remove the parts of the tree that carry no readable content."""
    for child in list(el.find_all(True)):
        if child.name in DROP_TAGS:
            child.decompose()
            continue
        if _is_hidden(child):
            child.decompose()
            continue
        if drop_boilerplate and _is_noise(child):
            child.decompose()
            continue
        if child.name == "header":
            try:
                has_nav = child.find("nav") is not None
            except Exception:
                has_nav = False
            if has_nav:
                # A site header wraps the navigation; an article header holds
                # the title and the date, which is content.
                child.decompose()


def _collapse(text: str) -> str:
    """Squash runs of whitespace into single spaces."""
    return re.sub(r"\s+", " ", text or "").strip()


# A line break the page asked for explicitly.  It travels through the
# conversion as a sentinel so the whitespace of the HTML source (which is
# usually just formatting) can still be collapsed into a single space.
# The sentinel is deliberately not whitespace, otherwise the collapse below
# would eat it before it could be turned back into a line break.
_BREAK = "\x00"


def _normalize_inline(text: str, strip: bool = True) -> str:
    """Collapse source whitespace without losing an explicit line break."""
    text = re.sub(r"\s+", " ", text or "")
    text = text.replace(_BREAK, "\n")
    return text.strip() if strip else text


def _absolute(base_url: str, href: str) -> str:
    """Resolve a possibly relative link against the page it came from."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return href
    if base_url:
        try:
            return urljoin(base_url, href)
        except Exception:
            return href
    return href


# ── inline conversion ────────────────────────────────────────────────────────

def _control_text(el: Tag) -> str:
    """What a form control currently shows."""
    name = el.name
    if name == "textarea":
        return _collapse(el.get_text())
    if name == "select":
        chosen = [o for o in el.find_all("option") if o.has_attr("selected")]
        if not chosen:
            chosen = el.find_all("option")[:1]
        return _collapse(", ".join(o.get_text() for o in chosen))
    if name == "option":
        return _collapse(el.get_text())
    if name == "input":
        kind = str(el.get("type", "text")).lower()
        if kind in ("checkbox", "radio"):
            return "[x]" if el.has_attr("checked") else "[ ]"
        value = el.get("value")
        if value in (None, ""):
            value = el.get("placeholder") or el.get("aria-label") or ""
        return _collapse(str(value))
    return ""


def _wrap_inline(el: Tag, marker: str, base_url: str = "") -> str:
    """Wrap an element's text in a marker, keeping the spacing outside.

    ``<li>Monday<strong> 18 C</strong></li>`` has to become
    "Monday **18 C**" and not "Monday**18 C**", so the whitespace that
    belongs to the sentence stays outside the emphasis.
    """
    raw = _inline_children(el, base_url=base_url, strip=False)
    lead = raw[:len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    core = raw.strip()
    if not core:
        return raw
    return lead + marker + core + marker + trail


def _inline(el, base_url: str = "") -> str:
    """The inline Markdown of one node (text, tag or string)."""
    if isinstance(el, Comment):
        return ""
    if isinstance(el, NavigableString):
        # The raw text is kept: the spaces around an inline tag belong to
        # the sentence, and dropping them turns "It is <b>warm</b> and"
        # into "It iswarm and".
        return str(el)
    if not isinstance(el, Tag):
        return ""

    name = (el.name or "").lower()
    if name in DROP_TAGS or _is_hidden(el):
        return ""

    if name == "br":
        return _BREAK
    if name == "img":
        alt = _collapse(str(el.get("alt", "")))
        src = _absolute(base_url, el.get("src") or el.get("data-src") or "")
        if not src:
            return alt
        return f"![{alt}]({src})"
    if name == "a":
        text = _inline_children(el, base_url=base_url)
        href = _absolute(base_url, el.get("href", ""))
        if not text:
            return f"<{href}>" if href else ""
        if not href or href.startswith("javascript:"):
            return text
        return f"[{text}]({href})"
    if name in ("strong", "b"):
        return _wrap_inline(el, "**", base_url)
    if name in ("em", "i"):
        return _wrap_inline(el, "*", base_url)
    if name in ("s", "del", "strike"):
        return _wrap_inline(el, "~~", base_url)
    if name in ("code", "kbd", "samp", "var"):
        return _wrap_inline(el, "`", base_url)
    if name in ("input", "textarea", "select", "option"):
        return _control_text(el)
    if name in ("sub", "sup"):
        return _inline_children(el, base_url=base_url)
    return _inline_children(el, base_url=base_url)


def _inline_children(el: Tag, base_url: str = "",
                     strip: bool = True) -> str:
    """The inline Markdown of everything inside an element."""
    parts = []
    for child in el.children:
        piece = _inline(child, base_url)
        if piece:
            parts.append(piece)
    text = _normalize_inline("".join(parts), strip=strip)
    return text


# ── block conversion ─────────────────────────────────────────────────────────

def _is_block(el: Tag) -> bool:
    return (el.name or "").lower() in BLOCK_TAGS


def _render_inline_run(run: list, base_url: str = "") -> str:
    """Render a run of inline nodes as one line of Markdown.

    A label sitting next to its value ("Search" next to a text field) is
    rendered as one "**Search**: value" line instead of two loose words.
    """
    parts: list[str] = []
    index = 0
    while index < len(run):
        node = run[index]
        following = run[index + 1] if index + 1 < len(run) else None
        if (isinstance(node, Tag) and isinstance(following, Tag)
                and _is_label_ish(node) and _is_value_ish(following)):
            label = _collapse(node.get_text(" "))
            value = _leaf_value(following)
            parts.append(f"**{label}**: {value}".rstrip())
            index += 2
            continue
        piece = _inline(node, base_url)
        if piece:
            parts.append(piece)
        index += 1
    return _normalize_inline("".join(parts))


def _blocks(el: Tag, base_url: str) -> list[str]:
    """The Markdown blocks of an element's children."""
    blocks: list[str] = []
    run: list = []

    def flush() -> None:
        if not run:
            return
        text = _render_inline_run(run, base_url)
        run.clear()
        text = _collapse(text.replace("\n", " "))
        if text:
            blocks.append(text)

    for child in el.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            if str(child).strip():
                run.append(child)
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in DROP_TAGS or _is_hidden(child):
            continue
        if _is_block(child):
            flush()
            blocks.extend(_tag_blocks(child, base_url))
        else:
            run.append(child)
    flush()
    return blocks


def _inline_children_of(nodes: list, base_url: str = "") -> str:
    parts = []
    for node in nodes:
        piece = _inline(node, base_url)
        if piece:
            parts.append(piece)
    return "".join(parts)


def _tag_blocks(el: Tag, base_url: str) -> list[str]:
    """The Markdown blocks of one element."""
    name = (el.name or "").lower()

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        text = _inline_children(el, base_url=base_url)
        return [f"{'#' * int(name[1])} {text}"] if text else []

    if name == "p":
        text = _inline_children(el, base_url=base_url)
        return [text] if text else []

    if name == "hr":
        return ["---"]

    if name in ("ul", "ol"):
        lines = _list_blocks(el, base_url)
        return ["\n".join(lines)] if lines else []

    if name == "table":
        return _table_blocks(el, base_url)

    if name == "pre":
        text = el.get_text()
        if not text.strip():
            return []
        language = ""
        for cls in _classes(el):
            if cls.startswith("language-"):
                language = cls[len("language-"):]
                break
        return ["```" + language + "\n" + text.rstrip() + "\n```"]

    if name == "blockquote":
        inner = _join_blocks(_blocks(el, base_url))
        if not inner:
            return []
        return ["\n".join("> " + line if line else ">"
                          for line in inner.splitlines())]

    if name == "dl":
        return _dl_blocks(el, base_url)

    if name in GENERIC_CONTAINER_TAGS and _looks_like_data_container(el):
        # A box of labelled values ("temperature: 20 C", "AQI: 42") reads
        # far better as a list than as a row of loose paragraphs.
        outline = _outline(el, base_url)
        if outline:
            return ["\n".join(outline)]

    return _blocks(el, base_url)


def _list_blocks(el: Tag, base_url: str, depth: int = 0) -> list[str]:
    """A (nested) list as Markdown."""
    ordered = (el.name or "").lower() == "ol"
    lines: list[str] = []
    number = 0
    for item in el.find_all("li", recursive=False):
        number += 1
        marker = f"{number}." if ordered else "-"
        own: list = []
        nested: list = []
        for child in item.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                own.append(child)
                continue
            if not isinstance(child, Tag):
                continue
            if (child.name or "").lower() in ("ul", "ol"):
                nested.append(child)
            else:
                own.append(child)
        text = _collapse(_inline_children_of(own, base_url).replace("\n", " "))
        pad = "  " * depth
        if text:
            lines.append(f"{pad}{marker} {text}")
        elif not nested:
            lines.append(f"{pad}{marker}")
        for child in nested:
            sub = _list_blocks(child, base_url, depth + 1)
            if sub:
                if lines:
                    lines.append("")
                lines.extend(sub)
    return lines


def _table_blocks(el: Tag, base_url: str) -> list[str]:
    """A table as a Markdown table."""
    rows = []
    for row in el.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            cells = row.find_all(["th", "td"])
        if not cells:
            continue
        values = []
        for cell in cells:
            text = _collapse(_inline_children(cell, base_url).replace("\n", " "))
            values.append(text)
        rows.append(values)
    if not rows:
        return []

    width = max(len(r) for r in rows)
    header = rows[0] + [""] * (width - len(rows[0]))
    body = [r + [""] * (width - len(r)) for r in rows[1:]]

    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return ["\n".join(lines)]


def _dl_blocks(el: Tag, base_url: str) -> list[str]:
    """A definition list as a Markdown list."""
    lines: list[str] = []
    term = ""
    for child in el.find_all(["dt", "dd"], recursive=False):
        if child.name == "dt":
            term = _collapse(_inline_children(child, base_url))
            if term:
                lines.append(f"- **{term}**")
        else:
            value = _collapse(_inline_children(child, base_url))
            if value:
                if term:
                    lines.append(f"  {value}")
                else:
                    lines.append(f"- {value}")
    return ["\n".join(lines)] if lines else []


def _join_blocks(blocks: list[str]) -> str:
    """Join Markdown blocks with a blank line between them."""
    cleaned = [b.strip("\n") for b in blocks if b and b.strip()]
    out = "\n\n".join(cleaned)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ── public API ───────────────────────────────────────────────────────────────

def html_to_markdown(html: str, base_url: str = "",
                     drop_boilerplate: bool = True) -> str:
    """Convert a page's HTML into readable Markdown.

    The boilerplate (navigation, footers, cookie banners, ad slots, hidden
    panels) is removed first, so the result holds the content a human would
    actually read.
    """
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    _clean(soup, drop_boilerplate=drop_boilerplate)
    root = soup.body or soup
    if root is None:
        return ""
    return _join_blocks(_blocks(root, base_url))


# ── the element outline (the ``search`` argument) ────────────────────────────

def _label_from_class(el: Tag) -> str:
    """A human-readable name for an element, taken from its classes."""
    for token in _classes(el):
        if token in LABEL_CLASS_TOKENS:
            return token.replace("-", " ").replace("_", " ")
    for token in _classes(el):
        if token in UNINFORMATIVE_CLASS_TOKENS or token in VALUE_CLASS_TOKENS:
            continue
        if len(token) < 2 or len(token) > 40:
            continue
        if not _WORDY_CLASS_RE.match(token):
            continue
        return token.replace("-", " ").replace("_", " ")
    return ""


def _explicit_label(el: Tag) -> str:
    """A label the page itself provides (attribute, not class)."""
    for attr in ("data-label", "aria-label", "title", "alt"):
        value = el.get(attr)
        if value:
            text = _collapse(str(value))
            if text:
                return text
    return ""


def _element_label(el: Tag) -> str:
    """The best name for an element: explicit label first, then class."""
    explicit = _explicit_label(el)
    if explicit:
        return explicit
    return _label_from_class(el)


def _leaf_value(el: Tag) -> str:
    """The readable value of a leaf element."""
    if el.name in ("input", "textarea", "select", "option"):
        return _control_text(el)
    return _collapse(el.get_text(" "))


def _child_elements(el: Tag) -> list[Tag]:
    """The child elements that carry content."""
    kids = []
    for child in el.children:
        if isinstance(child, Comment) or isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in DROP_TAGS or _is_hidden(child):
            continue
        kids.append(child)
    return kids


def _is_label_ish(el: Tag) -> bool:
    if el.name == "label":
        return True
    tokens = _classes(el)
    return any(t in LABEL_CLASS_TOKENS for t in tokens)


def _is_value_ish(el: Tag) -> bool:
    if el.name in ("input", "textarea", "select", "option"):
        return True
    tokens = _classes(el)
    return any(t in VALUE_CLASS_TOKENS for t in tokens)


def _looks_like_data_container(el: Tag) -> bool:
    """True when a container holds a box of labelled values.

    Such a box -- the weather widget, a price table, a key/value list --
    is worth rendering as a list of "label: value" lines, because the
    labels are the only thing that says what the values *mean*.
    """
    kids = [c for c in el.children
            if isinstance(c, Tag) and c.name not in DROP_TAGS
            and not _is_hidden(c)]
    if not kids:
        return False
    if any(c.name in STRUCTURAL_TAGS for c in kids):
        return False
    labelled = 0
    for child in kids:
        if _child_elements(child):
            continue
        if _element_label(child) or child.name in ("input", "textarea",
                                                   "select", "option"):
            labelled += 1
    return labelled >= 1 and labelled * 2 >= len(kids)


def _outline(el: Tag, base_url: str, depth: int = 0,
             lines: list[str] | None = None) -> list[str]:
    """The subtree of an element as an indented Markdown outline."""
    if lines is None:
        lines = []

    children = _child_elements(el)
    # A label/value pair sitting next to each other belongs on one line.
    pairs: list[tuple] = []
    index = 0
    while index < len(children):
        child = children[index]
        nxt = children[index + 1] if index + 1 < len(children) else None
        if nxt is not None and _is_label_ish(child) and _is_value_ish(nxt):
            pairs.append(("pair", child, nxt))
            index += 2
            continue
        pairs.append(("single", child, None))
        index += 1

    for kind, child, partner in pairs:
        sub_children = _child_elements(child)
        if kind == "pair":
            label = _collapse(child.get_text(" ")) or _element_label(child)
            value = _leaf_value(partner)
            if label or value:
                lines.append(f"{'  ' * depth}- **{label}**: {value}".rstrip())
            continue

        if not sub_children:
            value = _leaf_value(child)
            label = _element_label(child)
            if label and value and label.lower() not in value.lower():
                lines.append(f"{'  ' * depth}- **{label}**: {value}")
            elif value:
                lines.append(f"{'  ' * depth}- {value}")
            elif label:
                lines.append(f"{'  ' * depth}- **{label}**")
            continue

        label = _element_label(child)
        if label:
            lines.append(f"{'  ' * depth}- **{label}**")
            _outline(child, base_url, depth + 1, lines)
        else:
            _outline(child, base_url, depth, lines)

    return lines


def describe_element(el: Tag) -> str:
    """A short identity for an element: ``div.weather-data#forecast``."""
    parts = [(el.name or "?").lower()]
    for token in _classes(el)[:3]:
        parts.append("." + token)
    ident = el.get("id")
    if ident:
        parts.append("#" + str(ident))
    return "".join(parts)


def element_to_markdown(el: Tag, base_url: str = "",
                        heading: str | None = None) -> str:
    """One element and everything inside it, as a Markdown structure.

    The element's own identity (tag, classes, id) becomes the heading, and
    everything below it keeps the page's own structure: headings stay
    headings, lists stay lists, and a box of labelled values becomes a list
    of "label: value" lines.
    """
    if el is None:
        return ""
    title = heading if heading is not None else describe_element(el)
    body = html_to_markdown(str(el), base_url)
    if title and body:
        return f"### {title}\n\n{body}"
    return title or body


# ── finding elements by class, id, tag or selector ───────────────────────────

# How many elements one search may report.  A page can hold thousands of
# "div"s; reporting all of them would drown the answer.
MAX_MATCHES = 25


def _looks_like_selector(query: str) -> bool:
    """True when the query can only be read as a CSS selector.

    A dot or a hash cannot occur inside an HTML class name or id, so a
    query holding one is a selector ("div.row", "ul#main").
    """
    if query.startswith(("#", ".", "[", ":", "*")):
        return True
    return any(ch in query for ch in (".", "#", ">", "+", "~", ",", "(", ")"))


def classify_query(query: str) -> str:
    """Decide how a search query should be read.

    "selector" -- a full CSS selector.  "id" -- the id of an element.
    "tag" -- a tag name.  "class" -- a class name, possibly several
    classes written with spaces ("weather data"), which match an element
    carrying all of them.

    The browser and the no-browser fallback have to agree on this, so the
    decision lives here and both ask for it.
    """
    q = (query or "").strip()
    if not q:
        return ""
    if _looks_like_selector(q):
        return "selector"
    if " " in q:
        return "class"
    if q.lower() in KNOWN_TAGS:
        return "tag"
    return "id"


def find_elements(soup, query: str) -> list:
    """Find elements the way the browser tools' ``search`` argument does.

    The query may be a CSS selector (``#results .row``), a plain id
    (``searchResults``), a tag name (``div``, ``a``) or a class name --
    including a class name written with spaces (``weather data``), which
    matches an element carrying both of those classes.
    """
    q = (query or "").strip()
    if not q:
        return []

    mode = classify_query(q)
    if mode == "selector":
        try:
            found = soup.select(q)
            if found:
                return list(found)
        except Exception:
            pass
        # The selector found nothing; the query may have been meant as a
        # plain class or id name.
        mode = "id"

    if mode == "id":
        by_id = soup.find(id=q)
        if by_id is not None:
            return [by_id]
        mode = "class"

    if mode == "tag":
        return list(soup.find_all(q.lower()))

    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return []
    found = []
    for el in soup.find_all(True):
        classes = _classes(el)
        if all(token in classes for token in tokens):
            found.append(el)
    return found
