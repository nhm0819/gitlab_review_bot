"""Fit an arbitrarily large diff into a bounded context window.

Four standard long-context techniques are combined here:

1. Relevance ranking   - review-worthy source files are ordered ahead of
                         lockfiles, minified bundles, and generated code, so
                         that if anything is dropped it is the least useful.
2. Per-file condensing - one enormous file is reduced to its most
                         review-worthy hunks (security/error-handling/logic
                         changes kept, boilerplate and near-duplicate hunks
                         collapsed) instead of keeping only its first N chars.
3. Batching (map)      - the remaining files are packed into several
                         requests that each fit the context window.
4. Explicit accounting - whatever still does not fit is reported back rather
                         than silently discarded.

The reduce step that merges per-batch results lives in the CLIs.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .diff_parser import OMISSION_PREFIX

# Rough average for source code; used only for human-readable logging.
CHARS_PER_TOKEN = 3.5

# Files that are real changes but rarely worth model attention.
LOW_VALUE_PATTERNS = (
    "*.lock", "*-lock.json", "*.lockb", "go.sum", "Cargo.lock", "poetry.lock",
    "*.min.js", "*.min.css", "*.map", "*.snap",
    "*_pb2.py", "*_pb2_grpc.py", "*.pb.go", "*.g.dart", "*.freezed.dart",
    "*/vendor/*", "*/dist/*", "*/build/*", "*/node_modules/*",
    "*/generated/*", "*/__generated__/*", "*/migrations/*",
    "*.svg", "*.po", "*.mo",
)

SOURCE_EXTENSIONS = (
    ".py", ".go", ".java", ".kt", ".rb", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".ts", ".tsx", ".js", ".jsx", ".vue", ".swift", ".scala", ".php", ".sql",
    ".sh", ".bash", ".tf", ".yaml", ".yml", ".json", ".toml", ".gradle", ".dockerfile",
)


@dataclass
class FileChange:
    path: str
    diff: str
    added_lines: List[int] = field(default_factory=list)
    truncated: bool = False
    omitted_hunks: int = 0

    @property
    def size(self) -> int:
        return len(self.diff)


def estimate_tokens(text_length: int) -> int:
    return int(text_length / CHARS_PER_TOKEN)


def is_low_value(path: str) -> bool:
    lowered = path.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in LOW_VALUE_PATTERNS)


def priority(change: FileChange) -> float:
    """Higher means more worth spending context on.

    Capped so that a single huge file cannot outrank several normal ones just
    by being long, which is exactly the case that used to starve the budget.
    """
    score = float(min(len(change.added_lines), 400))
    if change.path.lower().endswith(SOURCE_EXTENSIONS):
        score *= 1.5
    if is_low_value(change.path):
        score *= 0.02
    return score


# Hunks whose changes match these score higher: this is where review actually
# pays off. Ordered high to low weight.
_RISK_PATTERNS: Tuple[Tuple[re.Pattern, float], ...] = (
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|credential|private[_-]?key"
                r"|auth\w*|permission|privilege|sudo|chmod|chown"
                r"|eval|exec|popen|subprocess|shell=True|os\.system"
                r"|pickle|unmarshal|deserial\w*|yaml\.load"
                r"|innerhtml|dangerouslysetinnerhtml"
                r"|sql|raw_query|execute\(|drop\s+table|truncate\s+table)\b"), 3.0),
    (re.compile(r"(?i)\b(except|catch|finally|raise|throw|panic|unwrap|recover|errors?\."
                r"|retry|timeout|deadline|fallback|rollback|nil\s*check|null\s*check)\b"), 2.0),
    (re.compile(r"(?i)\b(lock|mutex|semaphore|atomic|thread|goroutine|async|await|concurren\w*"
                r"|race|deadlock|transaction|commit|savepoint)\b"), 2.0),
    (re.compile(r"(?i)\b(open\(|close\(|read|write|delete|remove|unlink|request|response"
                r"|http|https?://|url|socket|connect|session|cursor|stream)\b"), 1.0),
    (re.compile(r"(?i)\b(def |func |class |function |interface |struct |impl |return )"), 1.0),
    (re.compile(r"(?i)(TODO|FIXME|HACK|XXX|WORKAROUND)"), 1.0),
)

_COMMENT_ONLY = re.compile(r"^\s*(#|//|/\*|\*|<!--|--)")
_IMPORT_ONLY = re.compile(r"(?i)^\s*(import|from|require|use|using|include|package)\b")
_NUMBER_RE = re.compile(r"\d+")
_STRING_RE = re.compile(r"(\"[^\"]*\"|'[^']*')")

# How many hunks sharing the same normalized shape are kept before the rest are
# collapsed. Mass renames produce hundreds of identical hunks; a couple of
# examples convey the pattern.
DUPLICATE_HUNKS_KEPT = 2


def split_hunks(diff: str) -> Tuple[str, List[str]]:
    """Split a unified diff into (preamble, [hunk, ...]). Each hunk keeps its @@ header."""
    lines = diff.splitlines(keepends=True)
    preamble: List[str] = []
    hunks: List[List[str]] = []
    for line in lines:
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            preamble.append(line)
    return "".join(preamble), ["".join(h) for h in hunks]


def _changed_lines(hunk: str) -> List[str]:
    return [
        line for line in hunk.splitlines()
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    ]


def hunk_signature(hunk: str) -> str:
    """Shape of a hunk with literals normalized, so mass-applied edits collide."""
    parts = []
    for line in _changed_lines(hunk):
        body = _STRING_RE.sub("S", _NUMBER_RE.sub("N", line[1:].strip()))
        parts.append(f"{line[0]}{' '.join(body.split())}")
    return "\n".join(parts)


def score_hunk(hunk: str) -> float:
    """How much review value this hunk carries. Higher is kept first."""
    changed = _changed_lines(hunk)
    if not changed:
        return 0.0

    bodies = [line[1:] for line in changed]
    meaningful = [b for b in bodies if b.strip()]
    if not meaningful:
        return 0.05  # whitespace-only

    if all(_COMMENT_ONLY.match(b) for b in meaningful):
        return 0.2
    if all(_IMPORT_ONLY.match(b) for b in meaningful):
        return 0.3

    text = "\n".join(meaningful)
    score = 1.0 + min(len(changed), 60) / 20.0
    for pattern, weight in _RISK_PATTERNS:
        if pattern.search(text):
            score += weight
    return score


def _truncate_lines(text: str, limit: int) -> str:
    """Last-resort trim for a single hunk that alone exceeds the budget."""
    if len(text) <= limit:
        return text
    kept: List[str] = []
    total = 0
    for line in text.splitlines(keepends=True):
        if total + len(line) > limit:
            break
        kept.append(line)
        total += len(line)
    return "".join(kept).rstrip("\n") + f"\n{OMISSION_PREFIX} hunk truncated\n"


def condense_diff(diff: str, limit: int) -> Tuple[str, bool, int]:
    """Reduce a diff to its most review-worthy hunks within `limit` chars.

    Unlike prefix truncation this keeps important changes wherever they sit in
    the file, and collapses near-duplicate hunks produced by mass edits.

    Returns (diff, was_condensed, omitted_hunk_count).
    """
    if len(diff) <= limit:
        return diff, False, 0

    preamble, hunks = split_hunks(diff)
    if not hunks:
        return _truncate_lines(diff, limit), True, 0

    # Collapse near-duplicates first: keep a few representatives per shape.
    seen: Dict[str, int] = {}
    candidates: List[Tuple[int, str, float]] = []
    duplicates_dropped = 0
    for index, hunk in enumerate(hunks):
        signature = hunk_signature(hunk)
        seen[signature] = seen.get(signature, 0) + 1
        if signature and seen[signature] > DUPLICATE_HUNKS_KEPT:
            duplicates_dropped += 1
            continue
        candidates.append((index, hunk, score_hunk(hunk)))

    # Take the highest-value hunks that fit, leaving room for markers.
    budget = max(limit - len(preamble) - 120, 0)
    chosen: List[Tuple[int, str]] = []
    used = 0
    for index, hunk, _score in sorted(candidates, key=lambda c: c[2], reverse=True):
        if used + len(hunk) > budget:
            continue
        chosen.append((index, hunk))
        used += len(hunk)

    if not chosen:
        # Even the best single hunk does not fit; trim it rather than emit nothing.
        best = max(candidates, key=lambda c: c[2])
        chosen = [(best[0], _truncate_lines(best[1], budget))]

    chosen.sort(key=lambda c: c[0])
    kept_indexes = {index for index, _ in chosen}
    omitted = len(hunks) - len(kept_indexes)

    # Reassemble in file order, annotating the gaps.
    out: List[str] = [preamble] if preamble else []
    previous = -1
    for index, hunk in chosen:
        gap = index - previous - 1
        if gap > 0:
            out.append(f"{OMISSION_PREFIX} {gap} less relevant hunk(s) omitted\n")
        out.append(hunk if hunk.endswith("\n") else hunk + "\n")
        previous = index
    trailing = len(hunks) - 1 - previous
    if trailing > 0:
        out.append(f"{OMISSION_PREFIX} {trailing} less relevant hunk(s) omitted\n")
    if duplicates_dropped:
        # A breakdown of the omissions above, not an additional count.
        out.append(
            f"{OMISSION_PREFIX} of those, {duplicates_dropped} were near-identical repeats "
            f"of the same change shown above\n"
        )
    return "".join(out), True, omitted


def prepare(
    changes: Sequence[FileChange],
    max_file_chars: int,
) -> List[FileChange]:
    """Condense oversized files and order everything by review value."""
    prepared: List[FileChange] = []
    for change in changes:
        diff, was_condensed, omitted = condense_diff(change.diff, max_file_chars)
        prepared.append(
            FileChange(
                path=change.path,
                diff=diff,
                added_lines=change.added_lines,
                truncated=was_condensed,
                omitted_hunks=omitted,
            )
        )
    prepared.sort(key=priority, reverse=True)
    return prepared


def batch(
    changes: Sequence[FileChange],
    batch_chars: int,
    max_batches: int,
) -> tuple[List[List[FileChange]], List[FileChange]]:
    """Pack files into context-sized batches.

    Returns (batches, dropped). `dropped` is non-empty only when the change set
    exceeds `max_batches` worth of context; those files are reported to the
    user instead of being silently ignored.
    """
    batches: List[List[FileChange]] = []
    current: List[FileChange] = []
    current_size = 0

    for change in changes:
        if current and current_size + change.size > batch_chars:
            batches.append(current)
            current, current_size = [], 0
            if len(batches) >= max_batches:
                break
        current.append(change)
        current_size += change.size

    if current and len(batches) < max_batches:
        batches.append(current)

    included = {id(c) for group in batches for c in group}
    dropped = [c for c in changes if id(c) not in included]
    return batches, dropped
