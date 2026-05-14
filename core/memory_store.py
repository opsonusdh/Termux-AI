import os
import re
import heapq
from collections import defaultdict

#  Paths 

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MEMORY_FILE = os.path.join(AI_ROOT, "memories.txt")

#  Stop-word filter 

STOP_WORDS = {
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "do", "be",
    "of", "and", "or", "for", "with", "that", "this", "i", "you", "we",
    "me", "my", "your", "how", "what", "when", "where", "can", "could",
    "would", "should", "will", "if", "then", "so", "are", "was", "were",
    "have", "has", "had", "not", "but", "from", "use", "get", "let",
    "run", "its", "just", "want", "need", "try", "also", "any", "some",
    "all", "no", "more", "about", "by", "up", "as", "into", "out", "now",
}

#  Category tree (semantic grouping) 

CATEGORY_TREE = {
    "preference": ["shell", "ui", "style", "commands", "help", "flag", "output"],
    "instruction": ["shutdown", "process", "kill", "safety", "behavior", "close"],
    "project":     ["termux", "tui", "ai_root", "workspace", "repo", "code"],
    "fact":        ["environment", "device", "installed", "paths", "api", "key"],
    "workflow":    ["git", "python", "download", "script", "build", "install"],
}

#  Entry patterns 

# New structured format: [type][tags][priority] text
_STRUCT_RE = re.compile(
    r"^\[(?P<type>\w+)\]\[(?P<tags>[^\]]*)\]\[(?P<priority>\d+)\]\s*(?P<text>.+)$"
)

# Legacy labeled format: "Learned: ...", "Instruction: ...", etc.
_LEGACY_RE = re.compile(
    r"^(?:Learned|Note|Instruction|Tip|Fact|Preference):\s*(.+)$",
    re.IGNORECASE,
)


#  Memory entry 

class MemoryEntry:
    __slots__ = ("id", "type", "tags", "priority", "text", "keywords")

    def __init__(self, id_, type_, tags_str, priority, text):
        self.id = id_
        self.type = type_.lower().strip()
        self.tags = {t.strip().lower() for t in tags_str.split(",") if t.strip()}
        self.priority = max(1, min(10, int(priority)))
        self.text = text.strip()
        # keyword universe = word tokens from text + explicit tags
        self.keywords = _tokenize(self.text) | self.tags

    def __repr__(self):
        tag_str = ",".join(sorted(self.tags))
        return f"<Mem [{self.type}][{tag_str}][{self.priority}] {self.text[:60]}>"


#  Helpers 

def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9_\-]+", text.lower())
    return {w for w in words if w not in STOP_WORDS and len(w) > 2}


def _infer_type_tags(text: str) -> tuple:
    """Guess type and tags from plain legacy text."""
    low = text.lower()
    if any(k in low for k in ("kill", "close", "shutdown", "goodbye", "exit", "process")):
        return "instruction", "shutdown,process"
    if any(k in low for k in ("prefer", "-h", "--help", "instead", "flag", "better")):
        return "preference", "shell,commands,help"
    if any(k in low for k in ("repo", "directory", "workspace", "folder", "project", "lives in")):
        return "project", "ai_root,workspace"
    if any(k in low for k in ("install", "package", "path", "bin", "env", "api")):
        return "fact", "environment"
    return "fact", "general"


#  Load 

def load_memories() -> list:
    """
    Parse memories.txt → list[MemoryEntry].
    Supports structured lines, legacy labeled lines, and bare plain text.
    Lines starting with '#' are treated as comments and skipped.
    """
    if not os.path.exists(MEMORY_FILE):
        return []

    entries = []

    with open(MEMORY_FILE, "r", encoding="utf-8") as fh:
        for idx, raw in enumerate(fh):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # 1. Structured
            m = _STRUCT_RE.match(line)
            if m:
                entries.append(MemoryEntry(
                    id_=idx,
                    type_=m.group("type"),
                    tags_str=m.group("tags"),
                    priority=m.group("priority"),
                    text=m.group("text"),
                ))
                continue

            # 2. Legacy labeled  ("Learned: ...")
            m2 = _LEGACY_RE.match(line)
            if m2:
                text = m2.group(1)
                type_, tags = _infer_type_tags(text)
                entries.append(MemoryEntry(
                    id_=idx,
                    type_=type_,
                    tags_str=tags,
                    priority=7,
                    text=text,
                ))
                continue

            # 3. Bare plain text fallback
            if len(line) > 8:
                type_, tags = _infer_type_tags(line)
                entries.append(MemoryEntry(
                    id_=idx,
                    type_=type_,
                    tags_str=tags,
                    priority=5,
                    text=line,
                ))

    return entries


