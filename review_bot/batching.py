"""Fit an arbitrarily large diff into a bounded context window.

Four standard long-context techniques are combined here:

1. Relevance ranking   - review-worthy source files are ordered ahead of
                         lockfiles, minified bundles, and generated code, so
                         that if anything is dropped it is the least useful.
2. Per-file truncation - one enormous file is trimmed at a hunk boundary
                         instead of being allowed to consume the whole budget
                         or being dropped outright.
3. Batching (map)      - the remaining files are packed into several
                         requests that each fit the context window.
4. Explicit accounting - whatever still does not fit is reported back rather
                         than silently discarded.

The reduce step that merges per-batch results lives in the CLIs.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import List, Sequence

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


def truncate_diff(diff: str, limit: int) -> tuple[str, bool]:
    """Trim a diff to `limit` chars, cutting at a hunk boundary where possible."""
    if len(diff) <= limit:
        return diff, False

    kept: List[str] = []
    total = 0
    last_hunk_end = 0
    for line in diff.splitlines(keepends=True):
        if total + len(line) > limit:
            break
        kept.append(line)
        total += len(line)
        if line.startswith("@@"):
            # Remember where the previous complete hunk ended.
            last_hunk_end = len(kept) - 1

    if last_hunk_end > 0:
        kept = kept[:last_hunk_end]

    body = "".join(kept).rstrip("\n")
    return f"{body}\n... [diff truncated: file too large to include in full]\n", True


def prepare(
    changes: Sequence[FileChange],
    max_file_chars: int,
) -> List[FileChange]:
    """Truncate oversized files and order everything by review value."""
    prepared: List[FileChange] = []
    for change in changes:
        diff, was_truncated = truncate_diff(change.diff, max_file_chars)
        prepared.append(
            FileChange(
                path=change.path,
                diff=diff,
                added_lines=change.added_lines,
                truncated=was_truncated,
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