#  Scoring 

def _score_entry(entry: MemoryEntry, prompt_kw: set, relevant_cats: set) -> float:
    keyword_overlap  = len(entry.keywords & prompt_kw)          # direct word hits
    tag_match_bonus  = len(entry.tags & prompt_kw) * 1.5        # tag hits weighted higher
    priority_weight  = entry.priority * 0.4                     # high-priority floats up
    parent_node_boost = 2.0 if entry.type in relevant_cats else 0.0  # category match

    return keyword_overlap + tag_match_bonus + priority_weight + parent_node_boost


def _relevant_categories(prompt_kw: set) -> set:
    """Score root category branches; return those above zero."""
    scores = defaultdict(float)
    for cat, subtags in CATEGORY_TREE.items():
        if cat in prompt_kw:
            scores[cat] += 2.0
        scores[cat] += len(set(subtags) & prompt_kw) * 1.5

    top = {cat for cat, s in scores.items() if s > 0}
    return top if top else set(CATEGORY_TREE.keys())   # fallback: all cats


#  Retrieve 

def retrieve(prompt: str, top_k: int = 5, threshold: float = 1.5) -> list:
    """
    Best-first memory retrieval.

    Parameters
    ----------
    prompt    : the user's raw input string
    top_k     : maximum number of memories to return
    threshold : minimum score to be included (filters noise)

    Returns
    -------
    List[MemoryEntry] ordered by descending relevance.
    """
    entries = load_memories()
    if not entries:
        return []

    prompt_kw = _tokenize(prompt)
    if not prompt_kw:
        # No keywords → return top-priority instructions only
        instructions = [e for e in entries if e.type == "instruction"]
        instructions.sort(key=lambda e: -e.priority)
        return instructions[:top_k]

    relevant_cats = _relevant_categories(prompt_kw)

    # Build max-heap (negated score for Python's min-heap)
    heap = []
    for entry in entries:
        score = _score_entry(entry, prompt_kw, relevant_cats)
        if score >= threshold:
            # Tiebreak by id (stable ordering)
            heapq.heappush(heap, (-score, entry.id, entry))

    # Always surface priority-10 instructions regardless of score
    mandatory = [e for e in entries if e.priority == 10 and e.type == "instruction"]

    results = []
    seen_ids = {e.id for e in mandatory}

    # Prepend mandatory items
    results.extend(mandatory)

    # Pop best-first until top_k
    while heap and len(results) < top_k:
        _, _, entry = heapq.heappop(heap)
        if entry.id not in seen_ids:
            seen_ids.add(entry.id)
            results.append(entry)

    return results[:top_k]


#  Format for prompt injection 

def build_memory_block(prompt: str) -> str:
    """
    Retrieve relevant memories and format them as a system-prompt block.
    Returns an empty string when nothing relevant is found.
    """
    hits = retrieve(prompt)
    if not hits:
        return ""

    lines = ["## RETRIEVED MEMORY"]
    for entry in hits:
        tag_str = ",".join(sorted(entry.tags)) if entry.tags else entry.type
        lines.append(f"- [{entry.type}][{tag_str}] {entry.text}")
    lines.append("")   # trailing newline for readability

    return "\n".join(lines)


#  Write 

def save_memory(
    text: str,
    type_: str = "fact",
    tags: str = "",
    priority: int = 7,
) -> str:
    """
    Append a new structured memory entry to memories.txt.

    Parameters
    ----------
    text     : the fact or preference to store
    type_    : preference | instruction | project | fact | workflow
    tags     : comma-separated keywords (e.g. "shell,help")
    priority : 1–10

    Returns the formatted line written to disk (empty string on failure).
    """
    text = text.strip()
    if not text:
        return ""

    type_ = type_.strip().lower() or "fact"
    tags  = tags.strip().lower()
    priority = max(1, min(10, int(priority)))

    line = f"[{type_}][{tags}][{priority}] {text}"

    try:
        with open(MEMORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return line
    except OSError as exc:
        return f"[ERROR saving memory: {exc}]"
